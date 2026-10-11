package com.saw.companion

/**
 * Phone → laptop lifecycle for one audio id. Must match server/audio_log.py.
 */
object AudioEvent {
    const val RECEIVED = "received"
    const val QUEUED = "queued"
    const val STARTED = "started"
    const val DONE = "done"
    const val INTERRUPTED = "interrupted"
    const val ERROR = "error"
    const val TTS_NOT_READY = "tts_not_ready"

    fun fields(
        id: String,
        event: String,
        volume: Int,
        muted: Boolean,
        errorCode: Int? = null,
    ): Map<String, Any?> = buildMap {
        put("id", id)
        put("event", event)
        put("volume", volume)
        put("muted", muted)
        if (errorCode != null) put("error_code", errorCode)
    }
}
