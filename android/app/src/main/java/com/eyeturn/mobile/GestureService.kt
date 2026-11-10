package com.eyeturn.mobile

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.accessibilityservice.GestureDescription.StrokeDescription
import android.content.Intent
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.RectF
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import kotlin.math.abs
import kotlin.math.min

/**
 * Turns pages and scrolls in whatever app is on screen, by sending touch
 * gestures, the way a finger would. This is the phone version of EyeTurn
 * desktop's winput.py. It needs to be switched on in Settings > Accessibility.
 *
 * A page turn is a drag of one screen minus the overlap, then a short pause
 * before lifting the finger, so the page stops exactly there instead of
 * flinging on. Continuous scrolling is one long drag made of short pieces whose
 * speed can change at any time.
 */
class GestureService : AccessibilityService(), Actions {
    private val main = Handler(Looper.getMainLooper())
    private lateinit var settings: Settings

    @Volatile private var busy = false          // a page turn or a drag is in progress
    @Volatile private var targetSpeed = 0f      // screens per second, + = forward
    @Volatile private var dragQueued = false
    @Volatile private var pauseUntil = 0L       // after you touch the screen yourself
    @Volatile private var ownOnScreen = false
    @Volatile private var turnStyle = TurnStyle.SCROLL
    @Volatile private var overlap = 0.1f

    private var touchSlop = 16

    // Continuous drag state (main thread only)
    private var dragStroke: StrokeDescription? = null
    private var dragX = 0f
    private var dragY = 0f
    private var dragArea = Rect()
    private var dragDir = 0
    private var jitter = 1f
    private var fraction = 0f

    private var dot: DotView? = null
    private var dotColor = 0

    override fun onServiceConnected() {
        super.onServiceConnected()
        settings = Settings(this)
        Engine.init(this)
        touchSlop = ViewConfiguration.get(this).scaledTouchSlop
        reloadSettings()
        instance = this
        main.post(watchForeground)
        refreshDot()
    }

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        main.removeCallbacksAndMessages(null)
        removeDot()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        removeDot()
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    fun reloadSettings() {
        turnStyle = settings.turnStyle
        overlap = settings.overlap
        main.post { refreshDot() }
    }

    fun eyeTurnOnScreen() = ownOnScreen

    /** Checks twice a second which app is on screen, so EyeTurn never scrolls its own screens. */
    private val watchForeground = object : Runnable {
        override fun run() {
            val pkg = runCatching { rootInActiveWindow?.packageName?.toString() }.getOrNull()
            if (pkg != null) ownOnScreen = pkg == packageName
            main.postDelayed(this, 500)
        }
    }

    // ---- Actions (called from the camera thread) ----

    override fun turnPage(next: Boolean): Boolean {
        if (busy || ownOnScreen || SystemClock.uptimeMillis() < pauseUntil) return false
        busy = true
        main.post { doTurn(next) }
        return true
    }

    override fun setScroll(screensPerSec: Float) {
        targetSpeed = screensPerSec
        if (screensPerSec != 0f && !busy && !dragQueued && !ownOnScreen && SystemClock.uptimeMillis() >= pauseUntil) {
            dragQueued = true
            main.post { dragQueued = false; startDrag() }
        }
    }

    // ---- Page turns ----

    private fun doTurn(next: Boolean) {
        val area = scrollArea()
        flashDot()
        if (turnStyle == TurnStyle.SWIPE) {
            // Book-style apps: a quick sideways swipe. Finger moves left for the next page.
            val y = area.exactCenterY()
            val x0 = area.left + area.width() * if (next) 0.85f else 0.15f
            val x1 = area.left + area.width() * if (next) 0.15f else 0.85f
            val p = Path().apply { moveTo(x0, y); lineTo(x1, y) }
            dispatch(StrokeDescription(p, 0, 180, false)) { busy = false }
            return
        }
        // Scrolling apps: drag exactly one screen minus the overlap, then hold still before lifting.
        val h = area.height().toFloat()
        val dist = min(h * (1 - overlap) + touchSlop, h * 0.96f)
        val x = area.exactCenterX()
        val cy = area.exactCenterY()
        val y0 = if (next) cy + dist / 2 else cy - dist / 2
        val y1 = if (next) cy - dist / 2 else cy + dist / 2
        val move = StrokeDescription(Path().apply { moveTo(x, y0); lineTo(x, y1) }, 0, 300, true)
        dispatch(move) { ok ->
            if (!ok) { busy = false; return@dispatch }
            val hold = move.continueStroke(Path().apply { moveTo(x, y1); lineTo(x + 1f, y1) }, 0, 150, false)
            dispatch(hold) { busy = false }
        }
    }

    // ---- Continuous scrolling ----

    private fun startDrag() {
        if (busy || targetSpeed == 0f || ownOnScreen || SystemClock.uptimeMillis() < pauseUntil) return
        busy = true
        dragArea = scrollArea()
        dragDir = if (targetSpeed > 0) 1 else -1
        dragX = dragArea.exactCenterX()
        val h = dragArea.height()
        // Forward = finger moves up, so start low.
        dragY = dragArea.top + h * if (dragDir > 0) 0.88f else 0.12f
        dragStroke = null
        fraction = 0f
        dragStep()
    }

    private fun dragStep() {
        val h = dragArea.height().toFloat()
        val v = targetSpeed
        val top = dragArea.top + h * 0.1f
        val bottom = dragArea.bottom - h * 0.1f
        val sameWay = v != 0f && (v > 0) == (dragDir > 0)
        val roomLeft = if (dragDir > 0) dragY > top else dragY < bottom
        val prev = dragStroke
        if (!sameWay || !roomLeft || SystemClock.uptimeMillis() < pauseUntil) {
            // Stop: hold still a moment so it doesn't fling, then lift. Start again if still scrolling.
            if (prev == null) { busy = false; return }
            val hold = prev.continueStroke(Path().apply { moveTo(dragX, dragY); lineTo(dragX + 1f, dragY) }, 0, 100, false)
            dispatch(hold) {
                busy = false
                dragStroke = null
                if (targetSpeed != 0f) main.postDelayed({ startDrag() }, 30)
            }
            return
        }
        var dy = abs(v) * h * SEGMENT_MS / 1000f + fraction
        // The first piece must get past the touch slop before the app starts scrolling.
        if (prev == null) dy += touchSlop
        val whole = dy.toInt().toFloat()
        fraction = dy - whole
        val y1 = if (dragDir > 0) maxOf(dragY - whole, top - 1f) else minOf(dragY + whole, bottom + 1f)
        jitter = -jitter
        // A piece must have some length; when hardly moving, shift a pixel sideways.
        val x1 = if (whole < 1f) dragX + jitter else dragX
        val path = Path().apply { moveTo(dragX, dragY); lineTo(x1, y1) }
        val stroke = prev?.continueStroke(path, 0, SEGMENT_MS, true) ?: StrokeDescription(path, 0, SEGMENT_MS, true)
        dragStroke = stroke
        dragX = x1
        dragY = y1
        dispatch(stroke) { ok ->
            if (ok) dragStep() else { busy = false; dragStroke = null }
        }
    }

    // ---- Helpers ----

    private fun dispatch(stroke: StrokeDescription, done: (Boolean) -> Unit) {
        val g = GestureDescription.Builder().addStroke(stroke).build()
        val sent = try {
            dispatchGesture(g, object : GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) = done(true)
                override fun onCancelled(gestureDescription: GestureDescription?) {
                    // Usually means you touched the screen yourself: give way for a moment.
                    pauseUntil = SystemClock.uptimeMillis() + 1500
                    done(false)
                }
            }, main)
        } catch (e: Exception) {
            Log.w("EyeTurn", "gesture", e)
            false
        }
        if (!sent) done(false)
    }

    /** The biggest scrollable area on screen (the page), or most of the screen if none is found. */
    private fun scrollArea(): Rect {
        val screen = Rect()
        val root = runCatching { rootInActiveWindow }.getOrNull()
        root?.getBoundsInScreen(screen)
        if (screen.isEmpty) {
            val m = resources.displayMetrics
            screen.set(0, 0, m.widthPixels, m.heightPixels)
        }
        var best: Rect? = null
        var bestArea = 0
        if (root != null) {
            val queue = ArrayDeque<AccessibilityNodeInfo>()
            queue.add(root)
            var seen = 0
            while (queue.isNotEmpty() && seen < 400) {
                val n = queue.removeFirst()
                seen++
                if (n.isScrollable && n.isVisibleToUser) {
                    val r = Rect()
                    n.getBoundsInScreen(r)
                    val a = r.width() * r.height()
                    if (a > bestArea) { bestArea = a; best = r }
                }
                for (i in 0 until n.childCount) n.getChild(i)?.let { queue.add(it) }
            }
        }
        val b = best
        if (b != null && bestArea >= screen.width() * screen.height() / 4) return b
        // No scrollable view found (e.g. a custom PDF view): use the screen without its top and bottom bars.
        val inset = (screen.height() * 0.08f).toInt()
        return Rect(screen.left, screen.top + inset, screen.right, screen.bottom - inset)
    }

    // ---- Status dot: green = sees you, grey = no face, orange = paused. Tap to pause. ----

    fun onStatus(st: Status) {
        val c = when {
            Engine.paused.value -> ORANGE
            !st.face -> GREY
            st.progress > 0f -> BLUE
            else -> GREEN
        }
        if (c != dotColor) {
            dotColor = c
            main.post { dot?.setColor(c) }
        }
    }

    fun refreshDot() {
        if (Looper.myLooper() != Looper.getMainLooper()) { main.post { refreshDot() }; return }
        val want = Engine.running.value && settings.showDot
        if (want && dot == null) addDot() else if (!want && dot != null) removeDot()
        dot?.setColor(if (Engine.paused.value) ORANGE else GREY)
    }

    private fun flashDot() {
        val d = dot ?: return
        d.animate().scaleX(1.6f).scaleY(1.6f).setDuration(120).withEndAction {
            d.animate().scaleX(1f).scaleY(1f).setDuration(200).start()
        }.start()
    }

    private fun addDot() {
        val wm = getSystemService(WindowManager::class.java)
        val size = (28 * resources.displayMetrics.density).toInt()
        val params = WindowManager.LayoutParams(
            size, size,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.END
            x = (6 * resources.displayMetrics.density).toInt()
            y = (90 * resources.displayMetrics.density).toInt()
        }
        val v = DotView(this)
        v.setOnClickListener { Engine.togglePause() }
        v.setOnLongClickListener {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            true
        }
        try {
            wm.addView(v, params)
            dot = v
        } catch (e: Exception) {
            Log.w("EyeTurn", "dot", e)
        }
    }

    private fun removeDot() {
        val d = dot ?: return
        runCatching { getSystemService(WindowManager::class.java).removeView(d) }
        dot = null
    }

    private class DotView(ctx: android.content.Context) : View(ctx) {
        private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = GREY }
        private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.WHITE; style = Paint.Style.STROKE; strokeWidth = 2 * ctx.resources.displayMetrics.density
        }
        init { alpha = 0.85f; contentDescription = "EyeTurn: tap to pause or resume" }
        fun setColor(c: Int) { fill.color = c; invalidate() }
        override fun onDraw(canvas: Canvas) {
            val r = RectF(0f, 0f, width.toFloat(), height.toFloat())
            r.inset(ring.strokeWidth, ring.strokeWidth)
            canvas.drawOval(r, fill)
            canvas.drawOval(r, ring)
        }
    }

    companion object {
        @Volatile var instance: GestureService? = null
        private const val SEGMENT_MS = 40L
        private val GREEN = Color.rgb(46, 160, 67)
        private val BLUE = Color.rgb(31, 111, 235)
        private val GREY = Color.rgb(140, 140, 140)
        private val ORANGE = Color.rgb(230, 140, 20)
    }
}
