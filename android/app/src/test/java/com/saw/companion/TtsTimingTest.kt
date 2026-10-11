package com.saw.companion

import org.junit.Assert.assertEquals
import org.junit.Test

class TtsTimingTest {
    @Test
    fun e2eIsSingleClock() {
        assertEquals(240L, TtsTiming.e2eMs(1_000L, 1_240L))
    }

    @Test
    fun reportIncludesCaptureAndRecv() {
        val fields = TtsTiming.reportFields(100L, 340L, 280L)
        assertEquals(100L, fields["capture_ts"])
        assertEquals(340L, fields["tts_start_ts"])
        assertEquals(280L, fields["say_recv_ts"])
        assertEquals(240L, fields["e2e_ms"])
    }

    @Test
    fun jsonRoundTripTtsStart() {
        val text = Protocol.encodeJson(Protocol.TYPE_TTS_START, TtsTiming.reportFields(5, 9, 8))
        val obj = Protocol.decodeJson(text)
        assertEquals("tts_start", obj.getString("type"))
        assertEquals(5, obj.getLong("capture_ts"))
        assertEquals(4, obj.getLong("e2e_ms"))
    }

    @Test
    fun pingPongJsonCarriesT0T1T2() {
        val ping = Protocol.decodeJson(Protocol.encodeJson(Protocol.TYPE_PING, mapOf("t0" to 11L, "t" to 11L)))
        assertEquals("ping", ping.getString("type"))
        assertEquals(11L, ping.getLong("t0"))
        val pong = Protocol.decodeJson(
            Protocol.encodeJson(Protocol.TYPE_PONG, mapOf("t0" to 11L, "t1" to 20L, "t2" to 21L, "t" to 11L)),
        )
        assertEquals(20L, pong.getLong("t1"))
    }

    @Test
    fun sayEchoesCaptureTs() {
        val obj = Protocol.decodeJson(
            Protocol.encodeJson(Protocol.TYPE_SAY, mapOf("text" to "stop", "urgent" to true, "capture_ts" to 99L)),
        )
        assertEquals(true, obj.getBoolean("urgent"))
        assertEquals(99L, obj.getLong("capture_ts"))
    }
}
