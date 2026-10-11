package com.saw.companion

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Build
import android.os.Process
import android.os.SystemClock
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log

/**
 * Plays the proximity cue: tones on a low-latency streaming AudioTrack plus haptics.
 *
 * One audio thread writes 20 ms chunks. Each chunk re-reads [CuePolicy.cue], so a
 * level change is heard within ~20 ms plus the track's buffer, and the watchdog
 * fires even when no messages arrive. The blocking write paces the loop.
 */
class ProximityCue(
    context: Context,
    private val policy: CuePolicy,
    private val speech: SpeechQueue,
    /** (frame_key, cue_start_ts epoch ms, state) once per frame, after its first chunk is written. */
    private val onAck: (Long, Long, String) -> Unit,
) {
    private val vibrator: Vibrator? = try {
        if (Build.VERSION.SDK_INT >= 31) {
            context.getSystemService(VibratorManager::class.java).defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Vibrator::class.java)
        }
    } catch (_: Exception) {
        null
    }

    @Volatile private var running = false
    private var thread: Thread? = null

    fun start() {
        if (running) return
        running = true
        thread = Thread({ loop() }, "saw-cue").also { it.start() }
    }

    fun stop() {
        running = false
        thread?.join(500)
        thread = null
    }

    private fun loop() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
        val chunkFrames = SAMPLE_RATE * CHUNK_MS / 1000
        val minBuf = AudioTrack.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT)
        val track = try {
            AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build(),
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(SAMPLE_RATE)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                        .build(),
                )
                .setBufferSizeInBytes(maxOf(minBuf, chunkFrames * 4 * 2))
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                .build()
        } catch (e: Exception) {
            Log.e(TAG, "cue AudioTrack failed", e)
            SawSentry.capture(e, "cue")
            return
        }
        val synth = ToneSynth(SAMPLE_RATE)
        val buf = ShortArray(chunkFrames * 2)
        try {
            track.play()
            while (running) {
                val now = SystemClock.elapsedRealtime()
                val cue = policy.cue(now)
                val pattern = CuePatterns.forState(cue.state)
                if (synth.render(pattern, cue.pan, buf) && pattern.vibrateMs > 0) vibrate(pattern.vibrateMs)
                policy.announcement(cue, now)?.let { speech.enqueueSay(it, urgent = true) }
                val ackKey = policy.takeAck(cue)
                val cueStartTs = System.currentTimeMillis() // same clock as capture_ts
                track.write(buf, 0, buf.size)
                if (ackKey != null) onAck(ackKey, cueStartTs, cue.state.name.lowercase())
            }
        } catch (e: Exception) {
            Log.e(TAG, "cue loop failed", e)
            SawSentry.capture(e, "cue")
        } finally {
            try {
                track.stop()
            } catch (_: Exception) {
            }
            track.release()
        }
    }

    private fun vibrate(ms: Long) {
        try {
            vibrator?.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
        } catch (_: Exception) {
        }
    }

    companion object {
        private const val TAG = "SawCue"
        private const val SAMPLE_RATE = 24_000
        private const val CHUNK_MS = 20
    }
}
