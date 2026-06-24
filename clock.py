#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Gtk, GLib, Gdk, Pango, PangoCairo, Gio
import cairo
import math
import json
import os
import sys
import signal
import warnings
warnings.filterwarnings('ignore', message='.*StatusIcon.*', category=DeprecationWarning)
import uuid
import wave
import array
import tempfile
import subprocess
import shutil
import base64
import ctypes
import ctypes.util
import re as _re
import random
from importlib.metadata import version as _pkg_version, PackageNotFoundError as _PKGNotFound
from datetime import datetime, timedelta

# WebKit2 is optional — tracker panel requires it; feature is absent if not installed
_WEBKIT_AVAILABLE = False
try:
    for _wk_ver in ('4.1', '4.0'):
        try:
            gi.require_version('WebKit2', _wk_ver)
            from gi.repository import WebKit2 as _WebKit2
            _WEBKIT_AVAILABLE = True
            break
        except (ValueError, ImportError):
            pass
except Exception:
    pass

# python-xlib is optional — used only to dismiss a ringing alarm with the
# Play/Pause media key from the lock screen, via XInput2 raw key events.
# Raw events bypass keyboard grabs (including the screen locker) and are
# observe-only, so they never interfere with normal media use. Absent if
# python-xlib isn't installed.
_XLIB_OK = False
try:
    from Xlib import display as _xdisplay
    from Xlib.ext import xinput as _xinput
    _XLIB_OK = True
except Exception:
    _XLIB_OK = False

APP_NAME        = 'Desktop Clock'
APP_PROJECT_KEY = 'desktop-clock'

try:
    APP_VERSION = _pkg_version('desktop-clock')
except _PKGNotFound:
    _pyproject = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pyproject.toml')
    try:
        _m = _re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', open(_pyproject).read(), _re.MULTILINE)
        APP_VERSION = _m.group(1) if _m else 'unknown'
    except Exception:
        APP_VERSION = 'unknown'

_libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
_libc.prctl(15, APP_PROJECT_KEY.encode(), 0, 0, 0)  # PR_SET_NAME

DATA_DIR        = os.path.expanduser('~/.local/share/desktop-clock')
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE  = os.path.join(DATA_DIR, 'state.json')
ALARMS_FILE = os.path.join(DATA_DIR, 'alarms.json')
TIMERS_FILE = os.path.join(DATA_DIR, 'timers.json')

# ─── Tracker config ──────────────────────────────────────────────────

TRACKER_KEYS = ('tracker_url', 'tracker_project_key', 'tracker_api_key')

def load_tracker_config():
    state = load_state() if os.path.exists(STATE_FILE) else {}
    return {k: state.get(k, '') for k in ('tracker_url', 'tracker_project_key',
                                           'tracker_api_key', 'tracker_intro_seen',
                                           'tracker_disabled')}

def save_tracker_config(cfg):
    state = load_state()
    state.update(cfg)
    save_state(state)

def tracker_configured():
    """True only when WebKit is available and all three config values are set."""
    if not _WEBKIT_AVAILABLE:
        return False
    cfg = load_tracker_config()
    return all(cfg.get(k, '').strip() for k in TRACKER_KEYS)

# ─── State persistence ────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(data):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


# ─── Fade-to-clear constants (v1.2.0, HLT desktop-clock #330) ─────────
# Durations in milliseconds. Animation tick is 50 ms (20 fps) — same
# rate as the eye animation. Default wait between fade-out and fade-in
# is 5 minutes (user-configurable from the tray Fade timeout submenu).
FADE_OUT_MS         = 10_000   # fade-out phase: 10 seconds
FADE_IN_MS          = 120_000  # fade-in phase: 2 minutes
FADE_FAST_IN_MS     = 2_500    # alarm/timer overrides → ~2.5 s back
FADE_TICK_MS        = 50       # 20 fps
FADE_WAIT_OPTIONS_MIN = (1, 5, 10, 30, 60)
FADE_WAIT_DEFAULT_MIN = 5

def monitor_state_file(geom):
    return os.path.join(DATA_DIR, f'state_monitor_{geom.x}_{geom.y}.json')

def load_monitor_state(geom):
    try:
        with open(monitor_state_file(geom)) as f:
            return json.load(f)
    except Exception:
        return {}

def save_monitor_state(geom, data):
    try:
        with open(monitor_state_file(geom), 'w') as f:
            json.dump(data, f)
    except Exception:
        pass

# ─── Timer persistence ────────────────────────────────────────────────

def save_timers(timers):
    try:
        data = [dict(t, saved_at=datetime.now().isoformat()) for t in timers
                if t['state'] in ('running', 'paused')]
        with open(TIMERS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def load_timers_on_start():
    """Load persisted timers, adjusting remaining time for elapsed. Returns list of timers."""
    try:
        with open(TIMERS_FILE) as f:
            data = json.load(f)
        os.remove(TIMERS_FILE)
    except Exception:
        return []
    now = datetime.now()
    restored = []
    for t in data:
        try:
            saved_at = datetime.fromisoformat(t.get('saved_at', now.isoformat()))
            elapsed  = (now - saved_at).total_seconds()
            if t['state'] == 'running':
                t['remaining'] = t['remaining'] - elapsed
            if t['remaining'] <= 0:
                # Fire immediately but flag for auto-dismiss (user is not present)
                t['remaining'] = 0
                t['state']     = 'completed'
                t['completed_at'] = now.timestamp()
                t['snooze_count'] = 3   # forces auto-dismiss after 2 min
            t.pop('saved_at', None)
            restored.append(t)
        except Exception:
            pass
    return restored

# ─── Alarm persistence ────────────────────────────────────────────────

def load_alarms():
    try:
        with open(ALARMS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_alarms(alarms):
    try:
        with open(ALARMS_FILE, 'w') as f:
            json.dump(alarms, f, indent=2)
    except Exception:
        pass

def new_alarm():
    return {
        'id':          str(uuid.uuid4()),
        'label':       'Alarm',
        'time':        '07:00',
        'repeat':      'Once',
        'style':       'both',
        'tone':        3,
        'dismiss':     'persist',
        'expire_mins': 5,
        'wake':        False,
        'enabled':     True,
        'last_fired':  None,
    }

# ─── Tone generation ──────────────────────────────────────────────────

TONE_NAMES = ['1 – Gentle', '2 – Soft', '3 – Medium', '4 – Strong', '5 – Unmissable']
_tone_cache = {}

def _build_tone(tone_num):
    SR = 44100

    def seg(freq, dur, vol, atk=0.01, dcy=0.08):
        n   = int(SR * dur)
        atk = int(atk * SR)
        dcy = int(dcy * SR)
        out = []
        for i in range(n):
            env = 1.0
            if i < atk:
                env = i / atk
            elif i > n - dcy:
                env = max(0.0, (n - i) / dcy)
            out.append(int(32767 * vol * env * math.sin(2 * math.pi * freq * i / SR)))
        return out

    def silence(dur):
        return [0] * int(SR * dur)

    frames = []

    if tone_num == 1:
        frames += seg(440, 2.5, 0.18, dcy=0.8)
    elif tone_num == 2:
        for f in [523, 659, 784]:
            frames += seg(f, 0.7, 0.30, dcy=0.35)
            frames += silence(0.12)
    elif tone_num == 3:
        for _ in range(3):
            frames += seg(880, 0.35, 0.50, dcy=0.10)
            frames += silence(0.10)
            frames += seg(880, 0.35, 0.50, dcy=0.10)
            frames += silence(0.35)
    elif tone_num == 4:
        for _ in range(7):
            frames += seg(800,  0.14, 0.72, atk=0.005, dcy=0.02)
            frames += seg(1200, 0.14, 0.72, atk=0.005, dcy=0.02)
    elif tone_num == 5:
        for _ in range(6):
            for i in range(20):
                frames += seg(500 + i * 25, 0.025, 1.0, atk=0.002, dcy=0.002)
            for i in range(20, 0, -1):
                frames += seg(500 + i * 25, 0.025, 1.0, atk=0.002, dcy=0.002)

    arr = array.array('h', [max(-32767, min(32767, f)) for f in frames])
    fd, path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    with wave.open(path, 'w') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(arr.tobytes())
    return path

def get_tone_file(tone_num):
    if tone_num not in _tone_cache:
        _tone_cache[tone_num] = _build_tone(tone_num)
    return _tone_cache[tone_num]

def play_tone(tone_num):
    path = get_tone_file(tone_num)
    for player in ['paplay', 'aplay']:
        if shutil.which(player):
            return subprocess.Popen([player, path],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
    return None

# ─── RTC wake scheduling ──────────────────────────────────────────────

RTC_WAKE_HELPER = '/usr/lib/desktop-clock/rtcwake-helper'
_rtc_wake_warned = False


def _rtc_is_local():
    """True if the hardware RTC holds LOCAL time (e.g. a Windows dual-boot box),
    per /etc/adjtime's third line. The kernel's wakealarm expects a UTC epoch, so
    when the RTC is local we must shift the armed time by the UTC offset or the
    alarm lands in the chip's past and is silently dropped."""
    try:
        with open('/etc/adjtime') as f:
            lines = f.read().splitlines()
        return len(lines) >= 3 and lines[2].strip().upper() == 'LOCAL'
    except Exception:
        return False


def _warn_rtc_wake(detail, loud):
    """Surface an RTC-arming failure instead of swallowing it.

    The pre-v1.2.11 bug: `sudo rtcwake` was run with capture_output and its
    result discarded, so when it failed (no passwordless sudo once the app runs
    as the user rather than root) wake-from-suspend failed *silently* and alarms
    never woke the machine. Now: always log to stderr (journald); show a desktop
    notice once per session only for the 'installed but misconfigured' case.
    """
    global _rtc_wake_warned
    sys.stderr.write(f'[desktop-clock] RTC wake not armed: {detail}\n')
    sys.stderr.flush()
    if loud and not _rtc_wake_warned:
        _rtc_wake_warned = True
        if shutil.which('notify-send'):
            try:
                subprocess.run(
                    ['notify-send', '-u', 'critical', 'Desktop Clock',
                     'A "wake from sleep" alarm could not be armed — the machine '
                     'may not wake for alarms.'],
                    capture_output=True)
            except Exception:
                pass


def schedule_rtc_wake(alarms, timers=None):
    """Arm the hardware RTC to wake ~60s before the soonest enabled wake-alarm
    (or running timer), via a tiny privileged helper the .deb installs (scoped,
    passwordless through sudoers.d) so the GTK app itself never runs as root."""
    now      = datetime.now()
    earliest = None
    for a in alarms:
        if not a.get('enabled') or not a.get('wake'):
            continue
        h, m  = map(int, a['time'].split(':'))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        if a.get('repeat') == 'Weekdays':
            while target.weekday() >= 5:
                target += timedelta(days=1)
        if earliest is None or target < earliest:
            earliest = target
    # Include running timers
    for tmr in (timers or []):
        if tmr['state'] != 'running': continue
        target = now + timedelta(seconds=max(1, tmr['remaining']))
        if earliest is None or target < earliest:
            earliest = target
    # 0 = clear any previously-armed alarm when nothing is pending.
    if earliest:
        epoch = int(earliest.timestamp()) - 60
        if _rtc_is_local():
            # RTC holds local time (Windows dual-boot): shift by the UTC offset AT
            # THE ALARM TIME (DST-correct) so the alarm registers match the chip.
            try:
                off = earliest.astimezone().utcoffset()
                if off:
                    epoch += int(off.total_seconds())
            except Exception:
                pass
        arg = str(epoch)
    else:
        arg = '0'
    if not os.path.exists(RTC_WAKE_HELPER):
        # Not installed (e.g. running from source) — can't arm; quiet note only.
        if earliest:
            _warn_rtc_wake(f'helper {RTC_WAKE_HELPER} not present (running from source?)',
                           loud=False)
        return
    try:
        r = subprocess.run(['sudo', '-n', RTC_WAKE_HELPER, arg],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            _warn_rtc_wake((r.stderr or '').strip() or f'helper exit {r.returncode}',
                           loud=True)
    except Exception as e:
        _warn_rtc_wake(str(e), loud=True)

# ─── Alert dialog ─────────────────────────────────────────────────────

class AlertDialog(Gtk.Window):
    def __init__(self, alarm, on_dismiss):
        super().__init__()
        self.alarm      = alarm
        self.on_dismiss = on_dismiss
        self._proc      = None
        self._expire_id = None

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_resizable(False)
        self.set_border_width(24)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        bell = Gtk.Label()
        bell.set_markup('<span font="32">🔔</span>')
        box.pack_start(bell, False, False, 0)
        lbl = Gtk.Label()
        lbl.set_markup(f'<span size="x-large" weight="bold">'
                       f'{GLib.markup_escape_text(alarm["label"])}</span>')
        box.pack_start(lbl, False, False, 0)
        time_lbl = Gtk.Label()
        time_lbl.set_markup(f'<span size="large">{alarm["time"]}</span>')
        box.pack_start(time_lbl, False, False, 4)
        btn = Gtk.Button(label='Dismiss')
        btn.set_size_request(120, 36)
        btn.connect('clicked', self._dismiss)
        box.pack_start(btn, False, False, 0)
        self.add(box)

        self.set_position(Gtk.WindowPosition.NONE)
        if alarm['style'] in ('sound', 'both'):
            self._start_sound()
        if alarm['dismiss'] == 'auto':
            secs = alarm.get('expire_mins', 5) * 60
            self._expire_id = GLib.timeout_add_seconds(secs, self._dismiss, None)

    def _start_sound(self):
        self._proc = play_tone(self.alarm['tone'])
        if self.alarm['dismiss'] == 'persist':
            GLib.timeout_add(400, self._loop_sound)

    def _loop_sound(self):
        if not self.get_visible():
            return False
        if self._proc and self._proc.poll() is not None:
            self._proc = play_tone(self.alarm['tone'])
        return True

    def _dismiss(self, *_):
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass
        if self._expire_id:
            GLib.source_remove(self._expire_id)
        self.hide()
        self.on_dismiss(self.alarm)
        return False

# ─── Alarm edit dialog ────────────────────────────────────────────────

REPEAT_OPTIONS = ['Once', 'Daily', 'Weekdays']

class AlarmEditDialog(Gtk.Dialog):
    def __init__(self, parent, alarm=None):
        super().__init__(title='Edit Alarm' if alarm else 'New Alarm',
                         transient_for=parent, modal=True)
        self.set_decorated(False)
        # v1.2.1 #481: spawned windows are always-on-top so they can't
        # get lost behind other windows while still being deliberately
        # placed by _find_clear_position to avoid overlap with each other.
        self.set_keep_above(True)
        self.alarm = alarm.copy() if alarm else new_alarm()
        self.add_button('Close', Gtk.ResponseType.CANCEL)
        self.add_button('Save',  Gtk.ResponseType.OK)
        title = 'Edit Alarm' if alarm else 'New Alarm'
        self.get_content_area().pack_start(_make_titlebar(self, title), False, False, 0)

        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        grid.set_margin_top(8); grid.set_margin_bottom(16)
        grid.set_margin_start(16); grid.set_margin_end(16)
        row = 0

        grid.attach(Gtk.Label(label='Label:', xalign=0), 0, row, 1, 1)
        self.label_entry = Gtk.Entry()
        self.label_entry.set_text(self.alarm['label'])
        self.label_entry.set_hexpand(True)
        grid.attach(self.label_entry, 1, row, 2, 1); row += 1

        grid.attach(Gtk.Label(label='Time:', xalign=0), 0, row, 1, 1)
        time_box = Gtk.Box(spacing=4)
        h, m = map(int, self.alarm['time'].split(':'))
        self.hour_spin = Gtk.SpinButton()
        self.hour_spin.set_adjustment(Gtk.Adjustment(value=h, lower=0, upper=23, step_increment=1))
        self.hour_spin.set_wrap(True); self.hour_spin.set_width_chars(2)
        self.min_spin = Gtk.SpinButton()
        self.min_spin.set_adjustment(Gtk.Adjustment(value=m, lower=0, upper=59, step_increment=1))
        self.min_spin.set_wrap(True); self.min_spin.set_width_chars(2)
        time_box.pack_start(self.hour_spin, False, False, 0)
        time_box.pack_start(Gtk.Label(label=':'), False, False, 2)
        time_box.pack_start(self.min_spin, False, False, 0)
        grid.attach(time_box, 1, row, 2, 1); row += 1

        grid.attach(Gtk.Label(label='Repeat:', xalign=0), 0, row, 1, 1)
        repeat_box = Gtk.Box(spacing=8)
        self._repeat_btns = {}; first = None
        for opt in REPEAT_OPTIONS:
            btn = Gtk.RadioButton.new_with_label_from_widget(first, opt)
            if first is None: first = btn
            if opt == self.alarm['repeat']: btn.set_active(True)
            self._repeat_btns[opt] = btn
            repeat_box.pack_start(btn, False, False, 0)
        grid.attach(repeat_box, 1, row, 2, 1); row += 1

        grid.attach(Gtk.Label(label='Alert:', xalign=0), 0, row, 1, 1)
        style_box = Gtk.Box(spacing=8)
        self._chk_sound  = Gtk.CheckButton(label='Sound')
        self._chk_dialog = Gtk.CheckButton(label='Dialog')
        self._chk_sound.set_active(self.alarm['style'] in ('sound', 'both'))
        self._chk_dialog.set_active(self.alarm['style'] in ('dialog', 'both'))
        style_box.pack_start(self._chk_sound, False, False, 0)
        style_box.pack_start(self._chk_dialog, False, False, 0)
        grid.attach(style_box, 1, row, 2, 1); row += 1

        grid.attach(Gtk.Label(label='Tone:', xalign=0), 0, row, 1, 1)
        tone_box = Gtk.Box(spacing=8)
        self._tone_combo = Gtk.ComboBoxText()
        for name in TONE_NAMES: self._tone_combo.append_text(name)
        self._tone_combo.set_active(self.alarm['tone'] - 1)
        preview_btn = Gtk.Button(label='▶ Preview')
        preview_btn.connect('clicked', lambda _: play_tone(self._tone_combo.get_active() + 1))
        tone_box.pack_start(self._tone_combo, False, False, 0)
        tone_box.pack_start(preview_btn, False, False, 0)
        grid.attach(tone_box, 1, row, 2, 1); row += 1

        grid.attach(Gtk.Label(label='Dismiss:', xalign=0), 0, row, 1, 1)
        dismiss_box = Gtk.Box(spacing=8)
        self._persist = Gtk.RadioButton.new_with_label(None, 'Until acknowledged')
        self._auto    = Gtk.RadioButton.new_with_label_from_widget(self._persist, 'Auto after')
        self._expire  = Gtk.SpinButton()
        self._expire.set_adjustment(Gtk.Adjustment(
            value=self.alarm.get('expire_mins', 5), lower=1, upper=120, step_increment=1))
        self._expire.set_width_chars(3)
        if self.alarm['dismiss'] == 'auto': self._auto.set_active(True)
        else: self._persist.set_active(True)
        dismiss_box.pack_start(self._persist, False, False, 0)
        dismiss_box.pack_start(self._auto, False, False, 0)
        dismiss_box.pack_start(self._expire, False, False, 0)
        dismiss_box.pack_start(Gtk.Label(label='min'), False, False, 0)
        grid.attach(dismiss_box, 1, row, 2, 1); row += 1

        self._wake = Gtk.CheckButton(label='Wake machine if asleep')
        self._wake.set_active(self.alarm.get('wake', False))
        grid.attach(self._wake, 1, row, 2, 1)

        self.get_content_area().add(grid)
        self.show_all()
        btn = self.get_widget_for_response(Gtk.ResponseType.CANCEL)
        if btn:
            css = Gtk.CssProvider()
            css.load_from_data(b'button { background: #c0392b; color: white; }')
            btn.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def get_alarm(self):
        h = int(self.hour_spin.get_value())
        m = int(self.min_spin.get_value())
        self.alarm['label'] = self.label_entry.get_text().strip() or 'Alarm'
        self.alarm['time']  = f'{h:02d}:{m:02d}'
        for opt, btn in self._repeat_btns.items():
            if btn.get_active(): self.alarm['repeat'] = opt
        s = self._chk_sound.get_active(); d = self._chk_dialog.get_active()
        self.alarm['style']       = 'both' if (s and d) else ('sound' if s else 'dialog')
        self.alarm['tone']        = self._tone_combo.get_active() + 1
        self.alarm['dismiss']     = 'auto' if self._auto.get_active() else 'persist'
        self.alarm['expire_mins'] = int(self._expire.get_value())
        self.alarm['wake']        = self._wake.get_active()
        self.alarm['last_fired']  = None
        return self.alarm

_panel_css = None

def _setup_undecorated_window(win, outer_box):
    """RGBA visual + transparent bg + rounded CSS corners for undecorated windows."""
    global _panel_css
    screen = win.get_screen()
    visual = screen.get_rgba_visual()
    if visual:
        win.set_visual(visual)
    win.set_app_paintable(True)

    def _draw_transparent(widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        return False

    win.connect('draw', _draw_transparent)

    if _panel_css is None:
        _panel_css = Gtk.CssProvider()
        _panel_css.load_from_data(b'''
            .clock-panel {
                background-color: @theme_bg_color;
                border-radius: 10px;
                border: 1px solid alpha(@borders, 0.55);
            }
        ''')
        Gtk.StyleContext.add_provider_for_screen(
            screen, _panel_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    outer_box.get_style_context().add_class('clock-panel')

def _make_titlebar(win, title):
    """Draggable title bar for undecorated windows — title only, no close button."""
    bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    bar.set_margin_start(10); bar.set_margin_end(10)
    bar.set_margin_top(6);    bar.set_margin_bottom(4)
    lbl = Gtk.Label(xalign=0)
    lbl.set_markup(f'<b>{GLib.markup_escape_text(title)}</b>')
    bar.pack_start(lbl, True, True, 0)

    drag_state = [None]   # [(offset_x, offset_y)] when dragging

    def _on_press(w, e):
        if e.button == 1:
            wx, wy = win.get_position()
            drag_state[0] = (int(e.x_root) - wx, int(e.y_root) - wy)
        return False

    def _on_motion(w, e):
        if drag_state[0]:
            ox, oy = drag_state[0]
            win.move(int(e.x_root) - ox, int(e.y_root) - oy)
        return False

    def _on_release(w, e):
        if e.button == 1:
            drag_state[0] = None
        return False

    # Press only on title bar — motion/release on window so drag works outside bar
    bar.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
    bar.connect('button-press-event', _on_press)
    win.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
    win.connect('motion-notify-event',  _on_motion)
    win.connect('button-release-event', _on_release)

    sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    outer.pack_start(bar, False, False, 0)
    outer.pack_start(sep, False, False, 0)
    return outer

def _red_close_button(label, callback):
    """A red styled close/exit button."""
    btn = Gtk.Button(label=label)
    css = Gtk.CssProvider()
    css.load_from_data(b'button { background: #c0392b; color: white; border-radius: 5px; }')
    btn.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    btn.connect('clicked', lambda _: callback())
    return btn

# ─── HomieLab Tracker integration ────────────────────────────────────

class TrackerIntroDialog(Gtk.Window):
    """First-run onboarding dialog explaining the tracker feature."""

    def __init__(self, on_configure, on_disable, on_later):
        super().__init__()
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_resizable(False)
        self.set_border_width(0)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _setup_undecorated_window(self, vbox)
        vbox.pack_start(_make_titlebar(self, 'Issue Tracker'), False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_start(20); body.set_margin_end(20)
        body.set_margin_top(12);   body.set_margin_bottom(16)

        heading = Gtk.Label()
        heading.set_markup(f'<b>Report bugs from {GLib.markup_escape_text(APP_NAME)}</b>')
        heading.set_xalign(0)
        body.pack_start(heading, False, False, 0)

        info = Gtk.Label()
        info.set_markup(
            f'{GLib.markup_escape_text(APP_NAME)} can connect to a <b>HomieLab Tracker</b> instance\n'
            'running on your local network, letting you log bugs and\n'
            f'feedback without leaving {GLib.markup_escape_text(APP_NAME)}.\n\n'
            '<b>This is a self-hosted feature.</b> It requires:\n'
            '  • A running HomieLab Tracker on your network\n'
            '  • A project key and API key for this app\n\n'
            'It will not work without a HomieLab Tracker instance\n'
            'and has no connection to any external service.'
        )
        info.set_xalign(0)
        info.set_line_wrap(True)
        body.pack_start(info, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        body.pack_start(sep, False, False, 4)

        btn_row = Gtk.Box(spacing=8)
        btn_row.set_halign(Gtk.Align.END)

        disable_btn = Gtk.Button(label='Disable')
        disable_btn.set_tooltip_text('Hide this feature permanently')
        disable_btn.connect('clicked', lambda _: self._respond(on_disable))
        btn_row.pack_start(disable_btn, False, False, 0)

        later_btn = Gtk.Button(label='Later')
        later_btn.connect('clicked', lambda _: self._respond(on_later))
        btn_row.pack_start(later_btn, False, False, 0)

        config_btn = Gtk.Button(label='Configure…')
        css = Gtk.CssProvider()
        css.load_from_data(b'button { background: #1a6496; color: white; }')
        config_btn.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        config_btn.connect('clicked', lambda _: self._respond(on_configure))
        btn_row.pack_start(config_btn, False, False, 0)

        body.pack_start(btn_row, False, False, 0)
        vbox.pack_start(body, False, False, 0)
        self.add(vbox)
        self.show_all()

    def _respond(self, callback):
        self.destroy()
        callback()


class TrackerConfigDialog(Gtk.Window):
    """Settings dialog for HomieLab Tracker connection."""

    def __init__(self, parent, on_save=None):
        super().__init__()
        self.set_transient_for(parent)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_resizable(False)
        self.set_border_width(0)
        self._on_save = on_save

        cfg = load_tracker_config()

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _setup_undecorated_window(self, vbox)
        vbox.pack_start(_make_titlebar(self, 'Issue Tracker — Configure'), False, False, 0)

        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        grid.set_margin_start(16); grid.set_margin_end(16)
        grid.set_margin_top(12);   grid.set_margin_bottom(8)
        row = 0

        grid.attach(Gtk.Label(label='Tracker URL:', xalign=0), 0, row, 1, 1)
        self._url = Gtk.Entry()
        self._url.set_text(cfg.get('tracker_url', '') or '')
        self._url.set_hexpand(True); self._url.set_width_chars(36)
        grid.attach(self._url, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label='Project key:', xalign=0), 0, row, 1, 1)
        self._key = Gtk.Entry()
        self._key.set_text(cfg.get('tracker_project_key', '') or APP_PROJECT_KEY)
        self._key.set_hexpand(True)
        hint = Gtk.Label()
        hint.set_markup('<small><i>Shown on the project page in your tracker</i></small>')
        hint.set_xalign(0)
        grid.attach(self._key, 1, row, 1, 1); row += 1
        grid.attach(hint, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label='API key:', xalign=0), 0, row, 1, 1)
        self._api = Gtk.Entry()
        self._api.set_text(cfg.get('tracker_api_key', ''))
        self._api.set_placeholder_text('hlt_…')
        self._api.set_visibility(False)
        self._api.set_hexpand(True)
        grid.attach(self._api, 1, row, 1, 1); row += 1

        self._status = Gtk.Label(label='')
        self._status.set_xalign(0)
        self._status.set_margin_top(2)
        grid.attach(self._status, 0, row, 2, 1); row += 1

        vbox.pack_start(grid, False, False, 0)

        btn_row = Gtk.Box(spacing=8)
        btn_row.set_margin_start(16); btn_row.set_margin_end(16); btn_row.set_margin_bottom(14)
        btn_row.set_halign(Gtk.Align.END)

        cancel_btn = _red_close_button('Cancel', self.destroy)
        btn_row.pack_start(cancel_btn, False, False, 0)

        test_btn = Gtk.Button(label='Test connection')
        test_btn.connect('clicked', self._test)
        btn_row.pack_start(test_btn, False, False, 0)

        save_btn = Gtk.Button(label='Save')
        css = Gtk.CssProvider()
        css.load_from_data(b'button { background: #1a6496; color: white; }')
        save_btn.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        save_btn.connect('clicked', self._save)
        btn_row.pack_start(save_btn, False, False, 0)

        vbox.pack_start(btn_row, False, False, 0)
        self.add(vbox)
        self.show_all()

    def _values(self):
        return (self._url.get_text().strip().rstrip('/'),
                self._key.get_text().strip(),
                self._api.get_text().strip())

    def _test(self, _):
        url, key, api = self._values()
        if not all([url, key, api]):
            self._status.set_markup('<span color="#c0392b">Fill in all three fields first.</span>')
            return
        try:
            import urllib.request
            req = urllib.request.Request(
                f'{url}/api/projects/{key}/issues',
                headers={'X-API-Key': api})
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
            self._status.set_markup('<span color="#27ae60">Connection successful.</span>')
        except Exception as e:
            self._status.set_markup(f'<span color="#c0392b">Failed: {GLib.markup_escape_text(str(e))}</span>')

    def _save(self, _):
        url, key, api = self._values()
        if not all([url, key, api]):
            self._status.set_markup('<span color="#c0392b">Fill in all three fields to save.</span>')
            return
        save_tracker_config({
            'tracker_url': url,
            'tracker_project_key': key,
            'tracker_api_key': api,
            'tracker_intro_seen': True,
            'tracker_disabled': False,
        })
        self.destroy()
        if self._on_save:
            self._on_save()


class TrackerPanelWindow(Gtk.Window):
    """GTK window containing an embedded WebKit2 panel.js view."""

    def __init__(self, parent):
        super().__init__(title=f'Report a bug — {APP_NAME}')
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_default_size(520, 640)

        cfg = load_tracker_config()
        url     = cfg.get('tracker_url', '').rstrip('/')
        key     = cfg.get('tracker_project_key', '')
        api_key = cfg.get('tracker_api_key', '')

        # Message handler lets JS close the GTK window cleanly
        ucm = _WebKit2.UserContentManager()
        ucm.register_script_message_handler('hltClose')
        ucm.connect('script-message-received::hltClose', lambda *_: self.destroy())

        self._wv = _WebKit2.WebView.new_with_user_content_manager(ucm)
        self._wv.set_hexpand(True)
        self._wv.set_vexpand(True)
        self._wv.connect('context-menu', lambda *_: True)
        self._wv.connect('decide-policy', self._on_policy)
        self._wv.connect('key-press-event', self._on_key_press)

        html = f'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ height: 100vh; overflow: hidden; background: #f8fafc; }}
  /* Make the panel fill the window instead of floating as an overlay */
  #hlt-overlay {{
    position: static !important;
    background: transparent !important;
    padding: 0 !important;
    display: flex !important;
    height: 100vh;
  }}
  #hlt-panel {{
    width: 100% !important;
    max-width: 100% !important;
    max-height: 100vh !important;
    border-radius: 0 !important;
    border: none !important;
    box-shadow: none !important;
  }}
</style>
</head>
<body>
<script src="{url}/panel.js"></script>
<script>
  document.addEventListener("DOMContentLoaded", function() {{
    HLTPanel.init({{
      trackerUrl: "{url}",
      project:    "{key}",
      apiKey:     "{api_key}",
      version:    "{APP_VERSION}"
    }});
    var _origClose = HLTPanel.close.bind(HLTPanel);
    HLTPanel.close = async function() {{
      try {{ await _origClose(); }} catch(e) {{ console.error('[HLT] close error', e); }}
      window.webkit.messageHandlers.hltClose.postMessage('close');
    }};
    HLTPanel.open();
  }});
</script>
</body>
</html>'''

        self._wv.load_html(html, f'{url}/')
        self.add(self._wv)
        self.connect('delete-event', self._on_delete)
        self.show_all()

    def _on_key_press(self, widget, event):
        # Intercept Ctrl+V when GTK clipboard holds an image. WebKit2GTK does
        # not expose image items via clipboardData.items, so the panel's paste
        # handler never sees them. We read the image here and inject it via JS.
        if event.keyval == Gdk.KEY_v and (event.state & Gdk.ModifierType.CONTROL_MASK):
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            if cb.wait_is_image_available():
                self._inject_clipboard_image(cb)
                return True  # Swallow — we handled it; don't paste as text
        return False

    def _inject_clipboard_image(self, cb):
        pixbuf = cb.wait_for_image()
        if pixbuf is None:
            return
        ok, png_bytes = pixbuf.save_to_bufferv('png', [], [])
        if not ok:
            return
        b64 = base64.b64encode(png_bytes).decode('ascii')
        js = (
            "(function(){"
            "var b='" + b64 + "';"
            "var bin=atob(b),buf=new Uint8Array(bin.length);"
            "for(var i=0;i<bin.length;i++)buf[i]=bin.charCodeAt(i);"
            "var f=new File([new Blob([buf],{type:'image/png'})],'paste.png',{type:'image/png'});"
            "HLTPanel._pasteFile(f);"
            "})();"
        )
        self._wv.run_javascript(js, None, None, None)

    def _on_delete(self, window, event):
        # Route WM × through JS so auto-save runs before the window closes.
        # HLTPanel.close() (async override) will save, then fire hltClose → destroy().
        self._wv.run_javascript('HLTPanel.close()', None, None, None)
        return True  # Block default GTK destroy; JS triggers it via hltClose

    def _on_policy(self, wv, decision, dtype):
        if dtype == _WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            if action.get_navigation_type() == _WebKit2.NavigationType.LINK_CLICKED:
                uri = action.get_request().get_uri()
                subprocess.Popen(['xdg-open', uri])
                decision.ignore()
                return True
        return False

# ─── Alarm manager window ─────────────────────────────────────────────

class AlarmManagerWindow(Gtk.Window):
    def __init__(self, parent):
        super().__init__(title='Alarms')
        self.set_transient_for(parent)
        self.set_decorated(False)
        self.set_default_size(460, 280)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(0)
        # v1.2.1 #481: always-on-top.
        self.set_keep_above(True)
        self.alarms = load_alarms()

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _setup_undecorated_window(self, vbox)
        vbox.pack_start(_make_titlebar(self, 'Alarms'), False, False, 0)
        bar = Gtk.Box(spacing=6)
        bar.set_margin_start(8); bar.set_margin_end(8)
        for label, cb in [('+ Add', self._add), ('Edit', self._edit), ('Delete', self._delete)]:
            btn = Gtk.Button(label=label); btn.connect('clicked', cb)
            bar.pack_start(btn, False, False, 0)
        vbox.pack_start(bar, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str, str, str, str, bool)
        self.tree  = Gtk.TreeView(model=self.store)
        tog = Gtk.CellRendererToggle(); tog.connect('toggled', self._toggle)
        self.tree.append_column(Gtk.TreeViewColumn('On', tog, active=7))
        for title, idx in [('Label',1),('Time',2),('Repeat',3),('Alert',4),('Tone',5),('Dismiss',6)]:
            r = Gtk.CellRendererText()
            self.tree.append_column(Gtk.TreeViewColumn(title, r, text=idx))
        self.tree.connect('row-activated', lambda t, p, c: self._edit(None))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.tree); vbox.pack_start(scroll, True, True, 0)

        close_row = Gtk.Box(spacing=6)
        close_row.set_margin_start(8); close_row.set_margin_end(8); close_row.set_margin_bottom(8)
        close_row.pack_end(_red_close_button('Close', self.destroy), False, False, 0)
        vbox.pack_start(close_row, False, False, 0)

        self.add(vbox); self._refresh()

    def _refresh(self):
        self.store.clear()
        for a in self.alarms:
            tone_name = TONE_NAMES[a['tone']-1] if 1 <= a['tone'] <= 5 else '?'
            dismiss   = f'Auto {a.get("expire_mins",5)}m' if a['dismiss'] == 'auto' else 'Persist'
            self.store.append([a['id'], a['label'], a['time'], a['repeat'],
                               a['style'].capitalize(), tone_name, dismiss, a['enabled']])

    def _selected(self):
        model, it = self.tree.get_selection().get_selected()
        if it is None: return None, None
        aid = model[it][0]
        for i, a in enumerate(self.alarms):
            if a['id'] == aid: return i, a
        return None, None

    def _add(self, _):
        dlg = AlarmEditDialog(self)
        if dlg.run() == Gtk.ResponseType.OK:
            self.alarms.append(dlg.get_alarm()); save_alarms(self.alarms)
            schedule_rtc_wake(self.alarms); self._refresh()
        dlg.destroy()

    def _edit(self, _):
        idx, alarm = self._selected()
        if alarm is None: return
        dlg = AlarmEditDialog(self, alarm)
        if dlg.run() == Gtk.ResponseType.OK:
            self.alarms[idx] = dlg.get_alarm(); save_alarms(self.alarms)
            schedule_rtc_wake(self.alarms); self._refresh()
        dlg.destroy()

    def _delete(self, _):
        idx, alarm = self._selected()
        if alarm is None: return
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO,
                                text=f'Delete alarm "{alarm["label"]}"?')
        if dlg.run() == Gtk.ResponseType.YES:
            self.alarms.pop(idx); save_alarms(self.alarms); self._refresh()
        dlg.destroy()

    def _toggle(self, renderer, path):
        it = self.store.get_iter(path); aid = self.store[it][0]
        for a in self.alarms:
            if a['id'] == aid:
                a['enabled'] = not a['enabled']; self.store[it][7] = a['enabled']
                save_alarms(self.alarms); schedule_rtc_wake(self.alarms); break

# ─── Timer ────────────────────────────────────────────────────────────

def new_timer(label='Timer', total_secs=60, tone=3, never_give_up=False, snooze_count=0, notes=''):
    return {
        'id':            str(uuid.uuid4()),
        'label':         label,
        'notes':         notes,
        'total':         total_secs,
        'remaining':     total_secs,
        'state':         'running',
        'tone':          tone,
        'never_give_up': never_give_up,
        'snooze_count':  snooze_count,
        'completed_at':  None,
    }

def _fmt_secs(secs):
    secs = max(0, int(secs))
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h: return f'{h}:{m:02d}:{s:02d}'
    return f'{m:02d}:{s:02d}'

class TimerAlertDialog(Gtk.Window):
    _AUTO_SNOOZE_SECS = 120   # wait before auto-snoozing
    _AUTO_SNOOZE_MAX  = 3     # give up after this many auto-snoozes

    def __init__(self, timer, on_dismiss, on_snooze=None):
        super().__init__()
        self._timer     = timer
        self.on_dismiss = on_dismiss
        self.on_snooze  = on_snooze
        self._proc      = None
        self._auto_id   = None
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_resizable(False)
        self.set_border_width(24)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        icon = Gtk.Label(); icon.set_markup('<span font="32">⏳</span>')
        box.pack_start(icon, False, False, 0)
        lbl = Gtk.Label()
        lbl.set_markup(f'<span size="x-large" weight="bold">'
                       f'{GLib.markup_escape_text(timer["label"])}</span>')
        box.pack_start(lbl, False, False, 0)
        box.pack_start(Gtk.Label(label='Timer complete!'), False, False, 4)

        snooze_box = Gtk.Box(spacing=6)
        snooze_box.pack_start(Gtk.Label(label='Snooze:'), False, False, 0)
        self._snooze_spin = Gtk.SpinButton()
        self._snooze_spin.set_adjustment(
            Gtk.Adjustment(value=5, lower=1, upper=60, step_increment=1))
        self._snooze_spin.set_width_chars(3)
        snooze_box.pack_start(self._snooze_spin, False, False, 0)
        snooze_box.pack_start(Gtk.Label(label='min'), False, False, 0)
        snooze_btn = Gtk.Button(label='Snooze')
        snooze_btn.set_size_request(90, 36)
        snooze_btn.connect('clicked', self._snooze)
        snooze_box.pack_start(snooze_btn, False, False, 0)
        box.pack_start(snooze_box, False, False, 0)

        dismiss_btn = Gtk.Button(label='Dismiss')
        dismiss_btn.set_size_request(120, 36)
        dismiss_btn.connect('clicked', self._dismiss)
        box.pack_start(dismiss_btn, False, False, 0)
        self.add(box)
        self._proc = play_tone(timer['tone'])
        self._tone = timer['tone']
        self.set_position(Gtk.WindowPosition.NONE)
        GLib.timeout_add(400, self._loop_sound)
        # Auto-snooze unless never_give_up
        if not timer.get('never_give_up', False):
            self._auto_id = GLib.timeout_add_seconds(
                self._AUTO_SNOOZE_SECS, self._auto_snooze)

    def _loop_sound(self):
        if not self.get_visible(): return False
        if self._proc and self._proc.poll() is not None:
            self._proc = play_tone(self._tone)
        return True

    def _stop_sound(self):
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass
        if self._auto_id:
            GLib.source_remove(self._auto_id)
            self._auto_id = None

    def _auto_snooze(self):
        count = self._timer.get('snooze_count', 0)
        self._stop_sound()
        self.hide()
        if count < self._AUTO_SNOOZE_MAX and self.on_snooze:
            self.on_snooze(self._AUTO_SNOOZE_SECS, count + 1)
        return False

    def _snooze(self, *_):
        self._stop_sound()
        secs = int(self._snooze_spin.get_value()) * 60
        self.hide()
        if self.on_snooze: self.on_snooze(secs, 0)
        return False

    def _dismiss(self, *_):
        self._stop_sound()
        self.hide()
        self.on_dismiss()
        return False

class TimerEditDialog(Gtk.Dialog):
    def __init__(self, parent):
        super().__init__(title='New Timer', transient_for=parent, modal=True)
        self.set_decorated(False)
        # v1.2.1 #481: always-on-top.
        self.set_keep_above(True)
        self.add_button('Close', Gtk.ResponseType.CANCEL)
        self.add_button('Start', Gtk.ResponseType.OK)
        self.get_content_area().pack_start(_make_titlebar(self, 'New Timer'), False, False, 0)
        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        grid.set_margin_top(8); grid.set_margin_bottom(16)
        grid.set_margin_start(16); grid.set_margin_end(16)
        row = 0
        grid.attach(Gtk.Label(label='Label:', xalign=0), 0, row, 1, 1)
        self.label_entry = Gtk.Entry()
        self.label_entry.set_text('Timer')
        self.label_entry.set_hexpand(True)
        grid.attach(self.label_entry, 1, row, 3, 1); row += 1
        grid.attach(Gtk.Label(label='Duration:', xalign=0), 0, row, 1, 1)
        dur_box = Gtk.Box(spacing=4)
        self.hour_spin = Gtk.SpinButton()
        self.hour_spin.set_adjustment(Gtk.Adjustment(value=0, lower=0, upper=23, step_increment=1))
        self.hour_spin.set_wrap(True); self.hour_spin.set_width_chars(2)
        self.min_spin = Gtk.SpinButton()
        self.min_spin.set_adjustment(Gtk.Adjustment(value=0, lower=0, upper=59, step_increment=1))
        self.min_spin.set_wrap(True); self.min_spin.set_width_chars(2)
        self.sec_spin = Gtk.SpinButton()
        self.sec_spin.set_adjustment(Gtk.Adjustment(value=0, lower=0, upper=59, step_increment=1))
        self.sec_spin.set_wrap(True); self.sec_spin.set_width_chars(2)
        for spin, lbl in [(self.hour_spin,'h'),(self.min_spin,'m'),(self.sec_spin,'s')]:
            dur_box.pack_start(spin, False, False, 0)
            dur_box.pack_start(Gtk.Label(label=lbl), False, False, 2)
        grid.attach(dur_box, 1, row, 3, 1); row += 1
        grid.attach(Gtk.Label(label='Tone:', xalign=0), 0, row, 1, 1)
        tone_box = Gtk.Box(spacing=8)
        self._tone_combo = Gtk.ComboBoxText()
        for name in TONE_NAMES: self._tone_combo.append_text(name)
        self._tone_combo.set_active(2)
        preview_btn = Gtk.Button(label='▶ Preview')
        preview_btn.connect('clicked', lambda _: play_tone(self._tone_combo.get_active() + 1))
        tone_box.pack_start(self._tone_combo, False, False, 0)
        tone_box.pack_start(preview_btn, False, False, 0)
        grid.attach(tone_box, 1, row, 3, 1); row += 1

        self._never_give_up = Gtk.CheckButton(label='Never give up (sound until dismissed)')
        grid.attach(self._never_give_up, 0, row, 4, 1); row += 1

        grid.attach(Gtk.Label(label='Notes:', xalign=0), 0, row, 1, 1)
        self.notes_entry = Gtk.Entry()
        self.notes_entry.set_placeholder_text('Optional reminder note')
        self.notes_entry.set_hexpand(True)
        grid.attach(self.notes_entry, 1, row, 3, 1)

        self.get_content_area().add(grid)
        self.show_all()
        btn = self.get_widget_for_response(Gtk.ResponseType.CANCEL)
        if btn:
            css = Gtk.CssProvider()
            css.load_from_data(b'button { background: #c0392b; color: white; }')
            btn.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def get_timer(self):
        h = int(self.hour_spin.get_value())
        m = int(self.min_spin.get_value())
        s = int(self.sec_spin.get_value())
        total = h * 3600 + m * 60 + s
        if total <= 0: total = 60
        return new_timer(
            label=self.label_entry.get_text().strip() or 'Timer',
            total_secs=total,
            tone=self._tone_combo.get_active() + 1,
            never_give_up=self._never_give_up.get_active(),
            notes=self.notes_entry.get_text().strip(),
        )

class TimerManagerWindow(Gtk.Window):
    def __init__(self, parent, manager):
        super().__init__(title='Timers')
        self.set_transient_for(parent)
        self.set_decorated(False)
        self.set_default_size(480, 240)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(0)
        # v1.2.1 #481: always-on-top.
        self.set_keep_above(True)
        self.manager = manager
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _setup_undecorated_window(self, vbox)
        vbox.pack_start(_make_titlebar(self, 'Timers'), False, False, 0)
        bar = Gtk.Box(spacing=6)
        bar.set_margin_start(8); bar.set_margin_end(8)
        for lbl, cb in [('+ Add', self._add), ('Pause/Resume', self._pause_resume), ('Delete', self._cancel)]:
            b = Gtk.Button(label=lbl); b.connect('clicked', cb)
            bar.pack_start(b, False, False, 0)
        vbox.pack_start(bar, False, False, 0)
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        for title, idx in [('Label',1),('Duration',2),('Remaining',3),('State',4)]:
            self.tree.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=idx))
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.tree); vbox.pack_start(scroll, True, True, 0)

        close_row = Gtk.Box(spacing=6)
        close_row.set_margin_start(8); close_row.set_margin_end(8); close_row.set_margin_bottom(8)
        close_row.pack_end(_red_close_button('Close', self.destroy), False, False, 0)
        vbox.pack_start(close_row, False, False, 0)

        self.add(vbox); self._refresh()
        self._tick_id = GLib.timeout_add(1000, self._on_tick)
        self.connect('destroy', lambda _: GLib.source_remove(self._tick_id))

    def _fmt_total(self, secs):
        h, r = divmod(secs, 3600)
        m, s = divmod(r, 60)
        if h: return f'{h}h {m:02d}m {s:02d}s'
        if m: return f'{m}m {s:02d}s'
        return f'{s}s'

    def _refresh(self):
        model, it = self.tree.get_selection().get_selected()
        selected_id = model[it][0] if it else None
        self.store.clear()
        for t in self.manager.timers:
            self.store.append([t['id'], t['label'],
                               self._fmt_total(t['total']),
                               _fmt_secs(t['remaining']),
                               t['state'].capitalize()])
        if selected_id:
            for row in self.store:
                if row[0] == selected_id:
                    self.tree.get_selection().select_iter(row.iter); break

    def _selected(self):
        model, it = self.tree.get_selection().get_selected()
        if it is None: return None, None
        tid = model[it][0]
        for i, t in enumerate(self.manager.timers):
            if t['id'] == tid: return i, t
        return None, None

    def _add(self, _):
        dlg = TimerEditDialog(self)
        if dlg.run() == Gtk.ResponseType.OK:
            self.manager.timers.append(dlg.get_timer())
            self._refresh()
        dlg.destroy()

    def _pause_resume(self, _):
        _, t = self._selected()
        if t is None or t['state'] == 'completed': return
        t['state'] = 'paused' if t['state'] == 'running' else 'running'
        self._refresh()

    def _cancel(self, _):
        idx, _ = self._selected()
        if idx is None: return
        self.manager.timers.pop(idx)
        self._refresh()

    def _on_tick(self):
        if self.get_visible(): self._refresh()
        return True

class TimerHoverOverlay(Gtk.Window):
    _css_loaded = False

    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual: self.set_visual(visual)
        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        if not TimerHoverOverlay._css_loaded:
            css = b'''
            .t-overlay { background-color: rgba(12,12,22,0.93);
                         border-radius: 6px;
                         border: 1px solid rgba(255,255,255,0.15); }
            .t-row     { color: #c8daf0; font-size: 11pt;
                         padding-top: 1px; padding-bottom: 1px;
                         padding-left: 10px; padding-right: 10px; }
            .t-done    { color: #78e878; }
            .t-paused  { color: #999999; }
            '''
            p = Gtk.CssProvider(); p.load_from_data(css)
            Gtk.StyleContext.add_provider_for_screen(
                screen, p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            TimerHoverOverlay._css_loaded = True
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class('t-overlay')
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._box.set_margin_top(6); self._box.set_margin_bottom(6)
        outer.add(self._box)
        self.add(outer)

    def update(self, timers, clock_x, clock_y, clock_w, clock_h, screen_h, alarms=None):
        now_ts = datetime.now().timestamp()
        active = [t for t in timers if t['state'] != 'completed' or
                  (t['completed_at'] and now_ts - t['completed_at'] < 5)]
        active_alarms = [a for a in (alarms or []) if a.get('enabled')]
        for child in self._box.get_children():
            self._box.remove(child)
        self.resize(1, 1)
        if not active and not active_alarms:
            self.hide(); return
        for t in active:
            if t['state'] == 'completed':
                text, css = f'✓  {t["label"]}', 't-done'
            elif t['state'] == 'paused':
                text, css = f'⏸  {t["label"]}  {_fmt_secs(t["remaining"])}', 't-paused'
            else:
                text, css = f'⏱  {t["label"]}  {_fmt_secs(t["remaining"])}', 't-row'
            lbl = Gtk.Label(label=text, xalign=0)
            lbl.get_style_context().add_class('t-row')
            if css != 't-row': lbl.get_style_context().add_class(css)
            self._box.pack_start(lbl, False, False, 0)
            notes = t.get('notes', '').strip()
            if notes:
                nlbl = Gtk.Label(label=f'    {notes}', xalign=0)
                nlbl.get_style_context().add_class('t-row')
                nlbl.get_style_context().add_class('t-paused')
                self._box.pack_start(nlbl, False, False, 0)
        for a in active_alarms:
            text = f'🔔  {a["label"]}  {a["time"]}'
            lbl = Gtk.Label(label=text, xalign=0)
            lbl.get_style_context().add_class('t-row')
            self._box.pack_start(lbl, False, False, 0)
        self._box.show_all(); self.show_all()
        self.move(clock_x, clock_y + clock_h + 4)
        GLib.idle_add(self._reposition, clock_x, clock_y, clock_h, screen_h)

    def _reposition(self, clock_x, clock_y, clock_h, screen_h):
        _, h = self.get_size()
        if clock_y + clock_h + 4 + h > screen_h:
            self.move(clock_x, clock_y - h - 4)
        return False

# ─── Clock themes / sizes ─────────────────────────────────────────────

THEMES = {
    'Dark': {
        'face':         (0.07, 0.07, 0.11, 0.90),
        'border':       (1,    1,    1,    0.60),
        'hour_marks':   (0.95, 0.85, 0.50),
        'min_marks':    (0.55, 0.70, 0.95),
        'hour_hand':    (0.53, 0.81, 0.98, 0.97),
        'min_hand':     (1,    1,    1,    0.95),
        'hands':        (1,    1,    1,    0.95),
        'second':       (0.96, 0.32, 0.22, 1.00),
        'digital':      (0.60, 0.95, 0.70),
        'marks':        (1,    1,    1),
        'date_bg':      (0.96, 0.96, 0.94, 0.97),
        'date_fg':      (0.05, 0.05, 0.05, 1.00),
        'date_border':  (0.78, 0.82, 0.87, 0.95),
        'inset_bg':     (0.74, 0.77, 0.80, 0.90),
        'inset_fg':     (0.08, 0.08, 0.10),
        'inset_border': (0.50, 0.53, 0.57, 0.95),
    },
    'Light': {
        'face':         (0.95, 0.95, 0.92, 0.92),
        'border':       (0,    0,    0,    0.40),
        'hour_marks':   (0.15, 0.35, 0.65),
        'min_marks':    (0.50, 0.50, 0.50),
        'hour_hand':    (0.53, 0.81, 0.98, 0.97),
        'min_hand':     (0.10, 0.10, 0.10, 0.90),
        'hands':        (0.10, 0.10, 0.10, 0.95),
        'second':       (0.80, 0.10, 0.10, 1.00),
        'digital':      (0.15, 0.45, 0.25),
        'marks':        (0,    0,    0),
        'date_bg':      (0.00, 0.00, 0.00, 0.97),
        'date_fg':      (1.00, 1.00, 1.00, 1.00),
        'date_border':  (0.30, 0.30, 0.30, 0.95),
        'inset_bg':     (0.18, 0.02, 0.02, 0.88),
        'inset_fg':     (1.00, 0.35, 0.35),
        'inset_border': (0.78, 0.82, 0.87, 0.95),
    },
    'Clear': {
        'face':         (0,    0,    0,    0.00),
        'border':       (1,    1,    1,    0.60),
        'hour_marks':   (0.95, 0.85, 0.50),
        'min_marks':    (0.55, 0.70, 0.95),
        'hour_hand':    (0.53, 0.81, 0.98, 0.97),
        'min_hand':     (1,    1,    1,    0.95),
        'hands':        (1,    1,    1,    0.95),
        'second':       (0.96, 0.32, 0.22, 1.00),
        'digital':      (0.60, 0.95, 0.70),
        'marks':        (1,    1,    1),
        'date_bg':      (0.96, 0.96, 0.94, 0.97),
        'date_fg':      (0.05, 0.05, 0.05, 1.00),
        'date_border':  (0.78, 0.82, 0.87, 0.95),
        'inset_bg':     (0.18, 0.02, 0.02, 0.88),
        'inset_fg':     (1.00, 0.35, 0.35),
        'inset_border': (0.78, 0.82, 0.87, 0.95),
    },
}

SIZES         = {'Small': 160, 'Medium': 260, 'Large': 360, 'XLarge': 480}
MODES         = ['Analog', 'Digital', 'Both']
MARKER_STYLES = ['Marks', 'Numbers', 'Roman']
EYE_STYLES    = ['x11', 'disney', 'authentic human', 'puppy dog', 'marvel']
HAND_STYLES   = ['Classic', 'Deco', 'Modern']
ROMAN         = ['I','II','III','IV','V','VI','VII','VIII','IX','X','XI','XII']
DIGITAL_STYLES = ['Font', '7 Seg', 'Split']
DIGITAL_FONTS  = ['DejaVu Sans Bold', 'DejaVu Sans Mono Bold',
                   'Ubuntu Bold', 'Liberation Sans Bold', 'FreeMono Bold']
SEG_PATTERNS   = {
    '0':'abcdef', '1':'bc',      '2':'abdeg',  '3':'abcdg',
    '4':'bcfg',   '5':'acdfg',   '6':'acdefg', '7':'abc',
    '8':'abcdefg','9':'abcdfg',  ':'  :':',    ' ':'',
}

# ─── Clock window (one per monitor) ──────────────────────────────────

class ClockWindow(Gtk.Window):
    def __init__(self, manager, monitor_idx, geom, state):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.manager      = manager
        self.monitor_idx  = monitor_idx
        self.monitor_geom = geom

        # Visual settings
        theme_name = state.get('theme_name', 'Dark')
        if theme_name == 'Minimal': theme_name = 'Clear'
        self.size          = SIZES.get(state.get('size_name', 'Medium'), SIZES['Medium'])
        self.theme         = THEMES.get(theme_name, THEMES['Dark'])
        self.opacity_level = float(state.get('opacity', 1.0))
        self.mode          = state.get('mode', 'Analog')
        self.show_seconds  = bool(state.get('show_seconds', True))
        self.show_date     = bool(state.get('show_date', True))
        self.show_eyes     = bool(state.get('show_eyes', False))
        _es                = state.get('eye_style', 'x11')
        self.eye_style     = _es if _es in EYE_STYLES else 'x11'
        self.marker_style  = state.get('marker_style', 'Marks')
        self.hand_style    = state.get('hand_style', 'Classic')
        self.digital_style = state.get('digital_style', 'Font')
        self.digital_font  = state.get('digital_font',  'DejaVu Sans Bold')

        # Window state
        saved_mode = state.get('window_mode', 'normal')
        self.window_mode  = 'normal' if saved_mode not in ('normal', 'hidden') else saved_mode
        self._drag_offset = None
        self._resizing    = False
        self._saved_pos   = (state['x'], state['y']) if 'x' in state else None

        # Eye animation state
        self._eye_angle       = 0.0
        self._eye_travel      = 0.0
        self._eye_tgt_angle   = random.uniform(-math.pi, math.pi)
        self._eye_tgt_travel  = random.uniform(0.2, 0.5)
        self._eye_blink       = 0.0
        self._eye_blink_phase = 'idle'
        self._eye_frames_left = random.randint(80, 160)
        self._eye_move_frames = random.randint(40, 100)
        self._eye_timer_id    = None

        # Fade state machine (v1.2.0, HLT desktop-clock #330)
        # idle → fading_out (10s) → faded (T min) → fading_in (2 min) → idle
        # Double-click on clock body (not bell/hourglass) triggers from idle.
        # Double-click during fading_out cancels and restores baseline.
        # Faded + fading_in are click-through ON; back to baseline = OFF.
        # Alarm/timer fire during cycle → fast-fade-in (2.5s) and exit cycle.
        # v1.2.1 (#482): wait minutes is captured at fade_start so changes
        # to the manager setting mid-cycle don't affect in-flight cycles.
        self._fade_state             = 'idle'
        self._fade_baseline_opacity  = None
        self._fade_baseline_click_t  = None  # user's click-through state pre-fade
        self._fade_state_start_ms    = 0
        self._fade_wait_min_snapshot = None

        self._build_window()
        self.props.opacity = 1.0 if self.theme is THEMES['Clear'] else self.opacity_level

        if self.show_eyes:
            GLib.idle_add(self._start_eye_timer)
        if self.window_mode == 'hidden':
            GLib.idle_add(lambda: self.set_window_mode('hidden') or False)

    # ── Window construction ───────────────────────────────────────────

    def _build_window(self):
        self.set_title(f'Clock-{self.monitor_idx}')
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)
        self.set_resizable(False)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        cw, ch = self._clock_size()
        self.set_default_size(cw, ch)

        g = self.monitor_geom
        if self._saved_pos:
            self.move(*self._saved_pos)
        else:
            self.move(g.x + g.width - cw - 20, g.y + 20)

        self.connect('configure-event', self._on_configure)
        self.connect('destroy', lambda _: self._save_all())

        self.da = Gtk.DrawingArea()
        self.da.connect('draw', self._draw)
        self.da.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK |
            Gdk.EventMask.ENTER_NOTIFY_MASK |
            Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.da.connect('button-press-event',   self._on_button_press)
        self.da.connect('button-release-event', self._on_button_release)
        self.da.connect('motion-notify-event',  self._on_motion)
        self.da.connect('enter-notify-event',   self._on_hover_enter)
        self.da.connect('leave-notify-event',   self._on_hover_leave)
        self.connect('key-press-event', self._on_key_press)
        self._hover_overlay = TimerHoverOverlay()

        child = self.get_child()
        if child: self.remove(child)
        self.add(self.da)
        self.show_all()
        GLib.idle_add(self._apply_window_size)

    # ── Window mode ───────────────────────────────────────────────────

    def set_window_mode(self, mode):
        self.window_mode = mode

        if mode == 'hidden':
            self._save_all()
            self.hide()
        elif mode == 'normal':
            self.show()
            self.queue_draw()
            self._save_all()

        return False

    # ── Persistence ───────────────────────────────────────────────────

    def _save_all(self, x=None, y=None):
        if x is None:
            if self._saved_pos: x, y = self._saved_pos
            else: x, y = self.get_position()

        size_name  = next((k for k, v in SIZES.items()  if v == self.size),  'Medium')
        theme_name = next((k for k, v in THEMES.items() if v is self.theme), 'Dark')
        data = {'x': x, 'y': y, 'size_name': size_name, 'theme_name': theme_name,
                'opacity': self.opacity_level, 'mode': self.mode,
                'show_seconds': self.show_seconds, 'show_date': self.show_date,
                'show_eyes': self.show_eyes, 'eye_style': self.eye_style,
                'marker_style': self.marker_style, 'hand_style': self.hand_style,
                'digital_style': self.digital_style, 'digital_font': self.digital_font,
                'window_mode': self.window_mode}
        self.manager.save_window_state(self, data)

    def _on_configure(self, window, event):
        if not self._resizing and self.window_mode == 'normal':
            self._saved_pos = (event.x, event.y)
            self._save_all(event.x, event.y)
        if not self._resizing:
            self._sync_monitor_geom()

    # ── Input handling ────────────────────────────────────────────────

    def _on_hover_enter(self, widget, event):
        self._update_hover_overlay()

    def _on_hover_leave(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            self._hover_overlay.hide()

    def _update_hover_overlay(self):
        now_ts = datetime.now().timestamp()
        active_timers = [t for t in self.manager.timers if t['state'] != 'completed' or
                         (t['completed_at'] and now_ts - t['completed_at'] < 5)]
        enabled_alarms = [a for a in load_alarms() if a.get('enabled')]
        if not active_timers and not enabled_alarms:
            self._hover_overlay.hide(); return
        x, y = self.get_position()
        w, h = self.get_size()
        screen_h = self.monitor_geom.y + self.monitor_geom.height
        self._hover_overlay.update(self.manager.timers, x, y, w, h, screen_h,
                                   alarms=enabled_alarms)

    def _on_key_press(self, widget, event):
        pass

    def _on_button_press(self, widget, event):
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and event.button == 1:
            ex, ey = event.x, event.y
            if hasattr(self, '_bell_draw_pos'):
                bx, by = self._bell_draw_pos
                if math.hypot(ex - bx, ey - by) < self._bell_hit_r:
                    self.manager.open_alarm_manager(self); return
            if hasattr(self, '_hg_draw_pos'):
                hx, hy = self._hg_draw_pos
                if math.hypot(ex - hx, ey - hy) < self._hg_hit_r:
                    self.manager.open_timer_manager(self); return
            # v1.2.0 #330 fade-to-clear cycle: double-click on the
            # clock body (not bell/hourglass) triggers the fade. A
            # second double-click during fading_out cancels and
            # restores. During faded/fading_in the clock is click-
            # through so this handler doesn't fire — the tray menu's
            # Restore clock handles it instead.
            if self._fade_state == 'fading_out':
                self.fade_cancel()
            elif self._fade_state == 'idle':
                self.fade_start()
            return
        if event.button == 1 and self.window_mode == 'normal':
            wx, wy = self.get_position()
            self._drag_offset = (event.x_root - wx, event.y_root - wy)
        elif event.button == 3:
            self._show_menu(event)

    def _on_button_release(self, widget, event):
        if event.button == 1:
            self._drag_offset = None

    def _on_motion(self, widget, event):
        if self._drag_offset:
            ox, oy = self._drag_offset
            l, t, r, b = self._constraint_rect()
            cw, ch = self._clock_size()
            nx = max(l, min(int(event.x_root - ox), r - cw))
            ny = max(t, min(int(event.y_root - oy), b - ch))
            self.move(nx, ny)
        else:
            self.queue_draw()

    # ── Right-click menu ──────────────────────────────────────────────

    def _show_menu(self, event):
        menu = Gtk.Menu()

        hide_item = Gtk.CheckMenuItem(label='Hide clock')
        hide_item.set_active(self.window_mode == 'hidden')
        hide_item.connect('activate', lambda i: self.set_window_mode('hidden' if i.get_active() else 'normal'))
        menu.append(hide_item)

        alarms_item = Gtk.MenuItem(label='Alarms…')
        alarms_item.connect('activate', lambda _: self.manager.open_alarm_manager(self))
        menu.append(alarms_item)

        timers_item = Gtk.MenuItem(label='Timers…')
        timers_item.connect('activate', lambda _: self.manager.open_timer_manager(self))
        menu.append(timers_item)

        menu.append(Gtk.SeparatorMenuItem())

        mode_item = Gtk.MenuItem(label='Display')
        mode_sub  = Gtk.Menu()
        for name in MODES:
            item = Gtk.CheckMenuItem(label=name)
            item.set_active(name == self.mode)
            item.connect('activate', self._set_mode, name)
            mode_sub.append(item)
        mode_item.set_submenu(mode_sub)
        menu.append(mode_item)

        if self.mode == 'Digital':
            style_item = Gtk.MenuItem(label='Style')
            style_sub  = Gtk.Menu()
            for sname in DIGITAL_STYLES:
                si = Gtk.CheckMenuItem(label=sname)
                si.set_active(sname == self.digital_style)
                si.connect('activate', self._set_digital_style, sname)
                style_sub.append(si)
            style_item.set_submenu(style_sub)
            menu.append(style_item)

            if self.digital_style == 'Font':
                font_item = Gtk.MenuItem(label='Font')
                font_sub  = Gtk.Menu()
                for fname in DIGITAL_FONTS:
                    fi = Gtk.CheckMenuItem(label=fname)
                    fi.set_active(fname == self.digital_font)
                    fi.connect('activate', self._set_digital_font, fname)
                    font_sub.append(fi)
                font_item.set_submenu(font_sub)
                menu.append(font_item)

        secs_item = Gtk.CheckMenuItem(label='Show seconds')
        secs_item.set_active(self.show_seconds)
        secs_item.connect('activate', self._toggle_seconds)
        menu.append(secs_item)

        date_item = Gtk.CheckMenuItem(label='Show date')
        date_item.set_active(self.show_date)
        date_item.connect('activate', self._toggle_date)
        menu.append(date_item)

        eyes_item = Gtk.CheckMenuItem(label='Show eyes')
        eyes_item.set_active(self.show_eyes)
        eyes_item.connect('activate', self._toggle_eyes)
        menu.append(eyes_item)

        eye_style_item = Gtk.MenuItem(label='Eye style')
        eye_style_sub  = Gtk.Menu()
        for _es in EYE_STYLES:
            _ei = Gtk.CheckMenuItem(label=_es.title())
            _ei.set_active(_es == self.eye_style)
            _ei.connect('activate', self._set_eye_style, _es)
            eye_style_sub.append(_ei)
        eye_style_item.set_submenu(eye_style_sub)
        menu.append(eye_style_item)

        mid_item = Gtk.CheckMenuItem(label='Show monitor ID')
        mid_item.set_active(self.manager.show_monitor_id)
        mid_item.connect('activate', lambda i: self.manager.set_show_monitor_id(i.get_active()))
        menu.append(mid_item)

        if self.mode in ('Analog', 'Both'):
            marker_item = Gtk.MenuItem(label='Hour markers')
            marker_sub  = Gtk.Menu()
            for name in MARKER_STYLES:
                item = Gtk.CheckMenuItem(label=name)
                item.set_active(name == self.marker_style)
                item.connect('activate', self._set_marker_style, name)
                marker_sub.append(item)
            marker_item.set_submenu(marker_sub)
            menu.append(marker_item)

            hand_item = Gtk.MenuItem(label='Hand style')
            hand_sub  = Gtk.Menu()
            for name in HAND_STYLES:
                item = Gtk.CheckMenuItem(label=name)
                item.set_active(name == self.hand_style)
                item.connect('activate', self._set_hand_style, name)
                hand_sub.append(item)
            hand_item.set_submenu(hand_sub)
            menu.append(hand_item)

        size_item = Gtk.MenuItem(label='Size')
        size_sub  = Gtk.Menu()
        for name, px in SIZES.items():
            item = Gtk.CheckMenuItem(label=f'{name}  ({px}px)')
            item.set_active(px == self.size)
            item.connect('activate', self._set_size, px)
            size_sub.append(item)
        size_item.set_submenu(size_sub)
        menu.append(size_item)

        theme_item = Gtk.MenuItem(label='Theme')
        theme_sub  = Gtk.Menu()
        for name, t in THEMES.items():
            item = Gtk.CheckMenuItem(label=name)
            item.set_active(t is self.theme)
            item.connect('activate', self._set_theme, t)
            theme_sub.append(item)
        theme_item.set_submenu(theme_sub)
        menu.append(theme_item)

        snap_item = Gtk.MenuItem(label='Snap to corner')
        snap_sub  = Gtk.Menu()
        for label, pos in [('Top right','tr'),('Top left','tl'),
                            ('Bottom right','br'),('Bottom left','bl')]:
            item = Gtk.MenuItem(label=label)
            item.connect('activate', self._snap, pos)
            snap_sub.append(item)
        snap_item.set_submenu(snap_sub)
        menu.append(snap_item)

        if self.theme is not THEMES['Clear']:
            opacity_item = Gtk.MenuItem(label='Opacity')
            opacity_sub  = Gtk.Menu()
            for label, val in [('100%',1.0),('90%',0.9),('80%',0.8),('70%',0.7),
                                ('60%',0.6),('50%',0.5),('40%',0.4),('30%',0.3),
                                ('20%',0.2),('10%',0.1)]:
                item = Gtk.CheckMenuItem(label=label)
                item.set_active(abs(val - self.opacity_level) < 0.05)
                item.connect('activate', self._set_opacity, val)
                opacity_sub.append(item)
            opacity_item.set_submenu(opacity_sub)
            menu.append(opacity_item)

        menu.append(Gtk.SeparatorMenuItem())
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        self._append_tracker_menu_items(menu, show_reset=shift)

        menu.show_all()
        menu.popup_at_pointer(event)

    def _toggle_seconds(self, item):
        self.show_seconds = item.get_active()
        if self.mode == 'Digital': self._apply_window_size()
        self._save_all(); self.queue_draw()

    def _toggle_date(self, item):
        self.show_date = item.get_active()
        if self.mode == 'Digital': self._apply_window_size()
        self._save_all(); self.queue_draw()

    def _toggle_eyes(self, item):
        self.show_eyes = item.get_active()
        if self.show_eyes:
            self._start_eye_timer()
        if self.mode == 'Digital': self._apply_window_size()
        self._save_all(); self.queue_draw()

    def _set_eye_style(self, item, name):
        if item.get_active():
            self.eye_style = name
            self._save_all()
            self.queue_draw()

    def _set_marker_style(self, item, name):
        if item.get_active(): self.marker_style = name; self._save_all(); self.queue_draw()

    def _set_hand_style(self, item, name):
        if item.get_active(): self.hand_style = name; self._save_all(); self.queue_draw()

    def _set_mode(self, item, name):
        if not item.get_active(): return
        old_cw, old_ch = self._clock_size()
        wx, wy = self.get_position()
        self.mode = name
        self._apply_window_size()
        new_cw, new_ch = self._clock_size()
        nx, ny = self._edge_anchored_pos(wx, wy, old_cw, old_ch, new_cw, new_ch)
        self.move(nx, ny)
        self._saved_pos = (nx, ny)
        self._save_all(nx, ny); self.queue_draw()

    def _set_digital_style(self, item, style):
        if not item.get_active(): return
        old_cw, old_ch = self._clock_size()
        wx, wy = self.get_position()
        self.digital_style = style
        if self.mode == 'Digital':
            self._apply_window_size()
            new_cw, new_ch = self._clock_size()
            nx, ny = self._edge_anchored_pos(wx, wy, old_cw, old_ch, new_cw, new_ch)
            self.move(nx, ny); self._saved_pos = (nx, ny)
        self._save_all(); self.queue_draw()

    def _set_digital_font(self, item, font):
        if not item.get_active(): return
        old_cw, old_ch = self._clock_size()
        wx, wy = self.get_position()
        self.digital_font = font
        if self.mode == 'Digital' and self.digital_style == 'Font':
            self._apply_window_size()
            new_cw, new_ch = self._clock_size()
            nx, ny = self._edge_anchored_pos(wx, wy, old_cw, old_ch, new_cw, new_ch)
            self.move(nx, ny); self._saved_pos = (nx, ny)
        self._save_all(); self.queue_draw()

    def _set_size(self, item, px):
        if not item.get_active(): return
        old_cw, old_ch = self._clock_size()
        wx, wy = self.get_position()
        self.size = px
        self._resizing = True
        self._apply_window_size()
        new_cw, new_ch = self._clock_size()
        nx, ny = self._edge_anchored_pos(wx, wy, old_cw, old_ch, new_cw, new_ch)
        self.move(nx, ny)
        GLib.idle_add(self._finish_resize, nx, ny)

    def _finish_resize(self, nx, ny):
        self.move(nx, ny)
        self._sync_monitor_geom()
        self.apply_click_through(self.manager.click_through)
        self._resizing = False
        self._saved_pos = (nx, ny)
        self._save_all(nx, ny)
        return False

    def _set_theme(self, item, theme):
        if item.get_active():
            self.theme = theme
            self.props.opacity = 1.0 if theme is THEMES['Clear'] else self.opacity_level
            self._save_all(); self.queue_draw()

    def _set_opacity(self, item, val):
        if item.get_active():
            self.opacity_level = val; self.props.opacity = val; self._save_all()

    # ── Geometry helpers ──────────────────────────────────────────────

    def _digital_content_size(self):
        if self.digital_style == '7 Seg': return self._7seg_content_size()
        if self.digital_style == 'Split': return self._split_content_size()
        return self._font_content_size()

    def _font_content_size(self):
        """Measure the font-style digital box using self.size as reference."""
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.size, self.size)
        cr   = cairo.Context(surf)
        now  = datetime.now()
        time_fmt  = '%H:%M:%S' if self.show_seconds else '%H:%M'
        time_str  = now.strftime(time_fmt)
        date_str  = now.strftime('%a %d %b')
        time_size = self._fit_font_size(cr, time_str, self.size * 0.88)
        date_size = max(8, time_size // 3)
        tw, th    = self._text_size(cr, time_str, time_size)
        dw, dh    = self._text_size(cr, date_str,  date_size)
        n      = self._active_alarm_count()
        show_d = self.show_date
        pad_x  = tw * 0.10; pad_y = th * 0.55
        g      = th * 0.18
        bs     = max(14, th * 0.55) if n > 0 else 0
        eye_r  = th * 0.27 if self.show_eyes else 0.0
        rows_h = th
        if n > 0:          rows_h += g + bs
        if self.show_eyes: rows_h += g + eye_r * 2.0
        if show_d:         rows_h += g + dh
        box_w = min(max(tw, dw if show_d else tw) + pad_x * 2, self.size - 16)
        box_h = rows_h + pad_y * 2
        return int(box_w) + 4, int(box_h) + 4

    # ── 7-segment layout helpers ──────────────────────────────────────

    def _7seg_layout(self):
        """Return (dh, dw, cw, igap, total_w) for current settings."""
        time_str = '00:00:00' if self.show_seconds else '00:00'
        n_dig    = sum(1 for c in time_str if c.isdigit())
        n_col    = time_str.count(':')
        max_w    = self.size * 0.90
        for dh in range(self.size, 6, -1):
            dw   = dh * 0.55
            cw   = dw * 0.40
            igap = max(1.0, dw * 0.10)
            tw   = n_dig * dw + n_col * cw + (n_dig + n_col - 1) * igap
            if tw <= max_w:
                return dh, dw, cw, igap, tw
        return 20, 11.0, 4.4, 1.1, 60.0

    def _7seg_content_size(self):
        dh, dw, cw, igap, total_w = self._7seg_layout()
        pad_x = max(4.0, total_w * 0.04)
        pad_y = max(28, dh * 0.55)
        box_h = dh + pad_y * 2
        if self.show_eyes: box_h += dh * 0.22 * 2.0 + igap * 2
        if self.show_date: box_h += max(6, dh * 0.28) + igap * 2
        return int(total_w + pad_x * 2) + 4, int(box_h) + 4

    # ── Split-flap layout helpers ─────────────────────────────────────

    def _split_layout(self):
        """Return (th, tw, t_gap, total_w) for current settings."""
        time_str = '00:00:00' if self.show_seconds else '00:00'
        n_chars  = len(time_str)
        max_w    = self.size * 0.94
        for th in range(self.size, 6, -1):
            tw    = th * 0.70
            t_gap = max(2.0, th * 0.05)
            total = n_chars * tw + (n_chars - 1) * t_gap
            if total <= max_w:
                return th, tw, t_gap, total
        return 20, 14.0, 1.0, 80.0

    def _split_content_size(self):
        th, tw, t_gap, total_w = self._split_layout()
        pad_x      = max(4.0, tw * 0.30)
        pad_y      = max(8,   th * 0.12)
        icon_strip = max(28,  th * 0.40)
        box_w = total_w + pad_x * 2
        box_h = th + pad_y * 2 + icon_strip
        if self.show_date:
            th2    = max(6, th * 0.50)
            tw2    = th2 * 0.70
            t_gap2 = max(1.0, th2 * 0.05)
            date_w = 10 * tw2 + 9 * t_gap2   # 'Fri 26 Apr' = 10 chars
            box_w  = max(box_w, date_w + pad_x * 2)
            box_h += th2 + t_gap * 2
        return int(box_w) + 4, int(box_h) + 4

    def _clock_size(self):
        """Return (width, height) of the clock window for the current mode."""
        if self.mode == 'Digital':
            return self._digital_content_size()
        return self.size, self.size

    def _current_monitor(self):
        """Return the GDK monitor the window is actually positioned on, by coordinates.
        Avoids trusting self.monitor_idx, which can become stale after sleep/wake when
        GDK re-enumerates monitors and their order changes."""
        display = Gdk.Display.get_default()
        wx, wy = self.get_position()
        cw, ch = self._clock_size()
        cx, cy = wx + cw // 2, wy + ch // 2
        for i in range(display.get_n_monitors()):
            g = display.get_monitor(i).get_geometry()
            if g.x <= cx < g.x + g.width and g.y <= cy < g.y + g.height:
                return display.get_monitor(i)
        # Window centre not inside any monitor — return closest by centre distance
        best, best_dist = display.get_monitor(0), float('inf')
        for i in range(display.get_n_monitors()):
            m = display.get_monitor(i)
            g = m.get_geometry()
            d = math.hypot(cx - (g.x + g.width // 2), cy - (g.y + g.height // 2))
            if d < best_dist:
                best, best_dist = m, d
        return best

    def _sync_monitor_geom(self):
        """Update monitor_geom and monitor_idx to match where the window actually is.
        Called on configure events and on monitors-changed so eyes tracking and
        constraint rects are always correct."""
        display = Gdk.Display.get_default()
        mon     = self._current_monitor()
        self.monitor_geom = mon.get_geometry()
        for i in range(display.get_n_monitors()):
            if display.get_monitor(i) == mon:
                self.monitor_idx = i
                break

    def _constraint_rect(self):
        """Return (left, top, right, bottom) of the draggable area for this monitor."""
        try:
            mon = self._current_monitor()
            wa  = mon.get_workarea()
            px_per_mm = mon.get_geometry().width / max(mon.get_width_mm(), 1)
            gap = max(8, round(5 * px_per_mm))
            return (wa.x + gap, wa.y + gap,
                    wa.x + wa.width  - gap,
                    wa.y + wa.height - gap)
        except Exception:
            g = self.monitor_geom
            gap = 40
            return (g.x + gap, g.y + gap,
                    g.x + g.width  - gap,
                    g.y + g.height - gap)

    def _apply_window_size(self):
        """Resize the window to match the current mode's content dimensions."""
        cw, ch = self._clock_size()
        self.set_size_request(cw, ch)
        self.resize(cw, ch)
        return False   # safe to use as GLib.idle_add callback

    def _edge_anchored_pos(self, wx, wy, old_cw, old_ch, new_cw, new_ch):
        """Return new (x, y) for a resized window, anchoring to the nearer constraint edge.

        For each axis independently: if the clock is closer to the left/top boundary,
        keep that edge fixed; if closer to right/bottom, keep that edge fixed; if
        exactly equidistant (genuinely centred), keep the centre.  No arbitrary
        percentage threshold — works correctly for any clock size on any monitor.
        """
        l, t, r, b = self._constraint_rect()
        left_dist  = wx - l
        right_dist = r  - (wx + old_cw)
        top_dist   = wy - t
        bot_dist   = b  - (wy + old_ch)
        if   left_dist  < right_dist: nx = wx
        elif right_dist < left_dist:  nx = wx + old_cw - new_cw
        else:                         nx = wx + old_cw // 2 - new_cw // 2
        if   top_dist   < bot_dist:   ny = wy
        elif bot_dist   < top_dist:   ny = wy + old_ch - new_ch
        else:                         ny = wy + old_ch // 2 - new_ch // 2
        return max(l, min(nx, r - new_cw)), max(t, min(ny, b - new_ch))

    def _snap(self, item, pos):
        l, t, r, b = self._constraint_rect()
        cw, ch = self._clock_size()
        if   pos == 'tl': nx, ny = l,      t
        elif pos == 'tr': nx, ny = r - cw, t
        elif pos == 'bl': nx, ny = l,      b - ch
        else:             nx, ny = r - cw, b - ch
        self.move(nx, ny)
        self._saved_pos = (nx, ny)
        self._save_all(nx, ny)

    # ── Drawing ───────────────────────────────────────────────────────

    def _active_alarm_count(self):
        return sum(1 for a in load_alarms() if a.get('enabled'))

    def _draw(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()

        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        now = datetime.now()
        if self.mode == 'Analog':
            self._draw_analog(cr, w, h, now)
        elif self.mode == 'Digital':
            self._draw_digital(cr, w, h, now)
        elif self.mode == 'Both':
            self._draw_analog(cr, w, h, now, digital_inset=True)

    def _id_mark_color(self, r, g, b):
        """Return a warm-tinted version of (r,g,b) for the monitor ID marker."""
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        d   = 0.18 + lum * 0.15          # 0.18–0.33 depending on brightness
        return (min(1.0, r + d), min(1.0, g + d * 0.25), max(0.0, b - d * 0.65))

    def _id_badge_colors(self, face_rgba):
        """Return (fill_rgba, text_rgba) slightly offset from face — subtle badge tones."""
        r, g, b, a = face_rgba
        if a < 0.1:  # Clear/transparent theme — use neutral
            return (0.50, 0.50, 0.50, 0.40), (0.72, 0.72, 0.72, 0.80)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if lum < 0.5:  # dark — lighten
            fill = (min(1, r+0.11), min(1, g+0.11), min(1, b+0.11), 0.80)
            text = (min(1, r+0.24), min(1, g+0.24), min(1, b+0.24), 0.92)
        else:          # light — darken
            fill = (max(0, r-0.11), max(0, g-0.11), max(0, b-0.11), 0.80)
            text = (max(0, r-0.24), max(0, g-0.24), max(0, b-0.24), 0.92)
        return fill, text

    def _draw_bell(self, cr, x, y, size, rgba, rotate=0, count=0):
        cr.save()
        cr.translate(x, y); cr.rotate(rotate)
        s = size / 2.0
        cr.set_source_rgba(*rgba)
        cr.new_sub_path()
        cr.arc(0, -s * 0.2, s * 0.65, math.pi, 0)
        cr.line_to(s * 0.65, s * 0.45); cr.line_to(s, s * 0.55)
        cr.line_to(-s, s * 0.55); cr.line_to(-s * 0.65, s * 0.45)
        cr.close_path(); cr.fill()
        cr.new_sub_path()
        cr.arc(0, s * 0.72, s * 0.14, 0, 2 * math.pi); cr.fill()
        if count > 0:
            cr.rotate(-rotate)
            layout = PangoCairo.create_layout(cr)
            layout.set_text(str(count), -1)
            font_size = max(6, int(size * 0.38))
            layout.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {font_size}'))
            lw, lh = layout.get_pixel_size()
            cr.move_to(-lw / 2, -lh / 2 - s * 0.05)
            cr.set_source_rgba(1, 1, 1, 1)
            PangoCairo.show_layout(cr, layout)
        cr.restore()

    def _draw_hourglass(self, cr, x, y, size, rgba, count=0):
        cr.save()
        cr.translate(x, y)
        col_r, col_g, col_b, col_a = rgba
        s   = size / 2.0
        w   = s * 0.56    # half-width at widest point
        p   = s * 0.09    # pinch half-width
        tv  = s * 0.82    # body top/bottom y (leaves room for cap)
        bx  = s * 0.30    # bezier bow — controls outward bulge
        cap = s - tv      # cap height
        # Hourglass body: bezier curves giving smooth "8" outline
        cr.move_to(-w, -tv)
        cr.line_to(w, -tv)                                          # flat top edge
        cr.curve_to( w + bx, -tv * 0.6,  p + bx, -tv * 0.05, p, 0)  # right upper
        cr.curve_to( p + bx,  tv * 0.05, w + bx,  tv * 0.6, w, tv)  # right lower
        cr.line_to(-w, tv)                                          # flat bottom edge
        cr.curve_to(-w - bx,  tv * 0.6, -p - bx,  tv * 0.05, -p, 0) # left lower
        cr.curve_to(-p - bx, -tv * 0.05,-w - bx, -tv * 0.6, -w,-tv) # left upper
        cr.close_path()
        cr.set_source_rgba(col_r, col_g, col_b, col_a); cr.fill_preserve()
        cr.set_source_rgba(col_r * 0.50, col_g * 0.50, col_b * 0.50, col_a * 0.85)
        cr.set_line_width(max(0.7, size * 0.035)); cr.stroke()
        # Top and bottom caps (flat bars extending slightly wider than body)
        for yy in (-s, s - cap * 2):
            cr.rectangle(-w * 1.18, yy, w * 2.36, cap * 2)
            cr.set_source_rgba(col_r * 0.82, col_g * 0.82, col_b * 0.82, col_a); cr.fill()
        if count > 0:
            layout = PangoCairo.create_layout(cr)
            font_size = max(6, int(size * 0.38))
            layout.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {font_size}'))
            layout.set_text(str(count), -1)
            lw2, lh = layout.get_pixel_size()
            cr.move_to(-lw2 / 2, -lh / 2)
            cr.set_source_rgba(1, 1, 1, 1)
            PangoCairo.show_layout(cr, layout)
        cr.restore()

    # ── Eye animation ─────────────────────────────────────────────────

    def _start_eye_timer(self):
        if not self._eye_timer_id:
            self._eye_timer_id = GLib.timeout_add(50, self._eye_tick)
        return False  # safe as GLib.idle_add callback

    def _eye_tick(self):
        if not self.show_eyes:
            self._eye_timer_id = None
            return False
        self._update_eye_idle()
        if self.get_visible() and self.window_mode != 'hidden':
            self.queue_draw()
        return True

    def _update_eye_idle(self):
        """Update eye animation state — blink always; idle drift when mouse not on this monitor."""
        _, mx, my = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
        g = self.monitor_geom
        mouse_here = g.x <= mx < g.x + g.width and g.y <= my < g.y + g.height

        # Blink state machine (runs regardless of mouse position)
        if self._eye_blink_phase == 'idle':
            self._eye_frames_left -= 1
            if self._eye_frames_left <= 0:
                self._eye_blink_phase = 'closing'
        elif self._eye_blink_phase == 'closing':
            self._eye_blink = min(1.0, self._eye_blink + 0.28)
            if self._eye_blink >= 1.0:
                self._eye_blink_phase = 'closed'
                self._eye_frames_left = 2
        elif self._eye_blink_phase == 'closed':
            self._eye_frames_left -= 1
            if self._eye_frames_left <= 0:
                self._eye_blink_phase = 'opening'
        elif self._eye_blink_phase == 'opening':
            self._eye_blink = max(0.0, self._eye_blink - 0.28)
            if self._eye_blink <= 0.0:
                self._eye_blink_phase = 'idle'
                self._eye_frames_left = random.randint(80, 180)  # 4-9 s at 20 fps

        if mouse_here:
            return

        # Lazy idle drift — slowly wander toward a random target
        da = self._eye_tgt_angle - self._eye_angle
        while da >  math.pi: da -= 2 * math.pi
        while da < -math.pi: da += 2 * math.pi
        self._eye_angle  += da  * 0.06
        self._eye_travel += (self._eye_tgt_travel - self._eye_travel) * 0.06

        self._eye_move_frames -= 1
        if self._eye_move_frames <= 0:
            self._eye_tgt_angle   = random.uniform(-math.pi, math.pi)
            self._eye_tgt_travel  = random.uniform(0.15, 0.65)
            self._eye_move_frames = random.randint(40, 120)  # 2-6 s between drifts

    def _draw_eyes(self, cr, ecx, ecy, eye_r):
        spacing = eye_r * 2.2
        wx, wy  = self.get_position()
        _, mx, my = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
        g = self.monitor_geom
        mouse_here = g.x <= mx < g.x + g.width and g.y <= my < g.y + g.height
        open_frac  = max(0.04, 1.0 - self._eye_blink)

        for sign in (-1, 1):
            ox = ecx + sign * spacing / 2
            oy = ecy
            if mouse_here:
                angle = math.atan2(my - (wy + oy), mx - (wx + ox))
                tfrac = min(1.0, math.hypot(mx - (wx + ox), my - (wy + oy)) / (eye_r * 3.0))
            else:
                angle = self._eye_angle
                tfrac = self._eye_travel
            style = self.eye_style
            _dispatch = {
                'disney':          self._draw_eye_disney,
                'authentic human': self._draw_eye_human,
                'puppy dog':       self._draw_eye_puppy,
                'marvel':          self._draw_eye_marvel,
            }
            _dispatch.get(style, self._draw_eye_x11)(
                cr, ox, oy, eye_r, open_frac, angle, tfrac)

    def _draw_eye_x11(self, cr, ox, oy, eye_r, open_frac, angle, tfrac):
        sx = 0.76
        cr.new_path()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(sx, open_frac)
        cr.arc(0, 0, eye_r, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(0.96, 0.96, 0.94, 0.93)
        cr.fill_preserve()
        cr.set_source_rgba(0.22, 0.22, 0.22, 0.72)
        cr.set_line_width(max(0.8, eye_r * 0.06))
        cr.stroke()
        if open_frac > 0.55:
            p_alpha = min(1.0, (open_frac - 0.55) / 0.45)
            pupil_r = eye_r * 0.40
            px = ox + (eye_r * sx - pupil_r) * 0.88 * tfrac * math.cos(angle)
            py = oy + (eye_r      - pupil_r) * 0.88 * tfrac * math.sin(angle)
            cr.new_path()
            cr.arc(px, py, pupil_r, 0, 2 * math.pi)
            cr.set_source_rgba(0.08, 0.08, 0.10, 0.96 * p_alpha)
            cr.fill()
            cr.new_path()
            cr.arc(px - pupil_r * 0.28, py - pupil_r * 0.32, pupil_r * 0.28, 0, 2 * math.pi)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.55 * p_alpha)
            cr.fill()

    def _draw_eye_disney(self, cr, ox, oy, eye_r, open_frac, angle, tfrac):
        """Large round eyes, massive iris, classic Disney sparkle."""
        sx      = 0.88
        iris_r  = eye_r * 0.64

        def _sclera():
            cr.new_path(); cr.save()
            cr.translate(ox, oy); cr.scale(sx, open_frac)
            cr.arc(0, 0, eye_r, 0, 2 * math.pi); cr.restore()

        _sclera()
        cr.set_source_rgba(0.98, 0.97, 0.96, 0.97); cr.fill_preserve()
        cr.set_source_rgba(0.10, 0.06, 0.04, 0.95)
        cr.set_line_width(max(1.0, eye_r * 0.11)); cr.stroke()

        if open_frac < 0.20: return
        p_alpha = min(1.0, (open_frac - 0.20) / 0.80)
        offset  = (eye_r * sx - iris_r * 0.85) * 0.55 * tfrac
        px = ox + offset * math.cos(angle)
        py = oy + offset * math.sin(angle)

        cr.save(); _sclera(); cr.clip()
        # Deep blue-purple iris
        cr.new_path(); cr.save(); cr.translate(px, py); cr.scale(1.0, open_frac)
        cr.arc(0, 0, iris_r, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(0.16, 0.22, 0.60, 0.92 * p_alpha); cr.fill()
        # Dark pupil
        cr.new_path(); cr.save(); cr.translate(px, py); cr.scale(1.0, open_frac)
        cr.arc(0, 0, iris_r * 0.50, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(0.04, 0.02, 0.02, 0.97 * p_alpha); cr.fill()
        cr.restore()

        # Large Disney sparkle — top-left of iris
        hl_r = iris_r * 0.30
        hlx  = px - iris_r * 0.28
        hly  = py - iris_r * 0.33 * open_frac
        cr.new_path(); cr.arc(hlx, hly, hl_r, 0, 2 * math.pi)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.92 * p_alpha); cr.fill()
        # Small secondary sparkle
        cr.new_path()
        cr.arc(hlx + iris_r * 0.35, hly + iris_r * 0.22 * open_frac, hl_r * 0.44, 0, 2 * math.pi)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.68 * p_alpha); cr.fill()

    def _draw_eye_human(self, cr, ox, oy, eye_r, open_frac, angle, tfrac):
        """Realistic almond shape, green-grey iris, subtle catch-light."""
        hw = eye_r * 1.00; hh = eye_r * 0.58

        def _almond(sy=None):
            sy = sy if sy is not None else open_frac
            cr.new_path(); cr.save(); cr.translate(ox, oy); cr.scale(1.0, sy)
            cr.move_to(-hw, 0)
            cr.curve_to(-hw * 0.35, -hh * 1.15, hw * 0.35, -hh * 1.15, hw, 0)
            cr.curve_to( hw * 0.30,  hh * 0.80,-hw * 0.30,  hh * 0.80,-hw, 0)
            cr.close_path(); cr.restore()

        _almond()
        cr.set_source_rgba(0.97, 0.96, 0.95, 0.96); cr.fill_preserve()
        cr.set_source_rgba(0.18, 0.14, 0.11, 0.82)
        cr.set_line_width(max(0.7, eye_r * 0.065)); cr.stroke()

        if open_frac < 0.22: return
        p_alpha = min(1.0, (open_frac - 0.22) / 0.78)
        iris_r  = hh * 0.88
        offset  = (hw - iris_r) * 0.65 * tfrac
        px = ox + offset * math.cos(angle)
        py = oy + offset * math.sin(angle) * 0.70

        cr.save(); _almond(); cr.clip()
        # Green-grey iris
        cr.new_path(); cr.save(); cr.translate(px, py); cr.scale(1.0, open_frac)
        cr.arc(0, 0, iris_r, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(0.24, 0.38, 0.32, 0.88 * p_alpha); cr.fill()
        # Dark limbal ring
        cr.new_path(); cr.save(); cr.translate(px, py); cr.scale(1.0, open_frac)
        cr.arc(0, 0, iris_r, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(0.08, 0.08, 0.08, 0.40 * p_alpha)
        cr.set_line_width(max(0.5, iris_r * 0.12)); cr.stroke()
        # Pupil
        cr.new_path(); cr.save(); cr.translate(px, py); cr.scale(1.0, open_frac)
        cr.arc(0, 0, iris_r * 0.42, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(0.05, 0.04, 0.04, 0.95 * p_alpha); cr.fill()
        # Upper lid shadow
        cr.new_path(); cr.save(); cr.translate(ox, oy); cr.scale(1.0, open_frac)
        cr.move_to(-hw, 0)
        cr.curve_to(-hw*0.35, -hh*1.15, hw*0.35, -hh*1.15, hw, 0)
        cr.line_to(hw, -hh*0.20)
        cr.curve_to(hw*0.35,-hh*1.32,-hw*0.35,-hh*1.32,-hw,-hh*0.20)
        cr.close_path(); cr.restore()
        cr.set_source_rgba(0.40, 0.28, 0.22, 0.18 * p_alpha); cr.fill()
        cr.restore()

        # Small natural catch-light
        cr.new_path()
        cr.arc(px - iris_r*0.18, py - iris_r*0.24*open_frac, iris_r*0.16, 0, 2*math.pi)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.60 * p_alpha); cr.fill()
        # Re-stroke outline over iris
        _almond()
        cr.set_source_rgba(0.16, 0.12, 0.10, 0.88)
        cr.set_line_width(max(0.8, eye_r * 0.07)); cr.stroke()

    def _draw_eye_puppy(self, cr, ox, oy, eye_r, open_frac, angle, tfrac):
        """Huge pleading eyes, watery whites, amber iris, droopy upper lid."""
        sx = 0.92
        iris_r = eye_r * 0.72

        def _sclera():
            cr.new_path(); cr.save()
            cr.translate(ox, oy); cr.scale(sx, open_frac)
            cr.arc(0, 0, eye_r, 0, 2 * math.pi); cr.restore()

        _sclera()
        cr.set_source_rgba(0.93, 0.96, 0.99, 0.96); cr.fill_preserve()  # watery blue-white
        cr.set_source_rgba(0.14, 0.10, 0.08, 0.88)
        cr.set_line_width(max(1.0, eye_r * 0.09)); cr.stroke()

        if open_frac < 0.18: return
        p_alpha = min(1.0, (open_frac - 0.18) / 0.82)
        offset  = (eye_r * sx - iris_r * 0.90) * 0.48 * tfrac
        px = ox + offset * math.cos(angle)
        py = oy + offset * math.sin(angle)

        cr.save(); _sclera(); cr.clip()
        # Warm amber-brown iris
        cr.new_path(); cr.save(); cr.translate(px, py); cr.scale(1.0, open_frac)
        cr.arc(0, 0, iris_r, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(0.58, 0.35, 0.08, 0.90 * p_alpha); cr.fill()
        # Lighter inner iris ring (depth)
        cr.new_path(); cr.save(); cr.translate(px, py); cr.scale(1.0, open_frac)
        cr.arc(0, 0, iris_r * 0.68, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(0.72, 0.48, 0.14, 0.45 * p_alpha); cr.fill()
        # Large round pupil
        cr.new_path(); cr.save(); cr.translate(px, py); cr.scale(1.0, open_frac)
        cr.arc(0, 0, iris_r * 0.48, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(0.04, 0.02, 0.01, 0.97 * p_alpha); cr.fill()
        # Droopy upper lid shadow
        cr.new_path()
        cr.rectangle(ox - eye_r * 1.5, oy - eye_r * open_frac * 3,
                     eye_r * 3.0, eye_r * open_frac * 1.55)
        cr.set_source_rgba(0.30, 0.18, 0.08, 0.28 * p_alpha); cr.fill()
        cr.restore()

        # Big shiny highlight — upper-left, watery
        hl_r = iris_r * 0.34
        hlx  = px - iris_r * 0.30
        hly  = py - iris_r * 0.36 * open_frac
        cr.new_path(); cr.arc(hlx, hly, hl_r, 0, 2 * math.pi)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.94 * p_alpha); cr.fill()
        cr.new_path()
        cr.arc(hlx + iris_r * 0.40, hly + iris_r * 0.26 * open_frac, hl_r * 0.50, 0, 2 * math.pi)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.68 * p_alpha); cr.fill()

    def _draw_eye_marvel(self, cr, ox, oy, eye_r, open_frac, angle, tfrac):
        """Spider-Man style: angular white lens, gaze shown via reflection."""
        hw = eye_r * 1.05; hh = eye_r * 0.66

        def _lens():
            cr.new_path(); cr.save(); cr.translate(ox, oy); cr.scale(1.0, open_frac)
            cr.move_to(-hw, 0)
            cr.curve_to(-hw*0.20, -hh*1.18, hw*0.12, -hh*1.22, hw, 0)
            cr.curve_to( hw*0.12,  hh*1.08,-hw*0.20,  hh*1.04,-hw, 0)
            cr.close_path(); cr.restore()

        # White/silver lens fill
        _lens()
        cr.set_source_rgba(0.88, 0.92, 0.96, 0.97); cr.fill()

        if open_frac > 0.20:
            p_alpha = min(1.0, (open_frac - 0.20) / 0.80)
            # Subtle inner shadow opposite gaze direction (depth/roundness)
            cr.save(); _lens(); cr.clip()
            cr.new_path(); cr.save()
            cr.translate(ox - hw * 0.14 * tfrac * math.cos(angle),
                         oy - hh * 0.10 * tfrac * math.sin(angle) * open_frac)
            cr.scale(hw * 0.72, hh * 0.54 * open_frac)
            cr.arc(0, 0, 1.0, 0, 2 * math.pi); cr.restore()
            cr.set_source_rgba(0.55, 0.65, 0.80, 0.32 * p_alpha); cr.fill()
            cr.restore()
            # Bright reflection oval offset toward gaze
            rx = ox + hw * 0.30 * tfrac * math.cos(angle)
            ry = oy + hh * 0.20 * tfrac * math.sin(angle) * open_frac
            cr.new_path(); cr.save()
            cr.translate(rx, ry - hh * 0.12 * open_frac)
            cr.scale(hw * 0.20, hh * 0.13 * open_frac)
            cr.arc(0, 0, 1.0, 0, 2 * math.pi); cr.restore()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.82 * p_alpha); cr.fill()

        # Strong outline: near-black navy
        _lens()
        cr.set_source_rgba(0.06, 0.06, 0.12, 0.95)
        cr.set_line_width(max(1.0, eye_r * 0.10)); cr.stroke()


    def _draw_analog(self, cr, w, h, now, digital_inset=False):
        cx, cy = w / 2.0, h / 2.0
        r  = min(cx, cy) - 8
        t  = self.theme

        hrs  = now.hour % 12
        mins = now.minute
        secs = now.second

        stud_r      = max(4,   r * 0.055)
        outer_mark  = r - max(2, r * 0.025)
        min_len     = max(3,   r * 0.048)
        hr_len      = min_len * (2.5 if self.marker_style == 'Marks' else 1.25)
        min_r       = outer_mark - min_len
        hr_r        = outer_mark - hr_len
        min_w       = max(0.8, r * 0.010)
        hr_w        = min_w * 1.5
        hand_hr_w   = max(3.5, r * 0.055)
        hand_min_w  = max(2.0, r * 0.035)
        hand_sec_w  = max(1.0, r * 0.013)
        hr, hg, hb  = t['hour_marks']
        mr, mg, mb  = t['min_marks']

        label_r        = r * 0.75
        font_size_lbl  = 6
        font_size_card = 6
        lw_3 = lh_3 = lw_4 = lh_4 = lw_6 = lh_6 = 10
        if self.marker_style in ('Numbers', 'Roman'):
            labels     = ROMAN if self.marker_style == 'Roman' else [str(i) for i in range(1, 13)]
            widest     = 'VIII' if self.marker_style == 'Roman' else '11'
            card_wide  = 'XII'  if self.marker_style == 'Roman' else '12'
            lbl_3      = 'III'  if self.marker_style == 'Roman' else '3'
            lbl_4      = 'IV'   if self.marker_style == 'Roman' else '4'
            lbl_6      = 'VI'   if self.marker_style == 'Roman' else '6'
            num_blip_r = outer_mark - min_len * 1.80
            for fs in range(int(r * 0.11), 4, -1):
                fs_card = max(4, int(round(fs * 1.10)))
                fs_norm = max(4, int(round(fs * 0.78)))
                tmp = PangoCairo.create_layout(cr)
                tmp.set_text(widest, -1)
                tmp.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {fs_norm}'))
                tlw_n, tlh_n = tmp.get_pixel_size()
                tmp.set_text(card_wide, -1)
                tmp.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {fs_card}'))
                tlw_c, tlh_c = tmp.get_pixel_size()
                max_half = max(tlw_n, tlh_n, tlw_c, tlh_c) / 2
                lr       = num_blip_r - max(4, r * 0.03) - max_half
                arc_gap  = 2 * math.pi * lr / 12
                if lr > stud_r + 4 and arc_gap >= max(tlw_n, tlw_c) * 1.10:
                    font_size_lbl  = fs_norm
                    font_size_card = fs_card
                    label_r = lr
                    tmp.set_text(lbl_3, -1)
                    tmp.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {fs_card}'))
                    lw_3, lh_3 = tmp.get_pixel_size()
                    tmp.set_text(lbl_4, -1)
                    tmp.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {fs_norm}'))
                    lw_4, lh_4 = tmp.get_pixel_size()
                    tmp.set_text(lbl_6, -1)
                    tmp.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {fs_card}'))
                    lw_6, lh_6 = tmp.get_pixel_size()
                    break

        sin_4 = math.sin((4 / 6.0) * math.pi)
        if self.marker_style == 'Marks':
            x_3_inner = hr_r
            x_4_left  = hr_r * sin_4
            y_6_top   = cy + hr_r
        else:
            x_3_inner = label_r - lw_3 / 2
            x_4_left  = label_r * sin_4 - lw_4 / 2
            y_6_top   = cy + label_r - lh_6 / 2

        # Face
        face = t['face']
        face_alpha = face[3] if (t is THEMES['Clear']) else (1.0 if self.opacity_level >= 1.0 else face[3])
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.set_source_rgba(face[0], face[1], face[2], face_alpha); cr.fill()
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.set_source_rgba(*t['border'])
        cr.set_line_width(max(1.5, r * 0.018)); cr.stroke()

        # Minute marks
        for i in range(60):
            if i % 5 != 0:
                angle = (i / 30.0) * math.pi
                cr.move_to(cx + outer_mark * math.sin(angle), cy - outer_mark * math.cos(angle))
                cr.line_to(cx + min_r      * math.sin(angle), cy - min_r      * math.cos(angle))
                cr.set_source_rgba(mr, mg, mb, 0.45)
                cr.set_line_width(min_w); cr.stroke()

        # Hour marks with 3D ridge
        mid       = self.monitor_idx + 1   # hour position for monitor ID highlight
        _CARDINAL = {3, 6, 9, 12}
        for i in range(1, 13):
            angle = (i / 6.0) * math.pi
            is_marks    = self.marker_style == 'Marks'
            is_cardinal = (i in _CARDINAL)
            if is_marks:
                this_hr_len = hr_len * (1.30 if is_cardinal else 1.0)
                inner = outer_mark - this_hr_len
                width = hr_w
                b_r, b_g, b_b = hr, hg, hb
            else:
                inner = outer_mark - min_len * 1.80
                width = max(1.5, r * 0.016)
                b_r, b_g, b_b = mr * 0.60, mg * 0.60, mb * 0.60
            if self.manager.show_monitor_id and i == mid <= 12:
                b_r, b_g, b_b = self._id_mark_color(b_r, b_g, b_b)
            perp = angle + math.pi / 2
            off  = max(0.6, r * 0.006)
            dx   = math.sin(perp) * off; dy = -math.cos(perp) * off
            ox   = outer_mark * math.sin(angle); oy = -outer_mark * math.cos(angle)
            ix   = inner * math.sin(angle);      iy = -inner * math.cos(angle)

            def _mark(xo=0, yo=0):
                cr.move_to(cx+ox+dx+xo, cy+oy+dy+yo); cr.line_to(cx+ix+dx+xo, cy+iy+dy+yo)
                cr.set_source_rgba(b_r*0.40, b_g*0.40, b_b*0.40, 0.80)
                cr.set_line_width(width); cr.stroke()
                cr.move_to(cx+ox-dx+xo, cy+oy-dy+yo); cr.line_to(cx+ix-dx+xo, cy+iy-dy+yo)
                cr.set_source_rgba(min(1,b_r*1.6), min(1,b_g*1.6), min(1,b_b*1.6), 0.80)
                cr.set_line_width(width); cr.stroke()
                cr.move_to(cx+ox+xo, cy+oy+yo); cr.line_to(cx+ix+xo, cy+iy+yo)
                cr.set_source_rgba(b_r, b_g, b_b, 0.95)
                cr.set_line_width(width); cr.stroke()

            if is_cardinal:
                sep = max(width * 2.0, r * 0.013)
                sx  = math.sin(perp) * sep / 2; sy = -math.cos(perp) * sep / 2
                _mark(-sx, -sy); _mark(sx, sy)
            else:
                _mark()

        # Numbers / Roman labels
        if self.marker_style in ('Numbers', 'Roman'):
            labels = ROMAN if self.marker_style == 'Roman' else [str(i) for i in range(1, 13)]
            for i in range(1, 13):
                angle  = (i / 6.0) * math.pi
                lx     = cx + label_r * math.sin(angle)
                ly     = cy - label_r * math.cos(angle)
                layout = PangoCairo.create_layout(cr)
                layout.set_text(labels[i - 1], -1)
                fs = font_size_card if i in _CARDINAL else font_size_lbl
                layout.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {fs}'))
                lw, lh = layout.get_pixel_size()
                cr.move_to(lx - lw / 2, ly - lh / 2)
                if self.manager.show_monitor_id and i == mid <= 12:
                    ir, ig, ib = self._id_mark_color(hr, hg, hb)
                    cr.set_source_rgba(ir, ig, ib, 0.98)
                else:
                    cr.set_source_rgba(hr, hg, hb, 0.95)
                PangoCairo.show_layout(cr, layout)

        # Date window (before hands)
        if self.show_date:
            self._draw_date_window(cr, cx, cy, r, now, t, stud_r, x_3_inner)

        # Eyes — midpoint between centre and the actual marker position
        # x_3_inner and y_6_top are the inner edges of the 3/6 markers (already computed above)
        if self.show_eyes:
            if digital_inset:
                # Both mode: midpoint between centre and 9 (left side; bottom taken by inset)
                self._draw_eyes(cr, cx - x_3_inner / 2, cy, r * 0.13)
            else:
                # Pure analog: midpoint between centre and 6 (bottom)
                self._draw_eyes(cr, cx, (cy + y_6_top) / 2, r * 0.13)

        # Digital inset (before hands)
        if digital_inset:
            gap        = max(4, r * 0.038)
            y_stud_bot = cy + stud_r
            inset_cy   = (y_stud_bot + y_6_top) / 2
            offset_cy  = inset_cy - cy
            chord_hw   = math.sqrt(max(0, r**2 - offset_cy**2))
            max_hw     = min(chord_hw - gap, x_4_left - gap)
            avail_h    = y_6_top - y_stud_bot
            fs_h       = max(5, int(avail_h * 0.32))
            fs_w       = self._fit_font_size(cr, '00:00:00', max_hw * 1.20)
            fs_inset   = min(fs_w, fs_h)
            time_fmt   = '%H:%M:%S' if self.show_seconds else '%H:%M'
            time_str   = now.strftime(time_fmt)
            tw_i, th_i = self._text_size(cr, time_str, fs_inset)
            pad_x  = max(3, r * 0.025); pad_y = max(2, r * 0.018)
            box_w  = min(tw_i + pad_x * 2, max_hw * 2)
            box_h  = th_i + pad_y * 2
            bx     = cx - box_w / 2; by = inset_cy - box_h / 2
            rad          = max(3, box_h * 0.28)
            inset_bg     = t.get('inset_bg',     (0.18, 0.02, 0.02, 0.88))
            inset_border = t.get('inset_border', (0.78, 0.82, 0.87, 0.95))
            inset_fg     = t.get('inset_fg',     (1.00, 0.35, 0.35))
            cr.set_source_rgba(*inset_bg)
            self._rounded_rect(cr, bx, by, box_w, box_h, rad); cr.fill()
            cr.set_source_rgba(*inset_border)
            cr.set_line_width(max(1.0, r * 0.009))
            self._rounded_rect(cr, bx, by, box_w, box_h, rad); cr.stroke()
            self._draw_text(cr, cx, inset_cy, time_str, fs_inset, inset_fg, anchor='centre')

        # Hands (in front of date/inset windows)
        h_angle = ((hrs + mins / 60.0) / 6.0) * math.pi
        self._draw_hand(cr, cx, cy, h_angle, r * 0.54, hand_hr_w + max(1.5, r * 0.020), (0.25, 0.25, 0.28, 0.85))
        self._draw_hand(cr, cx, cy, h_angle, r * 0.54, hand_hr_w, t['hour_hand'])
        m_angle = ((mins + secs / 60.0) / 30.0) * math.pi
        self._draw_hand(cr, cx, cy, m_angle, r * 0.78, hand_min_w + max(1.5, r * 0.018), (0.12, 0.12, 0.15, 0.80))
        self._draw_hand(cr, cx, cy, m_angle, r * 0.78, hand_min_w, t['min_hand'])
        if self.show_seconds:
            s_angle = (secs / 30.0) * math.pi
            cr.set_line_width(hand_sec_w); cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_source_rgba(*t['second'])
            cr.move_to(cx - 0.15 * r * 0.84 * math.sin(s_angle), cy + 0.15 * r * 0.84 * math.cos(s_angle))
            cr.line_to(cx + r * 0.84 * math.sin(s_angle), cy - r * 0.84 * math.cos(s_angle)); cr.stroke()

        # Metallic stud (topmost)
        grad = cairo.RadialGradient(cx - stud_r*0.35, cy - stud_r*0.35, stud_r*0.05, cx, cy, stud_r)
        grad.add_color_stop_rgba(0.0, 1.00, 1.00, 1.00, 1.0)
        grad.add_color_stop_rgba(0.3, 0.85, 0.87, 0.90, 1.0)
        grad.add_color_stop_rgba(0.7, 0.55, 0.58, 0.62, 1.0)
        grad.add_color_stop_rgba(1.0, 0.25, 0.27, 0.30, 1.0)
        cr.arc(cx, cy, stud_r, 0, 2 * math.pi); cr.set_source(grad); cr.fill()
        cr.arc(cx, cy, stud_r, 0, 2 * math.pi)
        cr.set_source_rgba(0.1, 0.1, 0.1, 0.6)
        cr.set_line_width(max(0.5, r * 0.006)); cr.stroke()

        # Ghost colour: border colour at low alpha (matches any theme)
        br, bg, bb = t['border'][0], t['border'][1], t['border'][2]

        _, mx, my = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
        wx, wy = self.get_position()
        mlx, mly = mx - wx, my - wy

        # Alarm bell — between 1 and 2 markers, halfway to the edge
        n = self._active_alarm_count()
        bell_size  = max(12, r * 0.221)
        _ba        = (1.5 / 6.0) * math.pi   # 1:30 clockwise from 12
        bell_x     = cx + r * 0.48 * math.sin(_ba)
        bell_y     = cy - r * 0.48 * math.cos(_ba)
        bell_hover = math.hypot(mlx - bell_x, mly - bell_y) < bell_size * 0.8
        bell_rgba  = (0.90, 0.15, 0.15, 0.95) if n > 0 else ((0.90, 0.15, 0.15, 0.35) if bell_hover else (br, bg, bb, 0.10))
        self._draw_bell(cr, bell_x, bell_y, bell_size, bell_rgba,
                        rotate=math.pi / 4, count=n if n > 0 else 0)
        self._bell_draw_pos = (bell_x, bell_y)
        self._bell_hit_r    = bell_size * 0.6

        # Timer hourglass — between 10 and 11 markers, halfway to the edge
        nt = sum(1 for tmr in self.manager.timers if tmr['state'] == 'running')
        _ha      = (10.5 / 6.0) * math.pi   # 10:30 clockwise from 12
        hg_x     = cx + r * 0.48 * math.sin(_ha)
        hg_y     = cy - r * 0.48 * math.cos(_ha)
        hg_size  = max(10, r * 0.18)
        hg_hover = math.hypot(mlx - hg_x, mly - hg_y) < hg_size * 0.8
        hg_rgba  = (0.95, 0.50, 0.10, 0.95) if nt > 0 else ((0.95, 0.50, 0.10, 0.35) if hg_hover else (br, bg, bb, 0.10))
        self._draw_hourglass(cr, hg_x, hg_y, hg_size, hg_rgba,
                             count=nt if nt > 0 else 0)
        self._hg_draw_pos = (hg_x, hg_y)
        self._hg_hit_r    = hg_size * 0.7

    def _draw_date_window(self, cr, cx, cy, r, now, t, stud_r=None, x_3_inner=None):
        if stud_r    is None: stud_r    = max(4, r * 0.055)
        if x_3_inner is None: x_3_inner = r - max(6, r * 0.095)

        date_str  = now.strftime('%a %-d').upper()
        font_size = max(5, int(r * 0.075))
        layout = PangoCairo.create_layout(cr)
        layout.set_text(date_str, -1)
        layout.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {font_size}'))
        lw, lh = layout.get_pixel_size()

        while font_size > 5 and lw + lh * 0.4 > (x_3_inner - stud_r) * 0.9:
            font_size -= 1
            layout.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {font_size}'))
            lw, lh = layout.get_pixel_size()

        pad_x = lh * 0.20; pad_y = lh * 0.18
        win_w = lw + pad_x * 2; win_h = lh + pad_y * 2
        win_cx = cx + (stud_r + x_3_inner) / 2
        wx = win_cx - win_w / 2; wy = cy - win_h / 2

        date_bg     = t.get('date_bg',     (0.96, 0.96, 0.94, 0.97))
        date_fg     = t.get('date_fg',     (0.05, 0.05, 0.05, 1.00))
        date_border = t.get('date_border', (0.78, 0.82, 0.87, 0.95))
        cr.set_source_rgba(*date_bg)
        self._rounded_rect(cr, wx, wy, win_w, win_h, max(2, win_h * 0.15)); cr.fill()
        cr.set_source_rgba(*date_border)
        cr.set_line_width(max(0.8, r * 0.009))
        self._rounded_rect(cr, wx, wy, win_w, win_h, max(2, win_h * 0.15)); cr.stroke()
        cr.move_to(wx + pad_x, wy + pad_y)
        cr.set_source_rgba(*date_fg)
        PangoCairo.show_layout(cr, layout)

    def _draw_digital(self, cr, w, h, now):
        t = self.theme
        if   self.digital_style == '7 Seg': self._draw_digital_7seg(cr, w, h, now, t)
        elif self.digital_style == 'Split': self._draw_digital_split(cr, w, h, now, t)
        else:                               self._draw_digital_font(cr, w, h, now, t)
        self._draw_digital_badge(cr, w, h, t)

    def _draw_digital_icons(self, cr, w, h, t):
        br, bg, bb = t['border'][0], t['border'][1], t['border'][2]
        icon_size = max(8, h * 0.16)
        pad = max(3, h * 0.06)
        # Bell — bottom-left
        n = self._active_alarm_count()
        bell_rgba = (0.90, 0.15, 0.15, 0.90) if n > 0 else (br, bg, bb, 0.18)
        bx = pad + icon_size * 0.5
        by = h - pad - icon_size * 0.5
        self._draw_bell(cr, bx, by, icon_size, bell_rgba,
                        rotate=math.pi / 4, count=n if n > 0 else 0)
        self._bell_draw_pos = (bx, by)
        self._bell_hit_r    = icon_size * 0.7
        # Hourglass — bottom-right
        nt = sum(1 for tmr in self.manager.timers if tmr['state'] == 'running')
        hg_rgba = (0.95, 0.50, 0.10, 0.90) if nt > 0 else (br, bg, bb, 0.18)
        hx = w - pad - icon_size * 0.5
        hy = h - pad - icon_size * 0.5
        self._draw_hourglass(cr, hx, hy, icon_size, hg_rgba,
                             count=nt if nt > 0 else 0)
        self._hg_draw_pos = (hx, hy)
        self._hg_hit_r    = icon_size * 0.7

    def _draw_digital_badge(self, cr, w, h, t):
        if not self.manager.show_monitor_id: return
        return  # all digital styles encode monitor ID via colon dot colours instead
        mid = self.monitor_idx + 1
        fill_c, text_c = self._id_badge_colors(t['face'])
        corner_r = 14
        badge_r = max(5, min(corner_r - 4, int(min(w, h) * 0.07)))
        bcx = w - 2 - corner_r;  bcy = h - 2 - corner_r
        cr.arc(bcx, bcy, badge_r, 0, 2 * math.pi)
        cr.set_source_rgba(*fill_c); cr.fill()
        layout = PangoCairo.create_layout(cr)
        layout.set_text(str(mid), -1)
        layout.set_font_description(Pango.FontDescription(f'DejaVu Sans Bold {max(5, int(badge_r * 0.95))}'))
        lw, lh = layout.get_pixel_size()
        cr.move_to(bcx - lw / 2, bcy - lh / 2)
        cr.set_source_rgba(*text_c); PangoCairo.show_layout(cr, layout)

    def _draw_digital_font(self, cr, w, h, now, t):
        cx = w / 2.0; cy = h / 2.0

        time_fmt  = '%H:%M:%S' if self.show_seconds else '%H:%M'
        time_str  = now.strftime(time_fmt)
        date_str  = now.strftime('%a %d %b')
        time_size = self._fit_font_size(cr, time_str, self.size * 0.88)
        date_size = max(8, time_size // 3)
        tw, th    = self._text_size(cr, time_str, time_size)
        dw, dh    = self._text_size(cr, date_str,  date_size)

        show_d = self.show_date
        pad_x  = tw * 0.10; pad_y = th * 0.55
        g      = th * 0.18
        eye_r  = th * 0.27 if self.show_eyes else 0.0

        rows_h = th
        if self.show_eyes: rows_h += g + eye_r * 2.0
        if show_d:         rows_h += g + dh

        box_w = min(max(tw, dw if show_d else tw) + pad_x * 2, self.size - 16)
        box_h = rows_h + pad_y * 2
        bx    = cx - box_w / 2; by = cy - box_h / 2

        face = t['face']
        face_alpha = face[3] if (t is THEMES['Clear']) else (1.0 if self.opacity_level >= 1.0 else face[3])
        cr.set_source_rgba(face[0], face[1], face[2], face_alpha)
        self._rounded_rect(cr, bx, by, box_w, box_h, 14); cr.fill()
        cr.set_source_rgba(*t['border']); cr.set_line_width(2.0)
        self._rounded_rect(cr, bx, by, box_w, box_h, 14); cr.stroke()

        def measure_w(s):
            lay = PangoCairo.create_layout(cr)
            lay.set_text(s, -1)
            lay.set_font_description(Pango.FontDescription(f'{self.digital_font} {time_size}'))
            return lay.get_pixel_size()[0]

        time_top_y = by + pad_y
        y = time_top_y
        self._draw_text(cr, cx, y, time_str, time_size, t['digital']); y += th

        # Monitor ID: recolour the FIRST colon's two dots to encode monitor identity.
        # We re-paint the font's OWN ':' glyph (clipped to each dot's half) rather than
        # drawing our own circles — so the dots keep the font's real shape (DejaVu Sans
        # Bold draws SQUARE colon dots, other fonts round) and match the unmodified
        # colon exactly. Monitor 1 = orange top + normal bottom; Monitor 2 = both orange.
        _ORANGE = (1.0, 0.55, 0.0, 0.95)
        if self.manager.show_monitor_id:
            dc = t['digital']
            seg_n = (dc[0], dc[1], dc[2], 0.95)
            top_c = _ORANGE
            bot_c = seg_n if self.monitor_idx == 0 else _ORANGE
            text_left = cx - tw / 2.0
            colon_lay = PangoCairo.create_layout(cr)
            colon_lay.set_text(':', -1)
            colon_lay.set_font_description(Pango.FontDescription(f'{self.digital_font} {time_size}'))
            ink, logical = colon_lay.get_pixel_extents()
            colon_left = text_left + measure_w(time_str[:2])
            split_y = time_top_y + ink.y + ink.height / 2.0  # gap between the two dots

            def _repaint_colon(color, y_top, y_bot):
                cr.save()
                cr.rectangle(colon_left, y_top, logical.width, y_bot - y_top)
                cr.clip()
                lay = PangoCairo.create_layout(cr)
                lay.set_text(':', -1)
                lay.set_font_description(Pango.FontDescription(f'{self.digital_font} {time_size}'))
                cr.move_to(colon_left, time_top_y)
                cr.set_source_rgba(*color)
                PangoCairo.show_layout(cr, lay)
                cr.restore()

            # Erase the font's first colon, then repaint each dot in its own colour
            # using the real glyph shape, clipped to the top / bottom half.
            cr.set_source_rgba(face[0], face[1], face[2], face_alpha)
            cr.rectangle(colon_left, time_top_y, logical.width, th)
            cr.fill()
            _repaint_colon(top_c, time_top_y, split_y)
            _repaint_colon(bot_c, split_y, time_top_y + th)
        if self.show_eyes:
            y += g
            self._draw_eyes(cr, cx, y + eye_r, eye_r)
            y += eye_r * 2.0
        if show_d:
            y += g
            self._draw_text(cr, cx, y, date_str, date_size, t['marks'], alpha=0.75)

        # Icons above the time text, in the top padding
        br_c, bg_c, bb_c = t['border'][0], t['border'][1], t['border'][2]
        icon_size = max(10, pad_y * 0.75)
        icon_y    = by + pad_y * 0.56
        text_x    = cx - tw / 2.0

        n  = self._active_alarm_count()
        nt = sum(1 for tmr in self.manager.timers if tmr['state'] == 'running')

        if self.show_seconds:
            # HH:MM:SS — icons above the two colons
            bell_x = text_x + measure_w(time_str[:2]) + measure_w(':') * 0.5
            hg_x   = text_x + measure_w(time_str[:5]) + measure_w(':') * 0.5
        else:
            # HH:MM — bell above HH centre, hourglass above MM centre
            w_hh  = measure_w(time_str[:2])
            w_col = measure_w(':')
            w_mm  = measure_w(time_str[3:])
            bell_x = text_x + w_hh * 0.5
            hg_x   = text_x + w_hh + w_col + w_mm * 0.5

        _, mx, my = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
        wx, wy = self.get_position()
        mlx, mly = mx - wx, my - wy
        bell_hover = math.hypot(mlx - bell_x, mly - icon_y) < icon_size * 0.8
        hg_hover   = math.hypot(mlx - hg_x,   mly - icon_y) < icon_size * 0.8
        bell_rgba = (0.90, 0.15, 0.15, 0.90) if n > 0 else ((0.90, 0.15, 0.15, 0.35) if bell_hover else (br_c, bg_c, bb_c, 0.10))
        hg_rgba   = (0.95, 0.50, 0.10, 0.90) if nt > 0 else ((0.95, 0.50, 0.10, 0.35) if hg_hover   else (br_c, bg_c, bb_c, 0.10))
        self._draw_bell(cr, bell_x, icon_y, icon_size, bell_rgba,
                        rotate=math.pi / 4, count=n if n > 0 else 0)
        self._draw_hourglass(cr, hg_x, icon_y, icon_size, hg_rgba,
                             count=nt if nt > 0 else 0)
        self._bell_draw_pos = (bell_x, icon_y)
        self._bell_hit_r    = icon_size * 0.8
        self._hg_draw_pos   = (hg_x, icon_y)
        self._hg_hit_r      = icon_size * 0.8

    # ── 7-segment renderer ────────────────────────────────────────────

    def _draw_digital_7seg(self, cr, w, h, now, t):
        time_fmt = '%H:%M:%S' if self.show_seconds else '%H:%M'
        time_str = now.strftime(time_fmt)
        dh, dw, cw, igap, total_w = self._7seg_layout()
        pad_y  = max(28, dh * 0.55)
        show_d  = self.show_date
        dh2     = max(6, dh * 0.28)
        eye_r   = dh * 0.22 if self.show_eyes else 0.0
        eyes_h  = eye_r * 2.0 + igap * 2 if self.show_eyes else 0.0
        total_h = dh + pad_y * 2 + eyes_h + (dh2 + igap * 2 if show_d else 0)

        # Background box
        face   = t['face']
        face_a = face[3] if (t is THEMES['Clear']) else (1.0 if self.opacity_level >= 1.0 else face[3])
        cr.set_source_rgba(face[0], face[1], face[2], face_a)
        self._rounded_rect(cr, 2, 2, w - 4, h - 4, 14); cr.fill()
        cr.set_source_rgba(*t['border']); cr.set_line_width(2.0)
        self._rounded_rect(cr, 2, 2, w - 4, h - 4, 14); cr.stroke()

        # Segment colours — lit and dim versions of theme digital colour
        dc     = t['digital']
        seg_on  = (dc[0], dc[1], dc[2], 0.95)
        seg_off = (dc[0] * 0.28, dc[1] * 0.28, dc[2] * 0.28, 0.18)

        # Colon dot colors encode monitor ID — replaces badge for 7 Seg style
        _ORANGE = (1.0, 0.55, 0.0, 0.95)
        if self.manager.show_monitor_id:
            colon_dots = (_ORANGE, seg_on) if self.monitor_idx == 0 else (_ORANGE, _ORANGE)
        else:
            colon_dots = None  # both dots use seg_on (normal)

        start_y = (h - total_h) / 2 + pad_y
        start_x = (w - total_w) / 2
        x = start_x
        for ch in time_str:
            if ch == ':':
                self._draw_7seg_char(cr, x, start_y, cw, dh, ch, seg_on, seg_off, colon_dots)
                x += cw + igap
            else:
                self._draw_7seg_char(cr, x, start_y, dw, dh, ch, seg_on, seg_off)
                x += dw + igap

        if self.show_eyes:
            eyes_y = start_y + dh + igap
            self._draw_eyes(cr, w / 2, eyes_y + eye_r, eye_r)

        if show_d:
            date_str  = now.strftime('%a %d %b')
            date_size = max(6, int(dh2 * 0.80))
            date_y    = start_y + dh + eyes_h + igap * 2
            self._draw_text(cr, w / 2, date_y, date_str, date_size,
                            t['marks'], alpha=0.70, font='DejaVu Sans Bold')

        # Icons at top — ghost colour matches off-segment colour
        n  = self._active_alarm_count()
        nt = sum(1 for tmr in self.manager.timers if tmr['state'] == 'running')
        icon_size = max(10, pad_y * 0.75)
        icon_y    = (h - total_h) / 2 + pad_y * 0.56
        if self.show_seconds:
            bell_x = start_x + 2 * (dw + igap) + cw * 0.5
            hg_x   = start_x + (2 * (dw + igap) + cw + igap + 2 * (dw + igap)) + cw * 0.5
        else:
            bell_x = start_x + dw + igap * 0.5
            hg_x   = start_x + 2 * (dw + igap) + cw + igap + dw + igap * 0.5
        _, mx, my = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
        wx, wy = self.get_position()
        mlx, mly = mx - wx, my - wy
        bell_hover = math.hypot(mlx - bell_x, mly - icon_y) < icon_size * 0.8
        hg_hover   = math.hypot(mlx - hg_x,   mly - icon_y) < icon_size * 0.8
        bell_rgba = (0.90, 0.15, 0.15, 0.90) if n  > 0 else ((0.90, 0.15, 0.15, 0.35) if bell_hover else seg_off)
        hg_rgba   = (0.95, 0.50, 0.10, 0.90) if nt > 0 else ((0.95, 0.50, 0.10, 0.35) if hg_hover   else seg_off)
        self._draw_bell(cr, bell_x, icon_y, icon_size, bell_rgba,
                        rotate=math.pi / 4, count=n if n > 0 else 0)
        self._draw_hourglass(cr, hg_x, icon_y, icon_size, hg_rgba,
                             count=nt if nt > 0 else 0)
        self._bell_draw_pos = (bell_x, icon_y); self._bell_hit_r = icon_size * 0.8
        self._hg_draw_pos   = (hg_x,   icon_y); self._hg_hit_r  = icon_size * 0.8

    def _draw_7seg_char(self, cr, x, y, cw, ch, char, seg_on, seg_off, dot_colors=None):
        if char == ':':
            dot_r = max(1.5, cw * 0.22)
            top_c = dot_colors[0] if dot_colors else seg_on
            bot_c = dot_colors[1] if dot_colors else seg_on
            cr.new_path()
            cr.set_source_rgba(*top_c)
            cr.arc(x + cw / 2, y + ch * 0.30, dot_r, 0, 2 * math.pi); cr.fill()
            cr.new_path()
            cr.set_source_rgba(*bot_c)
            cr.arc(x + cw / 2, y + ch * 0.70, dot_r, 0, 2 * math.pi); cr.fill()
            return
        pattern = SEG_PATTERNS.get(char, '')
        sw   = max(1.5, cw * 0.14)
        gap  = sw * 0.45
        hlen = cw - 2 * (sw + gap)
        vlen = ch / 2 - sw - 2 * gap
        r    = sw / 2

        def h_seg(lit, ry):
            cr.set_source_rgba(*(seg_on if lit else seg_off))
            self._rounded_rect(cr, x + sw + gap, ry - sw / 2, hlen, sw, r); cr.fill()

        def v_seg(lit, rx, ry):
            cr.set_source_rgba(*(seg_on if lit else seg_off))
            self._rounded_rect(cr, rx - sw / 2, ry, sw, vlen, r); cr.fill()

        h_seg('a' in pattern, y + sw / 2)
        v_seg('b' in pattern, x + cw - sw / 2, y + sw + gap)
        v_seg('c' in pattern, x + cw - sw / 2, y + ch / 2 + gap)
        h_seg('d' in pattern, y + ch - sw / 2)
        v_seg('e' in pattern, x + sw / 2,      y + ch / 2 + gap)
        v_seg('f' in pattern, x + sw / 2,      y + sw + gap)
        h_seg('g' in pattern, y + ch / 2)

    # ── Split-flap renderer ───────────────────────────────────────────

    def _draw_digital_split(self, cr, w, h, now, t):
        time_fmt = '%H:%M:%S' if self.show_seconds else '%H:%M'
        time_str = now.strftime(time_fmt)
        th, tw, t_gap, total_w = self._split_layout()
        show_d     = self.show_date
        th2        = max(6, th * 0.50) if show_d else 0
        tw2        = th2 * 0.70        if show_d else 0
        t_gap2     = max(1.0, th2 * 0.05) if show_d else 0
        icon_strip = max(28, th * 0.40)
        pad_y      = max(8,  th * 0.12)

        # Outer box (clock theme)
        face   = t['face']
        face_a = face[3] if (t is THEMES['Clear']) else (1.0 if self.opacity_level >= 1.0 else face[3])
        cr.set_source_rgba(face[0], face[1], face[2], face_a)
        self._rounded_rect(cr, 2, 2, w - 4, h - 4, 14); cr.fill()
        cr.set_source_rgba(*t['border']); cr.set_line_width(2.0)
        self._rounded_rect(cr, 2, 2, w - 4, h - 4, 14); cr.stroke()

        # Classic split-flap tile palette (dark, independent of clock theme)
        tile_bg  = (0.11, 0.11, 0.14, 0.97)
        tile_fg  = (0.94, 0.91, 0.84, 1.00)
        split_c  = (0.04, 0.04, 0.06, 1.00)
        tile_r   = max(2.0, th * 0.07)

        time_x  = (w - total_w) / 2
        time_y  = icon_strip + pad_y

        # Monitor ID encoded via colon dot colours
        _ORANGE = (1.0, 0.55, 0.0, 0.95)
        if self.manager.show_monitor_id:
            colon_dots = (_ORANGE, tile_fg) if self.monitor_idx == 0 else (_ORANGE, _ORANGE)
        else:
            colon_dots = None

        for ch in time_str:
            self._draw_split_tile(cr, time_x, time_y, tw, th, ch,
                                  tile_bg, tile_fg, split_c, tile_r,
                                  colon_dots if ch == ':' else None)
            time_x += tw + t_gap

        if show_d:
            date_str = now.strftime('%a %d %b')
            n_date   = len(date_str)
            date_w   = n_date * tw2 + (n_date - 1) * t_gap2
            date_x   = (w - date_w) / 2
            date_y   = time_y + th + t_gap * 2
            tile_r2  = max(1.5, th2 * 0.07)
            for ch in date_str:
                self._draw_split_tile(cr, date_x, date_y, tw2, th2, ch,
                                      tile_bg, tile_fg, split_c, tile_r2)
                date_x += tw2 + t_gap2

        # Icons at top — ghost matches dimmed tile fg
        n  = self._active_alarm_count()
        nt = sum(1 for tmr in self.manager.timers if tmr['state'] == 'running')
        icon_size = max(10, icon_strip * 0.75)
        icon_y    = icon_strip * 0.56
        tile_ghost = (tile_fg[0] * 0.22, tile_fg[1] * 0.22, tile_fg[2] * 0.22, 0.30)
        tx0 = (w - total_w) / 2
        if self.show_seconds:
            bell_x = tx0 + 2 * (tw + t_gap) + tw * 0.5
            hg_x   = tx0 + 5 * (tw + t_gap) + tw * 0.5
        else:
            bell_x = tx0 + tw + t_gap * 0.5
            hg_x   = tx0 + 3 * tw + t_gap * 3.5
        _, mx, my = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
        wx, wy = self.get_position()
        mlx, mly = mx - wx, my - wy
        bell_hover = math.hypot(mlx - bell_x, mly - icon_y) < icon_size * 0.8
        hg_hover   = math.hypot(mlx - hg_x,   mly - icon_y) < icon_size * 0.8
        bell_rgba = (0.90, 0.15, 0.15, 0.90) if n  > 0 else ((0.90, 0.15, 0.15, 0.35) if bell_hover else tile_ghost)
        hg_rgba   = (0.95, 0.50, 0.10, 0.90) if nt > 0 else ((0.95, 0.50, 0.10, 0.35) if hg_hover   else tile_ghost)
        self._draw_bell(cr, bell_x, icon_y, icon_size, bell_rgba,
                        rotate=math.pi / 4, count=0)
        self._draw_hourglass(cr, hg_x, icon_y, icon_size, hg_rgba, count=0)
        # Count as split tile alongside icon
        ct_sz = max(6, icon_size * 0.85)
        ct_r  = max(1.0, ct_sz * 0.12)
        if n > 0:
            self._draw_split_tile(cr, bell_x + icon_size * 0.6, icon_y - ct_sz * 0.5,
                                  ct_sz, ct_sz, str(n), tile_bg, tile_fg, split_c, ct_r)
        if nt > 0:
            self._draw_split_tile(cr, hg_x + icon_size * 0.6, icon_y - ct_sz * 0.5,
                                  ct_sz, ct_sz, str(nt), tile_bg, tile_fg, split_c, ct_r)
        self._bell_draw_pos = (bell_x, icon_y); self._bell_hit_r = icon_size * 0.8
        self._hg_draw_pos   = (hg_x,   icon_y); self._hg_hit_r  = icon_size * 0.8

    def _draw_split_tile(self, cr, x, y, tw, th, char, tile_bg, tile_fg, split_c, tile_r, dot_colors=None):
        # Tile background
        cr.set_source_rgba(*tile_bg)
        self._rounded_rect(cr, x, y, tw, th, tile_r); cr.fill()
        # Character — colon draws coloured dots when dot_colors provided
        if char == ':' and dot_colors is not None:
            dot_r = max(2.0, tw * 0.13)
            cr.new_path(); cr.set_source_rgba(*dot_colors[0])
            cr.arc(x + tw / 2, y + th * 0.30, dot_r, 0, 2 * math.pi); cr.fill()
            cr.new_path(); cr.set_source_rgba(*dot_colors[1])
            cr.arc(x + tw / 2, y + th * 0.70, dot_r, 0, 2 * math.pi); cr.fill()
        else:
            font_size = max(5, int(th * 0.68))
            layout = PangoCairo.create_layout(cr)
            layout.set_text(char, -1)
            layout.set_font_description(Pango.FontDescription(f'Ubuntu Bold {font_size}'))
            lw, lh = layout.get_pixel_size()
            cr.move_to(x + tw / 2 - lw / 2, y + th / 2 - lh / 2)
            cr.set_source_rgba(*tile_fg); PangoCairo.show_layout(cr, layout)
        # Split line — the defining detail
        cr.set_source_rgba(*split_c)
        cr.set_line_width(max(0.8, th * 0.020))
        cr.move_to(x + tile_r, y + th / 2)
        cr.line_to(x + tw - tile_r, y + th / 2); cr.stroke()
        # Tile edge highlight
        cr.set_source_rgba(tile_bg[0] + 0.12, tile_bg[1] + 0.12, tile_bg[2] + 0.12, 0.55)
        cr.set_line_width(max(0.5, th * 0.012))
        self._rounded_rect(cr, x, y, tw, th, tile_r); cr.stroke()

    def _text_size(self, cr, text, size, font=None):
        layout = PangoCairo.create_layout(cr)
        layout.set_text(text, -1)
        layout.set_font_description(Pango.FontDescription(f'{font or self.digital_font} {size}'))
        return layout.get_pixel_size()

    def _fit_font_size(self, cr, text, max_width, font=None):
        f = font or self.digital_font
        for size in range(72, 6, -1):
            layout = PangoCairo.create_layout(cr)
            layout.set_text(text, -1)
            layout.set_font_description(Pango.FontDescription(f'{f} {size}'))
            lw, _ = layout.get_pixel_size()
            if lw <= max_width: return size
        return 7

    def _draw_text(self, cr, x, y, text, size, rgb, alpha=1.0, anchor='top', font=None):
        layout = PangoCairo.create_layout(cr)
        layout.set_text(text, -1)
        layout.set_font_description(Pango.FontDescription(f'{font or self.digital_font} {size}'))
        lw, lh = layout.get_pixel_size()
        ty = y if anchor == 'top' else y - lh / 2
        cr.move_to(x - lw / 2, ty)
        cr.set_source_rgba(rgb[0], rgb[1], rgb[2], alpha)
        PangoCairo.show_layout(cr, layout)

    def _draw_hand(self, cr, cx, cy, angle, length, width, rgba, tail_frac=0.15):
        sa    = math.sin(angle); ca = math.cos(angle)
        tail  = length * tail_frac
        tip_x = cx + length * sa;  tip_y = cy - length * ca
        tl_x  = cx - tail   * sa;  tl_y  = cy + tail   * ca
        cr.save()
        cr.set_source_rgba(*rgba)
        if self.hand_style == 'Deco':
            pa  = angle + math.pi / 2
            ps  = math.sin(pa); pc = math.cos(pa)
            w   = width * 1.5
            wpx = cx + length * 0.35 * sa; wpy = cy - length * 0.35 * ca
            cr.new_sub_path()
            cr.move_to(tip_x, tip_y)
            cr.line_to(wpx + w * ps, wpy - w * pc)
            cr.line_to(tl_x, tl_y)
            cr.line_to(wpx - w * ps, wpy + w * pc)
            cr.close_path(); cr.fill()
        elif self.hand_style == 'Modern':
            pa   = angle + math.pi / 2
            ps   = math.sin(pa); pc = math.cos(pa)
            thin = max(1.0, width * 0.30)
            cr.new_sub_path()
            cr.move_to(tip_x, tip_y)
            cr.line_to(cx + thin * ps, cy - thin * pc)
            cr.line_to(tl_x, tl_y)
            cr.line_to(cx - thin * ps, cy + thin * pc)
            cr.close_path(); cr.fill()
            cr.arc(tl_x, tl_y, width * 0.85, 0, 2 * math.pi); cr.fill()
        else:  # Classic
            cr.set_line_width(width); cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(tl_x, tl_y); cr.line_to(tip_x, tip_y); cr.stroke()
        cr.restore()

    def _append_tracker_menu_items(self, menu, show_reset=False):
        cfg = load_tracker_config()
        if not cfg.get('tracker_disabled') and tracker_configured():
            bug_item = Gtk.MenuItem(label='Report a bug…')
            bug_item.connect('activate', lambda _: self.manager.open_tracker_panel(self))
            menu.append(bug_item)
        if show_reset:
            reset_item = Gtk.MenuItem(label='Reset tracker setup…')
            reset_item.connect('activate', lambda _: self.manager.reset_tracker(self))
            menu.append(reset_item)

    def apply_click_through(self, enabled):
        if enabled:
            self.input_shape_combine_region(cairo.Region())
        else:
            cw, ch = self._clock_size()
            full = cairo.Region(cairo.RectangleInt(0, 0, cw, ch))
            self.input_shape_combine_region(full)

    # ── Fade-to-clear cycle (v1.2.0, desktop-clock #330) ──────────────
    # State machine driven by ClockManager._fade_tick at 50 ms. Each
    # window holds its own state so multi-monitor clocks fade
    # independently. The methods here mutate state and opacity; the
    # manager-level tick decides when to call into _fade_advance().

    def _fade_now_ms(self):
        return int(GLib.get_monotonic_time() / 1000)

    def fade_start(self):
        """Idle → fading_out. Snapshots the current opacity baseline so
        the eventual fade-in restores to whatever the user had, not
        100%. Also snapshots `fade_wait_minutes` (v1.2.1 #482) so that
        changing the setting mid-cycle doesn't shorten/lengthen any
        in-flight cycle — each cycle uses the value that was in effect
        when it started. Multi-monitor: each clock captures
        independently."""
        if self._fade_state != 'idle':
            return
        # Snapshot baselines so we can restore exactly on cancel/end.
        self._fade_baseline_opacity  = self.props.opacity
        self._fade_baseline_click_t  = self.manager.click_through  # user's setting
        self._fade_wait_min_snapshot = self.manager.fade_wait_minutes
        self._fade_state = 'fading_out'
        self._fade_state_start_ms = self._fade_now_ms()
        self.manager._ensure_fade_tick()

    def fade_cancel(self, restore_opacity=True):
        """Cancel mid-cycle and restore baseline opacity + click-through.
        Used by:
          - double-click during fading_out
          - tray menu Restore clock
          - alarm/timer fire mid-cycle (with fast-in instead — see below)
        """
        if self._fade_state == 'idle':
            return
        self._fade_state = 'idle'
        self._fade_state_start_ms = 0
        if restore_opacity and self._fade_baseline_opacity is not None:
            self.props.opacity = self._fade_baseline_opacity
        # Only turn click-through off if the user didn't have it manually
        # enabled before the fade started.
        if self._fade_baseline_click_t is False:
            self.apply_click_through(False)
        self._fade_baseline_opacity  = None
        self._fade_baseline_click_t  = None
        self._fade_wait_min_snapshot = None

    def fade_force_fast_in(self):
        """Alarm/timer override: jump to a fast fade-in from current
        opacity. Used regardless of current cycle state — if we're in
        fading_out, faded, or fading_in, this overrides with a 2.5 s
        in to baseline."""
        if self._fade_state == 'idle':
            return
        self._fade_state = 'fast_fading_in'
        self._fade_state_start_ms = self._fade_now_ms()

    def _fade_advance(self):
        """Called by the manager tick. Returns True while cycle is
        active (so the manager keeps the tick alive), False when this
        window has returned to idle."""
        if self._fade_state == 'idle':
            return False
        now = self._fade_now_ms()
        elapsed = now - self._fade_state_start_ms
        baseline = self._fade_baseline_opacity or 1.0
        # v1.2.1 #482: use the snapshot, not the live manager value, so
        # mid-cycle changes don't shorten/lengthen this cycle.
        wait_min = self._fade_wait_min_snapshot
        if wait_min is None:
            wait_min = self.manager.fade_wait_minutes
        wait_ms = int(wait_min * 60_000)

        if self._fade_state == 'fading_out':
            t = min(1.0, elapsed / FADE_OUT_MS)
            self.props.opacity = baseline * (1.0 - t)
            if t >= 1.0:
                # Transition: now invisible, switch to click-through
                self.apply_click_through(True)
                self._fade_state = 'faded'
                self._fade_state_start_ms = now
            return True

        if self._fade_state == 'faded':
            self.props.opacity = 0.0
            if elapsed >= wait_ms:
                self._fade_state = 'fading_in'
                self._fade_state_start_ms = now
            return True

        if self._fade_state == 'fading_in':
            t = min(1.0, elapsed / FADE_IN_MS)
            self.props.opacity = baseline * t
            if t >= 1.0:
                # Back at baseline — exit cycle.
                return self._fade_finish()
            return True

        if self._fade_state == 'fast_fading_in':
            t = min(1.0, elapsed / FADE_FAST_IN_MS)
            current_floor = self.props.opacity
            # Smooth from whatever opacity we hit when override fired,
            # not from absolute baseline, so the fast-in feels continuous.
            self.props.opacity = current_floor + (baseline - current_floor) * t
            if t >= 1.0:
                return self._fade_finish()
            return True

        # Unknown state — return to idle defensively.
        return self._fade_finish()

    def _fade_finish(self):
        """End-of-cycle cleanup: restore baseline opacity exactly, turn
        click-through off (unless user had it on)."""
        if self._fade_baseline_opacity is not None:
            self.props.opacity = self._fade_baseline_opacity
        if self._fade_baseline_click_t is False:
            self.apply_click_through(False)
        self._fade_state = 'idle'
        self._fade_baseline_opacity  = None
        self._fade_baseline_click_t  = None
        self._fade_wait_min_snapshot = None
        return False

    def _rounded_rect(self, cr, x, y, w, h, radius):
        cr.new_sub_path()
        cr.arc(x + w - radius, y + radius,     radius, -math.pi/2, 0)
        cr.arc(x + w - radius, y + h - radius, radius, 0,          math.pi/2)
        cr.arc(x + radius,     y + h - radius, radius, math.pi/2,  math.pi)
        cr.arc(x + radius,     y + radius,     radius, math.pi,    3*math.pi/2)
        cr.close_path()


# ─── Clock manager ────────────────────────────────────────────────────

class ClockManager:
    def __init__(self):
        shared               = load_state()
        self.sync            = bool(shared.get('sync_settings',   True))
        self.click_through   = bool(shared.get('click_through',   False))
        self.show_monitor_id = bool(shared.get('show_monitor_id', False))
        # Fade timeout (v1.2.0 #330) — minutes the clock stays
        # invisible before fading back. Default 5 min, user-settable
        # from tray Fade timeout submenu.
        self.fade_wait_minutes = int(shared.get('fade_wait_minutes', FADE_WAIT_DEFAULT_MIN))
        self._fade_tick_id     = None
        self.windows          = []
        self._active_alert    = None
        self._alert_windows   = []   # all currently shown alert dialogs
        self.timers           = load_timers_on_start()
        self._alarm_mgr_win   = None
        self._timer_mgr_win   = None

        display = Gdk.Display.get_default()
        for i in range(display.get_n_monitors()):
            geom  = display.get_monitor(i).get_geometry()
            state = self._load_window_state(i, geom, shared)
            win   = ClockWindow(self, i, geom, state)
            self.windows.append(win)

        if self.click_through:
            for w in self.windows:
                w.apply_click_through(True)

        screen = Gdk.Screen.get_default()
        screen.connect('monitors-changed', self._on_monitors_changed)

        self._build_tray()
        self._tracker_panel  = None
        GLib.timeout_add(1000, self._tick)
        # Arm the RTC for any existing wake-alarm at startup (covers reboot /
        # clock-restart — the RTC was previously only armed on alarm edit/dismiss,
        # so a restart left existing alarms unable to wake the machine).
        schedule_rtc_wake(load_alarms(), self.timers)
        # Set up the media-key alarm-dismiss monitor (Play/Pause from lock screen).
        self._setup_media_key_monitor()
        # Show tracker first-run dialog if WebKit available and not yet seen/disabled
        if _WEBKIT_AVAILABLE:
            cfg = load_tracker_config()
            if not cfg.get('tracker_intro_seen') and not cfg.get('tracker_disabled'):
                GLib.idle_add(self._show_tracker_intro)
        # Fire any timers that expired while system was off/suspended
        for t in list(self.timers):
            if t['state'] == 'completed':
                GLib.idle_add(self._fire_timer, t.copy())

    def _load_window_state(self, idx, geom, shared):
        mon_data = load_monitor_state(geom)
        if self.sync:
            state = shared.copy()
            # Override with per-monitor position and window_mode
            if 'x' in mon_data: state['x'] = mon_data['x']
            if 'y' in mon_data: state['y'] = mon_data['y']
            state['window_mode'] = mon_data.get('window_mode', 'normal')
        else:
            state = mon_data if mon_data else shared.copy()
        return state

    def _on_monitors_changed(self, screen):
        for win in self.windows:
            win._sync_monitor_geom()
            win.queue_draw()

    def save_window_state(self, win, data):
        # Always save full state to monitor file
        save_monitor_state(win.monitor_geom, data)

        if self.sync:
            # Save shared visual settings (not position or window_mode)
            shared = load_state()
            for k in ('size_name','theme_name','opacity','mode',
                      'show_seconds','show_date','marker_style','hand_style'):
                if k in data: shared[k] = data[k]
            shared['sync_settings'] = self.sync
            save_state(shared)

            # Propagate visual settings to other windows
            for w in self.windows:
                if w is win: continue
                tn = data.get('theme_name', 'Dark')
                if tn == 'Minimal': tn = 'Clear'
                w.theme         = THEMES.get(tn, w.theme)
                w.opacity_level = float(data.get('opacity', w.opacity_level))
                w.mode          = data.get('mode', w.mode)
                w.show_seconds  = bool(data.get('show_seconds', w.show_seconds))
                w.show_date     = bool(data.get('show_date', w.show_date))
                w.marker_style  = data.get('marker_style', w.marker_style)
                w.hand_style    = data.get('hand_style',   w.hand_style)
                if w.theme is not THEMES['Clear']:
                    w.props.opacity = w.opacity_level
                else:
                    w.props.opacity = 1.0
                w.queue_draw()

    def _build_tray(self):
        self.tray = Gtk.StatusIcon()
        self.tray.set_from_pixbuf(self._make_tray_pixbuf())
        self._update_tray_tooltip()
        self.tray.set_visible(True)
        self.tray.connect('activate',   self._tray_left_click)
        self.tray.connect('popup-menu', self._tray_right_click)

    def _update_tray_tooltip(self):
        self.tray.set_tooltip_text(
            'Clock  [click-through active]' if self.click_through else 'Clock')

    def _make_tray_pixbuf(self):
        sz  = 24
        sur = cairo.ImageSurface(cairo.FORMAT_ARGB32, sz, sz)
        ctx = cairo.Context(sur)
        cx  = cy = sz / 2.0
        r   = sz / 2.0 - 1.5
        al  = 0.50 if self.click_through else 1.0

        # Face
        ctx.arc(cx, cy, r, 0, 2 * math.pi)
        ctx.set_source_rgba(0.12, 0.15, 0.22, al); ctx.fill()
        ctx.arc(cx, cy, r, 0, 2 * math.pi)
        ctx.set_source_rgba(0.65, 0.75, 0.90, al)
        ctx.set_line_width(1.2); ctx.stroke()

        # Hands (10:10 — classic advertising position)
        ctx.set_source_rgba(1, 1, 1, al)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        for ang, frac, lw in [
            ((10 / 6.0) * math.pi, 0.50, 1.8),   # hour  ~10
            (( 2 / 6.0) * math.pi, 0.70, 1.4),   # minute ~2
        ]:
            ctx.set_line_width(lw)
            ctx.move_to(cx, cy)
            ctx.line_to(cx + r * frac * math.sin(ang), cy - r * frac * math.cos(ang))
            ctx.stroke()

        # Centre stud
        ctx.arc(cx, cy, 1.5, 0, 2 * math.pi)
        ctx.set_source_rgba(0.95, 0.65, 0.20, al); ctx.fill()

        # Click-through indicator: orange badge top-right
        if self.click_through:
            br = 4.5; bx = sz - br - 0.5; by = br + 0.5
            ctx.arc(bx, by, br, 0, 2 * math.pi)
            ctx.set_source_rgba(1.0, 0.50, 0.08, 1.0); ctx.fill()
            ctx.arc(bx, by, br, 0, 2 * math.pi)
            ctx.set_source_rgba(1.0, 0.80, 0.40, 0.70)
            ctx.set_line_width(0.8); ctx.stroke()

        return Gdk.pixbuf_get_from_surface(sur, 0, 0, sz, sz)

    def set_click_through(self, enabled):
        self.click_through = enabled
        for w in self.windows:
            w.apply_click_through(enabled)
        shared = load_state()
        shared['click_through'] = enabled
        save_state(shared)
        self.tray.set_from_pixbuf(self._make_tray_pixbuf())
        self._update_tray_tooltip()

    def set_show_monitor_id(self, enabled):
        self.show_monitor_id = enabled
        shared = load_state()
        shared['show_monitor_id'] = enabled
        save_state(shared)
        for w in self.windows:
            w.queue_draw()

    def _tray_left_click(self, icon):
        self._show_main_menu(0, Gtk.get_current_event_time())

    def _tray_right_click(self, icon, button, time):
        menu = Gtk.Menu()
        about_item = Gtk.MenuItem(label='About')
        about_item.connect('activate', self._show_about)
        menu.append(about_item)
        menu.append(Gtk.SeparatorMenuItem())
        exit_item = Gtk.MenuItem(label='Exit')
        exit_item.connect('activate', lambda _: self._quit())
        menu.append(exit_item)
        menu.append(Gtk.SeparatorMenuItem())
        close_item = Gtk.MenuItem(label='Close menu')
        close_item.connect('activate', lambda _: None)
        menu.append(close_item)
        menu.show_all()
        menu.popup(None, None, None, None, button, time)

    def _show_about(self, _):
        dlg = Gtk.MessageDialog(
            parent=None, flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=f'{APP_NAME}  v{APP_VERSION}')
        dlg.format_secondary_text('Desktop clock for Linux\nPython + GTK3')
        dlg.run(); dlg.destroy()

    def _show_main_menu(self, button, time):
        menu = Gtk.Menu()
        display     = Gdk.Display.get_default()
        primary_mon = display.get_primary_monitor()
        primary_idx = next((i for i in range(display.get_n_monitors())
                            if display.get_monitor(i) == primary_mon), 0)

        # ── Fade-to-clear (v1.2.0 #330) ────────────────────────────
        # Restore clock — only shown while a fade cycle is active.
        # Tray is the user's escape hatch when the clock is invisible
        # or click-through, since they can't right-click the clock.
        if self.any_window_in_fade_cycle():
            restore_item = Gtk.MenuItem(label='Restore clock')
            restore_item.connect('activate', lambda _: self.restore_all_clocks())
            menu.append(restore_item)
            menu.append(Gtk.SeparatorMenuItem())

        # Fade timeout — how long the clock stays invisible before
        # fading back. Always visible regardless of cycle state so
        # the user can change it when they remember.
        fade_item = Gtk.MenuItem(label=f'Fade timeout: {self.fade_wait_minutes} min')
        fade_sub  = Gtk.Menu()
        for opt in FADE_WAIT_OPTIONS_MIN:
            mi = Gtk.CheckMenuItem(label=f'{opt} min')
            mi.set_active(opt == self.fade_wait_minutes)
            mi.connect('activate', lambda _, v=opt: self.set_fade_wait_minutes(v))
            fade_sub.append(mi)
        fade_item.set_submenu(fade_sub)
        menu.append(fade_item)
        menu.append(Gtk.SeparatorMenuItem())

        # Per-monitor: visibility + snap + options (when click-through active)
        for win in self.windows:
            label = f'Monitor {win.monitor_idx + 1}'
            if win.monitor_idx == primary_idx:
                label += ' (Primary)'
            mon_item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            hide_mi = Gtk.CheckMenuItem(label='Hide clock')
            hide_mi.set_active(win.window_mode == 'hidden')
            hide_mi.connect('activate', lambda i, w=win: w.set_window_mode('hidden' if i.get_active() else 'normal'))
            sub.append(hide_mi)
            sub.append(Gtk.SeparatorMenuItem())
            snap_item = Gtk.MenuItem(label='Snap to corner')
            snap_sub  = Gtk.Menu()
            for sl, sv in [('Top right','tr'),('Top left','tl'),
                            ('Bottom right','br'),('Bottom left','bl')]:
                si = Gtk.MenuItem(label=sl)
                si.connect('activate', lambda _, w=win, p=sv: w._snap(None, p))
                snap_sub.append(si)
            snap_item.set_submenu(snap_sub)
            sub.append(snap_item)

            if self.click_through:
                sub.append(Gtk.SeparatorMenuItem())

                mode_item = Gtk.MenuItem(label='Display')
                mode_sub  = Gtk.Menu()
                for name in MODES:
                    mi = Gtk.CheckMenuItem(label=name)
                    mi.set_active(name == win.mode)
                    mi.connect('activate', win._set_mode, name)
                    mode_sub.append(mi)
                mode_item.set_submenu(mode_sub); sub.append(mode_item)

                s_item = Gtk.CheckMenuItem(label='Show seconds')
                s_item.set_active(win.show_seconds)
                s_item.connect('activate', win._toggle_seconds)
                sub.append(s_item)

                d_item = Gtk.CheckMenuItem(label='Show date')
                d_item.set_active(win.show_date)
                d_item.connect('activate', win._toggle_date)
                sub.append(d_item)

                if win.mode in ('Analog', 'Both'):
                    mk_item = Gtk.MenuItem(label='Hour markers')
                    mk_sub  = Gtk.Menu()
                    for name in MARKER_STYLES:
                        mi = Gtk.CheckMenuItem(label=name)
                        mi.set_active(name == win.marker_style)
                        mi.connect('activate', win._set_marker_style, name)
                        mk_sub.append(mi)
                    mk_item.set_submenu(mk_sub); sub.append(mk_item)

                    hnd_item = Gtk.MenuItem(label='Hand style')
                    hnd_sub  = Gtk.Menu()
                    for name in HAND_STYLES:
                        mi = Gtk.CheckMenuItem(label=name)
                        mi.set_active(name == win.hand_style)
                        mi.connect('activate', win._set_hand_style, name)
                        hnd_sub.append(mi)
                    hnd_item.set_submenu(hnd_sub); sub.append(hnd_item)

                sz_item = Gtk.MenuItem(label='Size')
                sz_sub  = Gtk.Menu()
                for name, px in SIZES.items():
                    mi = Gtk.CheckMenuItem(label=f'{name}  ({px}px)')
                    mi.set_active(px == win.size)
                    mi.connect('activate', win._set_size, px)
                    sz_sub.append(mi)
                sz_item.set_submenu(sz_sub); sub.append(sz_item)

                th_item = Gtk.MenuItem(label='Theme')
                th_sub  = Gtk.Menu()
                for name, t in THEMES.items():
                    mi = Gtk.CheckMenuItem(label=name)
                    mi.set_active(t is win.theme)
                    mi.connect('activate', win._set_theme, t)
                    th_sub.append(mi)
                th_item.set_submenu(th_sub); sub.append(th_item)

                if win.theme is not THEMES['Clear']:
                    op_item = Gtk.MenuItem(label='Opacity')
                    op_sub  = Gtk.Menu()
                    for lbl, val in [('100%',1.0),('90%',0.9),('80%',0.8),('70%',0.7),
                                      ('60%',0.6),('50%',0.5),('40%',0.4),('30%',0.3),
                                      ('20%',0.2),('10%',0.1)]:
                        mi = Gtk.CheckMenuItem(label=lbl)
                        mi.set_active(abs(val - win.opacity_level) < 0.05)
                        mi.connect('activate', win._set_opacity, val)
                        op_sub.append(mi)
                    op_item.set_submenu(op_sub); sub.append(op_item)

            mon_item.set_submenu(sub)
            menu.append(mon_item)

        menu.append(Gtk.SeparatorMenuItem())

        alarms_item = Gtk.MenuItem(label='Alarms…')
        alarms_item.connect('activate', lambda _: self.open_alarm_manager(self.windows[0]))
        menu.append(alarms_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Click-through toggle
        ct_item = Gtk.CheckMenuItem(label='Click-through')
        ct_item.set_active(self.click_through)
        ct_item.connect('activate', lambda i: self.set_click_through(i.get_active()))
        menu.append(ct_item)

        mid_item = Gtk.CheckMenuItem(label='Show monitor IDs')
        mid_item.set_active(self.show_monitor_id)
        mid_item.connect('activate', lambda i: self.set_show_monitor_id(i.get_active()))
        menu.append(mid_item)

        menu.append(Gtk.SeparatorMenuItem())

        sync_item = Gtk.CheckMenuItem(label='Sync settings across monitors')
        sync_item.set_active(self.sync)
        sync_item.connect('activate', self._toggle_sync)
        menu.append(sync_item)

        menu.append(Gtk.SeparatorMenuItem())

        cfg = load_tracker_config()
        parent_win = self.windows[0] if self.windows else None
        if not cfg.get('tracker_disabled') and tracker_configured():
            bug_item = Gtk.MenuItem(label='Report a bug…')
            bug_item.connect('activate', lambda _, p=parent_win: self.open_tracker_panel(p))
            menu.append(bug_item)
        ev = Gtk.get_current_event()
        shift = bool(ev and ev.get_state()[1] & Gdk.ModifierType.SHIFT_MASK)
        if shift:
            reset_item = Gtk.MenuItem(label='Reset tracker setup…')
            reset_item.connect('activate', lambda _, p=parent_win: self.reset_tracker(p))
            menu.append(reset_item)

        menu.append(Gtk.SeparatorMenuItem())

        close_item = Gtk.MenuItem(label='Close menu')
        close_item.connect('activate', lambda _: None)
        menu.append(close_item)

        menu.show_all()
        menu.popup(None, None, None, None, button, time)

    def _set_win_mode(self, item, win, mode):
        if item.get_active():
            win.set_window_mode(mode)

    def _toggle_sync(self, item):
        self.sync = item.get_active()
        shared = load_state()
        shared['sync_settings'] = self.sync
        save_state(shared)

    def _tick(self):
        self._check_alarms()
        self._check_timers()
        for w in self.windows:
            if w.get_visible() and w.window_mode != 'hidden':
                w.queue_draw()
                if w._hover_overlay.get_visible():
                    w._update_hover_overlay()
        return True

    def _check_alarms(self):
        if self._active_alert and self._active_alert.get_visible():
            return
        now          = datetime.now()
        current_hhmm = now.strftime('%H:%M')
        current_dow  = now.weekday()
        alarms       = load_alarms()
        changed      = False

        for alarm in alarms:
            if not alarm.get('enabled'): continue
            if alarm['time'] != current_hhmm: continue
            last = alarm.get('last_fired')
            if last == now.strftime('%Y-%m-%d %H:%M'): continue
            if alarm.get('repeat','Once') == 'Weekdays' and current_dow >= 5: continue

            alarm['last_fired'] = now.strftime('%Y-%m-%d %H:%M')
            if alarm.get('repeat','Once') == 'Once': alarm['enabled'] = False
            changed = True
            GLib.idle_add(self._fire_alarm, alarm.copy())

        if changed:
            save_alarms(alarms)
            # Re-arm the RTC for the next occurrence after a fire (e.g. a Daily
            # alarm rolling to tomorrow). Previously only dismiss/edit re-armed,
            # so a fired repeating alarm left the wakealarm empty until next edit.
            schedule_rtc_wake(alarms, self.timers)

    def _set_clock_clickthrough(self, enabled):
        for w in self.windows:
            w.apply_click_through(enabled)

    # ── Fade-to-clear cycle (v1.2.0 #330) ─────────────────────────────
    # Per-window state machines tick from one shared 50 ms timer here.
    # The timer auto-stops when no window is in a fade cycle.
    def _ensure_fade_tick(self):
        if self._fade_tick_id is not None:
            return
        self._fade_tick_id = GLib.timeout_add(FADE_TICK_MS, self._fade_tick)

    def _fade_tick(self):
        any_active = False
        for w in self.windows:
            if w._fade_advance():
                any_active = True
        if not any_active:
            self._fade_tick_id = None
            return False
        return True

    def any_window_in_fade_cycle(self):
        return any(w._fade_state != 'idle' for w in self.windows)

    def restore_all_clocks(self):
        """Tray menu Restore clock — cancel any active fade, restore
        baseline on every window."""
        for w in self.windows:
            if w._fade_state != 'idle':
                w.fade_cancel()

    def force_fast_fade_in_all(self):
        """Alarm/timer fire — accelerate fade-in to ~2.5 s on any
        window currently in the fade cycle. No-op for windows already
        in idle."""
        for w in self.windows:
            if w._fade_state != 'idle':
                w.fade_force_fast_in()
        if self.any_window_in_fade_cycle():
            self._ensure_fade_tick()

    def set_fade_wait_minutes(self, minutes):
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return
        if minutes < 1:
            minutes = 1
        self.fade_wait_minutes = minutes
        shared = load_state()
        shared['fade_wait_minutes'] = minutes
        save_state(shared)

    def _on_alert_hide(self, dlg):
        if dlg in self._alert_windows:
            self._alert_windows.remove(dlg)
        if not self._alert_windows and not self.click_through:
            self._set_clock_clickthrough(False)

    def _fire_alarm(self, alarm):
        # v1.2.0 #330: if any clock is currently in the fade cycle, force
        # a fast fade-in (~2.5 s) so the user can SEE the alarm fire.
        # A silently ringing invisible clock is the opposite of useful.
        self.force_fast_fade_in_all()
        if alarm['style'] == 'sound':
            play_tone(alarm['tone'])
        else:
            parent = next((w for w in self.windows if w.get_visible()), self.windows[0])
            dlg = AlertDialog(alarm, self._alarm_dismissed)
            self._active_alert = dlg
            self._media_monitor_active(True)   # allow Play/Pause dismiss while ringing
            geom = getattr(parent, 'monitor_geom', None)
            nx, ny = self._find_clear_position(300, 200, geom)
            dlg.move(nx, ny)
            dlg.show_all()
            self._alert_windows.append(dlg)
            self._set_clock_clickthrough(True)
            dlg.connect('hide', self._on_alert_hide)
        return False

    def _check_timers(self):
        now_ts = datetime.now().timestamp()
        self.timers = [t for t in self.timers
                       if t['state'] != 'completed' or
                       (t['completed_at'] and now_ts - t['completed_at'] < 5)]
        changed = False
        for t in self.timers:
            if t['state'] != 'running': continue
            t['remaining'] -= 1
            changed = True
            if t['remaining'] <= 0:
                t['remaining'] = 0
                t['state'] = 'completed'
                t['completed_at'] = now_ts
                GLib.idle_add(self._fire_timer, t.copy())
        if changed:
            save_timers(self.timers)
            schedule_rtc_wake(load_alarms(), self.timers)

    def _fire_timer(self, timer):
        # v1.2.0 #330: as for alarms — bring any faded clock back fast.
        self.force_fast_fade_in_all()

        def on_snooze(secs, snooze_count=0):
            self.timers.append(new_timer(
                label=timer['label'],
                total_secs=secs,
                tone=timer['tone'],
                never_give_up=timer.get('never_give_up', False),
                snooze_count=snooze_count,
            ))
        parent = next((w for w in self.windows if w.get_visible()), self.windows[0])
        dlg = TimerAlertDialog(timer, lambda: None, on_snooze=on_snooze)
        geom = getattr(parent, 'monitor_geom', None)
        nx, ny = self._find_clear_position(300, 220, geom)
        dlg.move(nx, ny)
        dlg.show_all()
        self._alert_windows.append(dlg)
        self._set_clock_clickthrough(True)
        dlg.connect('hide', self._on_alert_hide)
        return False

    def _find_clear_position(self, w, h, geom=None, extra_occupied=None):
        """Return (x,y) that places a w×h window on the given monitor without overlap."""
        if geom:
            mx, my, mw, mh = geom.x, geom.y, geom.width, geom.height
        else:
            s = Gdk.Screen.get_default()
            mx, my, mw, mh = 0, 0, s.get_width(), s.get_height()

        # Default: centre of monitor
        cx = mx + (mw - w) // 2
        cy = my + (mh - h) // 2

        occupied = list(extra_occupied or [])
        for win in [self._alarm_mgr_win, self._timer_mgr_win] + self._alert_windows:
            if win and win.get_visible():
                try:
                    ox, oy = win.get_position()
                    ow, oh = win.get_size()
                    occupied.append((ox, oy, ow, oh))
                except Exception:
                    pass

        def _clear(nx, ny):
            # Clamp strictly to monitor
            nx = max(mx, min(nx, mx + mw - w))
            ny = max(my, min(ny, my + mh - h))
            return nx, ny, all(
                nx + w <= ox or nx >= ox + ow or ny + h <= oy or ny >= oy + oh
                for ox, oy, ow, oh in occupied)

        if not occupied:
            nx, ny, _ = _clear(cx, cy)
            return nx, ny

        step = 40
        for s in range(0, max(mw, mh), step):
            for dx, dy in [(s, s), (-s, s), (s, -s), (-s, -s),
                           (s, 0), (-s, 0), (0, s), (0, -s)]:
                nx, ny, ok = _clear(cx + dx, cy + dy)
                if ok:
                    return nx, ny

        # Fallback: cascade from last occupied window, clamped
        ox, oy = occupied[-1][0], occupied[-1][1]
        nx, ny, _ = _clear(ox + 40, oy + 40)
        return nx, ny

    def _show_window(self, win, w, h, geom=None, extra_occupied=None):
        """Position win at a clear spot then show it."""
        win.set_position(Gtk.WindowPosition.NONE)
        nx, ny = self._find_clear_position(w, h, geom, extra_occupied)
        win.move(nx, ny)
        win.show_all()

    def open_alarm_manager(self, parent):
        if self._alarm_mgr_win and self._alarm_mgr_win.get_visible():
            self._alarm_mgr_win.present(); return
        self._alarm_mgr_win = AlarmManagerWindow(parent)
        geom = getattr(parent, 'monitor_geom', None)
        self._show_window(self._alarm_mgr_win, 460, 300, geom)

    def open_timer_manager(self, parent):
        if self._timer_mgr_win and self._timer_mgr_win.get_visible():
            self._timer_mgr_win.present(); return
        self._timer_mgr_win = TimerManagerWindow(parent, self)
        geom = getattr(parent, 'monitor_geom', None)
        self._show_window(self._timer_mgr_win, 480, 260, geom)

    def _alarm_dismissed(self, alarm):
        self._active_alert = None
        self._media_monitor_active(False)
        for w in self.windows: w.queue_draw()

    # ── Media-key alarm dismiss (Play/Pause from the lock screen) ──────────
    def _setup_media_key_monitor(self):
        """Watch the Play/Pause (and Stop) media keys via XInput2 raw events so a
        ringing alarm can be dismissed from the lock screen with no login. Raw
        events bypass keyboard grabs (incl. the locker) and are observe-only, so
        they never consume the key or disturb normal media use. The selection is
        toggled on only WHILE an alarm is ringing."""
        self._xi_display = None
        self._xi_root = None
        self._xi_dismiss_codes = set()
        self._xi_active = False
        self._last_media_dismiss = 0
        if not _XLIB_OK:
            return
        try:
            d = _xdisplay.Display()
            d.query_extension('XInputExtension')
            root = d.screen().root
            for ks in (0x1008FF14, 0x1008FF31, 0x1008FF15):  # XF86Audio Play/Pause/Stop
                kc = d.keysym_to_keycode(ks)
                if kc:
                    self._xi_dismiss_codes.add(kc)
            if not self._xi_dismiss_codes:
                d.close(); return
            self._xi_display = d
            self._xi_root = root
            GLib.io_add_watch(d.fileno(), GLib.IO_IN, self._on_xi_event)
        except Exception:
            self._xi_display = None

    def _media_monitor_active(self, on):
        d = getattr(self, '_xi_display', None)
        if not d or bool(on) == self._xi_active:
            return
        try:
            mask = (1 << _xinput.RawKeyPress) if on else 0
            self._xi_root.xinput_select_events([(_xinput.AllDevices, mask)])
            d.sync()
            self._xi_active = bool(on)
        except Exception:
            pass

    def _on_xi_event(self, source, condition):
        d = getattr(self, '_xi_display', None)
        if not d:
            return False
        try:
            while d.pending_events() > 0:
                ev = d.next_event()
                if getattr(ev, 'evtype', None) != _xinput.RawKeyPress:
                    continue
                # XIRawEvent payload: deviceid(2) + time(4) + detail(4) -> keycode at offset 6
                kc = int.from_bytes(ev.data[6:10], 'little')
                if kc in self._xi_dismiss_codes:
                    self._on_media_dismiss_key()
        except Exception:
            pass
        return True   # keep the watch

    def _on_media_dismiss_key(self):
        now = GLib.get_monotonic_time()
        if now - self._last_media_dismiss < 500_000:   # debounce 0.5 s (raw events double up)
            return
        self._last_media_dismiss = now
        alert = self._active_alert
        if alert and alert.get_visible():
            alert._dismiss()                # stops sound + hides + _alarm_dismissed
            self._repause_media_players()   # undo any player the same key resumed

    def _repause_media_players(self):
        """The Play/Pause that dismissed the alarm also reaches any media player
        (e.g. Chrome) and may have resumed it. Tell every MPRIS player to Pause,
        a moment later, so a resumed video goes quiet again."""
        def _do():
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                res = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                    'org.freedesktop.DBus', 'ListNames', None,
                                    GLib.VariantType('(as)'), Gio.DBusCallFlags.NONE, 800, None)
                for nm in res.unpack()[0]:
                    if nm.startswith('org.mpris.MediaPlayer2.'):
                        try:
                            bus.call_sync(nm, '/org/mpris/MediaPlayer2',
                                          'org.mpris.MediaPlayer2.Player', 'Pause', None, None,
                                          Gio.DBusCallFlags.NONE, 800, None)
                        except Exception:
                            pass
            except Exception:
                pass
            return False
        GLib.timeout_add(250, _do)

    # ── Tracker ───────────────────────────────────────────────────────

    def _show_tracker_intro(self):
        parent = self.windows[0] if self.windows else None

        def on_configure():
            save_tracker_config({'tracker_intro_seen': True})
            dlg = TrackerConfigDialog(parent)
            nx, ny = self._find_clear_position(460, 260,
                                               getattr(parent, 'monitor_geom', None))
            dlg.move(nx, ny)
            dlg.show_all()

        def on_disable():
            save_tracker_config({'tracker_intro_seen': True, 'tracker_disabled': True})

        def on_later():
            pass  # flag stays False — will show again next launch

        dlg = TrackerIntroDialog(on_configure, on_disable, on_later)
        nx, ny = self._find_clear_position(440, 340,
                                           getattr(parent, 'monitor_geom', None))
        dlg.move(nx, ny)
        return False

    def open_tracker_panel(self, parent):
        if self._tracker_panel and self._tracker_panel.get_visible():
            self._tracker_panel.present()
            return
        self._tracker_panel = TrackerPanelWindow(parent)
        geom = getattr(parent, 'monitor_geom', None)
        nx, ny = self._find_clear_position(480, 600, geom)
        self._tracker_panel.move(nx, ny)

    def reset_tracker(self, parent):
        save_tracker_config({
            'tracker_url':         '',
            'tracker_project_key': '',
            'tracker_api_key':     '',
            'tracker_intro_seen':  False,
            'tracker_disabled':    False,
        })
        if _WEBKIT_AVAILABLE:
            GLib.idle_add(self._show_tracker_intro)

    def _quit(self):
        for w in self.windows: w._save_all()
        save_timers(self.timers)
        schedule_rtc_wake(load_alarms(), self.timers)
        Gtk.main_quit()


# ─── Entry point ──────────────────────────────────────────────────────

manager = ClockManager()

def _on_sigterm(signum, frame):
    for w in manager.windows: w._save_all()
    Gtk.main_quit()

signal.signal(signal.SIGTERM, _on_sigterm)
signal.signal(signal.SIGINT,  _on_sigterm)

Gtk.main()
