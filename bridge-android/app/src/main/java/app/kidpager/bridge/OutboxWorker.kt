package app.kidpager.bridge

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/** Polls `GET /bridge/outbox`, dispatches each new item and acks its result to the relay. */
class OutboxWorker(private val ctx: Context, private val scope: CoroutineScope) {
    companion object {
        private const val TAG = "outbox"
        private val kicks = Channel<Unit>(Channel.CONFLATED)
        /** Poll now. */
        fun kick() { kicks.trySend(Unit) }
    }

    private val client = RelayClient(ctx)
    private val tracker = AckTracker()
    private var job: Job? = null

    fun start() {
        if (job?.isActive == true) return
        job = scope.launch { loop() }
    }

    fun stop() { job?.cancel() }

    private suspend fun loop() {
        while (true) {
            var failed = false
            if (Prefs.isPaired(ctx)) {
                try {
                    pollOnce()
                } catch (e: RelayClient.Unauthorized) {
                    Log.w(TAG, "unauthorized; waiting for re-pair")
                    failed = true
                } catch (e: Exception) {
                    Log.w(TAG, "poll failed: ${e.message}")
                    failed = true
                }
            }
            // With a long-poll hint the request itself is the wait, so loop at once; after a
            // failure back off 2 s so a down relay does not spin.
            val period = when {
                Prefs.outboxWaitS(ctx) <= 0 || !Prefs.isPaired(ctx) -> Prefs.pollIntervalSec(ctx) * 1000L
                failed -> 2000L
                else -> 0L
            }
            withTimeoutOrNull(period) { kicks.receive() }
        }
    }

    private suspend fun pollOnce() {
        flushAcks()
        for (id in tracker.purge()) Log.w(TAG, "ack $id given up after ${AckTracker.MAX_ACK_ATTEMPTS} attempts / 24 h; dropped")
        var more = true
        var wait = Prefs.outboxWaitS(ctx) // only the first request of a pass may hold
        while (more) {
            val items = client.outbox(wait).items
            wait = 0
            more = false
            for (item in items) {
                if (!tracker.begin(item.id)) continue
                val started = System.currentTimeMillis()
                val out = try {
                    Dispatcher.dispatch(ctx, item)
                } catch (e: Exception) {
                    Log.e(TAG, "dispatch ${item.id} threw", e)
                    Dispatcher.Outcome("failed", "exception", 1)
                }
                tracker.complete(item.id, out.state, out.reason, out.tier, out.parts)
                Log.i(TAG, "ob=${item.id} kind=${item.kind} src=${item.source ?: "-"} state=${out.state} reason=${out.reason ?: "-"} tier=${out.tier} ms=${System.currentTimeMillis() - started}")
                more = true
            }
            flushAcks()
        }
    }

    private fun flushAcks() {
        for ((id, r) in tracker.pendingAcks()) {
            try {
                client.ack(id, r.state, r.reason, r.tier, r.parts)
                tracker.acked(id)
            } catch (e: RelayClient.Unauthorized) {
                throw e
            } catch (e: RelayClient.HttpError) {
                if (e.moved) { Log.w(TAG, "ack $id: relay has moved; kept for the new URL"); continue }
                when (e.code) {
                    404 -> { Log.w(TAG, "ack $id 404; relay forgot it, forgetting too"); tracker.forget(id) }
                    409, 422 -> { Log.w(TAG, "ack $id rejected ${e.code}; dropping"); tracker.acked(id) }
                    else -> Log.w(TAG, "ack $id failed ${e.code} (attempt ${tracker.ackFailed(id)}); will retry")
                }
            } catch (e: Exception) {
                Log.w(TAG, "ack $id failed: ${e.message} (attempt ${tracker.ackFailed(id)}); will retry")
            }
        }
    }
}
