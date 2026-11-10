package com.eyeturn.mobile

import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.min
import kotlin.math.pow

/** What the controller can do to the app being read. Implemented by GestureService. */
interface Actions {
    /** Turns one page. Returns false if it couldn't (busy, or EyeTurn itself is on screen). */
    fun turnPage(next: Boolean): Boolean

    /** Keeps scrolling at this speed, in screens per second (+ = forward/down, 0 = stop). */
    fun setScroll(screensPerSec: Float)
}

enum class Mode { EYES, HEAD }

/** User-adjustable settings (see Settings.kt for how they're stored). */
data class Tuning(
    val mode: Mode = Mode.EYES,
    val glanceSensitivity: Float = 0.5f, // 0 = needs a big glance, 1 = a small glance is enough
    val holdScroll: Boolean = true,
    val speed: Float = 1f,               // multiplies every scroll speed
    val headTurnPages: Boolean = true,
)

data class Status(
    val face: Boolean = false,
    val text: String = "Starting camera",
    val progress: Float = 0f,  // 0..1, how close the current gesture is to doing something
    val glance: Float = 0f,    // -1 left .. +1 right (eyes)
    val turn: Float = 0f,      // head turn, +1 = calibrated right turn
    val tip: Float = 0f,       // head tip, +1 = calibrated tip down
    val event: String? = null, // set on the frame something happened
)

/**
 * Glance to turn the page, ported from EyeTurn desktop (actions.py, PageTurner):
 * glance quickly past the right edge of the phone = next page, left = previous.
 * One glance turns one page; look back at the page before the next. Keep looking
 * past the edge for a second after the turn and it keeps scrolling (hold scroll),
 * slowly at first, faster over 3 seconds, until you look back.
 */
class GlanceControl {
    // Normalised units: 1.0 = the calibration glance.
    var glanceAt = 0.6f          // past this counts as glancing out
    val insideAt = 0.3f          // back within this = looking at the page again
    val holdMs = 180.0           // how long a glance must last
    val windowMs = 900.0         // a glance must start this soon after looking at the page (not a slow drift)
    val holdScrollAt = 0.45f     // holding needs a little less than a glance
    val holdScrollAfterMs = 1000.0
    val holdScrollGraceMs = 350.0
    val holdScrollSpeed = 0.35f  // screens per second at first...
    val holdScrollMaxSpeed = 1.0f // ...up to this
    val holdScrollRampS = 3.0
    val holdScrollAccel = 1.5f   // screens/s², so it starts softly (it stops at once)

    private var side = 0          // +1 right, -1 left, 0 inside
    private var sideT = 0.0
    private val insideT = doubleArrayOf(-1e9, -1e9) // last time inside, per side [left, right]
    private var ready = true
    private var hold = 0          // side of the turn being held, 0 = none
    private var holdTurnT = 0.0
    private var holdOutT = 0.0
    private var speed = 0f
    private var lastT: Double? = null

    fun reset() {
        side = 0; ready = true; hold = 0; speed = 0f; lastT = null
        insideT[0] = -1e9; insideT[1] = -1e9
    }

    /** g: glance from Calibration.glance (smoothed). Returns the status, and calls actions. */
    fun update(g: Float, t: Double, tuning: Tuning, actions: Actions): Status {
        val dt = lastT?.let { min(t - it, 0.1) } ?: 0.0
        lastT = t
        val scale = tuning.speed

        if (abs(g) < insideAt) {
            ready = true
            insideT[0] = t; insideT[1] = t
        }

        // Hold scroll: keep looking past the edge after a glance turn.
        if (hold != 0) {
            val stillOut = if (hold > 0) g > holdScrollAt else g < -holdScrollAt
            if (stillOut) holdOutT = t
            if (!tuning.holdScroll || t - holdOutT > holdScrollGraceMs / 1000) {
                hold = 0
                if (speed != 0f) { speed = 0f; actions.setScroll(0f) }
            } else if (t - holdTurnT >= holdScrollAfterMs / 1000) {
                val f = min((t - holdTurnT - holdScrollAfterMs / 1000) / holdScrollRampS, 1.0).toFloat()
                val target = holdScrollSpeed + f * (holdScrollMaxSpeed - holdScrollSpeed)
                speed = min(speed + holdScrollAccel * dt.toFloat(), target)
                actions.setScroll(hold * speed * scale)
                return Status(true, if (hold > 0) "Scrolling down" else "Scrolling up",
                    speed / holdScrollMaxSpeed, glance = g)
            } else {
                return Status(true, "Keep looking to scroll", ((t - holdTurnT) / (holdScrollAfterMs / 1000)).toFloat(), glance = g)
            }
        }

        val newSide = if (g > glanceAt) 1 else if (g < -glanceAt) -1 else 0
        if (newSide != side) { side = newSide; sideT = t }
        if (side != 0) {
            val idx = if (side > 0) 1 else 0
            val quick = sideT - insideT[idx] <= windowMs / 1000
            val held = t - sideT
            if (ready && quick && held >= holdMs / 1000) {
                ready = false
                val next = side > 0
                if (actions.turnPage(next)) {
                    hold = side; holdTurnT = t; holdOutT = t; speed = 0f
                    return Status(true, if (next) "Next page" else "Previous page", 1f, glance = g,
                        event = if (next) "next (glance)" else "previous (glance)")
                }
            }
            val word = if (side > 0) "right" else "left"
            return when {
                !ready -> Status(true, "Look back at the page", 1f, glance = g)
                !quick -> Status(true, "Glance $word: too slow, look back first", 0f, glance = g)
                else -> Status(true, "Glancing $word", min(held / (holdMs / 1000), 1.0).toFloat(), glance = g)
            }
        }
        return Status(true, "Reading", 0f, glance = g)
    }
}

/**
 * Head control, ported from EyeTurn desktop (head.py, HeadScroller): tip your
 * head down to scroll down, up to scroll up, faster the further you tip. Turn
 * your head right and hold it = next page, left = previous. Small movements
 * stay inside the dead zone, and the normal position slowly follows you there.
 */
class HeadControl {
    // Normalised units: 1.0 = the tip / turn done during calibration.
    val deadZone = 0.4f
    val fullTilt = 1.3f
    val exponent = 1.5f
    val maxSpeed = 1.2f      // screens per second, scrolling down
    val upMaxSpeed = 0.9f
    val accel = 2.0f         // screens/s²
    val turnAt = 0.75f
    val turnRearm = 0.35f
    val turnHoldMs = 400.0
    val smoothS = 0.1
    val learnS = 1.0
    val adaptS = 30.0

    private var neutralYaw: Float? = null
    private var neutralPitch = 0f
    private val learning = ArrayList<FloatArray>()
    private var learnT: Double? = null
    private var yaw = 0f
    private var pitch = 0f
    private var haveYaw = false
    private var speed = 0f
    private var lastT: Double? = null
    private var turnSide = 0
    private var turnT = 0.0
    private var turnArmed = true

    /** Learn the normal head position again (start, or the "Re-centre" button). */
    fun recenter() { neutralYaw = null; learning.clear(); learnT = null; reset() }

    fun reset() { speed = 0f; lastT = null; haveYaw = false; turnSide = 0; turnArmed = true }

    fun update(s: Signals, cal: Calibration, tuning: Tuning, actions: Actions): Status {
        val dt = lastT?.let { min(s.t - it, 0.1) } ?: 0.0
        lastT = s.t
        if (!haveYaw || dt == 0.0) {
            yaw = s.yaw; pitch = s.pitch; haveYaw = true
        } else {
            val k = (1 - exp(-dt / smoothS)).toFloat()
            yaw += (s.yaw - yaw) * k
            pitch += (s.pitch - pitch) * k
        }

        if (neutralYaw == null) {
            if (learnT == null) learnT = s.t
            learning.add(floatArrayOf(s.yaw, s.pitch))
            val elapsed = s.t - learnT!!
            if (elapsed < learnS || learning.size < 5) {
                return Status(true, "Hold still: learning how you hold your head", min(elapsed / learnS, 1.0).toFloat())
            }
            neutralYaw = median(learning.map { it[0] })
            neutralPitch = median(learning.map { it[1] })
            learning.clear(); learnT = null
        }
        val turn = cal.turn(yaw, neutralYaw!!)
        val tip = cal.tip(pitch, neutralPitch)
        var event: String? = null

        // Turning: hold the head turned past turnAt for turnHoldMs.
        val side = if (turn > turnAt) 1 else if (turn < -turnAt) -1 else 0
        if (abs(turn) < turnRearm) turnArmed = true
        if (side != turnSide) { turnSide = side; turnT = s.t }
        if (tuning.headTurnPages && side != 0 && turnArmed && s.t - turnT >= turnHoldMs / 1000) {
            turnArmed = false
            if (speed != 0f) { speed = 0f; actions.setScroll(0f) }
            if (actions.turnPage(side > 0)) event = if (side > 0) "next (head turn)" else "previous (head turn)"
        }

        // Scrolling: tip past the dead zone; not while the head is turned.
        var target = 0f
        if (abs(turn) < turnRearm) {
            val off = abs(tip) - deadZone
            if (off > 0) {
                val f = min(off / (fullTilt - deadZone), 1f).pow(exponent)
                target = if (tip > 0) maxSpeed * f else -upMaxSpeed * f
            }
        }
        val before = speed
        if (target == 0f || target * speed < 0) {
            speed = 0f // stops at once when you look back up
        } else {
            val step = accel * dt.toFloat()
            speed += (target - speed).coerceIn(-step, step)
        }
        if (speed != 0f || before != 0f) actions.setScroll(speed * tuning.speed)

        // Inside the dead zone the normal position slowly follows how you sit.
        if (abs(tip) < deadZone * 0.7f && abs(turn) < turnRearm * 0.7f && dt > 0) {
            val k = (dt / adaptS).toFloat()
            neutralYaw = neutralYaw!! + (yaw - neutralYaw!!) * k
            neutralPitch += (pitch - neutralPitch) * k
        }

        val text: String
        val progress: Float
        when {
            event != null -> { text = if (side > 0) "Next page" else "Previous page"; progress = 1f }
            speed != 0f -> { text = if (speed > 0) "Scrolling down" else "Scrolling up"; progress = abs(speed) / maxSpeed }
            side != 0 -> {
                text = "Head turned ${if (side > 0) "right" else "left"}"
                progress = if (turnArmed) min((s.t - turnT) / (turnHoldMs / 1000), 1.0).toFloat() else 1f
            }
            else -> { text = "Tip to scroll, turn to change page"; progress = 0f }
        }
        return Status(true, text, progress, turn = turn, tip = tip, event = event)
    }

    private fun median(v: List<Float>): Float {
        val s = v.sorted()
        return if (s.size % 2 == 1) s[s.size / 2] else (s[s.size / 2 - 1] + s[s.size / 2]) / 2
    }
}

/** Runs the chosen mode on each frame. Not thread-safe: call from one thread. */
class Controller(var calibration: Calibration?, var tuning: Tuning) {
    val glance = GlanceControl()
    val head = HeadControl()
    private var g = 0f
    private var gT: Double? = null
    private var lastBlink = -1e9
    private var lastMode: Mode? = null
    private var scrolling = false
    private val glanceSmoothS = 0.06

    fun reset() {
        glance.reset(); head.recenter(); gT = null; lastMode = null
    }

    fun update(s: Signals, actions: Actions): Status {
        val cal = calibration ?: return stop(actions, Status(s.face, "Calibrate first"))
        if (tuning.mode != lastMode) {
            lastMode = tuning.mode
            glance.reset(); head.recenter()
            stopScroll(actions)
        }
        if (!s.face) {
            glance.reset(); head.reset(); gT = null
            return stop(actions, Status(false, "No face in view"))
        }
        val tracking = ScrollTracker(actions)
        val st = when (tuning.mode) {
            Mode.EYES -> {
                glance.glanceAt = 0.85f - 0.45f * tuning.glanceSensitivity
                // Eyes look odd while blinking: keep the last value through a blink.
                if (s.blink > 0.45f) lastBlink = s.t
                if (s.t - lastBlink > 0.12) {
                    val raw = cal.glance(s.eyeX)
                    val prev = gT
                    g = if (prev == null) raw else g + (raw - g) * (1 - exp(-min(s.t - prev, 0.1) / glanceSmoothS)).toFloat()
                    gT = s.t
                }
                glance.update(g, s.t, tuning, tracking)
            }
            Mode.HEAD -> head.update(s, cal, tuning, tracking)
        }
        scrolling = tracking.speed?.let { it != 0f } ?: scrolling
        return st
    }

    private fun stop(actions: Actions, st: Status): Status {
        stopScroll(actions)
        return st
    }

    private fun stopScroll(actions: Actions) {
        if (scrolling) { actions.setScroll(0f); scrolling = false }
    }

    /** Passes actions through and remembers the last scroll speed set. */
    private class ScrollTracker(val inner: Actions) : Actions {
        var speed: Float? = null
        override fun turnPage(next: Boolean) = inner.turnPage(next)
        override fun setScroll(screensPerSec: Float) { speed = screensPerSec; inner.setScroll(screensPerSec) }
    }
}
