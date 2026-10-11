package com.saw.companion

import org.junit.Assert.assertEquals
import org.junit.Test

class StreamUrlTest {
    @Test fun usbDefault() = assertEquals("ws://127.0.0.1:8765/v1/stream", StreamUrl.build("127.0.0.1:8765"))
    @Test fun blankFallsBackToUsb() = assertEquals("ws://127.0.0.1:8765/v1/stream", StreamUrl.build("  "))
    @Test fun lanHostPort() = assertEquals("ws://192.168.1.20:8765/v1/stream", StreamUrl.build("192.168.1.20:8765"))
    @Test fun bareIpUsesDefaultPort() = assertEquals("ws://10.0.0.5:8765/v1/stream", StreamUrl.build("10.0.0.5"))
    @Test fun bareTunnelHostIsWss() =
        assertEquals("wss://abc-def.trycloudflare.com/v1/stream", StreamUrl.build("abc-def.trycloudflare.com"))
    @Test fun pastedHttpsTunnelUrl() =
        assertEquals("wss://abc-def.trycloudflare.com/v1/stream", StreamUrl.build("https://abc-def.trycloudflare.com/"))
    @Test fun explicitWssKeepsPath() =
        assertEquals("wss://example.com/custom", StreamUrl.build("wss://example.com/custom"))
    @Test fun explicitWsAddsPath() =
        assertEquals("ws://192.168.1.20:9000/v1/stream", StreamUrl.build("ws://192.168.1.20:9000"))
}