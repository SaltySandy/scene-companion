package com.saw.companion

/**
 * NTP-style clock offset from a ping/pong sample. The laptop offset is
 * `laptop ≈ phone + offset`. The minimum-RTT sample is kept as the best estimate
 * (least biased by delay).
 *
 * t0, t3 = phone clock; t1, t2 = laptop clock. All milliseconds.
 */
class ClockSync {
    data class Sample(
        val t0: Long,
        val t1: Long,
        val t2: Long,
        val t3: Long,
    ) {
        val rttMs: Long get() = (t3 - t0) - (t2 - t1)
        val offsetMs: Long get() = ((t1 - t0) + (t2 - t3)) / 2L
    }

    var best: Sample? = null
        private set
    var samples: Int = 0
        private set

    fun consider(sample: Sample): Sample {
        samples += 1
        val cur = best
        if (cur == null || sample.rttMs <= cur.rttMs) {
            best = sample
        }
        return best!!
    }

    fun consider(t0: Long, t1: Long, t2: Long, t3: Long): Sample =
        consider(Sample(t0, t1, t2, t3))

    fun reset() {
        best = null
        samples = 0
    }

    companion object {
        fun offsetMs(t0: Long, t1: Long, t2: Long, t3: Long): Long =
            ((t1 - t0) + (t2 - t3)) / 2L

        fun rttMs(t0: Long, t1: Long, t2: Long, t3: Long): Long =
            (t3 - t0) - (t2 - t1)
    }
}
