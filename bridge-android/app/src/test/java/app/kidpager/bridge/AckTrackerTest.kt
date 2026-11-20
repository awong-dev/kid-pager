package app.kidpager.bridge

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AckTrackerTest {
    @Test
    fun dispatchOncePerItem() {
        val t = AckTracker()
        assertTrue(t.begin("ob_1"))
        assertFalse(t.begin("ob_1"))
        assertEquals(AckTracker.Phase.IN_FLIGHT, t.phase("ob_1"))
        assertTrue(t.pendingAcks().isEmpty())
    }

    @Test
    fun completeThenAckLifecycle() {
        val t = AckTracker()
        t.begin("ob_1")
        assertTrue(t.complete("ob_1", "sent", null, 1))
        assertEquals(listOf("ob_1" to AckTracker.Result("sent", null, 1)), t.pendingAcks())
        // A retry sees the same pending result.
        assertEquals(listOf("ob_1" to AckTracker.Result("sent", null, 1)), t.pendingAcks())
        t.acked("ob_1")
        assertTrue(t.pendingAcks().isEmpty())
        assertEquals(AckTracker.Phase.ACKED, t.phase("ob_1"))
        assertFalse(t.begin("ob_1"))
    }

    @Test
    fun firstResultWins() {
        val t = AckTracker()
        t.begin("ob_2")
        assertTrue(t.complete("ob_2", "failed", "ui_changed", 2))
        assertFalse(t.complete("ob_2", "sent", null, 1))
        assertEquals(AckTracker.Result("failed", "ui_changed", 2), t.pendingAcks().single().second)
    }

    @Test
    fun completeUnknownIsIgnoredAndOldAckedForgotten() {
        val t = AckTracker()
        assertFalse(t.complete("nope", "sent", null, 1))
        t.begin("ob_3", now = 1000L)
        t.complete("ob_3", "sent", null, 1)
        t.acked("ob_3")
        t.forgetAckedBefore(500L)
        assertEquals(1, t.size())
        t.forgetAckedBefore(2000L)
        assertEquals(0, t.size())
        assertTrue(t.begin("ob_3"))
        // In-flight items are never forgotten.
        t.forgetAckedBefore(Long.MAX_VALUE)
        assertEquals(1, t.size())
    }
}

class AckTrackerPurgeTest {
    @Test
    fun forgetOn404AllowsRedispatch() {
        val t = AckTracker()
        t.begin("ob_1")
        t.complete("ob_1", "sent", null, 1)
        t.forget("ob_1")
        assertEquals(null, t.phase("ob_1"))
        assertTrue(t.begin("ob_1"))
    }

    @Test
    fun doneEntryPurgedAfterMaxAttemptsThenReissued() {
        val t = AckTracker(maxAttempts = 3)
        t.begin("ob_2", now = 1000L)
        t.complete("ob_2", "failed", "ui_changed", 2)
        repeat(2) { t.ackFailed("ob_2") }
        assertTrue(t.purge(now = 2000L).isEmpty())
        assertEquals(1, t.pendingAcks().size)
        assertEquals(3, t.ackFailed("ob_2"))
        assertEquals(listOf("ob_2"), t.purge(now = 2000L))
        assertTrue(t.pendingAcks().isEmpty())
        assertTrue(t.begin("ob_2", now = 2000L))
    }

    @Test
    fun doneEntryPurgedAfterWindowAndBeginAcceptsOldEntries() {
        val t = AckTracker(windowMs = 1000L)
        t.begin("ob_3", now = 0L)
        t.complete("ob_3", "sent", null, 1)
        assertTrue(t.purge(now = 500L).isEmpty())
        assertFalse(t.begin("ob_3", now = 500L))
        // Past the window, begin() accepts the re-issue without a purge call.
        assertTrue(t.begin("ob_3", now = 1500L))
        assertEquals(AckTracker.Phase.IN_FLIGHT, t.phase("ob_3"))
        assertEquals(0, t.attempts("ob_3"))
        t.complete("ob_3", "sent", null, 1)
        assertEquals(listOf("ob_3"), t.purge(now = 3000L))
    }

    @Test
    fun inFlightNeverPurgedOrReplaced() {
        val t = AckTracker(windowMs = 10L, maxAttempts = 1)
        t.begin("ob_4", now = 0L)
        assertTrue(t.purge(now = 1_000_000L).isEmpty())
        assertFalse(t.begin("ob_4", now = 1_000_000L))
        assertEquals(1, t.size())
    }

    @Test
    fun ackedEntriesAgeOut() {
        val t = AckTracker(windowMs = 100L)
        t.begin("ob_5", now = 0L)
        t.complete("ob_5", "sent", null, 1)
        t.acked("ob_5")
        t.purge(now = 50L)
        assertEquals(1, t.size())
        t.purge(now = 100L)
        assertEquals(0, t.size())
    }

    @Test
    fun partsSurviveRoundTrip() {
        val t = AckTracker()
        t.begin("ob_1")
        assertTrue(t.complete("ob_1", "sent", null, 1, 3))
        assertEquals(listOf("ob_1" to AckTracker.Result("sent", null, 1, 3)), t.pendingAcks())
        t.ackFailed("ob_1")
        assertEquals(3, t.pendingAcks().single().second.parts)
    }

    @Test
    fun ackBodyCarriesParts() {
        val sent = WireJson.encodeToString(RelayClient.AckRequest.serializer(), RelayClient.AckRequest("sent", null, 1, 2))
        assertTrue(sent, sent.contains("\"parts\":2"))
        val failed = WireJson.encodeToString(RelayClient.AckRequest.serializer(), RelayClient.AckRequest("failed", "x", 1))
        assertFalse(failed, failed.contains("parts"))
    }
}
