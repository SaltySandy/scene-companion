package com.saw.companion

import android.content.Context
import android.util.Log
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Rear camera via CameraX. STRATEGY_KEEP_ONLY_LATEST plus an in-flight send flag
 * so we drop frames instead of queueing them.
 */
class CameraStreamer(
    private val context: Context,
    private val lifecycleOwner: LifecycleOwner,
    private val prefs: Prefs,
    private val onJpeg: (ByteArray, Long) -> Boolean,
) {
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val capturing = AtomicBoolean(false)
    @Volatile private var lastSentMs = 0L
    private var provider: ProcessCameraProvider? = null

    fun start() {
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            try {
                bind(future.get())
            } catch (e: Exception) {
                Log.e(TAG, "camera provider failed", e)
                SawSentry.capture(e, "camera")
            }
        }, ContextCompat.getMainExecutor(context))
    }

    fun stop() {
        try {
            provider?.unbindAll()
        } catch (_: Exception) {
        }
        provider = null
    }

    fun shutdown() {
        stop()
        analysisExecutor.shutdown()
    }

    private fun bind(cameraProvider: ProcessCameraProvider) {
        provider = cameraProvider
        cameraProvider.unbindAll()
        val selector = ResolutionSelector.Builder()
            .setResolutionStrategy(
                ResolutionStrategy(
                    Size(prefs.width, prefs.height),
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                ),
            )
            .build()
        val analysis = ImageAnalysis.Builder()
            .setResolutionSelector(selector)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
            .build()
        val minIntervalMs = (1000L / prefs.fps.coerceIn(1, 15))
        analysis.setAnalyzer(analysisExecutor) { image ->
            try {
                val now = System.currentTimeMillis()
                if (now - lastSentMs < minIntervalMs) return@setAnalyzer
                if (!capturing.compareAndSet(false, true)) return@setAnalyzer
                try {
                    val jpeg = YuvJpeg.toJpeg(image, prefs.quality)
                    if (jpeg != null) {
                        val sent = onJpeg(jpeg, now)
                        if (sent) lastSentMs = now
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "frame encode failed: ${e.message}")
                    SawSentry.capture(e, "camera")
                } finally {
                    capturing.set(false)
                }
            } finally {
                image.close()
            }
        }
        cameraProvider.bindToLifecycle(lifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, analysis)
        Log.i(TAG, "camera bound ${prefs.width}x${prefs.height} @ ${prefs.fps}fps q=${prefs.quality}")
    }

    companion object {
        private const val TAG = "SawCamera"
    }
}
