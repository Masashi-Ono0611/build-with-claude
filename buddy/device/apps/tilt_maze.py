"""Tilt Maze — roll a marble through a labyrinth by tilting the device.

A hardware-native game for the Cardputer-Adv: the only control is the
on-board IMU (a BMI270, ``M5.Imu.getType() == 6``). Hold the device
flat like a wooden labyrinth board and tilt it to roll the marble; the
in-plane component of gravity accelerates the ball, friction bleeds it
off, so it has real momentum. Thread the gaps in three progressively
tighter walls to reach the green goal. It's a speedrun: clear all three
as fast as you can.

### Why tilt (and not the mic)

The original pitch for this slot was a *voice*-controlled game. The
Cardputer-Adv routes its microphone through an ES8311 codec that UIFlow
2.0's ``M5.Mic`` default config doesn't drive (it assumes the plain
Cardputer's directly-wired PDM mic), so on this board the capture path
returns a flat DC value — no audio. The IMU, speaker and vibration motor
are all confirmed working, so the "control it with the hardware" idea
moved from voice to tilt.

### Sensor model

At rest, screen-up, the accelerometer reads ``(~0, ~0, ~1g)`` — Z points
out of the screen. We capture that resting vector as the neutral baseline
at the start of each run; thereafter the X/Y deltas are the tilt. The
marble's acceleration is the in-plane tilt times a gain, so a steeper
tilt rolls it faster. ``_INV_X`` / ``_INV_Y`` flip the mapping if a given
unit's IMU axes are oriented the other way (verified on hardware).

### Layout

    Header:     y=0..19    (DARK bg, ORANGE hairline at y=20)
    Play area:  y=24..132  (108 px), x=3..236

### Controls

    (tilt)  — roll the marble
    R       — restart the run (also on the win screen)
    Q / Esc — back to the launcher (soft machine.reset(), as every app)
"""

import time

import M5
import machine
from kbheal import Keys


# ---- palette (inlined from ui_theme, same cut snake.py uses)
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_WALL = 0x4A4A6A          # muted indigo, distinct from ball + goal
_GOAL = 0x3FB950          # success green

_LCD = M5.Lcd

_W = 240

# Play area just below the header hairline at y=20.
_PX0 = 3
_PY0 = 24
_PX1 = 236
_PY1 = 132

_R = 4                    # marble radius (collides as a 2R+1 square box)
_BALL = _ORANGE

# Marble physics. Tuned for the ~108 px play area at ~30 fps. GAIN sets
# how hard a tilt accelerates; FRIC bleeds velocity each frame so the
# ball settles; VMAX caps speed so it can never tunnel a thin wall in
# one step (cap < wall thickness + ball diameter).
_GAIN = 90.0
_FRIC = 0.86
_VMAX = 5.0

# Hardware-verified tilt mapping (Cardputer-Adv, BMI270). X is inverted
# (dipping the right edge rolled the ball left otherwise); Y is not,
# because screen-y grows downward, which already supplies the one flip
# the far/near tilt needs. Net: the marble rolls toward whichever edge
# you dip, like a real labyrinth board.
_INV_X = True
_INV_Y = False

_FRAME_MS = 25

# Each level: start (x, y), goal rect, and interior wall rects. The
# play-area border is implicit (the ball clamps to it). Walls are thin
# bars with a gap; the gaps alternate side to force a serpentine path.
_LEVELS = (
    {
        "start": (16, 38),
        "goal": (212, 112, 18, 16),
        "walls": (
            (3, 60, 180, 7),       # gap on the right  (x > 183)
            (57, 96, 179, 7),      # gap on the left   (x < 57)
        ),
    },
    {
        "start": (16, 36),
        "goal": (210, 114, 18, 14),
        "walls": (
            (3, 50, 178, 6),       # gap on the right  (x > 181)
            (58, 78, 178, 6),      # gap on the left   (x < 58)
            (3, 106, 178, 6),      # gap on the right  (x > 181)
        ),
    },
    {
        "start": (16, 34),
        "goal": (212, 114, 18, 14),
        "walls": (
            (3, 48, 205, 6),       # narrow gap right  (x > 208)
            (32, 74, 204, 6),      # narrow gap left   (x < 32)
            (3, 100, 205, 6),      # narrow gap right  (x > 208)
        ),
    },
)


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("tilt_maze: setFont fallback:", e)


def _intent(k):
    """Collapse raw keys into intents. Mirrors snake.py: MatrixKeyboard
    hands back int ASCII or str glyphs; Esc=exit, Enter=restart."""
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:
            return "exit"
        if k in (0x0A, 0x0D):
            return "restart"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch == "q":
        return "exit"
    if ch == "r":
        return "restart"
    return None


def _read_accel():
    """Return (x, y) accel or None if the IMU isn't answering."""
    try:
        a = M5.Imu.getAccel()
        return a[0], a[1]
    except Exception as e:
        print("tilt_maze: IMU read failed:", e)
        return None


def _calibrate():
    """Sample the resting orientation as the neutral baseline. Shows a
    'hold flat' prompt and averages ~0.6 s of samples. Returns
    (bx, by) or None on IMU failure."""
    _LCD.fillScreen(_BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    t = "Lay flat - calibrating"
    _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 56)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    h = "hold still"
    _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, 76)

    sx = sy = 0.0
    n = 0
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < 600:
        a = _read_accel()
        if a is None:
            return None
        sx += a[0]
        sy += a[1]
        n += 1
        time.sleep_ms(20)
    return (sx / n, sy / n)


def _draw_chrome(level_idx):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("tilt maze {}/{}".format(level_idx + 1, len(_LEVELS)), 6, 5)


def _draw_time(secs):
    # Repaint only the right of the header so the title doesn't flash.
    _LCD.fillRect(150, 0, _W - 150, 20, _DARK)
    _LCD.setTextColor(_CREAM, _DARK)
    t = "{:.1f}s".format(secs)
    _LCD.drawString(t, _W - 6 - _LCD.textWidth(t), 5)


def _draw_walls(walls):
    for w in walls:
        _LCD.fillRect(w[0], w[1], w[2], w[3], _WALL)


def _draw_goal(goal):
    _LCD.fillRect(goal[0], goal[1], goal[2], goal[3], _GOAL)


def _draw_ball(x, y):
    _LCD.fillCircle(int(x), int(y), _R, _BALL)


def _rects_overlap(ax, ay, aw, ah, bx, by, bw, bh):
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def _hits(bx, by, walls):
    """True if the marble's bounding box at (bx, by) overlaps any wall."""
    x0 = bx - _R
    y0 = by - _R
    side = 2 * _R + 1
    for w in walls:
        if _rects_overlap(x0, y0, side, side, w[0], w[1], w[2], w[3]):
            return True
    return False


def _step_axis(pos, other, vel, walls, is_x):
    """Advance one axis with 1-px probes so the marble stops flush
    against a wall instead of tunneling. Returns (new_pos, new_vel);
    vel is zeroed on contact."""
    if vel == 0.0:
        return pos, vel
    lo = (_PX0 + _R) if is_x else (_PY0 + _R)
    hi = (_PX1 - _R) if is_x else (_PY1 - _R)
    target = pos + vel
    if target < lo:
        target, vel = lo, 0.0
    elif target > hi:
        target, vel = hi, 0.0
    step = 1.0 if target > pos else -1.0
    p = pos
    while (step > 0 and p + step <= target) or (step < 0 and p + step >= target):
        np = p + step
        hit = _hits(np, other, walls) if is_x else _hits(other, np, walls)
        if hit:
            return p, 0.0
        p = np
    # Final sub-pixel remainder.
    hit = _hits(target, other, walls) if is_x else _hits(other, target, walls)
    if hit:
        return p, 0.0
    return target, vel


def _erase_ball(x, y, walls, goal):
    """Repaint the marble's box black, then redraw any wall/goal it was
    overlapping (cheap full-rect redraw — walls are few and thin)."""
    side = 2 * _R + 1
    bx = int(x) - _R
    by = int(y) - _R
    _LCD.fillRect(bx, by, side, side, _BLACK)
    for w in walls:
        if _rects_overlap(bx, by, side, side, w[0], w[1], w[2], w[3]):
            _LCD.fillRect(w[0], w[1], w[2], w[3], _WALL)
    if _rects_overlap(bx, by, side, side, goal[0], goal[1], goal[2], goal[3]):
        _LCD.fillRect(goal[0], goal[1], goal[2], goal[3], _GOAL)


def _reached(x, y, goal):
    return (goal[0] <= x <= goal[0] + goal[2]
            and goal[1] <= y <= goal[1] + goal[3])


def _level_clear_fx():
    try:
        for f in (660, 880, 1320):
            M5.Speaker.tone(f, 90)
            time.sleep_ms(95)
        M5.Power.setVibration(160)
        time.sleep_ms(120)
        M5.Power.setVibration(0)
    except Exception as e:
        print("tilt_maze: fx warning:", e)


def _play_level(keys, level_idx, base):
    """Run one level. Returns 'done', 'exit', or 'restart'."""
    level = _LEVELS[level_idx]
    walls = level["walls"]
    goal = level["goal"]
    x, y = float(level["start"][0]), float(level["start"][1])
    vx = vy = 0.0

    _draw_chrome(level_idx)
    _draw_walls(walls)
    _draw_goal(goal)
    _draw_ball(x, y)

    t0 = time.ticks_ms()
    last_clock = -1
    last_bump = 0
    sign_x = -1.0 if _INV_X else 1.0
    sign_y = -1.0 if _INV_Y else 1.0

    while True:
        i = _intent(keys.get())
        if i == "exit":
            return "exit"
        if i == "restart":
            return "restart"

        a = _read_accel()
        if a is not None:
            vx += _GAIN * (a[0] - base[0]) * sign_x * (_FRAME_MS / 1000.0)
            vy += _GAIN * (a[1] - base[1]) * sign_y * (_FRAME_MS / 1000.0)
        vx *= _FRIC
        vy *= _FRIC
        if vx > _VMAX:
            vx = _VMAX
        elif vx < -_VMAX:
            vx = -_VMAX
        if vy > _VMAX:
            vy = _VMAX
        elif vy < -_VMAX:
            vy = -_VMAX

        ox, oy = x, y
        # Axis-separated: move X (fixed at old y), then move Y (fixed at
        # the new x). For the Y call, pos=y and other=nx — passing them
        # in the wrong order makes the Y step mutate the x-coordinate and
        # teleport the ball into a wall, where it embeds and can't escape.
        nx, vx = _step_axis(x, y, vx, walls, True)
        ny, vy = _step_axis(y, nx, vy, walls, False)

        # A bump (velocity killed by a wall this frame) gets a short
        # low tick + buzz, throttled so scraping a wall isn't a drone.
        now = time.ticks_ms()
        if (vx == 0.0 or vy == 0.0) and (abs(nx - ox) < 0.3 and abs(ny - oy) < 0.3):
            if time.ticks_diff(now, last_bump) > 250:
                try:
                    M5.Speaker.tone(160, 30)
                    M5.Power.setVibration(90)
                except Exception:
                    pass
                last_bump = now
        elif time.ticks_diff(now, last_bump) > 60:
            try:
                M5.Power.setVibration(0)
            except Exception:
                pass

        if int(nx) != int(ox) or int(ny) != int(oy):
            _erase_ball(ox, oy, walls, goal)
            _draw_ball(nx, ny)
        x, y = nx, ny

        secs = time.ticks_diff(now, t0) / 1000.0
        if int(secs * 10) != last_clock:
            _draw_time(secs)
            last_clock = int(secs * 10)

        if _reached(x, y, goal):
            try:
                M5.Power.setVibration(0)
            except Exception:
                pass
            _level_clear_fx()
            return "done"

        time.sleep_ms(_FRAME_MS)


def _win_screen(keys, total_secs):
    _LCD.fillScreen(_BLACK)
    _LCD.setTextSize(2)
    _LCD.setTextColor(_GOAL, _BLACK)
    t = "CLEAR!"
    _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 28)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    s = "time {:.1f}s".format(total_secs)
    _LCD.drawString(s, (_W - _LCD.textWidth(s)) // 2, 64)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    h = "R again   Q exit"
    _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, 92)
    try:
        for f in (880, 1100, 1320, 1760):
            M5.Speaker.tone(f, 110)
            time.sleep_ms(115)
    except Exception as e:
        print("tilt_maze: win fx warning:", e)

    while True:
        i = _intent(keys.get())
        if i == "restart":
            return "restart"
        if i == "exit":
            return "exit"
        time.sleep_ms(40)


def _intro(keys):
    """Title + how-to. Returns 'start' or 'exit'."""
    _LCD.fillScreen(_BLACK)
    _LCD.setTextSize(2)
    _LCD.setTextColor(_ORANGE, _BLACK)
    t = "tilt maze"
    _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 22)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    l1 = "Hold flat, tilt to roll"
    _LCD.drawString(l1, (_W - _LCD.textWidth(l1)) // 2, 58)
    l2 = "the marble to the goal"
    _LCD.drawString(l2, (_W - _LCD.textWidth(l2)) // 2, 74)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    h = "Enter start   Q exit"
    _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, 100)

    while True:
        i = _intent(keys.get())
        if i == "restart":
            return "start"
        if i == "exit":
            return "exit"
        time.sleep_ms(40)


def _run_once(keys):
    """One full run: intro -> calibrate -> all levels -> win screen.
    Returns 'restart' or 'exit'."""
    if _intro(keys) == "exit":
        return "exit"
    base = _calibrate()
    if base is None:
        _LCD.fillScreen(_BLACK)
        _LCD.setTextColor(_GRAY_MID, _BLACK)
        _LCD.drawString("IMU unavailable - Q exit", 6, 60)
        while _intent(keys.get()) != "exit":
            time.sleep_ms(40)
        return "exit"

    t0 = time.ticks_ms()
    for level_idx in range(len(_LEVELS)):
        r = _play_level(keys, level_idx, base)
        if r == "exit":
            return "exit"
        if r == "restart":
            return "restart"
    total = time.ticks_diff(time.ticks_ms(), t0) / 1000.0
    return _win_screen(keys, total)


def run():
    _set_font()
    try:
        M5.Speaker.begin()
        M5.Speaker.setVolume(110)
    except Exception as e:
        print("tilt_maze: speaker init warning:", e)
    keys = Keys()
    # Debounce the keypress that launched us from the App List.
    time.sleep_ms(400)
    try:
        while True:
            if _run_once(keys) == "exit":
                return
    finally:
        try:
            M5.Power.setVibration(0)
        except Exception:
            pass
        try:
            _LCD.fillScreen(_BLACK)
        except Exception as e:
            print("tilt_maze: clear warning:", e)
        time.sleep_ms(200)
        machine.reset()


# UIFlow's App List invokes apps both as __main__ and via import; the
# observed behavior is always-run, so call run() bare (matches snake.py).
run()
