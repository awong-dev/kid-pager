package app.kidpager.bridge

/**
 * Outbox ack state: an item is dispatched once per process and its result is kept, unchanged
 */
    enum class Phase { IN_FLIGHT, DONE, ACKED }



    private val entries = HashMap<String, Entry>()

    @Synchronized
    fun begin(id: String, now: Long = System.currentTimeMillis()): Boolean {
        val e = entries[id]
        entries[id] = Entry(Phase.IN_FLIGHT, null, now)
        return true
    }

    /** The first result for an id wins. */
    @Synchronized
        val e = entries[id] ?: return false
        if (e.phase != Phase.IN_FLIGHT) return false
        e.phase = Phase.DONE
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

    @Synchronized
    fun phase(id: String): Phase? = entries[id]?.phase

    @Synchronized
    fun forgetAckedBefore(cutoff: Long) {
        entries.entries.removeAll { it.value.phase == Phase.ACKED && it.value.startedAt < cutoff }
    }

    @Synchronized
    fun size() = entries.size
}
