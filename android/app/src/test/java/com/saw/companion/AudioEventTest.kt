package com.saw.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AudioEventTest {
    @Test
    fun jsonCarriesIdEventAndVolume() {
        val text = Protocol.encodeJson(
            Protocol.TYPE_AUDIO_EVENT,
            AudioEvent.fields("a0003", AudioEvent.STARTED, 7, false),
        )
        val obj = Protocol.decodeJson(text)
        assertEquals("audio_event", obj.getString("type"))
        assertEquals("a0003", obj.getString("id"))
        assertEquals("started", obj.getString("event"))
        assertEquals(7, obj.getInt("volume"))
        assertFalse(obj.getBoolean("muted"))
    }

    @Test
    fun errorIncludesCode() {
        val fields = AudioEvent.fields("a0001", AudioEvent.ERROR, 0, true, errorCode=5)
        assertEquals(5, fields["error_code"])
        assertEquals(true, fields["muted"])
    }

    @Test
    fun sayMessageCanCarryId() {
        val obj = Protocol.decodeJson(
            Protocol.encodeJson(
                Protocol.TYPE_SAY,
                mapOf("text" to "stop", "urgent" to true, "id" to "a0002", "capture_ts" to 9L),
            ),
        )
        assertEquals("a0002", obj.getString("id"))
        assertTrue(obj.getBoolean("urgent"))
    }

    @Test
    fun lifecycleNamesMatchServer() {
        assertEquals("received", AudioEvent.RECEIVED)
        assertEquals("queued", AudioEvent.QUEUED)
        assertEquals("started", AudioEvent.STARTED)
        assertEquals("done", AudioEvent.DONE)
        assertEquals("interrupted", AudioEvent.INTERRUPTED)
        assertEquals("error", AudioEvent.ERROR)
        assertEquals("tts_not_ready", AudioEvent.TTS_NOT_READY)
    }
}
