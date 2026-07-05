"""Unit tests for desktop-clock pure-logic functions.

All tests here exercise functions that have no GTK main-loop dependency.
Importing clock.py requires DISPLAY (GTK initialises at import time), so
the whole module is guarded by the requires_display mark from conftest.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import mock_open, patch

import pytest

from conftest import requires_display

pytestmark = requires_display

import clock  # noqa: E402 — must come after display guard


# ─── _fmt_secs ────────────────────────────────────────────────────────

class TestFmtSecs:
    def test_zero(self):
        assert clock._fmt_secs(0) == '00:00'

    def test_negative_treated_as_zero(self):
        assert clock._fmt_secs(-5) == '00:00'

    def test_under_a_minute(self):
        assert clock._fmt_secs(45) == '00:45'

    def test_exactly_one_minute(self):
        assert clock._fmt_secs(60) == '01:00'

    def test_minutes_and_seconds(self):
        assert clock._fmt_secs(90) == '01:30'

    def test_just_under_an_hour(self):
        assert clock._fmt_secs(3599) == '59:59'

    def test_exactly_one_hour(self):
        assert clock._fmt_secs(3600) == '1:00:00'

    def test_hours_minutes_seconds(self):
        assert clock._fmt_secs(3661) == '1:01:01'


# ─── FadeSliderOverlay._fmt ───────────────────────────────────────────

class TestFadeSliderFmt:
    """Static method — callable without instantiating the overlay."""

    def test_one_minute(self):
        assert clock.FadeSliderOverlay._fmt(1) == '1 min'

    def test_five_minutes(self):
        assert clock.FadeSliderOverlay._fmt(5) == '5 min'

    def test_fifty_nine_minutes(self):
        assert clock.FadeSliderOverlay._fmt(59) == '59 min'

    def test_exactly_one_hour(self):
        assert clock.FadeSliderOverlay._fmt(60) == '1 h'

    def test_one_hour_thirty(self):
        assert clock.FadeSliderOverlay._fmt(90) == '1 h 30 m'

    def test_two_hours(self):
        assert clock.FadeSliderOverlay._fmt(120) == '2 h'

    def test_two_hours_fifteen(self):
        assert clock.FadeSliderOverlay._fmt(135) == '2 h 15 m'

    def test_four_hours(self):
        assert clock.FadeSliderOverlay._fmt(240) == '4 h'

    def test_float_input_truncated(self):
        assert clock.FadeSliderOverlay._fmt(5.9) == '5 min'


# ─── new_alarm ────────────────────────────────────────────────────────

class TestNewAlarm:
    def test_has_all_required_keys(self):
        a = clock.new_alarm()
        for k in ('id', 'label', 'time', 'repeat', 'style', 'tone',
                  'dismiss', 'expire_mins', 'wake', 'enabled', 'last_fired'):
            assert k in a

    def test_enabled_by_default(self):
        assert clock.new_alarm()['enabled'] is True

    def test_wake_enabled_by_default(self):
        assert clock.new_alarm()['wake'] is True

    def test_last_fired_is_none(self):
        assert clock.new_alarm()['last_fired'] is None

    def test_ids_are_unique(self):
        ids = {clock.new_alarm()['id'] for _ in range(20)}
        assert len(ids) == 20

    def test_time_is_valid_hhmm(self):
        h, m = clock.new_alarm()['time'].split(':')
        assert 0 <= int(h) <= 23
        assert 0 <= int(m) <= 59


# ─── new_timer ────────────────────────────────────────────────────────

class TestNewTimer:
    def test_has_all_required_keys(self):
        t = clock.new_timer()
        for k in ('id', 'label', 'notes', 'total', 'remaining', 'state',
                  'tone', 'never_give_up', 'snooze_count', 'completed_at'):
            assert k in t

    def test_state_is_running(self):
        assert clock.new_timer()['state'] == 'running'

    def test_remaining_equals_total(self):
        t = clock.new_timer(total_secs=300)
        assert t['remaining'] == t['total'] == 300

    def test_label_passed_through(self):
        assert clock.new_timer(label='Coffee')['label'] == 'Coffee'

    def test_default_total_is_sixty_seconds(self):
        assert clock.new_timer()['total'] == 60


# ─── State persistence ────────────────────────────────────────────────

class TestStatePersistence:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'STATE_FILE', str(tmp_path / 'state.json'))
        data = {'theme': 'Dark', 'fade_wait_minutes': 10}
        clock.save_state(data)
        assert clock.load_state() == data

    def test_missing_file_returns_empty_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'STATE_FILE', str(tmp_path / 'missing.json'))
        assert clock.load_state() == {}


# ─── Alarm persistence ────────────────────────────────────────────────

class TestAlarmPersistence:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'ALARMS_FILE', str(tmp_path / 'alarms.json'))
        alarms = [clock.new_alarm(), clock.new_alarm()]
        clock.save_alarms(alarms)
        loaded = clock.load_alarms()
        assert len(loaded) == 2
        assert loaded[0]['id'] == alarms[0]['id']

    def test_missing_file_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'ALARMS_FILE', str(tmp_path / 'missing.json'))
        assert clock.load_alarms() == []


# ─── Timer restoration ────────────────────────────────────────────────

class TestLoadTimersOnStart:
    def _save(self, path, timers):
        with open(path, 'w') as f:
            json.dump(timers, f)

    def _make_timer(self, remaining=300, state='running', elapsed_secs=0):
        saved_at = (datetime.now() - timedelta(seconds=elapsed_secs)).isoformat()
        return {
            'id': 'test-id', 'label': 'T', 'notes': '',
            'total': 300, 'remaining': remaining, 'state': state,
            'tone': 3, 'never_give_up': False, 'snooze_count': 0,
            'completed_at': None, 'saved_at': saved_at,
        }

    def test_running_timer_deducts_elapsed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'TIMERS_FILE', str(tmp_path / 'timers.json'))
        self._save(tmp_path / 'timers.json', [self._make_timer(elapsed_secs=30)])
        restored = clock.load_timers_on_start()
        assert len(restored) == 1
        assert restored[0]['remaining'] == pytest.approx(270, abs=2)

    def test_paused_timer_keeps_remaining(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'TIMERS_FILE', str(tmp_path / 'timers.json'))
        self._save(tmp_path / 'timers.json',
                   [self._make_timer(remaining=150, state='paused', elapsed_secs=60)])
        restored = clock.load_timers_on_start()
        assert restored[0]['remaining'] == 150

    def test_expired_timer_becomes_completed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'TIMERS_FILE', str(tmp_path / 'timers.json'))
        self._save(tmp_path / 'timers.json', [self._make_timer(elapsed_secs=400)])
        restored = clock.load_timers_on_start()
        assert restored[0]['state'] == 'completed'
        assert restored[0]['remaining'] == 0

    def test_expired_timer_gets_auto_dismiss_snooze_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'TIMERS_FILE', str(tmp_path / 'timers.json'))
        self._save(tmp_path / 'timers.json', [self._make_timer(elapsed_secs=400)])
        restored = clock.load_timers_on_start()
        assert restored[0]['snooze_count'] == 3

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'TIMERS_FILE', str(tmp_path / 'missing.json'))
        assert clock.load_timers_on_start() == []

    def test_file_is_deleted_after_load(self, tmp_path, monkeypatch):
        p = tmp_path / 'timers.json'
        monkeypatch.setattr(clock, 'TIMERS_FILE', str(p))
        self._save(p, [self._make_timer()])
        clock.load_timers_on_start()
        assert not p.exists()


# ─── RTC local-time detection ─────────────────────────────────────────

class TestRtcIsLocal:
    def test_local_adjtime(self):
        with patch('builtins.open', mock_open(read_data='0.0 0 0.0\n0\nLOCAL\n')):
            assert clock._rtc_is_local() is True

    def test_utc_adjtime(self):
        with patch('builtins.open', mock_open(read_data='0.0 0 0.0\n0\nUTC\n')):
            assert clock._rtc_is_local() is False

    def test_missing_file_returns_false(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            assert clock._rtc_is_local() is False

    def test_short_adjtime_returns_false(self):
        with patch('builtins.open', mock_open(read_data='only one line\n')):
            assert clock._rtc_is_local() is False


# ─── Tracker config ───────────────────────────────────────────────────

class TestTrackerConfig:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'STATE_FILE', str(tmp_path / 'state.json'))
        cfg = {
            'tracker_url': 'http://tracker.lan',
            'tracker_project_key': 'myapp',
            'tracker_api_key': 'hlt_abc123',
            'tracker_intro_seen': True,
            'tracker_disabled': False,
        }
        clock.save_tracker_config(cfg)
        loaded = clock.load_tracker_config()
        assert loaded['tracker_url'] == 'http://tracker.lan'
        assert loaded['tracker_project_key'] == 'myapp'
        assert loaded['tracker_api_key'] == 'hlt_abc123'

    def test_missing_keys_return_empty_string(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'STATE_FILE', str(tmp_path / 'state.json'))
        loaded = clock.load_tracker_config()
        assert loaded['tracker_url'] == ''
        assert loaded['tracker_project_key'] == ''
        assert loaded['tracker_api_key'] == ''

    def test_configured_false_when_webkit_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'STATE_FILE', str(tmp_path / 'state.json'))
        monkeypatch.setattr(clock, '_WEBKIT_AVAILABLE', False)
        clock.save_tracker_config({
            'tracker_url': 'http://tracker.lan',
            'tracker_project_key': 'myapp',
            'tracker_api_key': 'hlt_abc',
        })
        assert clock.tracker_configured() is False

    def test_configured_false_when_url_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'STATE_FILE', str(tmp_path / 'state.json'))
        monkeypatch.setattr(clock, '_WEBKIT_AVAILABLE', True)
        clock.save_tracker_config({
            'tracker_url': '',
            'tracker_project_key': 'myapp',
            'tracker_api_key': 'hlt_abc',
        })
        assert clock.tracker_configured() is False

    def test_configured_true_when_all_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'STATE_FILE', str(tmp_path / 'state.json'))
        monkeypatch.setattr(clock, '_WEBKIT_AVAILABLE', True)
        clock.save_tracker_config({
            'tracker_url': 'http://tracker.lan',
            'tracker_project_key': 'myapp',
            'tracker_api_key': 'hlt_abc',
        })
        assert clock.tracker_configured() is True

    def test_configured_false_when_key_whitespace_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'STATE_FILE', str(tmp_path / 'state.json'))
        monkeypatch.setattr(clock, '_WEBKIT_AVAILABLE', True)
        clock.save_tracker_config({
            'tracker_url': 'http://tracker.lan',
            'tracker_project_key': '   ',
            'tracker_api_key': 'hlt_abc',
        })
        assert clock.tracker_configured() is False


# ─── save_timers ──────────────────────────────────────────────────────

class TestSaveTimers:
    def test_only_running_and_paused_are_saved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'TIMERS_FILE', str(tmp_path / 'timers.json'))
        timers = [
            clock.new_timer(label='Running'),
            dict(clock.new_timer(label='Paused'), state='paused'),
            dict(clock.new_timer(label='Done'), state='completed'),
        ]
        clock.save_timers(timers)
        with open(tmp_path / 'timers.json') as f:
            saved = json.load(f)
        labels = [t['label'] for t in saved]
        assert 'Running' in labels
        assert 'Paused' in labels
        assert 'Done' not in labels

    def test_saved_at_is_added(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clock, 'TIMERS_FILE', str(tmp_path / 'timers.json'))
        clock.save_timers([clock.new_timer()])
        with open(tmp_path / 'timers.json') as f:
            saved = json.load(f)
        assert 'saved_at' in saved[0]


# ─── _fmt_secs edge cases ─────────────────────────────────────────────

class TestFmtSecsExtra:
    def test_float_input_truncated(self):
        assert clock._fmt_secs(90.9) == '01:30'

    def test_large_value(self):
        assert clock._fmt_secs(86400) == '24:00:00'


# ─── tray menu: per-monitor label (v1.5.0 merged Hide/Fade/Snap) ──────
class TestMonitorMenuLabel:
    @staticmethod
    def _win(idx, status):
        return SimpleNamespace(monitor_idx=idx, fade_status_label=lambda: status)

    def test_primary_idle(self):
        w = self._win(0, None)
        assert clock.ClockManager._monitor_menu_label(w, 0) == 'Monitor 1 (Primary)'

    def test_secondary_idle(self):
        w = self._win(1, None)
        assert clock.ClockManager._monitor_menu_label(w, 0) == 'Monitor 2'

    def test_fade_status_rides_on_label(self):
        w = self._win(0, 'Away 3m 20s')
        assert clock.ClockManager._monitor_menu_label(w, 0) == 'Monitor 1 (Primary) — Away 3m 20s'

    def test_secondary_fading(self):
        w = self._win(1, 'Fading out…')
        assert clock.ClockManager._monitor_menu_label(w, 0) == 'Monitor 2 — Fading out…'


# ─── resize anchoring: stays within the given monitor's rect (bug #846) ──
class TestAnchorWithin:
    A = staticmethod(clock.ClockWindow._anchor_within)

    def test_centred_keeps_centre(self):
        # equidistant from left/right and top/bottom -> grow about the centre
        nx, ny = self.A((0, 0, 1000, 1000), 450, 450, 100, 100, 200, 200)
        assert (nx, ny) == (400, 400)          # centre stays at 500,500

    def test_near_left_keeps_left_edge(self):
        nx, ny = self.A((0, 0, 1000, 1000), 10, 10, 100, 100, 300, 300)
        assert (nx, ny) == (10, 10)

    def test_near_right_keeps_right_edge(self):
        nx, _ = self.A((0, 0, 1000, 1000), 890, 500, 100, 100, 300, 100)
        assert nx + 300 == 990                 # right edge unchanged

    def test_oversized_clock_pins_inside_its_own_monitor(self):
        # Monitor 2 sits to the RIGHT (rect starts at x=1920). A clock grown
        # WIDER than the monitor must pin to that monitor's left edge, never
        # cross back onto monitor 1 (the #846 hop).
        rect = (1920, 0, 3840, 1080)
        nx, _ = self.A(rect, 3500, 100, 200, 200, 2200, 900)
        assert nx == 1920                      # stays on monitor 2, not < 1920
