package com.eyeturn.mobile

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Connects the camera (TrackingService) to the controller and the controller to
 * the gestures (GestureService). Lives as long as the app process.
 */
object Engine {
    private val _status = MutableStateFlow(Status(text = "Stopped"))
    val status: StateFlow<Status> = _status

    /** Latest raw frame; the calibration screen records from it. */
    private val _signals = MutableStateFlow(Signals(0.0, false))
    val signals: StateFlow<Signals> = _signals

    private val _running = MutableStateFlow(false)
    val running: StateFlow<Boolean> = _running

    private val _paused = MutableStateFlow(false)
    val paused: StateFlow<Boolean> = _paused

    /** True while the calibration screen is recording: nothing gets scrolled. */
    @Volatile var calibrating = false

    private var settings: Settings? = null
    private var controller: Controller? = null
    private var lastEvent: String? = null
    private val prefsListener = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> reload() }

    fun init(context: Context) {
        if (settings != null) return
        val s = Settings(context)
        settings = s
        controller = Controller(s.calibration, s.tuning())
        s.prefs.registerOnSharedPreferenceChangeListener(prefsListener)
    }

    private fun reload() {
        val s = settings ?: return
        val c = controller ?: return
        synchronized(this) {
            c.calibration = s.calibration
            c.tuning = s.tuning()
        }
        GestureService.instance?.reloadSettings()
    }

    fun setRunning(on: Boolean) {
        _running.value = on
        if (!on) {
            GestureService.instance?.setScroll(0f)
            _status.value = Status(text = "Stopped")
        } else {
            synchronized(this) { controller?.reset() }
        }
        GestureService.instance?.refreshDot()
    }

    fun togglePause() = setPaused(!_paused.value)

    fun setPaused(p: Boolean) {
        _paused.value = p
        if (p) GestureService.instance?.setScroll(0f)
        synchronized(this) { controller?.reset() }
        GestureService.instance?.refreshDot()
    }

    /** Re-learns the normal head position (head mode). */
    fun recenter() = synchronized(this) { controller?.head?.recenter() }

    /** Called for every camera frame, on the camera thread. */
    fun onSignals(s: Signals) {
        _signals.value = s
        val c = controller ?: return
        val gestures = GestureService.instance
        val st: Status
        synchronized(this) {
            st = when {
                _paused.value -> Status(s.face, "Paused (tap the dot to resume)")
                calibrating -> Status(s.face, "Calibrating")
                gestures == null -> {
                    c.update(s, NoActions)
                    Status(s.face, "Turn on EyeTurn in Accessibility settings")
                }
                else -> c.update(s, gestures)
            }
        }
        if (st.event != null) lastEvent = st.event
        val shown = if (gestures?.eyeTurnOnScreen() == true && !_paused.value && !calibrating)
            st.copy(text = st.text + " (switch to your reading app to use it)") else st
        _status.value = shown
        gestures?.onStatus(shown)
    }

    fun onFaceLost(t: Double) = onSignals(Signals(t, false))

    private object NoActions : Actions {
        override fun turnPage(next: Boolean) = false
        override fun setScroll(screensPerSec: Float) {}
    }
}
