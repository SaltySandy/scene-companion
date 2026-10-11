package com.saw.companion

/**
 * Capture → TTS-start on the *phone* clock. This is the gold-standard e2e
 * figure the laptop echoes `capture_ts` in `say` to obtain.
 */
object TtsTiming {
    fun e2eMs(captureTs: Long, ttsStartTs: Long): Long = ttsStartTs - captureTs

    fun reportFields(
        captureTs: Long,
        ttsStartTs: Long,
        sayRecvTs: Long,
    ): Map<String, Any?> = mapOf(
        "capture_ts" to captureTs,
        "tts_start_ts" to ttsStartTs,
        "say_recv_ts" to sayRecvTs,
        "e2e_ms" to e2eMs(captureTs, ttsStartTs),
    )
}
