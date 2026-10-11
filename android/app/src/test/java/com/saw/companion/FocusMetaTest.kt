package com.saw.companion

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class FocusMetaTest {
    @Test
    fun matchExactSensorTimestamp() {
        val ring = FocusRing()
        ring.add(FocusSample(1_000_000_000L, 3.33f, FrameFocus.AF_PASSIVE_FOCUSED))
        ring.add(FocusSample(1_033_000_000L, 3.50f, FrameFocus.AF_PASSIVE_FOCUSED))
        val got = ring.match(1_033_000_000L)
        assertNotNull(got)
        assertEquals(3.50f, got!!.focusDistanceDiopters)
        assertEquals(FrameFocus.AF_PASSIVE_FOCUSED, got.afState)
    }

    @Test
    fun matchNearestWithinWindow() {
        val ring = FocusRing()
        ring.add(FocusSample(10_000_000_000L, 2.0f, FrameFocus.AF_PASSIVE_FOCUSED))
        ring.add(FocusSample(10_033_000_000L, 2.2f, FrameFocus.AF_FOCUSED_LOCKED))
        val got = ring.match(10_040_000_000L) // 7 ms after the second sample
        assertNotNull(got)
        assertEquals(2.2f, got!!.focusDistanceDiopters)
        assertEquals(FrameFocus.AF_FOCUSED_LOCKED, got.afState)
    }

    @Test
    fun noMatchOutsideWindow() {
        val ring = FocusRing()
        ring.add(FocusSample(5_000_000_000L, 1.5f, FrameFocus.AF_PASSIVE_FOCUSED))
        assertNull(ring.match(5_200_000_000L, windowNs = 50_000_000L))
    }

    @Test
    fun emptyRingMatchesNothing() {
        assertNull(FocusRing().match(1L))
    }

    @Test
    fun jsonSidecarRoundTrip() {
        val focus = FrameFocus(
            sensorTsNs = 99_000_000L,
            focusDistanceDiopters = 3.33f,
            afState = FrameFocus.AF_PASSIVE_FOCUSED,
            focusCalibration = FrameFocus.CALIBRATION_APPROXIMATE,
        )
        val text = Protocol.encodeJson(Protocol.TYPE_FRAME_META, focus.toFields(1_700_000_000_123L))
        val obj = Protocol.decodeJson(text)
        assertEquals(Protocol.TYPE_FRAME_META, obj.getString("type"))
        assertEquals(1_700_000_000_123L, obj.getLong("ts"))
        val parsed = FrameFocus.fromJson(obj)
        assertEquals(99_000_000L, parsed.sensorTsNs)
        assertEquals(3.33f, parsed.focusDistanceDiopters!!, 0.001f)
        assertEquals(FrameFocus.AF_PASSIVE_FOCUSED, parsed.afState)
        assertEquals(FrameFocus.CALIBRATION_APPROXIMATE, parsed.focusCalibration)
    }

    @Test
    fun jsonSidecarAllowsNullFocusDistance() {
        val text = Protocol.encodeJson(
            Protocol.TYPE_FRAME_META,
            mapOf(
                "ts" to 5L,
                "sensor_ts_ns" to 7L,
                "focus_distance_diopters" to null,
                "af_state" to FrameFocus.AF_PASSIVE_SCAN,
                "focus_calibration" to FrameFocus.CALIBRATION_UNCALIBRATED,
            ),
        )
        val parsed = FrameFocus.fromJson(Protocol.decodeJson(text))
        assertNull(parsed.focusDistanceDiopters)
        assertEquals(FrameFocus.AF_PASSIVE_SCAN, parsed.afState)
        assertEquals(FrameFocus.CALIBRATION_UNCALIBRATED, parsed.focusCalibration)
    }

    @Test
    fun jpegWithoutMetaStillDecodes() {
        val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0x00, 0x01)
        val msg = Protocol.decodeBinary(Protocol.encodeBinary(Protocol.KIND_JPEG, jpeg, 42L))
        assertEquals(Protocol.KIND_JPEG, msg.kind)
        assertEquals(0, msg.flags)
        assertEquals(42L, msg.timestampMs)
        org.junit.Assert.assertArrayEquals(jpeg, msg.payload)
    }

    @Test
    fun frameMetaTypeIsStable() {
        assertEquals("frame_meta", Protocol.TYPE_FRAME_META)
        val obj = JSONObject(Protocol.encodeJson(Protocol.TYPE_FRAME_META, mapOf("ts" to 1L)))
        assertEquals(1, obj.getInt("v"))
        assertEquals("frame_meta", obj.getString("type"))
    }
}
