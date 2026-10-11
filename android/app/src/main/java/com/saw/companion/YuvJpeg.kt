package com.saw.companion

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream

object YuvJpeg {
    /** Encode once, upright: the NV21 buffer is rotated before compression. */
    fun toJpeg(image: ImageProxy, quality: Int, rotationDegrees: Int): ByteArray? {
        val nv21 = yuv420ToNv21(image) ?: return null
        val rotation = FrameMath.normalizeRotation(rotationDegrees)
        val upright = FrameMath.rotateNv21(nv21, image.width, image.height, rotation)
        val swap = rotation == 90 || rotation == 270
        val w = if (swap) image.height else image.width
        val h = if (swap) image.width else image.height
        val yuv = YuvImage(upright, ImageFormat.NV21, w, h, null)
        val out = ByteArrayOutputStream()
        if (!yuv.compressToJpeg(Rect(0, 0, w, h), quality.coerceIn(20, 95), out)) {
            return null
        }
        return out.toByteArray()
    }

    private fun yuv420ToNv21(image: ImageProxy): ByteArray? {
        val yPlane = image.planes.getOrNull(0) ?: return null
        val uPlane = image.planes.getOrNull(1) ?: return null
        val vPlane = image.planes.getOrNull(2) ?: return null
        val width = image.width
        val height = image.height
        val ySize = width * height
        val nv21 = ByteArray(ySize + ySize / 2)

        val yBuf = yPlane.buffer
        val yRowStride = yPlane.rowStride
        val yPixStride = yPlane.pixelStride
        var out = 0
        if (yPixStride == 1 && yRowStride == width) {
            yBuf.get(nv21, 0, ySize)
            out = ySize
        } else {
            val yRow = ByteArray(yRowStride)
            for (row in 0 until height) {
                yBuf.position(row * yRowStride)
                yBuf.get(yRow, 0, minOf(yRowStride, yBuf.remaining()))
                if (yPixStride == 1) {
                    System.arraycopy(yRow, 0, nv21, out, width)
                    out += width
                } else {
                    var col = 0
                    while (col < width) {
                        nv21[out++] = yRow[col * yPixStride]
                        col++
                    }
                }
            }
        }

        // NV21 is VU interleaved. CameraX YUV_420_888 typically has pixelStride 2.
        val vBuf = vPlane.buffer
        val uBuf = uPlane.buffer
        val vRowStride = vPlane.rowStride
        val uRowStride = uPlane.rowStride
        val vPixStride = vPlane.pixelStride
        val uPixStride = uPlane.pixelStride
        val chromaHeight = height / 2
        val chromaWidth = width / 2
        for (row in 0 until chromaHeight) {
            val vRowStart = row * vRowStride
            val uRowStart = row * uRowStride
            for (col in 0 until chromaWidth) {
                val vIndex = vRowStart + col * vPixStride
                val uIndex = uRowStart + col * uPixStride
                if (vIndex < vBuf.capacity()) nv21[out++] = vBuf.get(vIndex)
                if (uIndex < uBuf.capacity()) nv21[out++] = uBuf.get(uIndex)
            }
        }
        return nv21
    }
}
