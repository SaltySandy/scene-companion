package com.saw.companion

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CuePolicyTest {
    private fun heartbeat(level: String?, state: String = "ok", left: String = "clear", right: String = "clear") =
        Protocol.parseProximity(
            JSONObject(
                """{"v":1,"type":"proximity","seq":1,"state":"$state","level":${level?.let { "\"$it\"" } ?: "null"},
                "levels":{"left":"$left","center":"${level ?: "clear"}","right":"$right"},"frame_key":42}""",
            ),
        )

    private fun connected() = CuePolicy().also { it.onConnection(true) }

    @Test
    fun nothingYetIsLost() {
        assertEquals(CueState.LOST, connected().cue(0).state)
    }

    @Test
    fun levelsMapToStates() {
        val p = connected()
        p.onProximity(heartbeat("near"), 1_000)
        assertEquals(CueState.NEAR, p.cue(1_100).state)
        assertEquals(42L, p.cue(1_100).frameKey)
        p.onProximity(heartbeat("danger"), 1_200)
        assertEquals(CueState.DANGER, p.cue(1_250).state)
        p.onProximity(heartbeat("clear"), 1_300)
        assertEquals(CueState.CLEAR, p.cue(1_350).state)
    }

    @Test
    fun watchdogUnknownAndDisconnectAreLost() {
        val p = connected()
        p.onProximity(heartbeat("clear"), 1_000)
        assertEquals(CueState.CLEAR, p.cue(2_000).state) // exactly 1 s: still fresh
        assertEquals(CueState.LOST, p.cue(2_001).state) // silence is not clear
        p.onProximity(heartbeat(null, state = "unknown"), 3_000)
        assertEquals(CueState.LOST, p.cue(3_010).state)
        p.onProximity(heartbeat("clear"), 3_100)
        p.onConnection(false)
        assertEquals(CueState.LOST, p.cue(3_110).state)
    }

    @Test
    fun panPointsAtTheWorseSide() {
        val p = connected()
        p.onProximity(heartbeat("clear", right = "danger"), 0)
        assertEquals(1f, p.cue(10).pan, 0f)
        p.onProximity(heartbeat("clear", left = "near"), 20)
        assertEquals(-0.5f, p.cue(30).pan, 0f)
    }

    @Test
    fun announcementsAreDebounced() {
        val p = connected()
        p.onProximity(heartbeat("clear"), 0)
        assertNull(p.announcement(p.cue(0), 0))
        assertEquals("sensor on", p.announcement(p.cue(500), 500))
        // One stale blip shorter than 1 s: no speech.
        p.onProximity(heartbeat(null, state = "unknown"), 600)
        assertNull(p.announcement(p.cue(700), 700))
        p.onProximity(heartbeat("clear"), 900)
        assertNull(p.announcement(p.cue(950), 950))
        // Real loss: spoken once after 1 s.
        assertNull(p.announcement(p.cue(2_000), 2_000))
        assertEquals("sensor lost", p.announcement(p.cue(3_000), 3_000))
        assertNull(p.announcement(p.cue(3_500), 3_500))
    }

    @Test
    fun eachFrameIsAckedOnce() {
        val p = connected()
        assertNull(p.takeAck(p.cue(0))) // no frame yet
        p.onProximity(heartbeat("near"), 100)
        assertEquals(42L, p.takeAck(p.cue(110)))
        assertNull(p.takeAck(p.cue(130))) // same frame, later chunk
    }

    @Test
    fun synthBeepsRestartAndPan() {
        val synth = ToneSynth(sampleRate = 1_000)
        val chunk = ShortArray(2 * 20) // 20 ms at 1 kHz
        assertTrue(synth.render(CuePatterns.NEAR, 0f, chunk)) // beep starts at once
        assertFalse(synth.render(CuePatterns.NEAR, 0f, chunk)) // same pattern: continues
        assertTrue(synth.render(CuePatterns.DANGER, 0f, chunk)) // change restarts
        val right = ShortArray(2 * 40)
        synth.render(CuePatterns.LOST, 1f, right) // fully right
        assertTrue((0 until 40).all { right[2 * it] == 0.toShort() })
        assertTrue((0 until 40).any { right[2 * it + 1] != 0.toShort() })
    }

    @Test
    fun clearTickIsMostlySilent() {
        val synth = ToneSynth(sampleRate = 1_000)
        val twoSeconds = ShortArray(2 * 2_000)
        synth.render(CuePatterns.CLEAR, 0f, twoSeconds)
        val loud = (0 until 2_000).count { twoSeconds[2 * it] != 0.toShort() }
        assertTrue("clear tick should sound briefly, was $loud ms", loud in 10..25)
    }
}
