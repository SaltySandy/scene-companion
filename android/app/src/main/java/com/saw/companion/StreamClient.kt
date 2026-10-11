package com.saw.companion

import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * WebSocket to the laptop. Frames are dropped (never queued) when a send is in flight
 * or the socket is down, so latency stays low.
 */
class StreamClient(
    private val prefs: Prefs,
    private val speech: SpeechQueue,
    private val listener: Listener,
) {
    interface Listener {
        fun onState(state: ConnectionState, spoken: String)
        fun onHaptic(connected: Boolean)
    }

    enum class ConnectionState { IDLE, CONNECTING, CONNECTED, RETRYING, STOPPED }

    private val main = Handler(Looper.getMainLooper())
    private val sending = AtomicBoolean(false)
    private val socketRef = AtomicReference<WebSocket?>(null)
    private val http = OkHttpClient.Builder()
        .pingInterval(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .connectTimeout(8, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    @Volatile private var running = false
    @Volatile var connected: Boolean = false
        private set
    private var backoffMs = 1_000L
    private var lastSpokenState = ""

    fun start() {
        running = true
        connect()
    }

    fun stop() {
        running = false
        connected = false
        main.removeCallbacksAndMessages(null)
        socketRef.getAndSet(null)?.cancel()
        setState(ConnectionState.STOPPED, "stopped")
    }

    fun sendJpeg(jpeg: ByteArray, timestampMs: Long): Boolean {
        val ws = socketRef.get() ?: return false
        if (!connected) return false
        // OkHttp queues outgoing frames. If anything is still on the wire, drop this one.
        if (ws.queueSize() > 0) return false
        if (!sending.compareAndSet(false, true)) return false
        val ok = try {
            val framed = Protocol.encodeBinary(Protocol.KIND_JPEG, jpeg, timestampMs)
            ws.send(framed.toByteString())
        } catch (_: Exception) {
            false
        }
        sending.set(false)
        return ok
    }

    fun sendMicPcm(pcm: ByteArray, timestampMs: Long): Boolean {
        val ws = socketRef.get() ?: return false
        if (!connected) return false
        return try {
            ws.send(Protocol.encodeBinary(Protocol.KIND_MIC, pcm, timestampMs).toByteString())
        } catch (_: Exception) {
            false
        }
    }

    private fun connect() {
        if (!running) return
        setState(
            if (lastSpokenState == "connected") ConnectionState.RETRYING else ConnectionState.CONNECTING,
            if (lastSpokenState == "connected" || lastSpokenState == "connection lost, retrying") {
                "connection lost, retrying"
            } else {
                "connecting"
            },
        )
        val host = prefs.parsedHost()
        val port = prefs.parsedPort()
        val url = "ws://$host:$port/v1/stream"
        Log.i(TAG, "connecting $url")
        val req = Request.Builder().url(url).build()
        val ws = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                socketRef.set(webSocket)
                connected = true
                backoffMs = 1_000L
                sending.set(false)
                webSocket.send(
                    Protocol.encodeJson(
                        Protocol.TYPE_HELLO,
                        mapOf("role" to "phone", "app" to "saw-android", "app_ver" to "0.1.0"),
                    ),
                )
                webSocket.send(
                    Protocol.encodeJson(
                        Protocol.TYPE_CONFIG,
                        mapOf(
                            "width" to prefs.width,
                            "height" to prefs.height,
                            "fps" to prefs.fps,
                            "quality" to prefs.quality,
                            "mic" to prefs.micEnabled,
                        ),
                    ),
                )
                setState(ConnectionState.CONNECTED, "connected")
                listener.onHaptic(true)
                SawSentry.breadcrumb("connected")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleText(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                handleBinary(bytes.toByteArray())
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onDrop("closed")
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "socket failure: ${t.message}")
                SawSentry.capture(t, "websocket")
                onDrop("failure")
            }
        })
        socketRef.set(ws)
    }

    private fun handleText(text: String) {
        val obj: JSONObject = try {
            Protocol.decodeJson(text)
        } catch (e: Exception) {
            Log.w(TAG, "bad json: ${e.message}")
            return
        }
        when (obj.optString("type")) {
            Protocol.TYPE_SAY -> {
                val spoken = obj.optString("text")
                val urgent = obj.optBoolean("urgent", false)
                speech.enqueueSay(spoken, urgent)
            }
            Protocol.TYPE_STATUS -> {
                if (obj.optString("msg") == "audio") {
                    // Following binary AUDIO_CLIP is the payload; urgent hint stored for next clip.
                    pendingUrgentClip = obj.optBoolean("urgent", false)
                }
            }
            Protocol.TYPE_PONG, Protocol.TYPE_HELLO_ACK -> Unit
        }
    }

    @Volatile private var pendingUrgentClip = false

    private fun handleBinary(data: ByteArray) {
        val msg = try {
            Protocol.decodeBinary(data)
        } catch (e: Exception) {
            Log.w(TAG, "bad binary: ${e.message}")
            return
        }
        if (msg.kind == Protocol.KIND_AUDIO) {
            val urgent = pendingUrgentClip
            pendingUrgentClip = false
            speech.enqueueClip(msg.payload, msg.flags, urgent)
        }
    }

    private fun onDrop(why: String) {
        val wasConnected = connected
        connected = false
        socketRef.getAndSet(null)?.cancel()
        if (!running) return
        if (wasConnected) {
            listener.onHaptic(false)
            SawSentry.breadcrumb("disconnected")
        }
        setState(ConnectionState.RETRYING, "connection lost, retrying")
        val delay = backoffMs
        backoffMs = (backoffMs * 2).coerceAtMost(15_000L)
        Log.i(TAG, "reconnect in ${delay}ms ($why)")
        main.postDelayed({ if (running) connect() }, delay)
    }

    private fun setState(state: ConnectionState, spoken: String) {
        if (spoken != lastSpokenState) {
            lastSpokenState = spoken
            listener.onState(state, spoken)
        } else {
            listener.onState(state, spoken)
        }
    }

    companion object {
        private const val TAG = "SawStream"
    }
}
