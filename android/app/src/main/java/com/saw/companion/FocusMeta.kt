package com.saw.companion

import org.json.JSONObject
import kotlin.math.abs

/**
 * Camera2 AF metadata attached to a JPEG. Values match
 * [android.hardware.camera2.CaptureResult] / [android.hardware.camera2.CameraCharacteristics]
 * so unit tests do not need the Camera2 SDK.
 *
 * Calibration: 0 UNCALIBRATED, 1 APPROXIMATE, 2 CALIBRATED.
 * AF state: 0 INACTIVE, 1 PASSIVE_SCAN, 2 PASSIVE_FOCUSED, 3 ACTIVE_SCAN,
 * 4 FOCUSED_LOCKED, 5 NOT_FOCUSED_LOCKED, 6 PASSIVE_UNFOCUSED.
 */
data class FrameFocus(
    val sensorTsNs: Long,
    val focusDistanceDiopters: Float?,
    val afState: Int?,
    val focusCalibration: Int?,
) {
    fun toFields(captureTsMs: Long): Map<String, Any?> = mapOf(
        "ts" to captureTsMs,
        "sensor_ts_ns" to sensorTsNs,
        "focus_distance_diopters" to focusDistanceDiopters,
        "af_state" to afState,
        "focus_calibration" to focusCalibration,
    )

    companion object {
        const val CALIBRATION_UNCALIBRATED = 0
        const val CALIBRATION_APPROXIMATE = 1
        const val CALIBRATION_CALIBRATED = 2

        const val AF_INACTIVE = 0
        const val AF_PASSIVE_SCAN = 1
        const val AF_PASSIVE_FOCUSED = 2
        const val AF_ACTIVE_SCAN = 3
        const val AF_FOCUSED_LOCKED = 4
        const val AF_NOT_FOCUSED_LOCKED = 5
        const val AF_PASSIVE_UNFOCUSED = 6

        fun fromJson(obj: JSONObject): FrameFocus {
            return FrameFocus(
                sensorTsNs = obj.optLong("sensor_ts_ns", 0L),
                focusDistanceDiopters = obj.optDoubleOrNull("focus_distance_diopters")?.toFloat(),
                afState = obj.optIntOrNull("af_state"),
                focusCalibration = obj.optIntOrNull("focus_calibration"),
            )
        }

        private fun JSONObject.optDoubleOrNull(key: String): Double? {
            if (!has(key) || isNull(key)) return null
            val v = optDouble(key, Double.NaN)
            return if (v.isNaN()) null else v
        }

        private fun JSONObject.optIntOrNull(key: String): Int? {
            if (!has(key) || isNull(key)) return null
            return optInt(key)
        }
    }
}

data class FocusSample(
    val sensorTsNs: Long,
    val focusDistanceDiopters: Float?,
    val afState: Int?,
)

/**
 * Ring of recent [CaptureResult] samples, matched to an [ImageProxy] by
 * [androidx.camera.core.ImageInfo.getTimestamp] (sensor timestamp).
 */
class FocusRing(private val capacity: Int = 24) {
    private val samples = ArrayDeque<FocusSample>()

    @Synchronized
    fun add(sample: FocusSample) {
        samples.addLast(sample)
        while (samples.size > capacity) {
            samples.removeFirst()
        }
    }

    @Synchronized
    fun match(frameSensorTsNs: Long, windowNs: Long = MATCH_WINDOW_NS): FocusSample? {
        if (samples.isEmpty()) return null
        val best = samples.minBy { abs(it.sensorTsNs - frameSensorTsNs) }
        return if (abs(best.sensorTsNs - frameSensorTsNs) <= windowNs) best else null
    }

    @Synchronized
    fun clear() {
        samples.clear()
    }

    @Synchronized
    fun size(): Int = samples.size

    companion object {
        const val MATCH_WINDOW_NS = 50_000_000L // 50 ms
    }
}
