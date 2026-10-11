package com.saw.companion

import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * SAW protocol v1. Must match server/protocol.py.
 *
 * Binary envelope:
 *   magic "SAW1" | kind u8 | flags u8 | reserved u16 | timestamp_ms u64 BE | payload_len u32 BE | payload
 */
object Protocol {
    const val VERSION = 1
    const val HEADER_SIZE = 20
    val MAGIC: ByteArray = byteArrayOf('S'.code.toByte(), 'A'.code.toByte(), 'W'.code.toByte(), '1'.code.toByte())

    const val KIND_JPEG: Int = 0x01
    const val KIND_MIC: Int = 0x02
    const val KIND_AUDIO: Int = 0x03

    const val FLAG_WAV: Int = 0
    const val FLAG_MP3: Int = 1
    const val FLAG_PCM: Int = 2

    const val TYPE_HELLO = "hello"
    const val TYPE_HELLO_ACK = "hello_ack"
    const val TYPE_PING = "ping"
    const val TYPE_PONG = "pong"
    const val TYPE_SAY = "say"
    const val TYPE_CONFIG = "config"
    const val TYPE_STATUS = "status"
    const val TYPE_STATS = "stats"
    const val TYPE_MIC = "mic"
    const val TYPE_SPEAKING = "speaking"
    const val TYPE_TRANSCRIPT = "transcript"
    const val TYPE_ERROR = "error"
    const val TYPE_PROXIMITY = "proximity"
    const val TYPE_ACK = "ack"
    const val TYPE_TTS_START = "tts_start"
    const val TYPE_AUDIO_EVENT = "audio_event"

    const val STATE_OK = "ok"
    const val STATE_UNKNOWN = "unknown"
    const val LEVEL_CLEAR = "clear"
    const val LEVEL_NEAR = "near"
    const val LEVEL_DANGER = "danger"
    val LEVELS = listOf(LEVEL_CLEAR, LEVEL_NEAR, LEVEL_DANGER)
    val SECTORS = listOf("left", "center", "right")

    /** A proximity heartbeat. [known] is false for state "unknown" or any level we don't understand. */
    data class Proximity(
        val seq: Long,
        val state: String,
        val level: String?,
        val levels: Map<String, String>,
        val sectorsM: Map<String, Double>,
        val frameKey: Long?,
        val ageMs: Long?,
    ) {
        val known: Boolean get() = state == STATE_OK && level in LEVELS
    }

    data class BinaryMessage(
        val kind: Int,
        val flags: Int,
        val timestampMs: Long,
        val payload: ByteArray,
    )

    fun encodeBinary(kind: Int, payload: ByteArray, timestampMs: Long, flags: Int = 0): ByteArray {
        val buf = ByteBuffer.allocate(HEADER_SIZE + payload.size).order(ByteOrder.BIG_ENDIAN)
        buf.put(MAGIC)
        buf.put(kind.toByte())
        buf.put(flags.toByte())
        buf.putShort(0)
        buf.putLong(timestampMs)
        buf.putInt(payload.size)
        buf.put(payload)
        return buf.array()
    }

    fun decodeBinary(data: ByteArray): BinaryMessage {
        if (data.size < HEADER_SIZE) {
            throw IllegalArgumentException("binary frame too short: ${data.size}")
        }
        val buf = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN)
        val magic = ByteArray(4)
        buf.get(magic)
        if (!magic.contentEquals(MAGIC)) {
            throw IllegalArgumentException("bad magic")
        }
        val kind = buf.get().toInt() and 0xFF
        val flags = buf.get().toInt() and 0xFF
        buf.short // reserved
        val ts = buf.long
        val len = buf.int
        if (len < 0 || data.size < HEADER_SIZE + len) {
            throw IllegalArgumentException("truncated payload")
        }
        if (kind != KIND_JPEG && kind != KIND_MIC && kind != KIND_AUDIO) {
            throw IllegalArgumentException("unknown kind: $kind")
        }
        val payload = data.copyOfRange(HEADER_SIZE, HEADER_SIZE + len)
        return BinaryMessage(kind, flags, ts, payload)
    }

    fun encodeJson(type: String, fields: Map<String, Any?> = emptyMap()): String {
        val obj = JSONObject()
        obj.put("v", VERSION)
        obj.put("type", type)
        for ((k, v) in fields) {
            obj.put(k, v ?: JSONObject.NULL)
        }
        return obj.toString()
    }

    fun decodeJson(text: String): JSONObject {
        val obj = JSONObject(text)
        if (obj.optInt("v", -1) != VERSION) {
            throw IllegalArgumentException("unsupported protocol version: ${obj.opt("v")}")
        }
        if (!obj.has("type")) {
            throw IllegalArgumentException("JSON message missing type")
        }
        return obj
    }

    fun parseProximity(obj: JSONObject): Proximity {
        val levelsObj = obj.optJSONObject("levels")
        val sectorsObj = obj.optJSONObject("sectors_m")
        val levels = mutableMapOf<String, String>()
        val sectors = mutableMapOf<String, Double>()
        for (s in SECTORS) {
            if (levelsObj != null && !levelsObj.isNull(s)) levels[s] = levelsObj.optString(s)
            if (sectorsObj != null && !sectorsObj.isNull(s)) sectors[s] = sectorsObj.optDouble(s)
        }
        return Proximity(
            seq = obj.optLong("seq", 0L),
            state = obj.optString("state", STATE_UNKNOWN),
            level = if (obj.isNull("level")) null else obj.optString("level"),
            levels = levels,
            sectorsM = sectors,
            frameKey = if (obj.isNull("frame_key")) null else obj.optLong("frame_key"),
            ageMs = if (obj.isNull("age_ms")) null else obj.optLong("age_ms"),
        )
    }

    fun mimeForFlag(flags: Int): String = when (flags) {
        FLAG_MP3 -> "audio/mpeg"
        FLAG_PCM -> "audio/l16"
        else -> "audio/wav"
    }

    fun nowMs(): Long = System.currentTimeMillis()
}
