package app.kidpager.bridge

import android.content.Context
import java.util.concurrent.atomic.AtomicBoolean

/** Feeds what the relay answered into [Mover] and keeps the resulting [MoveState] in [Prefs]. */
object Moves {
    private const val TAG = "move"
    private val lock = Any()
    private val busy = AtomicBoolean(false)

    private fun env(ctx: Context): Mover.Env {
        val client = RelayClient(ctx)
        return Mover.Env(
            bridgeId = Prefs.bridgeId(ctx),
            token = Prefs.token(ctx),
            nonce = Mover::randomNonce,
            hello = { url, bridge, nonce -> client.hello(url, bridge, nonce) },
            heartbeat = { url -> client.probe(url, Status.gather(ctx).toJson(), FcmTokens.current(ctx)) },
        )
    }

    /** Stores [after] if the URLs in use are still those of [before]; other fields keep their stored values. */
    private fun store(ctx: Context, before: MoveState, after: MoveState) {
        if (after == before) return
        synchronized(lock) {
            val cur = Prefs.moveState(ctx)
            if (cur.relayUrl != before.relayUrl || cur.prevUrl != before.prevUrl || cur.onOther != before.onOther) return
            Prefs.saveMoveState(ctx, after.copy(pendingUrl = cur.pendingUrl, pendingId = cur.pendingId, lastTryAt = cur.lastTryAt))
            if (Mover.activeUrl(after) != Mover.activeUrl(before)) Log.w(TAG, "relay in use: ${Mover.activeUrl(after)}")
        }
    }

    /** A non-401 HTTP answer from the URL in use, or a 503 that carries `moved`; [body] may carry `moved`. */
    fun answered(ctx: Context, body: String) {
        val moved = Mover.parseMoved(body)
        val now = System.currentTimeMillis()
        var verify: MoveState? = null
        var alternate: MoveState? = null
        synchronized(lock) {
            val before = Prefs.moveState(ctx)
            var s = Mover.onAnswer(before, now, moved != null)
            if (moved != null) {
                s = Mover.onMoved(s, moved.first, moved.second, now)
                if (Mover.shouldVerify(s, now)) { s = Mover.beginVerify(s, now); verify = s }
            }
            if (s != before) Prefs.saveMoveState(ctx, s)
            if (Mover.alternateDue(s, now)) alternate = s
        }
        verify?.let { check(ctx, it) }
        alternate?.let { tryOther(ctx, it) }
    }

    /** A 5xx without `moved` or a network failure from the URL in use. */
    fun failed(ctx: Context) {
        val now = System.currentTimeMillis()
        var alternate: MoveState? = null
        synchronized(lock) {
            val before = Prefs.moveState(ctx)
            val s = Mover.onFailure(before, now)
            if (s != before) Prefs.saveMoveState(ctx, s)
            if (Mover.alternateDue(s, now)) alternate = s
        }
        alternate?.let { tryOther(ctx, it) }
    }

    /** True when the token stays after a 401: the other URL took over or could not be reached. */
    fun unauthorized(ctx: Context): Boolean {
        val before = synchronized(lock) { Prefs.moveState(ctx) }
        if (before.prevUrl == null) return false
        val (s, keep) = Mover.onUnauthorized(before, env(ctx), System.currentTimeMillis())
        store(ctx, before, s)
        if (keep) Log.w(TAG, "401 from ${Mover.activeUrl(before)}; token kept")
        return keep
    }

    private fun tryOther(ctx: Context, before: MoveState) {
        if (!busy.compareAndSet(false, true)) return
        try {
            store(ctx, before, Mover.alternate(before, env(ctx), System.currentTimeMillis()))
        } finally {
            busy.set(false)
        }
    }

    private fun check(ctx: Context, started: MoveState) {
        val url = started.pendingUrl ?: return
        val committed = Mover.verify(started, env(ctx), System.currentTimeMillis())
        if (committed.relayUrl == started.relayUrl) { Log.w(TAG, "move to $url not verified; staying on ${Mover.activeUrl(started)}"); return }
        synchronized(lock) {
            val cur = Prefs.moveState(ctx)
            if (cur.relayUrl != started.relayUrl || cur.onOther != started.onOther || cur.pendingUrl != url || cur.pendingId != started.pendingId) return
            Prefs.saveMoveState(ctx, Mover.commit(cur, System.currentTimeMillis()))
            Log.i(TAG, "moved to $url (other URL kept 7 days)")
        }
        OutboxWorker.kick()
        EventQueue.kick()
    }
}
