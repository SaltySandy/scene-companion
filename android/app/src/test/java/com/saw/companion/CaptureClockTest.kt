package com.saw.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureClockTest {
    @Test
    fun realtimeSensorConvertsToWallClock() {
        val nowWall = 1_700_000_000_500L
        val nowElapsed = 80_000_000_000L
        val sensor = 79_880_000_000L // 120 ms earlier on elapsedRealtime
        val got = CaptureClock.wallMsFromSensor(sensor, nowWall, nowElapsed, nowUptimeNs = 10_000_000_000L)
        assertEquals(CaptureClock.SOURCE_REALTIME, got.source)
        assertEquals(1_700_000_000_380L, got.wallMs)
        assertEquals(120_000_000L, got.sensorAgeNs)
    }

    @Test
    fun uptimeSensorWhenRealtimeDoesNotMatch() {
        val nowWall = 5_000L
        val nowUptime = 40_000_000_000L
        val sensor = 39_750_000_000L // 250 ms earlier on uptime
        val got = CaptureClock.wallMsFromSensor(
            sensorNs = sensor,
            nowWallMs = nowWall,
            nowElapsedNs = 9_000_000_000_000L, // hours away — not this timebase
            nowUptimeNs = nowUptime,
        )
        assertEquals(CaptureClock.SOURCE_UPTIME, got.source)
        assertEquals(4_750L, got.wallMs)
    }

    @Test
    fun prefersCloserTimebaseWhenBothLookPlausible() {
        val nowWall = 10_000L
        val nowElapsed = 20_000_000_000L
        val nowUptime = 20_200_000_000L
        val sensor = 19_900_000_000L
        val got = CaptureClock.wallMsFromSensor(sensor, nowWall, nowElapsed, nowUptime)
        assertEquals(CaptureClock.SOURCE_REALTIME, got.source)
        assertEquals(9_900L, got.wallMs)
    }

    @Test
    fun futureTimestampFallsBack() {
        val got = CaptureClock.wallMsFromSensor(
            sensorNs = 90_000_000_000L,
            nowWallMs = 1_000L,
            nowElapsedNs = 10_000_000_000L,
            nowUptimeNs = 8_000_000_000L,
        )
        assertEquals(CaptureClock.SOURCE_FALLBACK, got.source)
        assertEquals(1_000L, got.wallMs)
    }

    @Test
    fun ancientTimestampFallsBack() {
        val got = CaptureClock.wallMsFromSensor(
            sensorNs = 1_000L,
            nowWallMs = 99_000L,
            nowElapsedNs = 80_000_000_000L,
            nowUptimeNs = 70_000_000_000L,
        )
        assertEquals(CaptureClock.SOURCE_FALLBACK, got.source)
        assertEquals(99_000L, got.wallMs)
    }

    @Test
    fun zeroSensorFallsBack() {
        val got = CaptureClock.wallMsFromSensor(0L, 42L, 1_000_000L, 1_000_000L)
        assertEquals(CaptureClock.SOURCE_FALLBACK, got.source)
        assertEquals(42L, got.wallMs)
    }

    @Test
    fun fallbackWhenPipelineDelayExceedsWindow() {
        val window = 200_000_000L // 200 ms
        val got = CaptureClock.wallMsFromSensor(
            sensorNs = 1_000_000_000L,
            nowWallMs = 5_000L,
            nowElapsedNs = 1_000_000_000L + window + 1,
            nowUptimeNs = 50_000_000_000L,
            matchWindowNs = window,
        )
        assertEquals(CaptureClock.SOURCE_FALLBACK, got.source)
        assertTrue(got.source == CaptureClock.SOURCE_FALLBACK)
    }
}
