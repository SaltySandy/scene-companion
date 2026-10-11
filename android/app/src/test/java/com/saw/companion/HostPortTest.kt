package com.saw.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HostPortTest {
    @Test
    fun acceptsIpv4WithPort() {
        assertEquals(HostPort("127.0.0.1", 8765), HostPort.parse("127.0.0.1:8765"))
    }

    @Test
    fun acceptsIpv4DefaultPort() {
        assertEquals(HostPort("10.57.162.97", 8765), HostPort.parse("10.57.162.97"))
    }

    @Test
    fun trimsWhitespace() {
        assertEquals(HostPort("10.57.162.97", 8765), HostPort.parse(" 10.57.162.97:8765 "))
    }

    @Test
    fun rejectsSpaceInHost() {
        assertNull(HostPort.parse("12710.57 162.97"))
    }

    @Test
    fun rejectsNonNumericPort() {
        assertNull(HostPort.parse("host:abc"))
    }

    @Test
    fun acceptsTailscaleHostname() {
        assertEquals(
            HostPort("my-laptop.tail1234.ts.net", 8765),
            HostPort.parse("my-laptop.tail1234.ts.net:8765"),
        )
    }
}
