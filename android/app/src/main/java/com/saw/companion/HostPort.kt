package com.saw.companion

/**
 * Laptop address typed in the phone UI: IPv4 or hostname, optional :port.
 * Default port is 8765. Whitespace is trimmed; anything else is rejected.
 */
data class HostPort(val host: String, val port: Int) {
    fun asStored(): String = "$host:$port"

    companion object {
        const val DEFAULT_PORT = 8765
        const val INVALID_MESSAGE = "Invalid server address"

        fun parse(raw: String): HostPort? {
            val trimmed = raw.trim()
            if (trimmed.isEmpty()) return null
            if (trimmed.any { it.isWhitespace() }) return null
            if (trimmed.count { it == ':' } > 1) return null

            val host: String
            val port: Int
            val colon = trimmed.indexOf(':')
            if (colon >= 0) {
                host = trimmed.substring(0, colon)
                val portStr = trimmed.substring(colon + 1)
                port = portStr.toIntOrNull() ?: return null
                if (port !in 1..65535) return null
            } else {
                host = trimmed
                port = DEFAULT_PORT
            }
            if (!isValidHost(host)) return null
            return HostPort(host, port)
        }

        private fun isValidHost(host: String): Boolean {
            if (host.isEmpty() || host.length > 253) return false
            if (looksLikeIpv4(host)) return isValidIpv4(host)
            return isHostname(host)
        }

        private fun looksLikeIpv4(host: String): Boolean {
            val parts = host.split('.')
            return parts.size == 4 && parts.all { it.isNotEmpty() && it.all(Char::isDigit) }
        }

        private fun isValidIpv4(host: String): Boolean {
            val parts = host.split('.')
            if (parts.size != 4) return false
            return parts.all { part ->
                if (part.isEmpty() || part.length > 3) return false
                if (part.length > 1 && part.startsWith('0')) return false
                val n = part.toIntOrNull() ?: return false
                n in 0..255
            }
        }

        private fun isHostname(host: String): Boolean {
            val name = host.trimEnd('.')
            if (name.isEmpty() || name.length > 253) return false
            val labels = name.split('.')
            if (labels.isEmpty()) return false
            return labels.all { label ->
                label.length in 1..63 &&
                    label.first().isLetterOrDigit() &&
                    label.last().isLetterOrDigit() &&
                    label.all { it.isLetterOrDigit() || it == '-' }
            }
        }
    }
}
