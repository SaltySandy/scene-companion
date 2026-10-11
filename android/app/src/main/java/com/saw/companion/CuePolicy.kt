package com.saw.companion

import kotlin.math.PI
import kotlin.math.sin

enum class CueState { CLEAR, NEAR, DANGER, LOST }

/** What to play now. [pan] is -1 (left) to 1 (right). [frameKey] is the frame behind it, if any. */
data class Cue(val state: CueState, val pan: Float, val frameKey: Long?)

/**
 * Decides what the walker hears. Silence never means clear: a dropped socket, no
 * heartbeat for [watchdogMs], an "unknown" heartbeat, or a level we don't know all
 * give LOST. Thread-safe: heartbeats arrive on the socket thread, the audio thread
 * polls [cue]. Times are a monotonic clock (SystemClock.elapsedRealtime).
 */
class CuePolicy(private val watchdogMs: Long = 1_000L) {
    private var last: Protocol.Proximity? = null
    private var lastAtMs = 0L
    private var connected = false

    // Spoken transitions, debounced so one stale frame does not cause chatter.
    private var currentlyLost = true
    private var lostSinceMs = 0L
    private var spokenLost = true // at start the sensor is off and the tones already say so

    @Synchronized
    fun onProximity(p: Protocol.Proximity, nowMs: Long) {
        last = p
        lastAtMs = nowMs
    }

    @Synchronized
    fun onConnection(isConnected: Boolean) {
        connected = isConnected
        if (!isConnected) last = null
    }

    @Synchronized
    fun cue(nowMs: Long): Cue {
        val p = last
        if (!connected || p == null || nowMs - lastAtMs > watchdogMs || !p.known) {
            return Cue(CueState.LOST, 0f, p?.frameKey)
        }
        val state = when (p.level) {
            Protocol.LEVEL_DANGER -> CueState.DANGER
            Protocol.LEVEL_NEAR -> CueState.NEAR
            else -> CueState.CLEAR
        }
        return Cue(state, pan(p.levels), p.frameKey)
    }

    /** "sensor lost" after 1 s of LOST following a working sensor; "sensor on" after 0.5 s back. */
    @Synchronized
    fun announcement(cue: Cue, nowMs: Long): String? {
        val lost = cue.state == CueState.LOST
        if (lost != currentlyLost) {
            currentlyLost = lost
            lostSinceMs = nowMs
        }
        if (lost && !spokenLost && nowMs - lostSinceMs >= 1_000L) {
            spokenLost = true
            return "sensor lost"
        }
        if (!lost && spokenLost && nowMs - lostSinceMs >= 500L) {
            spokenLost = false
            return "sensor on"
        }
        return null
    }

    private fun severity(level: String?): Int = when (level) {
        Protocol.LEVEL_DANGER -> 2
        Protocol.LEVEL_NEAR -> 1
        else -> 0
    }

    private fun pan(levels: Map<String, String>): Float =
        ((severity(levels["right"]) - severity(levels["left"])) / 2f).coerceIn(-1f, 1f)
}

/** One repeating beep. The first half of the on-time uses [freqHz], the second half [freq2Hz]. */
data class Pattern(
    val freqHz: Double,
    val freq2Hz: Double,
    val onMs: Int,
    val periodMs: Int,
    val gain: Double,
    val vibrateMs: Long,
)

object CuePatterns {
    val CLEAR = Pattern(440.0, 440.0, onMs = 25, periodMs = 2_000, gain = 0.12, vibrateMs = 0)
    val NEAR = Pattern(880.0, 880.0, onMs = 70, periodMs = 350, gain = 0.5, vibrateMs = 30)
    val DANGER = Pattern(1320.0, 1320.0, onMs = 120, periodMs = 150, gain = 0.9, vibrateMs = 100)
    val LOST = Pattern(660.0, 330.0, onMs = 500, periodMs = 1_000, gain = 0.7, vibrateMs = 400)

    fun forState(state: CueState): Pattern = when (state) {
        CueState.CLEAR -> CLEAR
        CueState.NEAR -> NEAR
        CueState.DANGER -> DANGER
        CueState.LOST -> LOST
    }
}

/**
 * Renders patterns into interleaved stereo 16-bit PCM, one chunk per call.
 * A new pattern restarts at its beginning, so a new warning sounds at once.
 */
class ToneSynth(private val sampleRate: Int = 24_000) {
    private var pattern: Pattern? = null
    private var pos = 0L
    private var phase = 0.0

    /** Fills [out] (stereo, interleaved). Returns true if a beep starts in this chunk. */
    fun render(p: Pattern, pan: Float, out: ShortArray): Boolean {
        if (p != pattern) {
            pattern = p
            pos = 0L
            phase = 0.0
        }
        val period = (p.periodMs.toLong() * sampleRate / 1000).coerceAtLeast(1L)
        val on = (p.onMs.toLong() * sampleRate / 1000).coerceAtLeast(1L)
        val fade = minOf(on / 4, sampleRate * 5L / 1000).coerceAtLeast(1L).toDouble()
        val left = p.gain * minOf(1.0, 1.0 - pan)
        val right = p.gain * minOf(1.0, 1.0 + pan)
        var beepStarted = false
        for (i in 0 until out.size / 2) {
            val t = pos % period
            if (t == 0L) beepStarted = true
            var s = 0.0
            if (t < on) {
                val f = if (t < on / 2) p.freqHz else p.freq2Hz
                phase += 2 * PI * f / sampleRate
                if (phase > 2 * PI) phase -= 2 * PI
                val env = minOf(1.0, t / fade, (on - t) / fade)
                s = sin(phase) * env
            }
            out[2 * i] = (s * left * Short.MAX_VALUE).toInt().toShort()
            out[2 * i + 1] = (s * right * Short.MAX_VALUE).toInt().toShort()
            pos++
        }
        return beepStarted
    }
}
