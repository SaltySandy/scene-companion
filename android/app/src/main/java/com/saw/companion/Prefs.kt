package com.saw.companion

import android.content.Context

class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences("saw", Context.MODE_PRIVATE)

    var hostPort: String
        get() = sp.getString(KEY_HOST, DEFAULT_HOST) ?: DEFAULT_HOST
        set(value) {
            val parsed = HostPort.parse(value) ?: return
            sp.edit().putString(KEY_HOST, parsed.asStored()).apply()
        }

    var micEnabled: Boolean
        get() = sp.getBoolean(KEY_MIC, DEFAULT_MIC)
        set(value) {
            sp.edit().putBoolean(KEY_MIC, value).apply()
        }

    /** True only after the user taps Listen. Older installs had KEY_MIC=false; ignore that until they toggle. */
    var micUserSet: Boolean
        get() = sp.getBoolean(KEY_MIC_USER, false)
        set(value) {
            sp.edit().putBoolean(KEY_MIC_USER, value).apply()
        }

    val width: Int get() = sp.getInt(KEY_W, 640)
    val height: Int get() = sp.getInt(KEY_H, 480)
    val fps: Int get() = sp.getInt(KEY_FPS, 8)
    val quality: Int get() = sp.getInt(KEY_Q, 70)

    /** Extra clockwise rotation for the mount. 0 = phone upright in portrait on the chest. */
    val mountRotation: Int get() = FrameMath.normalizeRotation(sp.getInt(KEY_MOUNT, 0))

    fun parsedHost(): String = HostPort.parse(hostPort)?.host ?: "127.0.0.1"

    fun parsedPort(): Int = HostPort.parse(hostPort)?.port ?: DEFAULT_PORT

    companion object {
        const val DEFAULT_HOST = "127.0.0.1:8765"
        const val DEFAULT_PORT = 8765
        const val DEFAULT_MIC = true
        private const val KEY_HOST = "host_port"
        private const val KEY_MIC = "mic"
        private const val KEY_MIC_USER = "mic_user"
        private const val KEY_W = "width"
        private const val KEY_H = "height"
        private const val KEY_FPS = "fps"
        private const val KEY_Q = "quality"
        private const val KEY_MOUNT = "mount_rotation"
    }
}
