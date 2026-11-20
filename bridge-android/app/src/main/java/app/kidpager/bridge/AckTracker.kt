package app.kidpager.bridge

/**
 * Outbox ack state: an item is dispatched once per process and its result is kept, unchanged
 * across retries, until the relay accepts the ack.
 */
class AckTracker(
    private val windowMs: Long = WINDOW_MS,
    private val maxAttempts: Int = MAX_ACK_ATTEMPTS,
) {
    companion object {
        const val WINDOW_MS = 24L * 3600 * 1000
        const val MAX_ACK_ATTEMPTS = 20
    }

    enum class Phase { IN_FLIGHT, DONE, ACKED }

    data class Result(val state: String, val reason: String?, val tier: Int, val parts: Int? = null)

    private class Entry(var phase: Phase, var result: Result?, val startedAt: Long, var attempts: Int = 0)

    private val entries = HashMap<String, Entry>()

    /** True when `id` is new or its finished entry is older than the window (a relay re-issue). */
    @Synchronized
    fun begin(id: String, now: Long = System.currentTimeMillis()): Boolean {
        val e = entries[id]
        if (e != null && (e.phase == Phase.IN_FLIGHT || now - e.startedAt < windowMs)) return false
        entries[id] = Entry(Phase.IN_FLIGHT, null, now)
        return true
    }

    /** The first result for an id wins. */
    @Synchronized
    fun complete(id: String, state: String, reason: String?, tier: Int, parts: Int? = null): Boolean {
        val e = entries[id] ?: return false
        if (e.phase != Phase.IN_FLIGHT) return false
        e.phase = Phase.DONE
        e.result = Result(state, reason, tier, parts)
        return true
    }

    /** Items whose ack the relay has not accepted yet. */
    @Synchronized
    fun pendingAcks(): List<Pair<String, Result>> =
        entries.filter { it.value.phase == Phase.DONE }.map { it.key to it.value.result!! }

    @Synchronized
    fun acked(id: String) {
        entries[id]?.let { it.phase = Phase.ACKED }
    }

    /** For a 404 on ack: the relay no longer knows the item. */
    @Synchronized
    fun forget(id: String) { entries.remove(id) }

    /** Returns the attempt count so far. */
    @Synchronized
    fun ackFailed(id: String): Int {
        val e = entries[id] ?: return 0
        e.attempts += 1
        return e.attempts
    }

    @Synchronized
    fun phase(id: String): Phase? = entries[id]?.phase

    @Synchronized
    fun attempts(id: String): Int = entries[id]?.attempts ?: 0

    /** Drops entries past the window or out of ack attempts; returns the unacked ids given up on. */
    @Synchronized
    fun purge(now: Long = System.currentTimeMillis()): List<String> {
        val dropped = ArrayList<String>()
        val it = entries.entries.iterator()
        while (it.hasNext()) {
            val (id, e) = it.next()
            val old = now - e.startedAt >= windowMs
            when (e.phase) {
                Phase.ACKED -> if (old) it.remove()
                Phase.DONE -> if (old || e.attempts >= maxAttempts) { it.remove(); dropped += id }
                Phase.IN_FLIGHT -> {}
            }
        }
        return dropped
    }

    @Synchronized
    fun forgetAckedBefore(cutoff: Long) {
        entries.entries.removeAll { it.value.phase == Phase.ACKED && it.value.startedAt < cutoff }
    }

    @Synchronized
    fun size() = entries.size
}
