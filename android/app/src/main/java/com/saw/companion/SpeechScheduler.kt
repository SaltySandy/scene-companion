package com.saw.companion

/**
 * Queue policy for TTS / clips. Extracted so unit tests can prove urgent
 * speech interrupts a backlog of normal speech (a hazard must not sit behind
 * "frame received, N per second").
 */
class SpeechScheduler<T> {
    private val queue = ArrayDeque<T>()
    var interrupted: Int = 0
        private set

    val size: Int get() = queue.size
    val snapshot: List<T> get() = queue.toList()

    fun enqueue(item: T, urgent: Boolean): Boolean {
        val didInterrupt = urgent && (queue.isNotEmpty() || playing)
        if (urgent) {
            if (didInterrupt) interrupted += 1
            queue.clear()
            playing = false
        }
        queue.addLast(item)
        return didInterrupt
    }

    var playing: Boolean = false

    fun take(): T? {
        val item = queue.removeFirstOrNull() ?: return null
        playing = true
        return item
    }

    fun finished() {
        playing = false
    }

    fun clear() {
        queue.clear()
        playing = false
    }
}
