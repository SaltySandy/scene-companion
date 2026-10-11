package com.saw.companion

import android.app.Application
import io.sentry.Attachment
import io.sentry.Sentry
import io.sentry.SentryEvent
import io.sentry.SentryLevel
import io.sentry.android.core.SentryAndroid
import io.sentry.protocol.Message

/**
 * Crash, ANR, and non-fatal reporting. Camera frames and other image bytes
 * are never attached.
 */
object SawSentry {
    private val lastCaptureMs = HashMap<String, Long>()

    fun init(app: Application) {
        SentryAndroid.init(app) { options ->
            options.dsn = BuildConfig.SENTRY_DSN
            options.environment = "hackathon"
            options.release = BuildConfig.VERSION_NAME + "+" + BuildConfig.VERSION_CODE
            options.tracesSampleRate = 0.2
            options.isAnrEnabled = true
            options.isAnrReportInDebug = true
            options.isAttachScreenshot = false
            options.isAttachViewHierarchy = false
            options.beforeSend = io.sentry.SentryOptions.BeforeSendCallback { event, hint ->
                stripImages(hint)
                event
            }
        }
    }

    fun breadcrumb(message: String) {
        Sentry.addBreadcrumb(message)
    }

    /** Non-fatal. Throttled per source so a retry loop does not flood Sentry. */
    fun capture(error: Throwable, where: String) {
        val now = android.os.SystemClock.elapsedRealtime()
        val last = lastCaptureMs[where] ?: 0L
        if (now - last < 30_000L) return
        lastCaptureMs[where] = now
        Sentry.withScope { scope ->
            scope.setTag("where", where)
            Sentry.captureException(error)
        }
    }

    /** Long-press on the title. One explicit test event. */
    fun debugTrigger() {
        val event = SentryEvent().apply {
            level = SentryLevel.ERROR
            message = Message().apply { formatted = "sightline android sentry debug" }
        }
        Sentry.captureEvent(event)
    }

    private fun stripImages(hint: io.sentry.Hint) {
        val kept = hint.attachments.filterNot { isImage(it) }
        hint.clearAttachments()
        if (kept.isNotEmpty()) hint.addAttachments(kept)
        hint.screenshot = null
        hint.viewHierarchy = null
    }

    private fun isImage(attachment: Attachment): Boolean {
        val type = attachment.contentType?.lowercase().orEmpty()
        val name = attachment.filename?.lowercase().orEmpty()
        return type.startsWith("image/") ||
            name.endsWith(".jpg") ||
            name.endsWith(".jpeg") ||
            name.endsWith(".png") ||
            name.endsWith(".webp")
    }
}
