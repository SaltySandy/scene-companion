package com.saw.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.lifecycle.LifecycleService

class StreamService : LifecycleService() {
    private lateinit var prefs: Prefs
    private lateinit var speech: SpeechQueue
    private var client: StreamClient? = null
    private var camera: CameraStreamer? = null
    private var mic: MicStreamer? = null
    private val cuePolicy = CuePolicy()
    private var cue: ProximityCue? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        speech = SpeechQueue(this)
        speech.start()
        cue = ProximityCue(this, cuePolicy, speech) { key, ts, state -> client?.sendAck(key, ts, state) }
            .also { it.start() }
        instance = this
        running = true
        startInForeground()
        acquireWakeLock()
        val streamClient = StreamClient(prefs, speech, object : StreamClient.Listener {
            override fun onState(state: StreamClient.ConnectionState, spoken: String) {
                cuePolicy.onConnection(state == StreamClient.ConnectionState.CONNECTED)
                lastSpoken = spoken
                sendBroadcast(
                    Intent(ACTION_STATUS)
                        .setPackage(packageName)
                        .putExtra(EXTRA_STATUS, spoken)
                        .putExtra(EXTRA_RUNNING, true),
                )
                if (spoken == "connected" || spoken == "connection lost, retrying") {
                    speech.enqueueSay(spoken, urgent = true)
                }
            }

            override fun onHaptic(connected: Boolean) {
                pulse(if (connected) 40 else 80)
            }

            override fun onProximity(p: Protocol.Proximity) {
                cuePolicy.onProximity(p, SystemClock.elapsedRealtime())
            }
        })
        client = streamClient
        speech.onSpeakingChanged = { active -> streamClient.sendSpeaking(active) }
        streamClient.start()
        camera = CameraStreamer(this, this, prefs, streamClient::readyToSend) { jpeg, ts ->
            streamClient.sendJpeg(jpeg, ts)
        }.also { it.start() }
        if (prefs.micEnabled) {
            mic = MicStreamer { pcm, ts -> streamClient.sendMicPcm(pcm, ts) }.also { it.start() }
        }
    }

    fun setListening(enabled: Boolean) {
        prefs.micEnabled = enabled
        val streamClient = client
        if (enabled && mic == null && streamClient != null) {
            mic = MicStreamer { pcm, ts -> streamClient.sendMicPcm(pcm, ts) }.also { it.start() }
        } else if (!enabled) {
            mic?.stop()
            mic = null
        }
        if (running) {
            startInForeground()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        instance = null
        mic?.stop()
        camera?.shutdown()
        client?.stop()
        cue?.stop()
        speech.shutdown()
        releaseWakeLock()
        sendBroadcast(
            Intent(ACTION_STATUS)
                .setPackage(packageName)
                .putExtra(EXTRA_STATUS, "stopped")
                .putExtra(EXTRA_RUNNING, false),
        )
        super.onDestroy()
    }

    override fun onBind(intent: Intent): IBinder? {
        super.onBind(intent)
        return null
    }

    private fun startInForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(CHANNEL, getString(R.string.notif_channel), NotificationManager.IMPORTANCE_LOW)
        channel.description = getString(R.string.notif_channel_desc)
        nm.createNotificationChannel(channel)

        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this,
            1,
            Intent(this, StreamService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(getString(R.string.notif_text))
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentIntent(open)
            .addAction(0, getString(R.string.stop), stop)
            .setOngoing(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()

        val types = if (prefs.micEnabled) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        } else {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        }
        if (Build.VERSION.SDK_INT >= 29) {
            ServiceCompat.startForeground(this, NOTIF_ID, notification, types)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    private fun acquireWakeLock() {
        val pm = getSystemService(PowerManager::class.java)
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "saw:stream").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseWakeLock() {
        try {
            wakeLock?.release()
        } catch (_: Exception) {
        }
        wakeLock = null
    }

    private fun pulse(ms: Long) {
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                val vm = getSystemService(VibratorManager::class.java)
                vm.defaultVibrator.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
            } else {
                @Suppress("DEPRECATION")
                val v = getSystemService(Vibrator::class.java)
                v.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
            }
        } catch (_: Exception) {
        }
    }

    companion object {
        const val ACTION_STOP = "com.saw.companion.STOP"
        const val ACTION_STATUS = "com.saw.companion.STATUS"
        const val EXTRA_STATUS = "status"
        const val EXTRA_RUNNING = "running"
        private const val CHANNEL = "saw_stream"
        private const val NOTIF_ID = 17

        @Volatile var running: Boolean = false
            private set
        @Volatile var lastSpoken: String = "stopped"
            private set
        @Volatile var instance: StreamService? = null
            private set

        fun start(context: Context) {
            val i = Intent(context, StreamService::class.java)
            context.startForegroundService(i)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, StreamService::class.java))
        }
    }
}
