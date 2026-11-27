package app.kidpager.bridge

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Client for the relay's `/bridge/...` HTTP API. Every call but `pair` carries the bearer
 * token. A 401 clears the token, so the phone must be paired again, unless a move left a
 * previous URL to fall back to (see [Mover]); answers are also fed to [Moves].
 */
class RelayClient(private val ctx: Context) {
    /** [detail] is the relay's `{"detail": "..."}` text; null when `detail` is not a string. */
    class HttpError(val code: Int, val body: String) : IOException("HTTP $code: ${body.take(200)}") {
        val detail: String? by lazy {
            try { (WireJson.parseToJsonElement(body) as? JsonObject)?.get("detail")?.let { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content } }
            catch (e: Exception) { null }
        }
        /** A 503 that redirects: the relay has moved, nothing was applied. */
        val moved: Boolean by lazy { Mover.parseMoved(body) != null }
        fun reason(): String = detail ?: body.take(120)
    }
    class Unauthorized : IOException("401 unauthorized")
    /** A 401 within the move window kept the token and the phone tries the other URL; the caller retries later. */
    class FellBack : IOException("401 handled by the move failover")

    companion object {
        private const val TAG = "relay"
        private val JSON = "application/json; charset=utf-8".toMediaType()
        @Volatile var onUnauthorized: ((Context) -> Unit)? = null
    }

    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(40, TimeUnit.SECONDS) // must exceed the relay's 25 s cap on outbox ?wait=
        .writeTimeout(20, TimeUnit.SECONDS)
        .callTimeout(60, TimeUnit.SECONDS)
        .build()

    @Serializable
    private data class HelloResponse(val bridgeId: String? = null, val moveId: String? = null, val sig: String? = null)

    private fun noRedirects() = http.newBuilder().followRedirects(false).followSslRedirects(false).build()

    /** `GET {url}/bridge/hello?bridge=&nonce=`, no credentials; null unless a 2xx answer parses. */
    fun hello(url: String, bridgeId: String, nonce: String): HelloAnswer? {
        val req = try {
            Request.Builder().url("$url/bridge/hello".toHttpUrl().newBuilder()
                .addQueryParameter("bridge", bridgeId).addQueryParameter("nonce", nonce).build())
                .get().header("User-Agent", "kidpager-bridge/${BuildConfig.VERSION_NAME}").build()
        } catch (e: IllegalArgumentException) { return null }
        return try {
            noRedirects().newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) { Log.w(TAG, "hello $url -> ${resp.code}"); return null }
                val r = WireJson.decodeFromString(HelloResponse.serializer(), resp.body?.string().orEmpty())
                HelloAnswer(r.bridgeId, r.moveId, r.sig)
            }
        } catch (e: Exception) {
            Log.w(TAG, "hello $url failed: ${e.javaClass.simpleName}")
            null
        }
    }

    /** An authenticated heartbeat to [url] that records nothing in [Moves]; for a URL not in use. */
    fun probe(url: String, status: JsonObject, fcmToken: String?): Probe {
        val tok = Prefs.token(ctx) ?: return Probe.FAIL
        val body = buildJsonObject {
            put("status", status)
            if (fcmToken != null) put("fcmToken", JsonPrimitive(fcmToken))
        }
        val req = try {
            Request.Builder().url("$url/bridge/heartbeat")
                .post(WireJson.encodeToString(JsonElement.serializer(), body).toRequestBody(JSON))
                .header("Authorization", "Bearer $tok")
                .header("User-Agent", "kidpager-bridge/${BuildConfig.VERSION_NAME}").build()
        } catch (e: IllegalArgumentException) { return Probe.FAIL }
        return try {
            noRedirects().newCall(req).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                when {
                    resp.code == 401 -> Probe.UNAUTH
                    Mover.parseMoved(text) != null -> Probe.MOVED
                    resp.isSuccessful -> Probe.OK
                    else -> Probe.FAIL
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "probe $url failed: ${e.javaClass.simpleName}")
            Probe.FAIL
        }
    }

    private fun base(): String = Mover.activeUrl(Prefs.moveState(ctx))

    @Serializable
    data class Caps(val sms: Boolean, val gchat: Boolean, val gvoice: Boolean, val whatsapp: Boolean = false)

    @Serializable
    data class PairRequest(
        val code: String,
        val version: String,
        val accounts: List<String>,
        val simNumber: String? = null,
        val voiceNumber: String? = null,
        val caps: Caps,
    )

    @Serializable
    data class PairResponse(val bridgeId: String, val token: String, val outboxWaitS: Int = 0)

    fun pair(req: PairRequest): PairResponse {
        val body = post("/bridge/pair", WireJson.encodeToString(req), auth = false, retries = 0)
        val r = WireJson.decodeFromString(PairResponse.serializer(), body)
        Prefs.setOutboxWaitS(ctx, r.outboxWaitS)
        return r
    }

    fun events(events: List<BridgeEvent>): EventsResponse {
        val body = post("/bridge/events", WireJson.encodeToString(EventsRequest(events)))
        return WireJson.decodeFromString(EventsResponse.serializer(), body)
    }

    /** `title` is the only handle the UI send path has for a WhatsApp group. */
    @Serializable
    data class OutboxTo(val phone: String? = null, val conversationId: String? = null, val link: String? = null, val title: String? = null)

    @Serializable
    data class OutboxItem(
        val id: String,
        val kind: String,
        val source: String? = null,
        val to: OutboxTo? = null,
        val text: String? = null,
        val link: String? = null,
        val msgId: String? = null,
        val bid: String? = null,
        val replyHint: String? = null,
        val sim: Int? = null,
    )

    @Serializable
    data class OutboxResponse(val items: List<OutboxItem> = emptyList())

    /** `wait` is the long-poll time in seconds; the relay caps it at 25. */
    fun outbox(wait: Int = 0): OutboxResponse {
        val body = get("/bridge/outbox?wait=$wait")
        return WireJson.decodeFromString(OutboxResponse.serializer(), body)
    }

    @Serializable
    data class AckRequest(val state: String, val reason: String? = null, val tier: Int, val parts: Int? = null)

    fun ack(obId: String, state: String, reason: String?, tier: Int, parts: Int? = null) {
        post("/bridge/outbox/$obId/ack", WireJson.encodeToString(AckRequest(state, reason, tier, parts)))
    }

    @Serializable
    data class HeartbeatResponse(val pending: Int = 0, val outboxWaitS: Int = 0)

    /** Returns the relay's count of pending outbox items. */
    fun heartbeat(status: JsonObject, fcmToken: String?): Int {
        val body = buildJsonObject {
            put("status", status)
            if (fcmToken != null) put("fcmToken", JsonPrimitive(fcmToken))
        }
        val text = post("/bridge/heartbeat", WireJson.encodeToString(JsonElement.serializer(), body))
        if (text.isBlank()) return 0
        val r = try { WireJson.decodeFromString(HeartbeatResponse.serializer(), text) } catch (e: Exception) { return 0 }
        Prefs.setOutboxWaitS(ctx, r.outboxWaitS)
        return r.pending
    }

    private fun get(path: String): String = exec(Request.Builder().url(base() + path).get(), auth = true, retries = 2)

    private fun post(path: String, json: String, auth: Boolean = true, retries: Int = 2): String =
        exec(Request.Builder().url(base() + path).post(json.toRequestBody(JSON)), auth, retries)

    private fun exec(rb: Request.Builder, auth: Boolean, retries: Int): String {
        if (auth) {
            val tok = Prefs.token(ctx) ?: throw Unauthorized()
            rb.header("Authorization", "Bearer $tok")
        }
        rb.header("User-Agent", "kidpager-bridge/${BuildConfig.VERSION_NAME}")
        var attempt = 0
        while (true) {
            try {
                http.newCall(rb.build()).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    when {
                        resp.isSuccessful -> { if (auth) Moves.answered(ctx, text); return text }
                        resp.code == 401 && auth -> {
                            if (Moves.unauthorized(ctx)) throw FellBack()
                            Log.w(TAG, "401 from relay; token cleared, re-pair needed")
                            Prefs.clearToken(ctx)
                            onUnauthorized?.invoke(ctx)
                            throw Unauthorized()
                        }
                        resp.code >= 500 && attempt < retries && Mover.parseMoved(text) == null ->
                            Log.w(TAG, "HTTP ${resp.code}, retry ${attempt + 1}")
                        else -> {
                            if (auth) {
                                when {
                                    resp.code < 500 || Mover.parseMoved(text) != null -> Moves.answered(ctx, text)
                                    else -> Moves.failed(ctx)
                                }
                            }
                            throw HttpError(resp.code, text)
                        }
                    }
                }
            } catch (e: Unauthorized) {
                throw e
            } catch (e: FellBack) {
                throw e
            } catch (e: HttpError) {
                throw e
            } catch (e: IOException) {
                if (attempt >= retries) { if (auth) Moves.failed(ctx); throw e }
                Log.w(TAG, "IO ${e.javaClass.simpleName}, retry ${attempt + 1}")
            }
            attempt++
            Thread.sleep(1000L shl (attempt - 1))
        }
    }
}
