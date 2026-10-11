package com.saw.companion

/**
 * Turns whatever is typed in the address field into a WebSocket URL.
 *
 * - `ws://...` / `wss://...` are used as given (path defaults to /v1/stream).
 * - `http://...` / `https://...` become `ws://...` / `wss://...` (so a pasted tunnel URL works).
 * - `host:port` keeps the old behaviour: `ws://host:port/v1/stream` (USB adb reverse, LAN).
 * - A bare IP or `localhost` without a port means the laptop on 8765 over plain `ws://`.
 * - Any other bare hostname (e.g. `xyz.trycloudflare.com`) is a public server: `wss://host/v1/stream`.
 */
object StreamUrl {
    const val PATH = "/v1/stream"
    const val DEFAULT_PORT = 8765

    private val ipv4 = Regex("""^\d{1,3}(\.\d{1,3}){3}$""")

    fun build(raw: String?): String {
        var s = (raw ?: "").trim().ifBlank { "127.0.0.1:$DEFAULT_PORT" }
        val lower = s.lowercase()
        val scheme = when {
            lower.startsWith("wss://") -> "wss"
            lower.startsWith("ws://") -> "ws"
            lower.startsWith("https://") -> "wss"
            lower.startsWith("http://") -> "ws"
            else -> null
        }
        if (scheme != null) {
            s = s.substring(s.indexOf("://") + 3)
            val slash = s.indexOf('/')
            val authority = if (slash >= 0) s.substring(0, slash) else s
            val path = if (slash >= 0) s.substring(slash).trimEnd('/') else ""
            return "$scheme://$authority${path.ifEmpty { PATH }}"
        }
        s = s.trimEnd('/')
        val idx = s.lastIndexOf(':')
        if (idx > 0 && s.substring(idx + 1).toIntOrNull() != null) {
            return "ws://$s$PATH"
        }
        val host = if (idx > 0) s.substring(0, idx) else s
        return if (host == "localhost" || ipv4.matches(host)) {
            "ws://$host:$DEFAULT_PORT$PATH"
        } else {
            "wss://$host$PATH"
        }
    }
}