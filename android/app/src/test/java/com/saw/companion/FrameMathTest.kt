package com.saw.companion

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class FrameMathTest {
    // 4x2 NV21: Y = 0..7, then 2 VU pairs (V=100+i, U=200+i).
    private val w = 4
    private val h = 2
    private val img = ByteArray(w * h) { it.toByte() } +
        byteArrayOf(100, 200.toByte(), 101, 201.toByte())

    @Test
    fun rotate90MovesTopLeftToTopRight() {
        val out = FrameMath.rotateNv21(img, w, h, 90)
        // Rotated image is 2 wide, 4 tall. Source row 0 becomes the right column.
        // Y rows: [4,0], [5,1], [6,2], [7,3]
        assertArrayEquals(byteArrayOf(4, 0, 5, 1, 6, 2, 7, 3), out.copyOfRange(0, 8))
        // Chroma grid 2x1 -> 1x2: pair 0 on top, pair 1 below.
        assertArrayEquals(byteArrayOf(100, 200.toByte(), 101, 201.toByte()), out.copyOfRange(8, 12))
    }

    @Test
    fun rotate180ReversesBothPlanes() {
        val out = FrameMath.rotateNv21(img, w, h, 180)
        assertArrayEquals(byteArrayOf(7, 6, 5, 4, 3, 2, 1, 0), out.copyOfRange(0, 8))
        assertArrayEquals(byteArrayOf(101, 201.toByte(), 100, 200.toByte()), out.copyOfRange(8, 12))
    }

    @Test
    fun fourQuarterTurnsAreIdentity() {
        var cur = img
        var cw = w
        var ch = h
        repeat(4) {
            cur = FrameMath.rotateNv21(cur, cw, ch, 90)
            val t = cw; cw = ch; ch = t
        }
        assertArrayEquals(img, cur)
        // 180 keeps 4x2, then 90 gives 2x4: same as 270 directly.
        assertArrayEquals(
            FrameMath.rotateNv21(img, w, h, 270),
            FrameMath.rotateNv21(FrameMath.rotateNv21(img, w, h, 180), w, h, 90),
        )
    }

    @Test
    fun normalizeRotation() {
        assertEquals(0, FrameMath.normalizeRotation(360))
        assertEquals(270, FrameMath.normalizeRotation(-90))
        assertEquals(90, FrameMath.normalizeRotation(100))
    }

    @Test
    fun sensorTimeOnElapsedRealtimeBase() {
        // Frame exposed 60 ms ago on the elapsedRealtime clock.
        val ts = FrameMath.sensorToEpochMs(
            sensorNs = 5_000_000_000L - 60_000_000L,
            elapsedNowNs = 5_000_000_000L,
            uptimeNowNs = 3_000_000_000L,
            epochNowMs = 1_700_000_000_000L,
        )
        assertEquals(1_700_000_000_000L - 60, ts)
    }

    @Test
    fun sensorTimeOnUptimeBase() {
        // Device slept 2 s, so elapsed is 2 s ahead of uptime; sensor uses uptime.
        val ts = FrameMath.sensorToEpochMs(
            sensorNs = 3_000_000_000L - 45_000_000L,
            elapsedNowNs = 5_000_000_000L,
            uptimeNowNs = 3_000_000_000L,
            epochNowMs = 1_700_000_000_000L,
        )
        assertEquals(1_700_000_000_000L - 45, ts)
    }

    @Test
    fun unknownSensorBaseFallsBackToNow() {
        val ts = FrameMath.sensorToEpochMs(42L, 9_000_000_000_000L, 8_000_000_000_000L, 1_700_000_000_000L)
        assertEquals(1_700_000_000_000L, ts)
    }
}
