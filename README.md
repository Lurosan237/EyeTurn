# EyeTurn

Hands-free reading with a normal webcam. EyeTurn watches *how* you read, not
just where you look. It **turns the page when you finish it**, or
**scrolls at your reading pace**, and pauses when you stop reading. It sends
ordinary key presses and mouse-wheel events, so it works with any viewer:
Acrobat, SumatraPDF, Edge/Chrome PDF viewer, Word, Kindle for PC, and so on.

## How it works

1. **Tracking** (`eyeturn/tracker.py`): MediaPipe Face Landmarker finds 478
   face points in each webcam frame, including both irises. EyeTurn uses where
   each iris sits inside the eye, how open the eyes are, head rotation and
   position, and MediaPipe's own eye-direction estimates.
2. **Calibration** (`eyeturn/calibration.py`): you look at 13 dots, then keep
   looking at 3 more while gently moving your head, so the model learns to
   tell head movement apart from eye movement. EyeTurn tries several
   models (`eyeturn/gaze_model.py`) and keeps the one that best predicts dots
   it wasn't trained on. That held-out error is the "Expected accuracy" it
   shows you. The tracker also re-measures the iris centre from the
   full-resolution camera image. The model selection decides whether that
   helps for your eyes and camera.
3. **Learning from your clicks**: people look at what they click, so every
   mouse click becomes another calibration point. Recent clicks count most.
   Accuracy improves as you use your computer normally and adapts when you
   change how you sit. The status panel shows how many clicks it has learned.
4. **Smoothing** (`eyeturn/filters.py`): a short median filter removes
   one-frame spikes, then a One Euro filter keeps the gaze point steady.
5. **Reading detection** (`eyeturn/reading.py`): at the end of each line your
   eyes jump back to the left margin. That jump is large compared with
   webcam error, so EyeTurn can detect it even when the gaze dot is a few
   centimetres off. Several jumps that start from the same right edge, land
   on the same left margin and step down line by line mean you are reading.
   Scanning a picture doesn't produce that pattern.
6. **L2CS-Net** (`eyeturn/gaze_net.py`, optional): a neural network that
   estimates gaze direction from the whole face image, as a second opinion
   next to the eye landmarks. It runs in the background on the latest frame.
   Calibration also tries models that use it, and only keeps one if it
   improves the expected accuracy (the model name then includes "l2cs").
   Turn it off with `"gaze_net": {"enabled": false}`. The code and model are
   MIT licensed, but the model was trained on the Gaze360 dataset, which is
   for research use only. Check that before selling anything that ships it.
7. **Actions** (`eyeturn/actions.py`):
   - *Page mode*: once you've read the last lines of a page, the jump your eyes
     make back up to the top-left turns it. You can also hold your gaze in the
     bottom-right corner (next page) or top-left corner (previous page).
   - *Scroll mode*: while you're reading, the text drifts up whenever your eyes
     drop below the middle of the screen, so the current line settles at a
     comfortable height. When you stop reading (to look at a figure, think, or
     look away) scrolling stops. To scroll on purpose, hold your gaze at the
     bottom or top edge.

## Setup (Windows)

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The model files are not in this repository, so download them into a `models`
folder. The face model is required:

```bash
mkdir models
```

```bash
curl -L -o models\face_landmarker.task https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task
```

The hand model is needed for hand gestures:

```bash
curl -L -o models\hand_landmarker.task https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task
```

The L2CS-Net gaze model (about 48 MB) is optional. Read the note under
[Third-party models](#third-party-models) first:

```bash
curl -L -o models\l2cs_gaze360.tflite https://huggingface.co/litert-community/L2CS-Gaze360-LiteRT/resolve/main/gaze_fp16.tflite
```

Without it EyeTurn still works, using the eye landmarks only.

## Usage

Double-click **EyeTurn** on the desktop or in the Start menu. It starts
without a console window; messages go to `logs/eyeturn.log`. To create those
shortcuts (again), run:

```bash
powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
```

Add `-Startup` to also start EyeTurn with Windows, `-NoStartup` to undo that.
From a console you can still start it with `run.bat` and the options below:

```bash
run.bat --show-gaze
```

The first time you run it, calibration opens. Press Space, then follow the dots.
The app starts in **page mode**. Press **F10** to switch to scroll mode, or
start in it with `--mode scroll`. Then:

1. Click into your document so it has keyboard focus. The mouse pointer can
   be anywhere: in scroll mode EyeTurn sends each scroll from the spot you're
   looking at in the focused window (or its middle), then puts the pointer
   back. To turn that off, set `"scroll": {"follow_focus": false}`; scrolling
   then goes to whatever window is under the pointer.
2. Read.

| Hotkey | Action |
| --- | --- |
| F8 | Pause / resume |
| F10 | Switch between page and scroll mode |
| F9 | Recalibrate |
| F7 | Fix drift: look at the mouse pointer, then press F7 |
| F6 | Touch-up: 5 dots to re-fit the calibration to how you sit now |
| Ctrl+Alt+Q | Quit |

Options: `--mode scroll`, `--camera 1`, `--show-gaze` (shows a dot where you're
looking, which helps when tuning), `--recalibrate`.

## Tuning

Copy any part of the defaults in `eyeturn/config.py` into a `config.json` file
in the project folder. Only the keys you set are overridden. For example:

```json
{
  "mode": "scroll",
  "page": { "next_key": "page_down", "prev_key": "page_up", "next_dwell_ms": 600 },
  "scroll": { "down_start": 0.55, "max_wheel_per_sec": 600 }
}
```

- Zones are `[x0, y0, x1, y1]`, where `(0,0)` is the screen's top-left and
  `(1,1)` is its bottom-right. Values past 0–1 are off-screen, so the default
  "next" zone also counts looking past the corner.
- Key names come from pynput: `right`, `left`, `page_down`, `page_up`, `space`,
  `down`, or a single character.
- Text in a very narrow column: lower `reading.sweep_min`, the smallest
  jump that counts as a new line (as a fraction of screen width).
- To turn pages only with the corners, set `"page": {"auto_turn": false}`.
- Page turns glide the text up by one screenful over `page.glide_ms`. Each
  turn starts with a small nudge and measures on screen how far the text moved,
  so it adapts to any viewer and zoom level. `page.overlap` is how much of the
  screen stays on screen after a turn (0.10 = the bottom 10%). An automatic turn also scrolls no further than the lowest line you read. If the
  measurement fails, it scrolls `glide_wheel` wheel units instead. For the old instant turn with a key press, set
  `"page": {"turn_style": "key"}`.
- If the gaze dot is too jumpy, raise `filter.fixation_radius_px` (the dot
  stays put while your eyes stay within this distance of one spot) or lower
  `filter.min_cutoff`. If it feels sluggish or sticky, do the opposite.
- To turn on purpose, glance quickly past the right edge of the screen (next)
  or the left edge (previous) and back. `page.glance_out_right` and
  `page.glance_out_left` are how far past each edge counts (0.08 = 8% of the
  screen width), `page.glance_hold_ms` how long the glance must stay there,
  and `page.glance_window_ms` how quickly it must leave the page, so a slow
  drift doesn't turn. `page.glance_y_range` is the gaze height that still
  counts (looking far above or below the screen doesn't). One glance turns one
  page; look back at the page before the next. Set `"glance_turn": false` to
  turn it off. If glances on one side don't register, lower that side's
  `glance_out_*`; if pages turn when you look away or while reading, raise it
  or `glance_hold_ms`.
- Keep looking past the edge after a glance turn to keep scrolling: right
  scrolls down, left scrolls up, starting `page.hold_scroll_after_ms` after
  the turn and speeding up from `hold_scroll_speed` to `hold_scroll_max_speed`
  (wheel units per second; 120 is one wheel notch) over `hold_scroll_ramp_s`.
  Look back at the page to stop. Set `"hold_scroll": false` to turn it off.
- The **Eyes** and **Hands** buttons on the status panel switch eye gestures
  (glance turns, automatic turns, auto-scroll) and hand gestures on or off
  separately, so you can use either one alone. Switched off, that tracker
  stops running, which leaves more CPU for the other. EyeTurn remembers the
  choice in `config.json`.
- Air scroll, like on a phone: flick an open hand up in front of the webcam
  and the text follows it up (scrolls down); flick it down to scroll back up.
  A quick flick keeps gliding, and flicking again while it glides speeds it
  up. Bringing your hand back after a flick doesn't scroll back, so repeated
  flicks keep going the same way; to go the other way, hold still for
  `hands.reverse_pause_ms`, then flick. `hands.scroll_gain` is how far the text moves with your hand,
  `hands.fling_gain` / `fling_tau_s` how far a flick glides, and
  `hands.start_speed` how quick a movement must be to count. Set
  `"air_scroll": false` to turn it off.
- Or swipe an open hand across the webcam's view: right to left turns to the
  next screen, left to right to the previous one (`hands.swap_directions`
  flips them). The swipe has to cover `hands.swipe_min_dx` of the camera's
  width within `hands.swipe_ms`; your hand going back afterwards is ignored.
  It works in both modes and needs `models/hand_landmarker.task` from
  `https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task`.
  Set `"hands": {"enabled": false}` to turn it off.
- The old corner dwell is off by default; set `"corner_turn": true` to use it.
  If pages then turn while you're still reading the last line, raise
  `next_dwell_ms` or move `next_zone` further into the corner.

## Staying calibrated when you move

A calibration is exact only for the posture it was made in. EyeTurn watches
where your head is (sideways, up/down, distance from the screen). Once you've
settled at least 4 cm sideways, 3 cm up or down or 7 cm nearer or further
than before, it counts as a new posture:

- If you've sat like that before, EyeTurn switches to the correction it
  learned for that posture, with no dots.
- If not, it shows a **touch-up**: 5 small red dots (corners and middle),
  about 6 seconds, when you pause reading. It never takes the keyboard focus
  from your document. Press Esc to skip it, F6 or the Touch-up button to run
  it any time. Set `"touchup": "ask"` to only get a note in the panel.
- Clicks keep refining the correction for the posture you're in, but only
  clicks you looked at: your gaze has to be steady just before the click and
  near it. Clicks on EyeTurn's own panel never count. F7 (look at the mouse
  pointer, press F7) feeds the same correction.

Postures are saved in `postures.json` and start over with each full
calibration. The log says what happened (`Posture: ...`, `Touch-up ...`).

## When something goes wrong

EyeTurn fixes most problems by itself and says so in the panel:

- **Camera trouble** (after the PC sleeps, a frozen or flickering picture,
  the camera unplugged or used by another app): it closes the camera and opens
  it again, retrying every few seconds ("Reconnecting the camera...").
- **A crash in eye or hand tracking**: that part restarts by itself after a
  couple of seconds. An error in the main loop is logged and skipped, so the
  panel never freezes.
- Starting EyeTurn twice brings the running one to the front instead of
  opening a second copy.
- Everything is logged in `logs/eyeturn.log` (kept under 2 MB, with the last
  3 files). Calibration and settings are saved so that a crash or power cut
  can't leave a broken file.
- If the panel shows a low speed (under about 15 fps), plug the PC in: on
  battery Windows slows the CPU, and turns then react later.

## Tips for accuracy

- Light your face from the front, and don't sit with a bright window behind
  you. Webcams lower their frame rate in dim light, and tracking gets worse.
- Put the camera at the top center of the screen. Calibrate in the same
  position you'll read in.
- Expect accuracy of about 1–3 cm on screen. That's plenty for corners and
  bands, but not for picking out single words. Reading detection works from
  the size of eye jumps, so it copes with that error. Clicking around
  normally keeps improving accuracy. If it drifts, use F7 or F6. If it stays
  off, recalibrate with F9.
- Only the primary monitor is supported.

## Third-party models

No model weights are included in this repository. EyeTurn uses
[MediaPipe](https://github.com/google-ai-edge/mediapipe), which is licensed
under Apache 2.0, and its Face and Hand Landmarker models are downloaded from
Google during setup. The optional L2CS-Net model was trained on the Gaze360
dataset, which is for research use only, so it is left to you to download and
to check that your use is allowed.
