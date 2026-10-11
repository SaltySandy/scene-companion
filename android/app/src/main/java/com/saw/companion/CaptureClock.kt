package com.saw.companion

/**
 * Convert a CameraX / Camera2 sensor timestamp (nanoseconds) to phone wall-clock ms.
 *
 * Camera2 `SENSOR_TIMESTAMP` is usually `elapsedRealtimeNanos`. Some devices use
 * uptime / `System.nanoTime()`. If neither timebase is within [0, MATCH_WINDOW_NS]
 * of "now", we fall back to the analyzer wall clock (legacy behaviour).
 */
object CaptureClock {
    const val MATCH_WINDOW_NS: Long = 5_000_000_000L

    const val SOURCE_REALTIME = "realtime"
    const val SOURCE_UPTIME = "uptime"
    const val SOURCE_FALLBACK = "fallback"

    data class Instant(
        val wallMs: Long,
        val source: String,
        val sensorAgeNs: Long,
    )

    fun wallMsFromSensor(
        sensorNs: Long,
        nowWallMs: Long,
        nowElapsedNs: Long,
        nowUptimeNs: Long,
        matchWindowNs: Long = MATCH_WINDOW_NS,
    ): Instant {
        if (sensorNs <= 0L) {
            return Instant(nowWallMs, SOURCE_FALLBACK, 0L)
        }
        val elapsedAge = nowElapsedNs - sensorNs
        val uptimeAge = nowUptimeNs - sensorNs
        val elapsedOk = elapsedAge in 0L until matchWindowNs
        val uptimeOk = uptimeAge in 0L until matchWindowNs
        return when {
            elapsedOk && (!uptimeOk || elapsedAge <= uptimeAge) ->
                Instant(nowWallMs - elapsedAge / 1_000_000L, SOURCE_REALTIME, elapsedAge)
            uptimeOk ->
                Instant(nowWallMs - uptimeAge / 1_000_000L, SOURCE_UPTIME, uptimeAge)
            else ->
                Instant(nowWallMs, SOURCE_FALLBACK, 0L)
        }
    }
}
