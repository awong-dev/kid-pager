package app.kidpager.bridge

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Which relay URLs the phone knows and which one it uses. [relayUrl] is home, preferred whenever
 * it answers; [prevUrl] is the other URL until [prevUntil] (epoch ms); [onOther] means the phone is
 * working on [prevUrl]. [homeMoveId] is the move id both relays echo in `hello`. [pendingUrl] and
 * [pendingId] come from a `moved` answer and are untrusted until `hello` on that URL proves them.
 * [fails] counts consecutive failures of the URL in use; [step] and [nextAt] pace the alternation
 * between the two URLs.
 */
data class MoveState(
    val relayUrl: String,
    val pendingUrl: String? = null,
    val pendingId: String? = null,
    val lastTryAt: Long = 0,
    val prevUrl: String? = null,
    val prevUntil: Long = 0,
    val homeMoveId: String? = null,
    val onOther: Boolean = false,
    val fails: Int = 0,
    val step: Int = 0,
    val nextAt: Long = 0,
)

/** What `GET {url}/bridge/hello` answered. */
data class HelloAnswer(val bridgeId: String?, val moveId: String?, val sig: String?)

/** Outcome of an authenticated heartbeat sent to probe a URL. */
enum class Probe { OK, MOVED, UNAUTH, FAIL }

/** The pure decisions of a server move; [Moves] persists the state and supplies the network calls. */
object Mover {
    const val VERIFY_RETRY_MS = 5 * 60_000L
    const val FALLBACK_MS = 7 * 24 * 3_600_000L
    const val FAILS_BEFORE_ALTERNATING = 3
    val BACKOFF_MS = longArrayOf(1, 2, 5, 15, 30, 60).map { it * 60_000L }

    /** What the decisions need from the outside: identity, a nonce source and the two calls. */
    class Env(
        val bridgeId: String?,
        val token: String?,
        val nonce: () -> String,
        val hello: (url: String, bridgeId: String, nonce: String) -> HelloAnswer?,
        val heartbeat: (url: String) -> Probe,
    )

    fun activeUrl(s: MoveState): String = if (s.onOther && s.prevUrl != null) s.prevUrl else s.relayUrl

    /** The `moved` object of a `/bridge/...` answer body as (url, id); null when absent or unusable. */
    fun parseMoved(body: String): Pair<String, String>? {
        if (!body.contains("\"moved\"")) return null
        val moved = try {
            (WireJson.parseToJsonElement(body) as? JsonObject)?.get("moved")?.jsonObject
        } catch (e: Exception) { return null } ?: return null
        val url = (moved["url"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return null
        val id = (moved["id"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return null
        val norm = normalizeUrl(url) ?: return null
        return norm to id
    }

    /** An https base URL, optionally with a path prefix, without a trailing slash, query, fragment or credentials. */
    fun normalizeUrl(raw: String): String? {
        val u = try { java.net.URI(raw.trim()) } catch (e: Exception) { return null }
        if (u.scheme != "https" || u.host.isNullOrEmpty() || u.userInfo != null || u.rawQuery != null || u.rawFragment != null) return null
        return raw.trim().trimEnd('/')
    }

    fun randomNonce(): String = ByteArray(16).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }

    private fun hex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }

    /** HMAC key of `hello`: the ASCII bytes of the lowercase hex SHA-256 of the token. */
    fun tokenHash(token: String): String = hex(MessageDigest.getInstance("SHA-256").digest(token.toByteArray(Charsets.UTF_8)))

    fun helloSig(token: String, nonce: String, bridgeId: String, moveId: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(tokenHash(token).toByteArray(Charsets.US_ASCII), "HmacSHA256"))
        return hex(mac.doFinal("kp-bridge-hello|$nonce|$bridgeId|$moveId".toByteArray(Charsets.US_ASCII)))
    }

    /** True when [a] carries this bridge's id, the expected move id and the signature only the token holder's relay can make. */
    fun helloValid(token: String?, ownBridgeId: String?, nonce: String, expectedMoveId: String?, a: HelloAnswer?): Boolean {
        if (a == null || token == null || ownBridgeId == null || expectedMoveId == null) return false
        if (a.bridgeId != ownBridgeId || a.moveId != expectedMoveId || a.sig == null) return false
        val want = helloSig(token, nonce, ownBridgeId, expectedMoveId)
        return MessageDigest.isEqual(want.toByteArray(Charsets.US_ASCII), a.sig.toByteArray(Charsets.US_ASCII))
    }

    /** Calls `hello` on [url] with a fresh nonce and checks the answer; sends no token. */
    fun helloVerifies(env: Env, url: String, expectedMoveId: String?): Boolean {
        val own = env.bridgeId ?: return false
        val nonce = env.nonce()
        return helloValid(env.token, own, nonce, expectedMoveId, env.hello(url, own, nonce))
    }

    /** Drops the second URL once the window has passed, keeping the one in use. */
    fun expire(s: MoveState, now: Long): MoveState {
        val p = s.prevUrl ?: return s
        if (now < s.prevUntil) return s
        return MoveState(relayUrl = if (s.onOther) p else s.relayUrl, pendingUrl = s.pendingUrl, pendingId = s.pendingId, lastTryAt = s.lastTryAt)
    }

    /** A `moved` answer from the URL in use. Pointing at that URL is ignored. */
    fun onMoved(s0: MoveState, url: String, id: String, now: Long): MoveState {
        val s = expire(s0, now)
        if (url == activeUrl(s)) return s
        if (url == s.pendingUrl && id == s.pendingId) return s
        return s.copy(pendingUrl = url, pendingId = id, lastTryAt = 0)
    }

    fun shouldVerify(s: MoveState, now: Long): Boolean =
        s.pendingUrl != null && now - s.lastTryAt >= VERIFY_RETRY_MS

    /** Marks an attempt before the network call so the 5-minute spacing holds if it dies. */
    fun beginVerify(s: MoveState, now: Long): MoveState = s.copy(lastTryAt = now)

    /** The target proved itself: switch to it and keep the URL in use as the other one. */
    fun commit(s: MoveState, now: Long): MoveState {
        val url = s.pendingUrl ?: return s
        return MoveState(relayUrl = url, prevUrl = activeUrl(s), prevUntil = now + FALLBACK_MS, homeMoveId = s.pendingId)
    }

    /** Runs the check for a pending move. */
    fun verify(s: MoveState, env: Env, now: Long): MoveState {
        val url = s.pendingUrl ?: return s
        return if (helloVerifies(env, url, s.pendingId)) commit(s, now) else s
    }

    /** An HTTP answer from the URL in use that is not a failure; one without `moved` means the move was called off. */
    fun onAnswer(s0: MoveState, now: Long, moved: Boolean): MoveState {
        var s = expire(s0, now)
        if (!moved && s.pendingUrl != null) s = s.copy(pendingUrl = null, pendingId = null)
        return if (s.fails != 0) s.copy(fails = 0) else s
    }

    /** A network, TLS or timeout error, or a 5xx without `moved`, from the URL in use. */
    fun onFailure(s0: MoveState, now: Long): MoveState {
        val s = expire(s0, now)
        return if (s.prevUrl == null) s else s.copy(fails = s.fails + 1)
    }

    /** Whether to try the other URL (home failing) or probe home (working on the other) now. */
    fun alternateDue(s: MoveState, now: Long): Boolean =
        s.prevUrl != null && now >= s.nextAt && (s.onOther || s.fails >= FAILS_BEFORE_ALTERNATING)

    private fun backedOff(s: MoveState, now: Long) =
        s.copy(step = s.step + 1, nextAt = now + BACKOFF_MS[minOf(s.step, BACKOFF_MS.size - 1)])

    /** [s] settled on the other URL after `hello` and an authenticated heartbeat. */
    private fun settledOther(s: MoveState, now: Long) =
        s.copy(onOther = true, fails = 0, step = 1, nextAt = now + BACKOFF_MS[0])

    private fun backHome(s: MoveState) = s.copy(onOther = false, fails = 0, step = 0, nextAt = 0)

    /**
     * Tries the URL not in use. On the other URL: `hello` first, then a heartbeat; settle only on
     * a 2xx without `moved`. Home is probed with a heartbeat only. A failed try backs off.
     */
    fun alternate(s0: MoveState, env: Env, now: Long): MoveState {
        val s = expire(s0, now)
        val other = s.prevUrl ?: return s
        if (s.onOther) return if (env.heartbeat(s.relayUrl) == Probe.OK) backHome(s) else backedOff(s, now)
        return if (helloVerifies(env, other, s.homeMoveId) && env.heartbeat(other) == Probe.OK) settledOther(s, now) else backedOff(s, now)
    }

    /**
     * A 401 from the URL in use. Within the window the other URL is tried and the token is kept
     * unless that answers 401 too; an other URL that settles after a 401 from home becomes home.
     * Returns the new state and whether the token stays.
     */
    fun onUnauthorized(s0: MoveState, env: Env, now: Long): Pair<MoveState, Boolean> {
        val s = expire(s0, now)
        val other = s.prevUrl ?: return s to false
        if (s.onOther) {
            return when (env.heartbeat(s.relayUrl)) {
                Probe.OK -> backHome(s) to true
                Probe.UNAUTH -> s to false
                else -> backedOff(s, now) to true
            }
        }
        if (!helloVerifies(env, other, s.homeMoveId)) return backedOff(s.copy(fails = maxOf(s.fails, FAILS_BEFORE_ALTERNATING)), now) to true
        return when (env.heartbeat(other)) {
            Probe.OK -> MoveState(relayUrl = other, prevUrl = s.relayUrl, prevUntil = s.prevUntil, homeMoveId = s.homeMoveId,
                pendingUrl = s.pendingUrl, pendingId = s.pendingId, lastTryAt = s.lastTryAt) to true
            Probe.UNAUTH -> s to false
            else -> backedOff(s.copy(fails = maxOf(s.fails, FAILS_BEFORE_ALTERNATING)), now) to true
        }
    }
}
