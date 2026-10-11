package com.saw.companion

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean

/** Optional 16 kHz mono PCM16 uplink. Off by default. */
class MicStreamer(
    private val onPcm: (ByteArray, Long) -> Unit,
) {
    private val running = AtomicBoolean(false)
    private var thread: Thread? = null

    @SuppressLint("MissingPermission")
    fun start() {
        if (!running.compareAndSet(false, true)) return
        val sampleRate = 16_000
        val min = AudioRecord.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val bufferSize = maxOf(min, sampleRate / 5 * 2) // ~200 ms
        val record = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferSize,
            )
        } catch (e: Exception) {
            Log.e(TAG, "AudioRecord failed", e)
            running.set(false)
            return
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            running.set(false)
            return
        }
        thread = Thread({
            val buf = ByteArray(bufferSize)
            try {
                record.startRecording()
                while (running.get()) {
                    val n = record.read(buf, 0, buf.size)
                    if (n > 0) {
                        onPcm(buf.copyOf(n), System.currentTimeMillis())
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "mic read: ${e.message}")
            } finally {
                try {
                    record.stop()
                } catch (_: Exception) {
                }
                record.release()
            }
        }, "saw-mic").also { it.start() }
    }

    fun stop() {
        running.set(false)
        thread?.interrupt()
        thread = null
    }

    companion object {
        private const val TAG = "SawMic"
    }
}
