#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Gtk, GLib, Gdk, Pango, PangoCairo
import cairo
import math
import json
import os
import signal
import warnings
warnings.filterwarnings('ignore', message='.*StatusIcon.*', category=DeprecationWarning)
import uuid
import wave
import array
import tempfile
import subprocess
import shutil
from datetime import datetime, timedelta

STATE_FILE  = os.path.expanduser('~/clock/state.json')
ALARMS_FILE = os.path.expanduser('~/clock/alarms.json')

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

def monitor_state_file(geom):
    return os.path.expanduser(f'~/clock/state_monitor_{geom.x}_{geom.y}.json')

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

def schedule_rtc_wake(alarms):
    if not shutil.which('rtcwake'):
        return
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
    if earliest:
        ts = int(earliest.timestamp()) - 60
        subprocess.run(['sudo', 'rtcwake', '-m', 'no', '-t', str(ts)],
                       capture_output=True)

# ─── Alert dialog ─────────────────────────────────────────────────────

class AlertDialog(Gtk.Window):
    def __init__(self, alarm, on_dismiss):
        super().__init__()
        self.alarm      = alarm
        self.on_dismiss = on_dismiss
        self._proc      = None
        self._expire_id = None

        self.set_title('Alarm')
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

        if alarm['style'] in ('sound', 'both'):
            self._start_sound()
        if alarm['dismiss'] == 'auto':
            secs = alarm.get('expire_mins', 5) * 60
            self._expire_id = GLib.timeout_add_seconds(secs, self._dismiss, None)
        self.show_all()

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
        self.alarm = alarm.copy() if alarm else new_alarm()
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.add_button('Save',   Gtk.ResponseType.OK)

        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        grid.set_margin_top(16); grid.set_margin_bottom(16)
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

# ─── Alarm manager window ─────────────────────────────────────────────

class AlarmManagerWindow(Gtk.Window):
    def __init__(self, parent):
        super().__init__(title='Alarms')
        self.set_transient_for(parent)
        self.set_default_size(460, 280)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(8)
        self.alarms = load_alarms()

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar = Gtk.Box(spacing=6)
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
        self.add(vbox); self._refresh(); self.show_all()

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
HAND_STYLES   = ['Classic', 'Deco', 'Modern']
ROMAN         = ['I','II','III','IV','V','VI','VII','VIII','IX','X','XI','XII']
WIN_MODES     = ['Normal', 'Hidden']
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
        super().__init__()
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

        self._build_window()
        self.props.opacity = 1.0 if self.theme is THEMES['Clear'] else self.opacity_level

        if self.window_mode == 'hidden':
            GLib.idle_add(lambda: self.set_window_mode('hidden') or False)

    # ── Window construction ───────────────────────────────────────────

    def _build_window(self):
        self.set_title(f'Clock-{self.monitor_idx}')
        self.set_decorated(False)
        self.set_keep_above(True)
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
            Gdk.EventMask.POINTER_MOTION_MASK)
        self.da.connect('button-press-event',   self._on_button_press)
        self.da.connect('button-release-event', self._on_button_release)
        self.da.connect('motion-notify-event',  self._on_motion)
        self.connect('key-press-event', self._on_key_press)

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
                'marker_style': self.marker_style, 'hand_style': self.hand_style,
                'digital_style': self.digital_style, 'digital_font': self.digital_font,
                'window_mode': self.window_mode}
        self.manager.save_window_state(self, data)

    def _on_configure(self, window, event):
        if not self._resizing and self.window_mode == 'normal':
            self._saved_pos = (event.x, event.y)
            self._save_all(event.x, event.y)

    # ── Input handling ────────────────────────────────────────────────

    def _on_key_press(self, widget, event):
        pass

    def _on_button_press(self, widget, event):
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and event.button == 1:
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

    # ── Right-click menu ──────────────────────────────────────────────

    def _show_menu(self, event):
        menu = Gtk.Menu()

        # Window mode submenu
        win_item = Gtk.MenuItem(label='This monitor')
        win_sub  = Gtk.Menu()
        for label, val in [('Normal','normal'),('Hide','hidden')]:
            mi = Gtk.CheckMenuItem(label=label)
            mi.set_active(self.window_mode == val)
            mi.connect('activate', lambda i, v=val: self.set_window_mode(v) if i.get_active() else None)
            win_sub.append(mi)
        win_item.set_submenu(win_sub)
        menu.append(win_item)

        alarms_item = Gtk.MenuItem(label='Alarms…')
        alarms_item.connect('activate', lambda _: AlarmManagerWindow(self))
        menu.append(alarms_item)

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
        pad_x  = tw * 0.10; pad_y = th * 0.30
        g      = th * 0.18
        bs     = max(14, th * 0.55) if n > 0 else 0
        rows_h = th
        if n > 0:  rows_h += g + bs
        if show_d: rows_h += g + dh
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
        pad_y = max(4.0, dh * 0.18)
        box_h = dh + pad_y * 2
        if self.show_date:
            box_h += max(6, dh * 0.28) + igap * 2
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
        pad_x = max(4.0, tw * 0.30)
        pad_y = max(4.0, th * 0.15)
        box_w = total_w + pad_x * 2
        box_h = th + pad_y * 2
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

    def _constraint_rect(self):
        """Return (left, top, right, bottom) of the draggable area for this monitor."""
        try:
            display = Gdk.Display.get_default()
            mon = display.get_monitor(self.monitor_idx)
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
        face_alpha = 1.0 if self.opacity_level >= 1.0 else face[3]
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

        # Alarm bell
        n = self._active_alarm_count()
        if n > 0:
            bell_size = max(14, r * 0.28)
            self._draw_bell(cr, cx, cy - r * 0.45, bell_size,
                            (0.90, 0.15, 0.15, 0.95), rotate=math.pi / 4, count=n)

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

    def _draw_digital_badge(self, cr, w, h, t):
        if not self.manager.show_monitor_id: return
        mid = self.monitor_idx + 1
        fill_c, text_c = self._id_badge_colors(t['face'])
        badge_r = max(7, int(h * 0.12))
        bcx = w - badge_r - 5;  bcy = h - badge_r - 5
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

        n      = self._active_alarm_count()
        show_d = self.show_date
        pad_x  = tw * 0.10; pad_y = th * 0.30
        g      = th * 0.18
        bs     = max(14, th * 0.55) if n > 0 else 0

        rows_h = th
        if n > 0:     rows_h += g + bs
        if show_d:    rows_h += g + dh

        box_w = min(max(tw, dw if show_d else tw) + pad_x * 2, self.size - 16)
        box_h = rows_h + pad_y * 2
        bx    = cx - box_w / 2; by = cy - box_h / 2

        face = t['face']
        face_alpha = 1.0 if self.opacity_level >= 1.0 else face[3]
        cr.set_source_rgba(face[0], face[1], face[2], face_alpha)
        self._rounded_rect(cr, bx, by, box_w, box_h, 14); cr.fill()
        cr.set_source_rgba(*t['border']); cr.set_line_width(2.0)
        self._rounded_rect(cr, bx, by, box_w, box_h, 14); cr.stroke()

        y = by + pad_y
        self._draw_text(cr, cx, y, time_str, time_size, t['digital']); y += th
        if n > 0:
            y += g
            self._draw_bell(cr, cx, y + bs / 2, bs, (0.90, 0.15, 0.15, 0.95),
                            rotate=math.pi / 4, count=n); y += bs
        if show_d:
            y += g
            self._draw_text(cr, cx, y, date_str, date_size, t['marks'], alpha=0.75)

    # ── 7-segment renderer ────────────────────────────────────────────

    def _draw_digital_7seg(self, cr, w, h, now, t):
        time_fmt = '%H:%M:%S' if self.show_seconds else '%H:%M'
        time_str = now.strftime(time_fmt)
        dh, dw, cw, igap, total_w = self._7seg_layout()
        pad_y  = max(4.0, dh * 0.18)
        show_d = self.show_date
        dh2    = max(6, dh * 0.28)
        total_h = dh + pad_y * 2 + (dh2 + igap * 2 if show_d else 0)

        # Background box
        face   = t['face']
        face_a = 1.0 if self.opacity_level >= 1.0 else face[3]
        cr.set_source_rgba(face[0], face[1], face[2], face_a)
        self._rounded_rect(cr, 2, 2, w - 4, h - 4, 14); cr.fill()
        cr.set_source_rgba(*t['border']); cr.set_line_width(2.0)
        self._rounded_rect(cr, 2, 2, w - 4, h - 4, 14); cr.stroke()

        # Segment colours — lit and dim versions of theme digital colour
        dc     = t['digital']
        seg_on  = (dc[0], dc[1], dc[2], 0.95)
        seg_off = (dc[0] * 0.28, dc[1] * 0.28, dc[2] * 0.28, 0.18)

        start_y = (h - total_h) / 2 + pad_y
        start_x = (w - total_w) / 2
        x = start_x
        for ch in time_str:
            if ch == ':':
                self._draw_7seg_char(cr, x, start_y, cw, dh, ch, seg_on, seg_off)
                x += cw + igap
            else:
                self._draw_7seg_char(cr, x, start_y, dw, dh, ch, seg_on, seg_off)
                x += dw + igap

        if show_d:
            date_str  = now.strftime('%a %d %b')
            date_size = max(6, int(dh2 * 0.80))
            date_y    = start_y + dh + igap * 2
            self._draw_text(cr, w / 2, date_y, date_str, date_size,
                            t['marks'], alpha=0.70, font='DejaVu Sans Bold')

    def _draw_7seg_char(self, cr, x, y, cw, ch, char, seg_on, seg_off):
        if char == ':':
            dot_r = max(1.5, cw * 0.22)
            cr.set_source_rgba(*seg_on)
            cr.arc(x + cw / 2, y + ch * 0.30, dot_r, 0, 2 * math.pi); cr.fill()
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
        show_d  = self.show_date
        th2     = max(6, th * 0.50) if show_d else 0
        tw2     = th2 * 0.70        if show_d else 0
        t_gap2  = max(1.0, th2 * 0.05) if show_d else 0

        # Outer box (clock theme)
        face   = t['face']
        face_a = 1.0 if self.opacity_level >= 1.0 else face[3]
        cr.set_source_rgba(face[0], face[1], face[2], face_a)
        self._rounded_rect(cr, 2, 2, w - 4, h - 4, 14); cr.fill()
        cr.set_source_rgba(*t['border']); cr.set_line_width(2.0)
        self._rounded_rect(cr, 2, 2, w - 4, h - 4, 14); cr.stroke()

        # Classic split-flap tile palette (dark, independent of clock theme)
        tile_bg  = (0.11, 0.11, 0.14, 0.97)
        tile_fg  = (0.94, 0.91, 0.84, 1.00)
        split_c  = (0.04, 0.04, 0.06, 1.00)
        tile_r   = max(2.0, th * 0.07)

        total_h = th + (th2 + t_gap * 2 if show_d else 0)
        pad_y   = (h - total_h) / 2
        time_x  = (w - total_w) / 2
        time_y  = pad_y

        for ch in time_str:
            self._draw_split_tile(cr, time_x, time_y, tw, th, ch,
                                  tile_bg, tile_fg, split_c, tile_r)
            time_x += tw + t_gap

        if show_d:
            date_str = now.strftime('%a %d %b')
            n_date   = len(date_str)
            date_w   = n_date * tw2 + (n_date - 1) * t_gap2
            date_x   = (w - date_w) / 2
            date_y   = pad_y + th + t_gap * 2
            tile_r2  = max(1.5, th2 * 0.07)
            for ch in date_str:
                self._draw_split_tile(cr, date_x, date_y, tw2, th2, ch,
                                      tile_bg, tile_fg, split_c, tile_r2)
                date_x += tw2 + t_gap2

    def _draw_split_tile(self, cr, x, y, tw, th, char, tile_bg, tile_fg, split_c, tile_r):
        # Tile background
        cr.set_source_rgba(*tile_bg)
        self._rounded_rect(cr, x, y, tw, th, tile_r); cr.fill()
        # Character
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

    def apply_click_through(self, enabled):
        if enabled:
            self.input_shape_combine_region(cairo.Region())
        else:
            full = cairo.Region(cairo.RectangleInt(0, 0, self.size, self.size))
            self.input_shape_combine_region(full)

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
        self.windows       = []
        self._active_alert = None

        display = Gdk.Display.get_default()
        for i in range(display.get_n_monitors()):
            geom  = display.get_monitor(i).get_geometry()
            state = self._load_window_state(i, geom, shared)
            win   = ClockWindow(self, i, geom, state)
            self.windows.append(win)

        if self.click_through:
            for w in self.windows:
                w.apply_click_through(True)

        self._build_tray()
        GLib.timeout_add(1000, self._tick)

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
            text='Clock')
        dlg.format_secondary_text('Desktop clock for Linux\nPython + GTK3')
        dlg.run(); dlg.destroy()

    def _show_main_menu(self, button, time):
        menu = Gtk.Menu()
        display     = Gdk.Display.get_default()
        primary_mon = display.get_primary_monitor()
        primary_idx = next((i for i in range(display.get_n_monitors())
                            if display.get_monitor(i) == primary_mon), 0)

        # Per-monitor: visibility + snap + options (when click-through active)
        for win in self.windows:
            label = f'Monitor {win.monitor_idx + 1}'
            if win.monitor_idx == primary_idx:
                label += ' (Primary)'
            mon_item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            for ml, mv in [('Normal', 'normal'), ('Hidden', 'hidden')]:
                mi = Gtk.CheckMenuItem(label=ml)
                mi.set_active(win.window_mode == mv)
                mi.connect('activate', self._set_win_mode, win, mv)
                sub.append(mi)
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
        alarms_item.connect('activate', lambda _: AlarmManagerWindow(self.windows[0]))
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
        for w in self.windows:
            if w.get_visible() and w.window_mode != 'hidden':
                w.queue_draw()
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

        if changed: save_alarms(alarms)

    def _fire_alarm(self, alarm):
        if alarm['style'] == 'sound':
            play_tone(alarm['tone'])
        else:
            parent = next((w for w in self.windows if w.get_visible()), self.windows[0])
            self._active_alert = AlertDialog(alarm, self._alarm_dismissed)
        return False

    def _alarm_dismissed(self, alarm):
        self._active_alert = None
        for w in self.windows: w.queue_draw()

    def _quit(self):
        for w in self.windows: w._save_all()
        Gtk.main_quit()


# ─── Entry point ──────────────────────────────────────────────────────

manager = ClockManager()

def _on_sigterm(signum, frame):
    for w in manager.windows: w._save_all()
    Gtk.main_quit()

signal.signal(signal.SIGTERM, _on_sigterm)
signal.signal(signal.SIGINT,  _on_sigterm)

Gtk.main()
