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
    private val onAudioEvent: ((id: String, event: String, errorCode: Int?) -> Unit)? = null,
) {
    private val app = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val queue = ArrayDeque<Item>()
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var ttsInitFailed = false
    private var mediaPlayer: MediaPlayer? = null
    private var audioTrack: AudioTrack? = null
    private var busy = false
    private val pending = HashMap<String, Pending>()
    private var playingId: String? = null
    val audioManager: AudioManager = app.getSystemService(AudioManager::class.java)

    private data class Pending(val captureTs: Long?, val sayRecvTs: Long?, val audioId: String?)

    private sealed class Item {
        abstract val audioId: String?
        data class Speak(
            val text: String,
            val captureTs: Long?,
            val sayRecvTs: Long?,
            override val audioId: String?,
        ) : Item()
        data class Clip(val bytes: ByteArray, val flags: Int, override val audioId: String?) : Item()
    }

    fun volumeSnapshot(): Pair<Int, Boolean> {
        val vol = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)
        val muted = if (android.os.Build.VERSION.SDK_INT >= 23) {
            audioManager.isStreamMute(AudioManager.STREAM_MUSIC)
        } else {
            vol == 0
        }
        return vol to muted
    }

    fun start() {
        main.post {
            if (tts != null) return@post
            tts = TextToSpeech(app) { status ->
                ttsReady = status == TextToSpeech.SUCCESS
                ttsInitFailed = status != TextToSpeech.SUCCESS
                if (ttsInitFailed) {
                    onAudioEvent?.invoke("", AudioEvent.TTS_NOT_READY, status)
                }
                tts?.language = Locale.getDefault()
                tts?.setSpeechRate(1.05f)
                tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) {
                        val wall = System.currentTimeMillis()
                        val meta = synchronized(pending) { pending[utteranceId] }
                        onTtsStart?.invoke(meta?.captureTs, wall, meta?.sayRecvTs)
                        meta?.audioId?.let { emit(it, AudioEvent.STARTED) }
                    }
                    override fun onDone(utteranceId: String?) {
                        val meta = synchronized(pending) { pending.remove(utteranceId) }
                        meta?.audioId?.let { emit(it, AudioEvent.DONE) }
                        main.post { onItemFinished() }
                    }
                    @Deprecated("deprecated")
                    override fun onError(utteranceId: String?) {
                        val meta = synchronized(pending) { pending.remove(utteranceId) }
                        meta?.audioId?.let { emit(it, AudioEvent.ERROR, -1) }
                        main.post { onItemFinished() }
                    }
                    override fun onError(utteranceId: String?, errorCode: Int) {
                        val meta = synchronized(pending) { pending.remove(utteranceId) }
                        meta?.audioId?.let { emit(it, AudioEvent.ERROR, errorCode) }
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
        audioId: String? = null,
    ) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        main.post {
            if (urgent) flushUrgent()
            queue.addLast(Item.Speak(clean, captureTs, sayRecvTs, audioId))
            audioId?.let { emit(it, AudioEvent.QUEUED) }
            pump()
        }
    }

    fun enqueueClip(bytes: ByteArray, flags: Int, urgent: Boolean, audioId: String? = null) {
        if (bytes.isEmpty()) return
        main.post {
            if (urgent) flushUrgent()
            queue.addLast(Item.Clip(bytes, flags, audioId))
            audioId?.let { emit(it, AudioEvent.QUEUED) }
            pump()
        }
    }

    private fun flushUrgent() {
        for (item in queue) {
            item.audioId?.let { emit(it, AudioEvent.INTERRUPTED) }
        }
        playingId?.let { emit(it, AudioEvent.INTERRUPTED) }
        queue.clear()
        synchronized(pending) { pending.clear() }
        stopCurrent()
    }

    private fun emit(id: String, event: String, errorCode: Int? = null) {
        if (id.isEmpty()) return
        onAudioEvent?.invoke(id, event, errorCode)
    }

    private fun pump() {
        if (busy) return
        val item = queue.pollFirst() ?: return
        busy = true
        when (item) {
            is Item.Speak -> speak(item.text, item.captureTs, item.sayRecvTs, item.audioId)
            is Item.Clip -> playClip(item)
        }
    }

    private fun speak(text: String, captureTs: Long?, sayRecvTs: Long?, audioId: String?) {
        val engine = tts
        if (ttsInitFailed) {
            audioId?.let { emit(it, AudioEvent.TTS_NOT_READY) }
            busy = false
            pump()
            return
        }
        if (engine == null || !ttsReady) {
            // TTS still loading — put it back and retry shortly.
            queue.addFirst(Item.Speak(text, captureTs, sayRecvTs, audioId))
            busy = false
            main.postDelayed({ pump() }, 150)
            return
        }
        val id = UUID.randomUUID().toString()
        playingId = audioId
        synchronized(pending) { pending[id] = Pending(captureTs, sayRecvTs, audioId) }
        val rc = engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, id)
        if (rc != TextToSpeech.SUCCESS) {
            synchronized(pending) { pending.remove(id) }
            audioId?.let { emit(it, AudioEvent.ERROR, rc) }
            onItemFinished()
        }
    }

    private fun playClip(item: Item.Clip) {
        playingId = item.audioId
        if (item.flags == Protocol.FLAG_PCM) {
            playPcm(item.bytes, item.audioId)
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
            player.setOnCompletionListener {
                item.audioId?.let { emit(it, AudioEvent.DONE) }
                onItemFinished()
            }
            player.setOnErrorListener { _, what, _ ->
                item.audioId?.let { emit(it, AudioEvent.ERROR, what) }
                onItemFinished()
                true
            }
            player.prepare()
            player.start()
            item.audioId?.let { emit(it, AudioEvent.STARTED) }
        } catch (_: Exception) {
            item.audioId?.let { emit(it, AudioEvent.ERROR, -1) }
            onItemFinished()
        }
    }

    private fun playPcm(pcm: ByteArray, audioId: String? = null) {
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
        audioId?.let { emit(it, AudioEvent.STARTED) }
        track.setPlaybackPositionUpdateListener(object : AudioTrack.OnPlaybackPositionUpdateListener {
            override fun onMarkerReached(audioTrack: AudioTrack?) {
                main.post {
                    audioId?.let { emit(it, AudioEvent.DONE) }
                    onItemFinished()
                }
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
        playingId = null
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
