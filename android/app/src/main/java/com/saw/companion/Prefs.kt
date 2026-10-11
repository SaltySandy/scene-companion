package com.saw.companion

import android.content.Context

class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences("saw", Context.MODE_PRIVATE)

    var hostPort: String
        get() = sp.getString(KEY_HOST, DEFAULT_HOST) ?: DEFAULT_HOST
        set(value) {
            sp.edit().putString(KEY_HOST, value.trim()).apply()
        }

    var micEnabled: Boolean
        get() = sp.getBoolean(KEY_MIC, false)
        set(value) {
            sp.edit().putBoolean(KEY_MIC, value).apply()
        }

    val width: Int get() = sp.getInt(KEY_W, 640)
    val height: Int get() = sp.getInt(KEY_H, 480)
    val fps: Int get() = sp.getInt(KEY_FPS, 8)
    val quality: Int get() = sp.getInt(KEY_Q, 70)

    fun parsedHost(): String {
        val raw = hostPort.trim()
        val idx = raw.lastIndexOf(':')
        return if (idx > 0) raw.substring(0, idx) else raw.ifBlank { "127.0.0.1" }
    }

    fun parsedPort(): Int {
        val raw = hostPort.trim()
        val idx = raw.lastIndexOf(':')
        return if (idx > 0) raw.substring(idx + 1).toIntOrNull() ?: DEFAULT_PORT else DEFAULT_PORT
    }

    companion object {
        const val DEFAULT_HOST = "127.0.0.1:8765"
        const val DEFAULT_PORT = 8765
        private const val KEY_HOST = "host_port"
        private const val KEY_MIC = "mic"
        private const val KEY_W = "width"
        private const val KEY_H = "height"
        private const val KEY_FPS = "fps"
        private const val KEY_Q = "quality"
    }
}
