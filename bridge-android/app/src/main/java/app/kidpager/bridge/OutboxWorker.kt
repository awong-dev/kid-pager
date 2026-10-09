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
            if (Prefs.isPaired(ctx)) {
                try {
                    pollOnce()
                } catch (e: RelayClient.Unauthorized) {
                    Log.w(TAG, "unauthorized; waiting for re-pair")
                } catch (e: Exception) {
                    Log.w(TAG, "poll failed: ${e.message}")
                }
            }
        }
    }

    private suspend fun pollOnce() {
        flushAcks()
        var more = true
        while (more) {
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
                Log.i(TAG, "ob=${item.id} kind=${item.kind} src=${item.source ?: "-"} state=${out.state} reason=${out.reason ?: "-"} tier=${out.tier} ms=${System.currentTimeMillis() - started}")
                more = true
            }
            flushAcks()
        }
    }

    private fun flushAcks() {
        for ((id, r) in tracker.pendingAcks()) {
            try {
                tracker.acked(id)
            } catch (e: RelayClient.Unauthorized) {
                throw e
            } catch (e: RelayClient.HttpError) {
            } catch (e: Exception) {
            }
        }
    }
}
