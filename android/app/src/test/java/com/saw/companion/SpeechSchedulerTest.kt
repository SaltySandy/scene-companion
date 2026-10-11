package com.saw.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SpeechSchedulerTest {
    @Test
    fun normalSpeechQueuesBehindCurrent() {
        val q = SpeechScheduler<String>()
        q.playing = true
        q.enqueue("frame received, 8 per second", urgent = false)
        q.enqueue("door on the left", urgent = false)
        assertEquals(2, q.size)
        assertTrue(q.playing)
        assertEquals(0, q.interrupted)
    }

    @Test
    fun urgentFlushesBacklogAndMarksInterrupt() {
        val q = SpeechScheduler<String>()
        q.playing = true
        q.enqueue("frame received, 8 per second", urgent = false)
        q.enqueue("nice weather", urgent = false)
        val interrupted = q.enqueue("stop, obstacle ahead", urgent = true)
        assertTrue(interrupted)
        assertEquals(1, q.size)
        assertEquals("stop, obstacle ahead", q.snapshot.single())
        assertFalse(q.playing)
        assertEquals(1, q.interrupted)
    }

    @Test
    fun urgentOnIdleDoesNotCountAsInterrupt() {
        val q = SpeechScheduler<String>()
        val interrupted = q.enqueue("stop, obstacle ahead", urgent = true)
        assertFalse(interrupted)
        assertEquals(1, q.size)
    }

    @Test
    fun takeThenUrgentDropsThePlayingItem() {
        val q = SpeechScheduler<String>()
        q.enqueue("hello", urgent = false)
        assertEquals("hello", q.take())
        assertTrue(q.playing)
        q.enqueue("hazard", urgent = true)
        assertFalse(q.playing)
        assertEquals("hazard", q.take())
    }

    @Test
    fun qualitySwitchDoesNotNeedTheScheduler() {
        // Placeholder: camera rebind is a capture gap, not a speech gap.
        val q = SpeechScheduler<Int>()
        q.enqueue(1, false)
        q.clear()
        assertEquals(0, q.size)
    }
}
