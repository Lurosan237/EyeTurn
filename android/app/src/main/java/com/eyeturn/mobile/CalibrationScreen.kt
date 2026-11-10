package com.eyeturn.mobile

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.VibrationEffect
import android.os.Vibrator
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

private class Step(val text: String, val arrow: String = "")

private val STEPS = listOf(
    Step("Look at the dot in the middle of the screen."),
    Step("Keep your head still and glance past the RIGHT edge of the phone. Look back when you hear the beep.", "→"),
    Step("Look at the middle again."),
    Step("Keep your head still and glance past the LEFT edge of the phone. Look back when you hear the beep.", "←"),
    Step("Look at the middle and tip your head DOWN a little, like a small nod. Hold it until the beep.", "↓"),
    Step("Keep looking at the screen and turn your head a little to the RIGHT. Hold it until the beep.", "↻"),
)
private const val MOVE_MS = 1500L   // time to get into position
private const val RECORD_MS = 1500L // then this long is measured

/**
 * About 20 seconds: middle, glance right, middle, glance left, tip head down,
 * turn head right. Learns which way is which for this phone and face, and how
 * big a deliberate glance or head move is, so normal reading stays below it.
 */
@Composable
fun CalibrationScreen(settings: Settings, onDone: () -> Unit) {
    val ctx = LocalContext.current
    var phase by remember { mutableStateOf("intro") } // intro, run, done, failed
    var stepIdx by remember { mutableIntStateOf(0) }
    var stepProgress by remember { mutableFloatStateOf(0f) }
    var message by remember { mutableStateOf("") }
    var attempt by remember { mutableIntStateOf(0) }
    val signals by Engine.signals.collectAsState()

    DisposableEffect(Unit) { onDispose { Engine.calibrating = false } }

    Column(
        Modifier.fillMaxSize().safeDrawingPadding().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Calibrate", fontSize = 26.sp, fontWeight = FontWeight.Bold)
        when (phase) {
            "intro" -> {
                Text("Hold the phone the way you do when reading, with your face in front of the front camera. " +
                    "This takes about 20 seconds. You'll hear a beep at the end of each step.", fontSize = 17.sp)
                Text(if (signals.face) "✓ I can see your face" else "Looking for your face…",
                    fontSize = 17.sp, fontWeight = FontWeight.Bold)
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Button(enabled = signals.face, onClick = { phase = "run"; attempt++ }) { Text("Begin") }
                    OutlinedButton(onClick = onDone) { Text("Cancel") }
                }
            }
            "run" -> {
                LaunchedEffect(attempt) {
                    Engine.calibrating = true
                    val beeper = runCatching { ToneGenerator(AudioManager.STREAM_NOTIFICATION, 80) }.getOrNull()
                    val results = ArrayList<FloatArray>() // per step: eyeX, yaw, pitch medians
                    var failed: String? = null
                    for (i in STEPS.indices) {
                        stepIdx = i
                        var got: FloatArray? = null
                        for (tryNo in 0 until 3) {
                            got = record { stepProgress = it }
                            if (got != null) break
                        }
                        beeper?.startTone(ToneGenerator.TONE_PROP_BEEP, 150)
                        buzz(ctx)
                        if (got == null) { failed = "I lost sight of your face. Keep your face in front of the camera and try again."; break }
                        results.add(got)
                    }
                    beeper?.release()
                    Engine.calibrating = false
                    if (failed == null) {
                        val c = Calibration(
                            eyeX0 = (results[0][0] + results[2][0]) / 2,
                            eyeRight = results[1][0],
                            eyeLeft = results[3][0],
                            yaw0 = (results[0][1] + results[2][1]) / 2,
                            yawRight = results[5][1],
                            pitch0 = (results[0][2] + results[2][2]) / 2,
                            pitchDown = results[4][2],
                        )
                        failed = c.problem()
                        if (failed == null) settings.calibration = c
                    }
                    message = failed ?: ""
                    phase = if (failed == null) "done" else "failed"
                }
                val step = STEPS[stepIdx]
                Text("Step ${stepIdx + 1} of ${STEPS.size}", fontSize = 15.sp)
                Text(step.text, fontSize = 22.sp, fontWeight = FontWeight.Bold)
                LinearProgressIndicator(progress = { stepProgress }, modifier = Modifier.fillMaxWidth())
                if (!signals.face) Text("I can't see your face", fontSize = 16.sp)
                Box(Modifier.fillMaxWidth().weight(1f)) {
                    val align = when (step.arrow) {
                        "→" -> Alignment.CenterEnd
                        "←" -> Alignment.CenterStart
                        else -> Alignment.Center
                    }
                    Text(if (step.arrow == "" ) "●" else step.arrow, fontSize = 64.sp, modifier = Modifier.align(align))
                }
            }
            "done" -> {
                Text("Done! EyeTurn is on.", fontSize = 22.sp, fontWeight = FontWeight.Bold)
                Text("Open your book or PDF and glance past the right edge of the phone to turn the page.", fontSize = 17.sp)
                Button(onClick = onDone) { Text("OK") }
            }
            else -> {
                Text("That didn't work", fontSize = 22.sp, fontWeight = FontWeight.Bold)
                Text(message, fontSize = 17.sp, textAlign = TextAlign.Start)
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Button(onClick = { phase = "run"; stepIdx = 0; attempt++ }) { Text("Try again") }
                    OutlinedButton(onClick = onDone) { Text("Back") }
                }
            }
        }
    }
}

/** Waits MOVE_MS, then returns the medians of (eyeX, yaw, pitch) over RECORD_MS, or null if the face was missing. */
private suspend fun record(progress: (Float) -> Unit): FloatArray? {
    val total = MOVE_MS + RECORD_MS
    val start = System.currentTimeMillis()
    val eye = ArrayList<Float>()
    val yaw = ArrayList<Float>()
    val pitch = ArrayList<Float>()
    var lastT = -1.0
    while (true) {
        val el = System.currentTimeMillis() - start
        if (el >= total) break
        progress(el.toFloat() / total)
        val s = Engine.signals.value
        if (el >= MOVE_MS && s.t != lastT && s.face && s.blink < 0.4f) {
            eye.add(s.eyeX); yaw.add(s.yaw); pitch.add(s.pitch)
        }
        lastT = s.t
        delay(25)
    }
    progress(1f)
    if (eye.size < 5) return null
    return floatArrayOf(median(eye), median(yaw), median(pitch))
}

private fun median(v: List<Float>): Float {
    val s = v.sorted()
    return if (s.size % 2 == 1) s[s.size / 2] else (s[s.size / 2 - 1] + s[s.size / 2]) / 2
}

private fun buzz(ctx: Context) {
    runCatching {
        val v = ctx.getSystemService(Vibrator::class.java)
        v?.vibrate(VibrationEffect.createOneShot(60, VibrationEffect.DEFAULT_AMPLITUDE))
    }
}
