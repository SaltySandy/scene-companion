package com.saw.companion

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityManager
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.saw.companion.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: Prefs
    private var lastAnnounced = ""

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { result ->
        val cameraOk = result[Manifest.permission.CAMERA] == true || hasCamera()
        if (!cameraOk) {
            announce("Camera permission is required to start")
            Toast.makeText(this, R.string.camera_required, Toast.LENGTH_LONG).show()
            return@registerForActivityResult
        }
        if (prefs.micEnabled && !hasMic()) {
            announce("Microphone permission denied. Streaming camera only.")
            prefs.micEnabled = false
            binding.micToggle.isChecked = false
        }
        startStreaming()
    }

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val status = intent?.getStringExtra(StreamService.EXTRA_STATUS) ?: return
            render(status, intent.getBooleanExtra(StreamService.EXTRA_RUNNING, StreamService.running))
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = Prefs(this)

        binding.hostInput.setText(prefs.hostPort)
        binding.micToggle.isChecked = prefs.micEnabled

        binding.hostInput.setOnFocusChangeListener { _, hasFocus ->
            if (!hasFocus) persistSettings()
        }
        binding.micToggle.setOnCheckedChangeListener { _, checked ->
            prefs.micEnabled = checked
            if (checked && !hasMic() && StreamService.running) {
                requestAndMaybeStart()
            }
        }
        binding.title.setOnLongClickListener {
            SawSentry.debugTrigger()
            Toast.makeText(this, R.string.sentry_test_sent, Toast.LENGTH_SHORT).show()
            true
        }
        binding.startStop.setOnClickListener {
            persistSettings()
            if (StreamService.running) {
                StreamService.stop(this)
            } else {
                requestAndMaybeStart()
            }
        }
        render(if (StreamService.running) StreamService.lastSpoken else "stopped", StreamService.running)
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter(StreamService.ACTION_STATUS)
        ContextCompat.registerReceiver(this, statusReceiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
        render(if (StreamService.running) StreamService.lastSpoken else "stopped", StreamService.running)
    }

    override fun onStop() {
        persistSettings()
        try {
            unregisterReceiver(statusReceiver)
        } catch (_: Exception) {
        }
        super.onStop()
    }

    private fun persistSettings() {
        prefs.hostPort = binding.hostInput.text?.toString()?.ifBlank { Prefs.DEFAULT_HOST } ?: Prefs.DEFAULT_HOST
        prefs.micEnabled = binding.micToggle.isChecked
    }

    private fun requestAndMaybeStart() {
        persistSettings()
        val needed = mutableListOf<String>()
        if (!hasCamera()) needed.add(Manifest.permission.CAMERA)
        if (prefs.micEnabled && !hasMic()) needed.add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isEmpty()) {
            startStreaming()
        } else {
            permissionLauncher.launch(needed.toTypedArray())
        }
    }

    private fun startStreaming() {
        StreamService.start(this)
        render("starting", true)
        announce("starting")
    }

    private fun render(status: String, running: Boolean) {
        binding.status.text = status.replaceFirstChar { if (it.isLowerCase()) it.titlecase() else it.toString() }
        binding.startStop.text = if (running) getString(R.string.stop_streaming) else getString(R.string.start_streaming)
        binding.startStop.contentDescription =
            if (running) getString(R.string.stop_streaming_cd) else getString(R.string.start_streaming_cd)
        binding.hostInput.isEnabled = !running
        binding.micToggle.isEnabled = !running
        if (status != lastAnnounced && status.isNotBlank()) {
            lastAnnounced = status
            announce(status)
        }
    }

    private fun announce(text: String) {
        binding.status.announceForAccessibility(text)
        val am = getSystemService(AccessibilityManager::class.java)
        if (am != null && am.isEnabled) {
            val ev = AccessibilityEvent.obtain(AccessibilityEvent.TYPE_ANNOUNCEMENT)
            ev.text.add(text)
            ev.className = javaClass.name
            ev.packageName = packageName
            am.sendAccessibilityEvent(ev)
        }
    }

    private fun hasCamera(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    private fun hasMic(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
}
