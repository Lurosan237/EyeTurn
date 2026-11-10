package com.eyeturn.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.sin

/** Simulated faces: checks the glance and head rules turn pages only when meant to. */
class ControllerTest {
    // Glancing right makes eyeX go negative here, to check calibration learns the sign.
    private val cal = Calibration(eyeX0 = 0f, eyeRight = -0.5f, eyeLeft = 0.45f,
        yaw0 = 0f, yawRight = -15f, pitch0 = 2f, pitchDown = 12f)

    private class Recorder : Actions {
        val turns = mutableListOf<Pair<Double, Boolean>>()
        val scrolls = mutableListOf<Pair<Double, Float>>()
        var now = 0.0
        var speed = 0f
        override fun turnPage(next: Boolean): Boolean { turns.add(now to next); return true }
        override fun setScroll(screensPerSec: Float) { speed = screensPerSec; scrolls.add(now to screensPerSec) }
    }

    /** Runs 20 fps frames from t0 to t1; eye(t) gives the glance (-1 left .. +1 right). */
    private fun runEyes(c: Controller, r: Recorder, t0: Double, t1: Double, blink: (Double) -> Float = { 0f }, eye: (Double) -> Float) {
        var t = t0
        while (t < t1) {
            r.now = t
            val g = eye(t)
            val raw = if (g >= 0) g * cal.eyeRight else -g * cal.eyeLeft
            c.update(Signals(t, true, eyeX = raw, blink = blink(t)), r)
            t += 0.05
        }
    }

    private fun eyes() = Controller(cal, Tuning(mode = Mode.EYES))

    @Test fun readingNeverTurns() {
        val c = eyes(); val r = Recorder()
        // Lines: eyes sweep left to right (-0.28..0.28) over 2.5 s with small saccade wobble, then jump back.
        runEyes(c, r, 0.0, 60.0) { t ->
            val p = (t % 2.5) / 2.5
            (-0.28 + 0.56 * p + 0.03 * sin(t * 40)).toFloat()
        }
        assertEquals(0, r.turns.size)
        assertTrue(r.scrolls.all { it.second == 0f })
    }

    @Test fun quickGlanceRightTurnsOnePage() {
        val c = eyes(); val r = Recorder()
        runEyes(c, r, 0.0, 6.0) { t -> if (t in 2.0..2.5) 1.0f else 0f }
        assertEquals(1, r.turns.size)
        assertTrue(r.turns[0].second)
        assertTrue("turned within 0.35 s", r.turns[0].first < 2.35)
        assertTrue(r.scrolls.none { it.second != 0f })
    }

    @Test fun glanceLeftTurnsBack() {
        val c = eyes(); val r = Recorder()
        runEyes(c, r, 0.0, 4.0) { t -> if (t in 1.0..1.4) -1.0f else 0f }
        assertEquals(listOf(false), r.turns.map { it.second })
    }

    @Test fun twoGlancesTurnTwoPages() {
        val c = eyes(); val r = Recorder()
        runEyes(c, r, 0.0, 6.0) { t -> if (t in 1.0..1.4 || t in 3.0..3.4) 1.0f else 0f }
        assertEquals(2, r.turns.size)
    }

    @Test fun holdingTheGlanceScrollsThenStops() {
        val c = eyes(); val r = Recorder()
        runEyes(c, r, 0.0, 9.0) { t -> if (t in 1.0..6.0) 1.0f else 0f }
        assertEquals(1, r.turns.size)
        val moving = r.scrolls.filter { it.second > 0f }
        assertTrue("scrolls after about a second", moving.first().first in 2.0..2.5)
        assertTrue("speeds up", moving.last().second > 0.9f)
        assertEquals(0f, r.speed)
        assertTrue("stops soon after looking back", r.scrolls.last().first < 6.6)
    }

    @Test fun slowDriftDoesNotTurn() {
        val c = eyes(); val r = Recorder()
        // Eyes creep right over 4 seconds and stay there.
        runEyes(c, r, 0.0, 8.0) { t -> (t / 4.0).coerceAtMost(1.0).toFloat() }
        assertEquals(0, r.turns.size)
    }

    @Test fun blinksDoNotTurn() {
        val c = eyes(); val r = Recorder()
        // During a blink the eye values jump about; the blink score is high then.
        val blinking = { t: Double -> (t % 3.0) in 1.0..1.25 }
        runEyes(c, r, 0.0, 30.0, blink = { if (blinking(it)) 0.9f else 0f }) { t -> if (blinking(t)) 1.0f else 0f }
        assertEquals(0, r.turns.size)
    }

    @Test fun lowSensitivityNeedsABiggerGlance() {
        val c = Controller(cal, Tuning(mode = Mode.EYES, glanceSensitivity = 0f)); val r = Recorder()
        runEyes(c, r, 0.0, 4.0) { t -> if (t in 1.0..1.5) 0.7f else 0f }
        assertEquals(0, r.turns.size)
        val c2 = Controller(cal, Tuning(mode = Mode.EYES, glanceSensitivity = 1f)); val r2 = Recorder()
        runEyes(c2, r2, 0.0, 4.0) { t -> if (t in 1.0..1.5) 0.7f else 0f }
        assertEquals(1, r2.turns.size)
    }

    @Test fun losingTheFaceStopsScrolling() {
        val c = eyes(); val r = Recorder()
        runEyes(c, r, 0.0, 4.0) { 1.0f.takeIf { it > 0 }!!.let { v -> if (r.now >= 1.0) v else 0f } }
        assertTrue(r.speed > 0f)
        c.update(Signals(4.05, false), r)
        assertEquals(0f, r.speed)
    }

    private fun runHead(c: Controller, r: Recorder, t0: Double, t1: Double, pose: (Double) -> Pair<Float, Float>) {
        var t = t0
        while (t < t1) {
            r.now = t
            val (turn, tip) = pose(t) // in calibration units
            c.update(Signals(t, true, yaw = turn * cal.yawRight, pitch = cal.pitch0 + tip * (cal.pitchDown - cal.pitch0)), r)
            t += 0.05
        }
    }

    @Test fun headTipScrollsAndSmallMovesDoNot() {
        val c = Controller(cal, Tuning(mode = Mode.HEAD)); val r = Recorder()
        runHead(c, r, 0.0, 5.0) { t -> 0.1f * sin(t * 3).toFloat() to 0.2f * sin(t * 2).toFloat() }
        assertTrue(r.scrolls.none { it.second != 0f })
        assertEquals(0, r.turns.size)
        runHead(c, r, 5.0, 8.0) { 0f to 1.0f }
        assertTrue("scrolls down", r.speed > 0.3f)
        runHead(c, r, 8.0, 9.0) { 0f to 0f }
        assertEquals(0f, r.speed)
        runHead(c, r, 9.0, 11.0) { 0f to -1.0f }
        assertTrue("scrolls up", r.speed < -0.2f)
    }

    @Test fun headTurnHeldTurnsOnePage() {
        val c = Controller(cal, Tuning(mode = Mode.HEAD)); val r = Recorder()
        runHead(c, r, 0.0, 2.0) { 0f to 0f }
        runHead(c, r, 2.0, 5.0) { 1.0f to 0f }
        runHead(c, r, 5.0, 6.0) { 0f to 0f }
        runHead(c, r, 6.0, 8.0) { -1.0f to 0f }
        assertEquals(listOf(true, false), r.turns.map { it.second })
    }

    @Test fun calibrationChecks() {
        assertEquals(null, cal.problem())
        assertTrue(cal.copy(eyeLeft = -0.4f).problem() != null)
        assertTrue(cal.copy(eyeRight = 0.01f).problem() != null)
    }
}
