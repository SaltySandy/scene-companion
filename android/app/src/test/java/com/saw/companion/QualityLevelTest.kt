package com.saw.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class QualityLevelTest {
    @Test fun unknownKeyFallsBackToDefault() = assertEquals(QualityLevel.DEFAULT, QualityLevel.fromKey("bogus"))
    @Test fun nullKeyFallsBackToDefault() = assertEquals(QualityLevel.DEFAULT, QualityLevel.fromKey(null))
    @Test fun roundTripsByName() = QualityLevel.values().forEach { assertEquals(it, QualityLevel.fromKey(it.name)) }
    @Test fun defaultMatchesOldStream() {
        val d = QualityLevel.DEFAULT
        assertEquals(listOf(640, 480, 8, 70), listOf(d.width, d.height, d.fps, d.jpegQuality))
    }
    @Test fun higherLevelsAreSharperAndSlower() {
        val l = QualityLevel.values()
        for (i in 1 until l.size) {
            assertTrue(l[i].width > l[i - 1].width)
            assertTrue(l[i].jpegQuality > l[i - 1].jpegQuality)
            assertTrue(l[i].fps < l[i - 1].fps)
        }
    }
}