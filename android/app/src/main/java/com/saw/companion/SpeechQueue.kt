package com.saw.companion

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.media.MediaDataSource
import android.media.MediaPlayer
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.ArrayDeque
import java.util.Locale
import java.util.UUID

/**
 * Lowest-latency speech path is Android TTS (offline). Audio clips use MediaPlayer / AudioTrack.
 * Urgent items flush the queue and interrupt whatever is playing.
 */
class SpeechQueue(
    context: Context,
    private val onTtsStart: ((captureTs: Long?, ttsStartTs: Long, sayRecvTs: Long?) -> Unit)? = null,
) {
    private val app = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val queue = ArrayDeque<Item>()
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var mediaPlayer: MediaPlayer? = null
    private var audioTrack: AudioTrack? = null
    private var busy = false
    private val pending = HashMap<String, Pending>()

    private data class Pending(val captureTs: Long?, val sayRecvTs: Long?)

    private sealed class Item {
        data class Speak(val text: String, val captureTs: Long?, val sayRecvTs: Long?) : Item()
        data class Clip(val bytes: ByteArray, val flags: Int) : Item()
    }

    fun start() {
        main.post {
            if (tts != null) return@post
            tts = TextToSpeech(app) { status ->
                ttsReady = status == TextToSpeech.SUCCESS
                tts?.language = Locale.getDefault()
                tts?.setSpeechRate(1.05f)
                tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) {
                        val wall = System.currentTimeMillis()
                        val meta = synchronized(pending) { pending.remove(utteranceId) }
                        onTtsStart?.invoke(meta?.captureTs, wall, meta?.sayRecvTs)
                    }
                    override fun onDone(utteranceId: String?) {
                        main.post { onItemFinished() }
                    }
                    @Deprecated("deprecated")
                    override fun onError(utteranceId: String?) {
                        main.post { onItemFinished() }
                    }
                    override fun onError(utteranceId: String?, errorCode: Int) {
                        main.post { onItemFinished() }
                    }
                })
                pump()
            }
        }
    }

    fun shutdown() {
        main.post {
            queue.clear()
            stopCurrent()
            tts?.shutdown()
            tts = null
            ttsReady = false
        }
    }

    fun enqueueSay(
        text: String,
        urgent: Boolean,
        captureTs: Long? = null,
        sayRecvTs: Long? = null,
    ) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        main.post {
            if (urgent) {
                queue.clear()
                synchronized(pending) { pending.clear() }
                stopCurrent()
            }
            queue.addLast(Item.Speak(clean, captureTs, sayRecvTs))
            pump()
        }
    }

    fun enqueueClip(bytes: ByteArray, flags: Int, urgent: Boolean) {
        if (bytes.isEmpty()) return
        main.post {
            if (urgent) {
                queue.clear()
                stopCurrent()
            }
            queue.addLast(Item.Clip(bytes, flags))
            pump()
        }
    }

    private fun pump() {
        if (busy) return
        val item = queue.pollFirst() ?: return
        busy = true
        when (item) {
            is Item.Speak -> speak(item.text, item.captureTs, item.sayRecvTs)
            is Item.Clip -> playClip(item)
        }
    }

    private fun speak(text: String, captureTs: Long?, sayRecvTs: Long?) {
        val engine = tts
        if (engine == null || !ttsReady) {
            // TTS still loading — put it back and retry shortly.
            queue.addFirst(Item.Speak(text, captureTs, sayRecvTs))
            busy = false
            main.postDelayed({ pump() }, 150)
            return
        }
        val id = UUID.randomUUID().toString()
        synchronized(pending) { pending[id] = Pending(captureTs, sayRecvTs) }
        val rc = engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, id)
        if (rc != TextToSpeech.SUCCESS) {
            synchronized(pending) { pending.remove(id) }
            onItemFinished()
        }
    }

    private fun playClip(item: Item.Clip) {
        if (item.flags == Protocol.FLAG_PCM) {
            playPcm(item.bytes)
            return
        }
        try {
            val player = MediaPlayer()
            mediaPlayer = player
            player.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            player.setDataSource(ByteArrayDataSource(item.bytes))
            player.setOnCompletionListener { onItemFinished() }
            player.setOnErrorListener { _, _, _ ->
                onItemFinished()
                true
            }
            player.prepare()
            player.start()
        } catch (_: Exception) {
            onItemFinished()
        }
    }

    private fun playPcm(pcm: ByteArray) {
        val sampleRate = 16_000
        val min = AudioTrack.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val track = AudioTrack(
            AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build(),
            AudioFormat.Builder()
                .setSampleRate(sampleRate)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build(),
            maxOf(min, pcm.size),
            AudioTrack.MODE_STATIC,
            AudioManager.AUDIO_SESSION_ID_GENERATE,
        )
        audioTrack = track
        track.write(pcm, 0, pcm.size)
        track.setNotificationMarkerPosition(pcm.size / 2)
        track.setPlaybackPositionUpdateListener(object : AudioTrack.OnPlaybackPositionUpdateListener {
            override fun onMarkerReached(audioTrack: AudioTrack?) {
                main.post { onItemFinished() }
            }
            override fun onPeriodicNotification(audioTrack: AudioTrack?) = Unit
        })
        track.play()
        // Fallback if marker never fires.
        val durationMs = (pcm.size / 2) * 1000L / sampleRate + 80
        main.postDelayed({
            if (audioTrack === track) onItemFinished()
        }, durationMs)
    }

    private fun stopCurrent() {
        synchronized(pending) { pending.clear() }
        try {
            tts?.stop()
        } catch (_: Exception) {
        }
        try {
            mediaPlayer?.setOnCompletionListener(null)
            mediaPlayer?.stop()
        } catch (_: Exception) {
        }
        mediaPlayer?.release()
        mediaPlayer = null
        try {
            audioTrack?.pause()
            audioTrack?.flush()
        } catch (_: Exception) {
        }
        audioTrack?.release()
        audioTrack = null
        busy = false
    }

    private fun onItemFinished() {
        try {
            mediaPlayer?.release()
        } catch (_: Exception) {
        }
        mediaPlayer = null
        try {
            audioTrack?.release()
        } catch (_: Exception) {
        }
        audioTrack = null
        busy = false
        pump()
    }

    private class ByteArrayDataSource(private val data: ByteArray) : MediaDataSource() {
        override fun readAt(position: Long, buffer: ByteArray, offset: Int, size: Int): Int {
            if (position >= data.size) return -1
            val toRead = minOf(size, data.size - position.toInt())
            System.arraycopy(data, position.toInt(), buffer, offset, toRead)
            return toRead
        }

        override fun getSize(): Long = data.size.toLong()
        override fun close() = Unit
    }
}
