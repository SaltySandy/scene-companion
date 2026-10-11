package com.saw.companion

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.CaptureResult
import android.hardware.camera2.TotalCaptureResult
import android.hardware.camera2.params.MeteringRectangle
import android.os.SystemClock
import android.util.Log
import android.util.Size
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max

/**
 * Rear camera via CameraX. STRATEGY_KEEP_ONLY_LATEST plus an in-flight send flag
 * so we drop frames instead of queueing them.
 *
 * Autofocus: Camera2 interop requests continuous-video (else continuous-picture)
 * AF with a center metering region, and a session capture callback records
 * [CaptureResult.LENS_FOCUS_DISTANCE] keyed by sensor timestamp so it can be
 * matched to the ImageAnalysis frame.
 */
class CameraStreamer(
    private val context: Context,
    private val lifecycleOwner: LifecycleOwner,
    private val prefs: Prefs,
    private val onJpeg: (ByteArray, Long, FrameFocus?) -> Boolean,
) {
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val capturing = AtomicBoolean(false)
    @Volatile private var lastSentMs = 0L
    private var provider: ProcessCameraProvider? = null
    private var boundSize: Size? = null
    private val focusRing = FocusRing()
    @Volatile private var focusCalibration: Int? = null

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
        boundSize = null
        focusRing.clear()
    }

    /**
     * Picks up a new quality level while streaming. JPEG quality and fps are read per frame;
     * a resolution change rebinds the camera on the main thread.
     */
    fun applyQuality() {
        val want = Size(prefs.width, prefs.height)
        if (boundSize == want) return
        ContextCompat.getMainExecutor(context).execute {
            val p = provider ?: return@execute
            try {
                bind(p)
            } catch (e: Exception) {
                Log.e(TAG, "camera rebind failed", e)
                SawSentry.capture(e, "camera")
            }
        }
    }

    fun shutdown() {
        stop()
        analysisExecutor.shutdown()
    }

    private fun bind(cameraProvider: ProcessCameraProvider) {
        provider = cameraProvider
        cameraProvider.unbindAll()
        focusRing.clear()
        val size = Size(prefs.width, prefs.height)
        boundSize = size
        val selector = ResolutionSelector.Builder()
            .setResolutionStrategy(
                ResolutionStrategy(
                    size,
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                ),
            )
            .build()
        val analysisBuilder = ImageAnalysis.Builder()
            .setResolutionSelector(selector)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
        val chars = rearCharacteristics()
        focusCalibration = chars?.get(CameraCharacteristics.LENS_INFO_FOCUS_DISTANCE_CALIBRATION)
        applyAfInterop(analysisBuilder, chars)
        val analysis = analysisBuilder.build()
        analysis.setAnalyzer(analysisExecutor) { image ->
            try {
                val now = System.currentTimeMillis()
                val capture = CaptureClock.wallMsFromSensor(
                    sensorNs = image.imageInfo.timestamp,
                    nowWallMs = now,
                    nowElapsedNs = SystemClock.elapsedRealtimeNanos(),
                    nowUptimeNs = SystemClock.uptimeMillis() * 1_000_000L,
                )
                val minIntervalMs = 1000L / prefs.fps.coerceIn(1, 15)
                if (now - lastSentMs < minIntervalMs) return@setAnalyzer
                if (!capturing.compareAndSet(false, true)) return@setAnalyzer
                try {
                    val sample = focusRing.match(image.imageInfo.timestamp)
                    val focus = FrameFocus(
                        sensorTsNs = image.imageInfo.timestamp,
                        focusDistanceDiopters = sample?.focusDistanceDiopters,
                        afState = sample?.afState,
                        focusCalibration = focusCalibration,
                    )
                    val jpeg = YuvJpeg.toJpeg(image, prefs.quality)
                    if (jpeg != null) {
                        val sent = onJpeg(jpeg, capture.wallMs, focus)
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
        Log.i(
            TAG,
            "camera bound ${prefs.width}x${prefs.height} @ ${prefs.fps}fps q=${prefs.quality} " +
                "afCal=$focusCalibration",
        )
    }

    private fun applyAfInterop(builder: ImageAnalysis.Builder, chars: CameraCharacteristics?) {
        val extender = Camera2Interop.Extender(builder)
        extender.setCaptureRequestOption(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
        val modes = chars?.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES)
        val afMode = when {
            modes == null -> CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO
            CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO in modes ->
                CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO
            CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE in modes ->
                CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE
            else -> CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO
        }
        extender.setCaptureRequestOption(CaptureRequest.CONTROL_AF_MODE, afMode)
        val active = chars?.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
        if (active != null) {
            val cx = active.centerX()
            val cy = active.centerY()
            val rw = max(1, active.width() / 3)
            val rh = max(1, active.height() / 3)
            val rect = android.graphics.Rect(
                (cx - rw / 2).coerceAtLeast(active.left),
                (cy - rh / 2).coerceAtLeast(active.top),
                (cx + rw / 2).coerceAtMost(active.right),
                (cy + rh / 2).coerceAtMost(active.bottom),
            )
            val region = arrayOf(MeteringRectangle(rect, MeteringRectangle.METERING_WEIGHT_MAX))
            extender.setCaptureRequestOption(CaptureRequest.CONTROL_AF_REGIONS, region)
            extender.setCaptureRequestOption(CaptureRequest.CONTROL_AE_REGIONS, region)
        }
        extender.setSessionCaptureCallback(object : CameraCaptureSession.CaptureCallback() {
            override fun onCaptureCompleted(
                session: CameraCaptureSession,
                request: CaptureRequest,
                result: TotalCaptureResult,
            ) {
                val ts = result.get(CaptureResult.SENSOR_TIMESTAMP) ?: return
                focusRing.add(
                    FocusSample(
                        sensorTsNs = ts,
                        focusDistanceDiopters = result.get(CaptureResult.LENS_FOCUS_DISTANCE),
                        afState = result.get(CaptureResult.CONTROL_AF_STATE),
                    ),
                )
            }
        })
    }

    @SuppressLint("MissingPermission")
    private fun rearCharacteristics(): CameraCharacteristics? {
        return try {
            val mgr = context.getSystemService(CameraManager::class.java) ?: return null
            for (id in mgr.cameraIdList) {
                val chars = mgr.getCameraCharacteristics(id)
                if (chars.get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK) {
                    return chars
                }
            }
            null
        } catch (e: Exception) {
            Log.w(TAG, "camera characteristics: ${e.message}")
            null
        }
    }

    companion object {
        private const val TAG = "SawCamera"
    }
}
