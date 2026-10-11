package com.saw.companion

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean

/** 16 kHz mono PCM16 uplink, in 100 ms chunks. On by default for the demo; toggle Listen to stop. */
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
        val canceller = enableEchoCanceler(record.audioSessionId)
        thread = Thread({
            val buf = ByteArray(bufferSize)
            var pending = ByteArray(0)
            try {
                record.startRecording()
                while (running.get()) {
                    val n = record.read(buf, 0, buf.size)
                    if (n <= 0) continue
                    val merged = ByteArray(pending.size + n)
                    System.arraycopy(pending, 0, merged, 0, pending.size)
                    System.arraycopy(buf, 0, merged, pending.size, n)
                    var offset = 0
                    while (merged.size - offset >= CHUNK_BYTES) {
                        onPcm(merged.copyOfRange(offset, offset + CHUNK_BYTES), System.currentTimeMillis())
                        offset += CHUNK_BYTES
                    }
                    pending = if (offset == 0) merged else merged.copyOfRange(offset, merged.size)
                }
            } catch (e: Exception) {
                Log.w(TAG, "mic read: ${e.message}")
            } finally {
                try {
                    record.stop()
                } catch (_: Exception) {
                }
                try {
                    canceller?.release()
                } catch (_: Exception) {
                }
                record.release()
            }
        }, "saw-mic").also { it.start() }
    }

    private fun enableEchoCanceler(sessionId: Int): AcousticEchoCanceler? {
        if (!AcousticEchoCanceler.isAvailable()) return null
        return try {
            AcousticEchoCanceler.create(sessionId)?.also { it.enabled = true }
        } catch (e: Exception) {
            Log.w(TAG, "echo canceler unavailable: ${e.message}")
            null
        }
    }

    fun stop() {
        running.set(false)
        thread?.interrupt()
        thread = null
    }

    companion object {
        private const val TAG = "SawMic"
        const val SAMPLE_RATE = 16_000
        /** 100 ms of 16 kHz mono PCM16. */
        const val CHUNK_BYTES = SAMPLE_RATE / 10 * 2
    }
}
