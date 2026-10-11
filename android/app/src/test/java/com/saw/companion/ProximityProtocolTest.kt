package com.saw.companion

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProximityProtocolTest {
    @Test
    fun parsesAnOkHeartbeat() {
        val obj = Protocol.decodeJson(
            """{"v":1,"type":"proximity","seq":7,"state":"ok","level":"near",
            "levels":{"left":"clear","center":"near","right":"clear"},
            "sectors_m":{"left":3.1,"center":1.2,"right":2.8},"frame_key":1700000000123,"age_ms":180}""",
        )
        val p = Protocol.parseProximity(obj)
        assertTrue(p.known)
        assertEquals(7L, p.seq)
        assertEquals("near", p.level)
        assertEquals("near", p.levels["center"])
        assertEquals(1.2, p.sectorsM["center"]!!, 1e-9)
        assertEquals(1_700_000_000_123L, p.frameKey)
        assertEquals(180L, p.ageMs)
    }

    @Test
    fun unknownStateAndNullsAreNotKnown() {
        val p = Protocol.parseProximity(
            JSONObject("""{"v":1,"type":"proximity","seq":8,"state":"unknown","level":null,"levels":null,"sectors_m":null,"frame_key":null,"age_ms":null}"""),
        )
        assertFalse(p.known)
        assertNull(p.level)
        assertNull(p.frameKey)
        assertTrue(p.levels.isEmpty())
    }

    @Test
    fun unrecognizedLevelIsNotKnown() {
        val p = Protocol.parseProximity(JSONObject("""{"v":1,"type":"proximity","state":"ok","level":"stop"}"""))
        assertFalse(p.known)
    }
}
