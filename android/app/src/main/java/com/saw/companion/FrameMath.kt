package com.saw.companion

/** Pure frame helpers, kept free of Android types so JVM unit tests can run them. */
object FrameMath {
    private const val MAX_AGE_NS = 1_000_000_000L

    /**
     * Convert a camera sensor timestamp to epoch ms.
     *
     * The sensor clock is either elapsedRealtime or uptime depending on the device
     * (CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE). A real frame is less
     * than a second old, so whichever base gives an age in [0, 1 s] is the right
     * one. If neither does, fall back to "now" rather than invent a time.
     */
    fun sensorToEpochMs(sensorNs: Long, elapsedNowNs: Long, uptimeNowNs: Long, epochNowMs: Long): Long {
        val ageNs = when {
            elapsedNowNs - sensorNs in 0..MAX_AGE_NS -> elapsedNowNs - sensorNs
            uptimeNowNs - sensorNs in 0..MAX_AGE_NS -> uptimeNowNs - sensorNs
            else -> 0L
        }
        return epochNowMs - ageNs / 1_000_000L
    }

    /** Snap any angle to 0/90/180/270. */
    fun normalizeRotation(degrees: Int): Int = ((degrees / 90) * 90).mod(360)

    /**
     * Rotate an NV21 image clockwise by 0/90/180/270 degrees. Width and height
     * must be even. For 90 and 270 the result is height x width.
     */
    fun rotateNv21(src: ByteArray, width: Int, height: Int, degrees: Int): ByteArray {
        val rot = normalizeRotation(degrees)
        if (rot == 0) return src
        require(width % 2 == 0 && height % 2 == 0) { "NV21 needs even dimensions" }
        val ySize = width * height
        val dst = ByteArray(src.size)
        for (y in 0 until height) {
            for (x in 0 until width) {
                val to = when (rot) {
                    90 -> x * height + (height - 1 - y)
                    180 -> (height - 1 - y) * width + (width - 1 - x)
                    else -> (width - 1 - x) * height + y
                }
                dst[to] = src[y * width + x]
            }
        }
        // VU pairs on a (width/2) x (height/2) grid move together.
        val cw = width / 2
        val ch = height / 2
        for (cy in 0 until ch) {
            for (cx in 0 until cw) {
                val to = when (rot) {
                    90 -> cx * ch + (ch - 1 - cy)
                    180 -> (ch - 1 - cy) * cw + (cw - 1 - cx)
                    else -> (cw - 1 - cx) * ch + cy
                }
                val from = ySize + 2 * (cy * cw + cx)
                dst[ySize + 2 * to] = src[from]
                dst[ySize + 2 * to + 1] = src[from + 1]
            }
        }
        return dst
    }
}
