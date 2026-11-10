package com.eyeturn.mobile

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings as SysSettings
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import kotlin.math.abs

class MainActivity : ComponentActivity() {
    private lateinit var settings: Settings
    private var refresh by mutableIntStateOf(0) // bumped on resume so permission checks redraw

    private val askCamera = registerForActivityResult(ActivityResultContracts.RequestPermission()) { refresh++ }
    private val askNotify = registerForActivityResult(ActivityResultContracts.RequestPermission()) { refresh++ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Engine.init(this)
        settings = Settings(this)
        setContent {
            val dark = isSystemInDarkTheme()
            MaterialTheme(colorScheme = if (dark) darkColorScheme() else lightColorScheme()) {
                Surface(Modifier.fillMaxSize()) {
                    var calibrating by remember { mutableStateOf(false) }
                    @Suppress("UNUSED_EXPRESSION") refresh
                    if (calibrating) {
                        BackHandler { calibrating = false }
                        CalibrationScreen(settings) { calibrating = false; refresh++ }
                    } else {
                        Home(onCalibrate = {
                            TrackingService.start(this)
                            calibrating = true
                        })
                    }
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        refresh++
    }

    private fun hasCamera() =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    private fun hasNotify() = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    @Composable
    private fun Home(onCalibrate: () -> Unit) {
        val running by Engine.running.collectAsState()
        val paused by Engine.paused.collectAsState()
        val status by Engine.status.collectAsState()
        val camera = hasCamera()
        val notify = hasNotify()
        val a11y = accessibilityOn(this)
        val calibrated = settings.calibration != null
        var mode by remember { mutableStateOf(settings.mode) }
        var style by remember { mutableStateOf(settings.turnStyle) }
        var sens by remember { mutableFloatStateOf(settings.glanceSensitivity) }
        var speed by remember { mutableFloatStateOf(settings.speed) }
        var overlap by remember { mutableFloatStateOf(settings.overlap) }
        var hold by remember { mutableStateOf(settings.holdScroll) }
        var showDot by remember { mutableStateOf(settings.showDot) }

        Column(
            Modifier.fillMaxSize().safeDrawingPadding().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("EyeTurn", fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text("Turns pages and scrolls in any reading app when you glance past the edge of the screen or tip your head.")

            if (!camera || !notify || !a11y || !calibrated) {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        Text("Set up", fontWeight = FontWeight.Bold, fontSize = 18.sp)
                        SetupRow("1. Let EyeTurn use the front camera", camera) { askCamera.launch(Manifest.permission.CAMERA) }
                        if (Build.VERSION.SDK_INT >= 33) {
                            SetupRow("2. Allow its notification (shows while it's on, with a Stop button)", notify) {
                                askNotify.launch(Manifest.permission.POST_NOTIFICATIONS)
                            }
                        }
                        SetupRow("3. Let EyeTurn scroll other apps: Accessibility > EyeTurn > On", a11y) {
                            startActivity(Intent(SysSettings.ACTION_ACCESSIBILITY_SETTINGS))
                        }
                        if (!a11y) {
                            Text(
                                "If Android says the setting is restricted: open App info below, tap the ⋮ menu at the top right, " +
                                    "choose \"Allow restricted settings\", then try step 3 again.",
                                fontSize = 13.sp,
                            )
                            OutlinedButton(onClick = {
                                startActivity(Intent(SysSettings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
                            }) { Text("Open App info") }
                        }
                        SetupRow("4. Calibrate (about 20 seconds)", calibrated, enabled = camera, action = "Start") { onCalibrate() }
                    }
                }
            }

            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(if (running) (if (paused) "Paused" else "On") else "Off",
                            fontWeight = FontWeight.Bold, fontSize = 20.sp, modifier = Modifier.weight(1f))
                        if (running) {
                            OutlinedButton(onClick = { Engine.togglePause() }) { Text(if (paused) "Resume" else "Pause") }
                            Spacer(Modifier.width(8.dp))
                        }
                        Button(
                            enabled = running || (camera && calibrated),
                            onClick = { if (running) TrackingService.stop(this@MainActivity) else TrackingService.start(this@MainActivity) },
                        ) { Text(if (running) "Stop" else "Start") }
                    }
                    if (running) {
                        Text(status.text)
                        LinearProgressIndicator(progress = { status.progress.coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth())
                        if (mode == Mode.EYES) Meter("Eyes", status.glance, "left", "right")
                        else {
                            Meter("Head turn", status.turn, "left", "right")
                            Meter("Head tip", status.tip, "up", "down")
                        }
                        Text(
                            "Now open your book or PDF. The dot at the top right shows EyeTurn sees you (green). Tap it to pause; hold it to come back here.",
                            fontSize = 13.sp,
                        )
                    } else if (camera && calibrated) {
                        Text("Press Start, then open your reading app.", fontSize = 13.sp)
                    }
                }
            }

            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Control with", fontWeight = FontWeight.Bold)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        FilterChip(mode == Mode.EYES, { mode = Mode.EYES; settings.mode = mode }, label = { Text("Eyes") })
                        FilterChip(mode == Mode.HEAD, { mode = Mode.HEAD; settings.mode = mode }, label = { Text("Head") })
                    }
                    Text(
                        if (mode == Mode.EYES)
                            "Glance past the right edge of the phone for the next page, the left edge for the previous one. " +
                                "Keep looking past the edge to keep scrolling."
                        else
                            "Tip your head down to scroll down, up to scroll up. Turn your head right and hold it for the next page, left for the previous one.",
                        fontSize = 13.sp,
                    )
                    if (mode == Mode.HEAD && running) {
                        OutlinedButton(onClick = { Engine.recenter() }) { Text("Re-learn how I hold my head") }
                    }

                    Text("Page turn", fontWeight = FontWeight.Bold)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        FilterChip(style == TurnStyle.SCROLL, { style = TurnStyle.SCROLL; settings.turnStyle = style },
                            label = { Text("Scroll (PDF, web)") })
                        FilterChip(style == TurnStyle.SWIPE, { style = TurnStyle.SWIPE; settings.turnStyle = style },
                            label = { Text("Swipe (books)") })
                    }

                    if (mode == Mode.EYES) {
                        Text("Glance sensitivity: ${pct(sens)}")
                        Slider(sens, { sens = it }, onValueChangeFinished = { settings.glanceSensitivity = sens })
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("Keep looking to keep scrolling", Modifier.weight(1f))
                            Switch(hold, { hold = it; settings.holdScroll = it })
                        }
                    }
                    Text("Scroll speed: ${"%.1f".format(speed)}x")
                    Slider(speed, { speed = it }, valueRange = 0.3f..2.5f, onValueChangeFinished = { settings.speed = speed })
                    if (style == TurnStyle.SCROLL) {
                        Text("Lines kept after a page turn: ${pct(overlap)} of the screen")
                        Slider(overlap, { overlap = it }, valueRange = 0f..0.3f, onValueChangeFinished = { settings.overlap = overlap })
                    }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Show the status dot", Modifier.weight(1f))
                        Switch(showDot, { showDot = it; settings.showDot = it })
                    }
                    if (calibrated) OutlinedButton(enabled = camera, onClick = onCalibrate) { Text("Calibrate again") }
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }

    @Composable
    private fun SetupRow(label: String, done: Boolean, enabled: Boolean = true, action: String = "Allow", onClick: () -> Unit) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text((if (done) "✓ " else "") + label, Modifier.weight(1f), fontSize = 15.sp)
            if (!done) {
                Spacer(Modifier.width(8.dp))
                Button(onClick = onClick, enabled = enabled) { Text(action) }
            }
        }
    }

    companion object {
        fun pct(v: Float) = "${(v * 100).toInt()}%"

        fun accessibilityOn(ctx: Context): Boolean {
            val enabled = SysSettings.Secure.getString(ctx.contentResolver, SysSettings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
                ?: return false
            val me = ComponentName(ctx, GestureService::class.java)
            return enabled.split(':').any { ComponentName.unflattenFromString(it) == me }
        }
    }
}

/** A bar from -1 to +1 showing a live value, so you can see EyeTurn follow you. */
@Composable
fun Meter(label: String, value: Float, neg: String, pos: String) {
    Column {
        Text("$label: ${if (abs(value) < 0.15f) "middle" else if (value > 0) pos else neg}", fontSize = 13.sp)
        Row(Modifier.fillMaxWidth()) {
            LinearProgressIndicator(
                progress = { (-value).coerceIn(0f, 1f) },
                // Left half fills from the middle outwards.
                modifier = Modifier.weight(1f).padding(end = 2.dp).graphicsLayer { scaleX = -1f },
                trackColor = MaterialTheme.colorScheme.surfaceVariant,
            )
            LinearProgressIndicator(
                progress = { value.coerceIn(0f, 1f) },
                modifier = Modifier.weight(1f).padding(start = 2.dp),
                trackColor = MaterialTheme.colorScheme.surfaceVariant,
            )
        }
    }
}
