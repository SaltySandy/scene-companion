package com.saw.companion

/** Stream presets. Higher levels trade frame rate for sharper, larger frames. */
enum class QualityLevel(
    val width: Int,
    val height: Int,
    val fps: Int,
    val jpegQuality: Int,
    val spoken: String,
) {
    LOW(640, 480, 8, 70, "low quality"),
    MEDIUM(1280, 720, 5, 80, "medium quality"),
    HIGH(1920, 1080, 3, 85, "high quality");

    companion object {
        val DEFAULT = LOW

        fun fromKey(key: String?): QualityLevel = values().firstOrNull { it.name == key } ?: DEFAULT
    }
}