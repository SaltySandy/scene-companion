package com.saw.companion

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ProtocolTest {
    private val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0x00, 0x01, 0x02, 0xFF.toByte(), 0xD9.toByte())

    @Test
    fun jpegRoundTrip() {
        val raw = Protocol.encodeBinary(Protocol.KIND_JPEG, jpeg, 1_700_000_000_123L)
        val msg = Protocol.decodeBinary(raw)
        assertEquals(Protocol.KIND_JPEG, msg.kind)
        assertEquals(0, msg.flags)
        assertEquals(1_700_000_000_123L, msg.timestampMs)
        assertArrayEquals(jpeg, msg.payload)
        assertEquals(Protocol.HEADER_SIZE + jpeg.size, raw.size)
        assertEquals('S'.code.toByte(), raw[0])
        assertEquals('1'.code.toByte(), raw[3])
    }

    @Test
    fun audioClipFlags() {
        val wav = "RIFF".toByteArray()
        val raw = Protocol.encodeBinary(Protocol.KIND_AUDIO, wav, 42L, Protocol.FLAG_WAV)
        val msg = Protocol.decodeBinary(raw)
        assertEquals(Protocol.KIND_AUDIO, msg.kind)
        assertEquals(Protocol.FLAG_WAV, msg.flags)
        assertArrayEquals(wav, msg.payload)
        assertEquals("audio/wav", Protocol.mimeForFlag(msg.flags))
    }

    @Test
    fun micRoundTrip() {
        val pcm = ByteArray(256) { it.toByte() }
        val msg = Protocol.decodeBinary(Protocol.encodeBinary(Protocol.KIND_MIC, pcm, 9L))
        assertEquals(Protocol.KIND_MIC, msg.kind)
        assertArrayEquals(pcm, msg.payload)
    }

    @Test
    fun badMagic() {
        val raw = Protocol.encodeBinary(Protocol.KIND_JPEG, byteArrayOf(1), 1L)
        raw[0] = 'X'.code.toByte()
        assertThrows(IllegalArgumentException::class.java) { Protocol.decodeBinary(raw) }
    }

    @Test
    fun truncatedHeader() {
        assertThrows(IllegalArgumentException::class.java) {
            Protocol.decodeBinary("SAW1".toByteArray())
        }
    }

    @Test
    fun truncatedPayload() {
        val raw = Protocol.encodeBinary(Protocol.KIND_JPEG, "hello-jpeg".toByteArray(), 1L)
        assertThrows(IllegalArgumentException::class.java) {
            Protocol.decodeBinary(raw.copyOf(raw.size - 1))
        }
    }

    @Test
    fun unknownKind() {
        val raw = Protocol.encodeBinary(Protocol.KIND_JPEG, byteArrayOf(1), 1L)
        raw[4] = 0x99.toByte()
        assertThrows(IllegalArgumentException::class.java) { Protocol.decodeBinary(raw) }
    }

    @Test
    fun jsonRoundTrip() {
        val text = Protocol.encodeJson(Protocol.TYPE_SAY, mapOf("text" to "door on the left", "urgent" to true))
        val obj = Protocol.decodeJson(text)
        assertEquals(1, obj.getInt("v"))
        assertEquals("say", obj.getString("type"))
        assertEquals("door on the left", obj.getString("text"))
        assertEquals(true, obj.getBoolean("urgent"))
    }

    @Test
    fun jsonRejectsWrongVersion() {
        assertThrows(IllegalArgumentException::class.java) {
            Protocol.decodeJson("""{"v":2,"type":"say"}""")
        }
    }

    @Test
    fun jsonRequiresType() {
        assertThrows(IllegalArgumentException::class.java) {
            Protocol.decodeJson("""{"v":1}""")
        }
    }
}
