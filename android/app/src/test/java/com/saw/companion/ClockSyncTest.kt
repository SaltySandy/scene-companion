package com.saw.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ClockSyncTest {
    @Test
    fun zeroSkewSymmetricPath() {
        // Phone and laptop agree. 10 ms each way.
        val offset = ClockSync.offsetMs(1_000, 1_010, 1_012, 1_022)
        val rtt = ClockSync.rttMs(1_000, 1_010, 1_012, 1_022)
        assertEquals(0L, offset)
        assertEquals(20L, rtt)
    }

    @Test
    fun recoversConstantSkew() {
        // Phone is 250 ms ahead of the laptop.
        val phoneSkew = 250L
        val t0 = 10_000L + phoneSkew
        val t1 = 10_008L
        val t2 = 10_009L
        val t3 = 10_017L + phoneSkew
        assertEquals(-250L, ClockSync.offsetMs(t0, t1, t2, t3))
    }

    @Test
    fun minRttWinsOverJitterySamples() {
        val sync = ClockSync()
        // Jittery Wi-Fi sample, 80 ms RTT, biased.
        sync.consider(0, 40, 42, 120)
        // Clean USB-like sample, 8 ms RTT, true offset +30.
        val best = sync.consider(1_000, 1_034, 1_035, 1_009)
        assertEquals(8L, best.rttMs)
        assertEquals(30L, best.offsetMs)
        assertEquals(2, sync.samples)
    }

    @Test
    fun asymmetricPathBiasesOffset() {
        // 5 ms up, 45 ms down — NTP offset is wrong by (down-up)/2 = 20 ms.
        val offset = ClockSync.offsetMs(0, 5, 6, 51)
        assertEquals(-20L, offset)
        assertEquals(50L, ClockSync.rttMs(0, 5, 6, 51))
    }

    @Test
    fun driftShowsUpAsChangingOffset() {
        val sync = ClockSync()
        sync.consider(0, 10, 11, 21) // offset 0
        // Phone ran 15 ms fast over the next second.
        val later = sync.consider(1_000, 995, 996, 1_006)
        // Second sample has smaller RTT?  (1006-1000)-(996-995)=6-1=5 vs first 10.
        assertTrue(later.rttMs <= 10)
        assertEquals(-7L, later.offsetMs) // ((995-1000)+(996-1006))/2 = (-5 + -10)/2
    }

    @Test
    fun resetClearsBest() {
        val sync = ClockSync()
        sync.consider(0, 1, 1, 2)
        sync.reset()
        assertEquals(null, sync.best)
        assertEquals(0, sync.samples)
    }
}
