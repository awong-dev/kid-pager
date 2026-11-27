package app.kidpager.bridge

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MoverTest {
    private val src = "https://cloud.example.com"
    private val dst = "https://home.example.org/kid-pager"
    private val me = "b_0123456789"
    private val token = "b_0123456789.test-secret"
    private val nonce = "00112233445566778899aabbccddeeff"
    private val mv = "mv_0123456789ab"
    private val t0 = 1_000_000L
    private val min = 60_000L

    /** A fake world: which URLs answer hello (with which move id) and what a heartbeat gets. */
    private class World(val token: String, val me: String, val relayToken: String = token) {
        val helloMove = mutableMapOf<String, String>()
        val beats = mutableMapOf<String, Probe>()
        val helloCalls = mutableListOf<String>()
        val beatCalls = mutableListOf<String>()
        var badSig = false
        fun env() = Mover.Env(
            bridgeId = me, token = token, nonce = { "00112233445566778899aabbccddeeff" },
            hello = { url, b, n ->
                helloCalls += url
                helloMove[url]?.let { m -> HelloAnswer(b, m, if (badSig) "0".repeat(64) else Mover.helloSig(relayToken, n, b, m)) }
            },
            heartbeat = { url -> beatCalls += url; beats[url] ?: Probe.FAIL },
        )
    }

    private fun world() = World(token, me).apply {
        helloMove[src] = mv; helloMove[dst] = mv
        beats[src] = Probe.OK; beats[dst] = Probe.OK
    }

    private fun pending(now: Long = t0) = Mover.onMoved(MoveState(src), dst, mv, now)
    private fun committed(now: Long = t0) = Mover.commit(pending(now), now)

    private fun failN(s0: MoveState, n: Int, now: Long): MoveState {
        var s = s0
        repeat(n) { s = Mover.onFailure(s, now) }
        return s
    }

    @Test
    fun helloVector() {
        assertEquals("3f7a3cbb91d0d0017834072b515b9b3bf46761e5ed05063cea0db33fdef35b32", Mover.tokenHash(token))
        val sig = Mover.helloSig(token, nonce, me, mv)
        assertEquals("c987c28916b52dc0add4d1bbcd6fa99983b765d36944a0aec29d7ab709f5566d", sig)
        assertTrue(Mover.helloValid(token, me, nonce, mv, HelloAnswer(me, mv, sig)))
    }

    @Test
    fun helloRejectsWrongSigIdsAndMissingParts() {
        val sig = Mover.helloSig(token, nonce, me, mv)
        assertFalse(Mover.helloValid(token, me, nonce, mv, HelloAnswer(me, mv, sig.replaceRange(0, 1, if (sig[0] == '0') "1" else "0"))))
        assertFalse(Mover.helloValid(token, me, nonce, mv, HelloAnswer(me, mv, null)))
        assertFalse(Mover.helloValid(token, me, nonce, mv, HelloAnswer(me, mv, sig.uppercase())))
        assertFalse(Mover.helloValid(token, me, nonce, mv, HelloAnswer("b_other", mv, Mover.helloSig(token, nonce, "b_other", mv))))
        assertFalse(Mover.helloValid(token, me, nonce, mv, HelloAnswer(me, "mv_x", Mover.helloSig(token, nonce, me, "mv_x"))))
        assertFalse(Mover.helloValid(token, me, "ff" + nonce.drop(2), mv, HelloAnswer(me, mv, sig)))
        assertFalse(Mover.helloValid("other-token", me, nonce, mv, HelloAnswer(me, mv, sig)))
        assertFalse(Mover.helloValid(token, me, nonce, mv, null))
        assertFalse(Mover.helloValid(null, me, nonce, mv, HelloAnswer(me, mv, sig)))
        assertFalse(Mover.helloValid(token, null, nonce, mv, HelloAnswer(me, mv, sig)))
    }

    @Test
    fun nonceIs32LowercaseHex() {
        val n = Mover.randomNonce()
        assertTrue(Regex("^[0-9a-f]{32}$").matches(n))
        assertFalse(n == Mover.randomNonce())
    }

    @Test
    fun pendingVerifiedCommitted() {
        val w = world()
        val s = pending()
        assertEquals(dst, s.pendingUrl)
        assertEquals(src, Mover.activeUrl(s))
        assertTrue(Mover.shouldVerify(s, t0))
        val c = Mover.verify(Mover.beginVerify(s, t0), w.env(), t0)
        assertEquals(dst, c.relayUrl)
        assertEquals(src, c.prevUrl)
        assertEquals(t0 + Mover.FALLBACK_MS, c.prevUntil)
        assertEquals(mv, c.homeMoveId)
        assertNull(c.pendingUrl)
        assertTrue(w.beatCalls.isEmpty())
    }

    @Test
    fun verificationFailureStaysAndRetriesEvery5Minutes() {
        val s = Mover.beginVerify(pending(), t0)
        val silent = world().apply { helloMove.clear() }
        assertEquals(src, Mover.verify(s, silent.env(), t0).relayUrl)
        assertEquals(dst, Mover.verify(s, silent.env(), t0).pendingUrl)
        assertEquals(src, Mover.verify(s, world().apply { badSig = true }.env(), t0).relayUrl)
        assertEquals(src, Mover.verify(s, world().apply { helloMove[dst] = "mv_other" }.env(), t0).relayUrl)
        assertEquals(src, Mover.verify(s, World(token, me, relayToken = "other-token").apply { helloMove[dst] = mv }.env(), t0).relayUrl)
        assertFalse(Mover.shouldVerify(s, t0 + Mover.VERIFY_RETRY_MS - 1))
        assertTrue(Mover.shouldVerify(s, t0 + Mover.VERIFY_RETRY_MS))
        assertFalse(Mover.shouldVerify(Mover.onMoved(s, dst, mv, t0 + 1000), t0 + 1000))
    }

    @Test
    fun newMoveIdRestartsVerification() {
        val s = Mover.onMoved(Mover.beginVerify(pending(), t0), dst, "mv_2", t0 + 10)
        assertEquals("mv_2", s.pendingId)
        assertTrue(Mover.shouldVerify(s, t0 + 10))
    }

    @Test
    fun movedToCurrentUrlOrCleartextIsIgnored() {
        assertEquals(MoveState(src), Mover.onMoved(MoveState(src), src, mv, t0))
        assertNull(Mover.parseMoved("""{"moved":{"url":"http://home.example.org","id":"mv_1"}}"""))
        assertNull(Mover.parseMoved("""{"moved":{"url":"https://u:p@home.example.org","id":"mv_1"}}"""))
        assertNull(Mover.parseMoved("""{"moved":{"url":"https://home.example.org","id":5}}"""))
        assertNull(Mover.parseMoved("""{"pending":0}"""))
        assertNull(Mover.parseMoved("not json \"moved\""))
    }

    @Test
    fun parsesMovedFromHeartbeatAndFrozenEventsAndAcks() {
        val want = dst to mv
        assertEquals(want, Mover.parseMoved("""{"pending":0,"moved":{"url":"https://home.example.org/kid-pager/","id":"$mv"}}"""))
        assertEquals(want, Mover.parseMoved("""{"detail":"moved","moved":{"url":"https://home.example.org/kid-pager","id":"$mv"}}"""))
    }

    @Test
    fun answerWithoutMovedClearsPending() {
        val s = Mover.onAnswer(pending(), t0 + 1, moved = false)
        assertNull(s.pendingUrl)
        assertEquals(src, s.relayUrl)
    }

    @Test
    fun noAlternatingBeforeAMove() {
        val s = failN(MoveState(src), 10, t0)
        assertFalse(Mover.alternateDue(s, t0 + 100 * min))
        assertEquals(0, s.fails)
    }

    @Test
    fun threeFailuresThenOtherUrlAfterHelloAndHeartbeat() {
        val w = world()
        val c = committed()
        val two = failN(c, 2, t0 + min)
        assertFalse(Mover.alternateDue(two, t0 + min))
        val three = Mover.onFailure(two, t0 + min)
        assertTrue(Mover.alternateDue(three, t0 + min))
        val s = Mover.alternate(three, w.env(), t0 + min)
        assertTrue(s.onOther)
        assertEquals(src, Mover.activeUrl(s))
        assertEquals(dst, s.relayUrl)
        assertEquals(listOf(src), w.helloCalls)
        assertEquals(listOf(src), w.beatCalls)
        assertEquals(0, s.fails)
    }

    @Test
    fun otherUrlWithBadHelloGetsNoToken() {
        val w = world().apply { badSig = true }
        val three = failN(committed(), 3, t0)
        val s = Mover.alternate(three, w.env(), t0)
        assertFalse(s.onOther)
        assertTrue(w.beatCalls.isEmpty())
        assertEquals(t0 + min, s.nextAt)
    }

    @Test
    fun otherUrlNotSettledUnlessHeartbeatIs2xxWithoutMoved() {
        for (p in listOf(Probe.MOVED, Probe.UNAUTH, Probe.FAIL)) {
            val w = world().apply { beats[src] = p }
            val s = Mover.alternate(failN(committed(), 3, t0), w.env(), t0)
            assertFalse("$p", s.onOther)
            assertEquals(listOf(src), w.beatCalls)
        }
    }

    @Test
    fun backoffSequence() {
        val w = world().apply { helloMove.clear() }
        var s = failN(committed(), 3, t0)
        var now = t0
        val gaps = mutableListOf<Long>()
        repeat(8) {
            assertTrue(Mover.alternateDue(s, now))
            s = Mover.alternate(s, w.env(), now)
            gaps += s.nextAt - now
            assertFalse(Mover.alternateDue(s, s.nextAt - 1))
            now = s.nextAt
        }
        assertEquals(listOf(1L, 2, 5, 15, 30, 60, 60, 60).map { it * min }, gaps)
    }

    @Test
    fun onOtherProbesHomeAndReturnsOnlyWhenItAnswersOk() {
        val w = world()
        var s = Mover.alternate(failN(committed(), 3, t0), w.env(), t0)
        assertTrue(s.onOther)
        assertFalse(Mover.alternateDue(s, t0 + min - 1))
        assertTrue(Mover.alternateDue(s, t0 + min))
        w.beats[dst] = Probe.MOVED
        s = Mover.alternate(s, w.env(), t0 + min)
        assertTrue(s.onOther)
        assertEquals(t0 + 3 * min, s.nextAt)
        w.beats[dst] = Probe.OK
        s = Mover.alternate(s, w.env(), s.nextAt)
        assertFalse(s.onOther)
        assertEquals(dst, Mover.activeUrl(s))
        assertEquals(src, s.prevUrl)
        assertEquals(0, s.step)
    }

    @Test
    fun answerOnHomeResetsFailureStreak() {
        val s = Mover.onAnswer(failN(committed(), 2, t0), t0, moved = false)
        assertEquals(0, s.fails)
        assertFalse(Mover.alternateDue(Mover.onFailure(s, t0), t0))
    }

    @Test
    fun sourceGoneTargetOutageThenRecovers() {
        val w = world().apply { helloMove.remove(src); beats[src] = Probe.FAIL; beats[dst] = Probe.FAIL }
        var s = committed()
        var now = t0
        repeat(3) { now += min; s = Mover.onFailure(s, now) }
        repeat(4) {
            if (Mover.alternateDue(s, now)) s = Mover.alternate(s, w.env(), now)
            assertFalse(s.onOther)
            now += 20 * min
        }
        assertNotNullBoth(s)
        w.beats[dst] = Probe.OK
        s = Mover.onAnswer(s, now, moved = false)
        assertEquals(dst, Mover.activeUrl(s))
        assertEquals(0, s.fails)
    }

    private fun assertNotNullBoth(s: MoveState) {
        assertEquals(dst, s.relayUrl)
        assertEquals(src, s.prevUrl)
    }

    @Test
    fun targetGoneAfterCommitSourceStillUp() {
        val w = world().apply { beats[dst] = Probe.FAIL }
        var s = committed()
        repeat(3) { s = Mover.onFailure(s, t0 + 5 * min) }
        s = Mover.alternate(s, w.env(), t0 + 5 * min)
        assertEquals(src, Mover.activeUrl(s))
        // Home stays down: probes back off and neither URL is dropped.
        var now = s.nextAt
        repeat(6) {
            s = Mover.alternate(s, w.env(), now)
            assertTrue(s.onOther)
            now = s.nextAt
        }
        assertEquals(dst, s.relayUrl)
        assertEquals(src, s.prevUrl)
        // Home is back.
        w.beats[dst] = Probe.OK
        s = Mover.alternate(s, w.env(), now)
        assertEquals(dst, Mover.activeUrl(s))
    }

    @Test
    fun failuresOnOtherDoNotStrandThePhone() {
        val w = world()
        var s = Mover.alternate(failN(committed(), 3, t0), w.env(), t0)
        w.beats[src] = Probe.FAIL
        w.beats[dst] = Probe.OK
        s = failN(s, 3, t0 + min)
        assertTrue(Mover.alternateDue(s, t0 + min))
        s = Mover.alternate(s, w.env(), t0 + min)
        assertEquals(dst, Mover.activeUrl(s))
    }

    @Test
    fun home401MovesToOtherWhichBecomesHome() {
        val w = world()
        val c = committed()
        val (s, keep) = Mover.onUnauthorized(c, w.env(), t0 + 1000)
        assertTrue(keep)
        assertEquals(src, s.relayUrl)
        assertEquals(dst, s.prevUrl)
        assertFalse(s.onOther)
        assertEquals(c.prevUntil, s.prevUntil)
        assertEquals(listOf(src), w.beatCalls)
    }

    @Test
    fun home401AndOther401ClearsToken() {
        val w = world().apply { beats[src] = Probe.UNAUTH }
        assertFalse(Mover.onUnauthorized(committed(), w.env(), t0 + 1000).second)
    }

    @Test
    fun home401WithOtherUnreachableKeepsTokenAndBacksOff() {
        val w = world().apply { helloMove.clear() }
        val (s, keep) = Mover.onUnauthorized(committed(), w.env(), t0 + 1000)
        assertTrue(keep)
        assertEquals(dst, Mover.activeUrl(s))
        assertTrue(w.beatCalls.isEmpty())
        assertTrue(s.nextAt > t0 + 1000)
        assertTrue(s.fails >= 3)
    }

    @Test
    fun other401WhileOnOtherProbesHome() {
        val w = world()
        val s = Mover.alternate(failN(committed(), 3, t0), w.env(), t0)
        val (a, keepA) = Mover.onUnauthorized(s, w.env(), t0 + 1000)
        assertTrue(keepA)
        assertEquals(dst, Mover.activeUrl(a))
        w.beats[dst] = Probe.UNAUTH
        assertFalse(Mover.onUnauthorized(s, w.env(), t0 + 1000).second)
    }

    @Test
    fun unauthorizedWithoutWindowClearsTokenAsBefore() {
        val w = world()
        assertFalse(Mover.onUnauthorized(MoveState(src), w.env(), t0).second)
        assertFalse(Mover.onUnauthorized(committed(), w.env(), t0 + Mover.FALLBACK_MS).second)
        assertTrue(w.beatCalls.isEmpty())
    }

    @Test
    fun windowEndForgetsTheUnusedUrlOnly() {
        val c = committed()
        assertEquals(src, Mover.expire(c, t0 + Mover.FALLBACK_MS - 1).prevUrl)
        val home = Mover.expire(c, t0 + Mover.FALLBACK_MS)
        assertNull(home.prevUrl)
        assertEquals(dst, Mover.activeUrl(home))
        val onOther = Mover.expire(c.copy(onOther = true), t0 + Mover.FALLBACK_MS)
        assertNull(onOther.prevUrl)
        assertEquals(src, onOther.relayUrl)
        assertEquals(src, Mover.activeUrl(onOther))
        assertFalse(Mover.alternateDue(failN(home, 5, t0), t0 + Mover.FALLBACK_MS + 100 * min))
    }

    @Test
    fun sendBridgesBackIsFollowedTheSameWay() {
        val c = committed()
        val back = Mover.onMoved(c, src, mv, t0 + 5000)
        assertEquals(src, back.pendingUrl)
        assertEquals(dst, back.relayUrl)
        val bad = Mover.verify(back, world().apply { badSig = true }.env(), t0 + 5000)
        assertEquals(dst, bad.relayUrl)
        val ok = Mover.verify(back, world().env(), t0 + 5000)
        assertEquals(src, ok.relayUrl)
        assertEquals(dst, ok.prevUrl)
        assertNull(ok.pendingUrl)
        assertEquals(t0 + 5000 + Mover.FALLBACK_MS, ok.prevUntil)
    }

    @Test
    fun movedFromTheOtherUrlWhileOnOtherCommitsHome() {
        val w = world()
        val s = Mover.alternate(failN(committed(), 3, t0), w.env(), t0)
        val p = Mover.onMoved(s, dst, mv, t0 + min)
        assertEquals(dst, p.pendingUrl)
        val c = Mover.verify(p, w.env(), t0 + min)
        assertEquals(dst, c.relayUrl)
        assertEquals(src, c.prevUrl)
        assertFalse(c.onOther)
    }
}
