"""Agreed edge-case contracts for the pure attendance report engine.

In-memory journal only: no GUI, no database, no device/serial access, no files written.
Each case pins a behaviour that was agreed during review; the engine may still need work.
"""
import calendar
from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app import workforce_engine
from windows_app.workforce_engine import build_report

ALI, AYSE, UNKNOWN, RESET = 'A1B2C3D4', '11223344', 'DEADBEEF', '00000000'
NOW = '2026-03-10 12:00:00'


def epoch(text):
    return calendar.timegm(datetime.strptime(text, '%Y-%m-%d %H:%M:%S').timetuple())


def journal(*events):
    lines = ['sequence,epoch,uid,event,seconds']
    for seq, (stamp, person, kind, seconds) in enumerate(events, 1):
        value = epoch(stamp) if isinstance(stamp, str) else stamp
        lines.append(f'{seq},{value},{person},{kind},{seconds}')
    return ('\n'.join(lines) + '\n').encode('utf-8')


def status(now=NOW, clock_ok=True, people=((ALI, 'Ali Veli', True), (AYSE, 'Ayşe Yılmaz', True))):
    """Device snapshot; `now` may be raw epoch 0 when the device clock was never set."""
    return {'now': epoch(now) if isinstance(now, str) else now, 'clock_ok': clock_ok,
            'employees': [{'uid': uid, 'name': name, 'active': active, 'inside': False}
                          for uid, name, active in people]}


def shift(sid='gunduz', start='09:00', end='18:00', weekdays=None, break_minutes=0,
          break_after_minutes=0, late_tolerance=0, early_tolerance=0, overtime_after_minutes=0):
    return {'id': sid, 'name': sid, 'start': start, 'end': end,
            'weekdays': list(range(7)) if weekdays is None else weekdays,
            'break_minutes': break_minutes, 'break_after_minutes': break_after_minutes,
            'late_tolerance': late_tolerance, 'early_tolerance': early_tolerance,
            'overtime_after_minutes': overtime_after_minutes}


def assignment(uid, sid='gunduz', start='2024-01-01', end=''):
    return {'uid': uid, 'shift_id': sid, 'effective_from': start, 'effective_to': end}


def config(shifts=(), assignments=(), leaves=(), holidays=None, people=None):
    from windows_app.workforce_model import default_config
    data = default_config()
    data['shifts'] = list(shifts)
    data['assignments'] = list(assignments)
    data['leaves'] = list(leaves)
    data['holidays'] = dict(holidays or {})
    data['people'] = dict(people or {})
    return data


def report(events, cfg, start, end, now=NOW, clock_ok=True,
           people=((ALI, 'Ali Veli', True), (AYSE, 'Ayşe Yılmaz', True))):
    data = journal(*events) if isinstance(events, tuple) else events
    return build_report(status(now, clock_ok, people), data, cfg, start, end)


def day_row(result, person, day):
    found = [row for row in result['rows'] if row['uid'] == person and row['date'] == day]
    assert len(found) == 1, (found, [row['date'] for row in result['rows']])
    return found[0]


def person_summary(result, person):
    found = [item for item in result['summary'] if item['uid'] == person]
    assert len(found) == 1, found
    return found[0]


class NightShiftEdgeTests(unittest.TestCase):
    def test_early_in_and_late_out_stay_on_the_prior_workday(self):
        """21:30 -> 06:30 for a 22:00-06:00 shift is one 9h workday, overtime tail included."""
        cfg = config([shift('gece', '22:00', '06:00')], [assignment(ALI, 'gece')])
        result = report((('2026-03-02 21:30:00', ALI, 'I', 0),
                         ('2026-03-03 06:30:00', ALI, 'O', 32400)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual(row['card_seconds'], 32400)
        self.assertEqual(row['net_seconds'], 32400)          # full 21:30-06:30, nothing split off
        self.assertEqual(row['first_in'], '2026-03-02 21:30:00')
        self.assertEqual(row['last_out'], '2026-03-03 06:30:00')  # physical last out
        self.assertEqual(row['overtime_seconds'], 3600)      # 1h beyond the 8h shift
        self.assertEqual(row['shift'], 'gece')
        self.assertEqual((row['late_minutes'], row['early_minutes']), (0, 0))
        self.assertNotIn('unassigned', row['flags'])

    def test_following_date_alone_never_shows_the_prior_night_workday(self):
        """Querying only 2026-03-03 must not resurrect the 03-02 night workday."""
        cfg = config([shift('gece', '22:00', '06:00')], [assignment(ALI, 'gece')])
        result = report((('2026-03-02 21:30:00', ALI, 'I', 0),
                         ('2026-03-03 06:30:00', ALI, 'O', 32400)), cfg, '2026-03-03', '2026-03-03')
        mine = [row for row in result['rows'] if row['uid'] == ALI]
        for row in mine:
            self.assertEqual(row['date'], '2026-03-03')
            self.assertEqual(row['card_seconds'], 0)
            self.assertEqual(row['net_seconds'], 0)
            self.assertEqual((row['first_in'], row['last_out']), ('', ''))
        summary = person_summary(result, ALI)
        self.assertEqual(summary['net_seconds'], 0)
        self.assertEqual(summary['card_seconds'], 0)
        self.assertEqual(summary['overtime_seconds'], 0)

    def test_overnight_snapshot_before_shift_end_is_pending_not_absent(self):
        """At 03:00 yesterday's night shift is ongoing, not an absent/finalised day."""
        cfg = config([shift('gece', '22:00', '06:00')], [assignment(ALI, 'gece')])
        result = report((('2026-03-02 22:00:00', ALI, 'I', 0),), cfg, '2026-03-02', '2026-03-02',
                        now='2026-03-03 03:00:00')
        row = day_row(result, ALI, '2026-03-02')
        self.assertNotIn('absent', row['flags'])
        # Pending means ongoing/not finalised, including after midnight.
        self.assertIn('pending', row['flags'])
        self.assertNotIn('Devamsız', row['status'])
        self.assertIn('Devam ediyor', row['status'])
        self.assertEqual(row['net_seconds'], 0)       # open session earns nothing yet
        self.assertEqual(row['overtime_seconds'], 0)  # no overtime before the shift ends
        self.assertEqual(person_summary(result, ALI)['absent_days'], 0)


class MixedSourceTests(unittest.TestCase):
    def test_badge_day_plus_earlier_manual_segment_is_not_manual_only(self):
        """Card 09:00-12:00 plus manual 08:00-09:00: mixed day, badge times come from the card."""
        cfg = config([shift('gunduz')], [assignment(ALI)])
        result = report((('2026-03-02 09:00:00', ALI, 'M', 3600),
                         ('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 12:00:00', ALI, 'O', 10800)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['card_seconds'], row['manual_seconds']), (10800, 3600))
        self.assertEqual(row['net_seconds'], 14400)
        self.assertNotIn('manual_only', row['flags'])
        self.assertNotIn('Manuel mesai (kart kaydı yok)', row['status'])
        self.assertEqual(row['first_in'], '2026-03-02 09:00:00')   # not 08:00 manual start
        self.assertEqual(row['last_out'], '2026-03-02 12:00:00')
        self.assertNotIn('overlap', row['flags'])

    def test_late_minute_after_tolerance_keeps_raw_times_across_later_recovery(self):
        """11 min late with 10 min tolerance = 1 late minute; raw times stay, R keeps done work."""
        cfg = config([shift('gunduz', late_tolerance=10, early_tolerance=10)], [assignment(ALI)])
        result = report((('2026-03-02 09:11:00', ALI, 'I', 0),
                         ('2026-03-02 12:00:00', ALI, 'O', 10140),
                         ('2026-03-02 12:30:00', RESET, 'R', 0)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual(row['late_minutes'], 1)                      # exception, not rounded away
        self.assertIn('Geç giriş', row['status'])
        self.assertEqual(row['first_in'], '2026-03-02 09:11:00')       # raw badge time unchanged
        self.assertEqual(row['last_out'], '2026-03-02 12:00:00')
        self.assertEqual(row['card_seconds'], 10140)                  # completed session preserved
        self.assertEqual(row['net_seconds'], 10140)
        self.assertNotIn('clock_recovery', row['flags'])
        self.assertEqual(person_summary(result, ALI)['late_count'], 1)


class ClockTrustTests(unittest.TestCase):
    def test_unknown_clock_with_zero_now_still_reports_raw_history(self):
        """clock_ok=false and now=0 (clock never set) must still report raw punches, no OT/absence."""
        cfg = config([shift('gunduz')], [assignment(ALI)])
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 12:00:00', ALI, 'O', 10800)), cfg, '2026-03-02', '2026-03-02',
                        now=0, clock_ok=False)
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual(row['card_seconds'], 10800)
        self.assertEqual(row['net_seconds'], 10800)
        self.assertIn('unknown_clock', row['flags'])
        self.assertIn('Saat kapsamı belirsiz', row['status'])
        self.assertNotIn('absent', row['flags'])
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertEqual(person_summary(result, ALI)['absent_days'], 0)
        self.assertTrue(any('clock_ok=false' in text for text in result['warnings']))

    def test_backward_recovery_gap_across_scheduled_day_blocks_absence(self):
        """R rewinding the clock over a scheduled day makes that day unproven, not absent."""
        cfg = config([shift('gunduz')], [assignment(ALI)])
        result = report((('2026-03-02 12:00:00', RESET, 'R', epoch('2026-03-01 09:00:00')),),
                        cfg, '2026-03-02', '2026-03-02')
        mine = [row for row in result['rows'] if row['uid'] == ALI]
        for row in mine:
            self.assertNotIn('absent', row['flags'])
            self.assertNotIn('Devamsız', row['status'])
        self.assertEqual(person_summary(result, ALI)['absent_days'], 0)

    def test_forward_recovery_gap_across_scheduled_days_blocks_absence(self):
        """A forward R gap over 03-02..03-04 proves nothing, and its seconds are never worked time."""
        cfg = config([shift('gunduz')], [assignment(ALI)])
        result = report((('2026-03-02 10:00:00', RESET, 'R', epoch('2026-03-04 10:00:00')),),
                        cfg, '2026-03-02', '2026-03-04')
        mine = [row for row in result['rows'] if row['uid'] == ALI]
        for row in mine:
            self.assertNotIn('absent', row['flags'])
            self.assertNotIn('Devamsız', row['status'])
            self.assertEqual(row['card_seconds'], 0)          # R seconds are never worked time
            self.assertEqual(row['manual_seconds'], 0)
            self.assertEqual(row['net_seconds'], 0)
            self.assertEqual(row['overtime_seconds'], 0)
            self.assertEqual((row['first_in'], row['last_out']), ('', ''))
        summary = person_summary(result, ALI)
        self.assertEqual(summary['absent_days'], 0)
        self.assertEqual(summary['net_seconds'], 0)


class FlagPolicyTests(unittest.TestCase):
    def test_duplicate_entry_blocks_overtime(self):
        """A duplicate badge in makes the day advisory: no overtime candidate."""
        cfg = config([shift('gunduz', overtime_after_minutes=60)], [assignment(ALI)])
        result = report((('2026-03-02 08:00:00', ALI, 'I', 0),
                         ('2026-03-02 08:05:00', ALI, 'I', 0),
                         ('2026-03-02 17:00:00', ALI, 'O', 32100)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertIn('duplicate_entry', row['flags'])
        self.assertIn('Yinelenen giriş', row['status'])
        self.assertEqual(row['card_seconds'], 32100)
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertEqual(person_summary(result, ALI)['overtime_seconds'], 0)

    def test_unknown_employee_blocks_overtime(self):
        """Hours of a UID missing from the device roster never become an overtime candidate."""
        cfg = config([shift('gunduz', overtime_after_minutes=60)], [assignment('*')])
        result = report((('2026-03-02 09:00:00', UNKNOWN, 'I', 0),
                         ('2026-03-02 17:00:00', UNKNOWN, 'O', 28800)), cfg,
                        '2026-03-02', '2026-03-02')
        row = day_row(result, UNKNOWN, '2026-03-02')
        self.assertIn('unknown_employee', row['flags'])
        self.assertEqual(row['net_seconds'], 28800)
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertEqual(person_summary(result, UNKNOWN)['overtime_seconds'], 0)

    def test_nested_overlap_flags_the_day_and_blocks_overtime(self):
        """A long session containing two short ones is one overlapping day: no overtime."""
        cfg = config([shift('gunduz', overtime_after_minutes=60)], [assignment(ALI)])
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 18:00:00', ALI, 'O', 32400),
                         ('2026-03-02 11:00:00', ALI, 'M', 3600),
                         ('2026-03-02 13:00:00', ALI, 'M', 3600)), cfg,
                        '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['card_seconds'], row['manual_seconds']), (32400, 7200))
        self.assertEqual(row['net_seconds'], 39600)
        self.assertIn('overlap', row['flags'])
        self.assertIn('Çakışan kayıt', row['status'])
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertTrue(any('çakışan kayıt' in text for text in result['warnings']))

    def test_nested_overlap_marks_every_affected_interval(self):
        """Nested intervals must all carry the overlap flag, not only the first adjacent pair."""
        mark = getattr(workforce_engine, '_mark_overlaps', None)
        self.assertIsNotNone(mark, 'engine helper _mark_overlaps is missing; nested overlap '
                                   'cannot be verified for every interval')
        long_card = {'start': epoch('2026-03-02 09:00:00'), 'end': epoch('2026-03-02 18:00:00'),
                     'flags': set(), 'manual': False}
        first_short = {'start': epoch('2026-03-02 10:00:00'), 'end': epoch('2026-03-02 11:00:00'),
                       'flags': set(), 'manual': True}
        second_short = {'start': epoch('2026-03-02 12:00:00'), 'end': epoch('2026-03-02 13:00:00'),
                        'flags': set(), 'manual': True}
        cards = {ALI: [long_card]}
        manual = {ALI: [first_short, second_short]}
        warnings = []
        mark(cards, manual, [], warnings)
        for item in (long_card, first_short, second_short):
            self.assertIn('overlap', item['flags'], item)
        self.assertTrue(any(ALI in text and 'çakışan kayıt' in text for text in warnings))
        self.assertEqual(len([text for text in warnings if 'çakışan kayıt' in text]), 1)


class CoverageTests(unittest.TestCase):
    def test_first_journal_event_midday_does_not_prove_absence(self):
        """The journal's first event at 15:00 cannot prove a colleague missed a 09:00 shift."""
        cfg = config([shift('gunduz')], [assignment('*')])
        result = report((('2026-03-02 15:00:00', ALI, 'I', 0),
                         ('2026-03-02 16:00:00', ALI, 'O', 3600)), cfg, '2026-03-02', '2026-03-02',
                        now='2026-03-02 20:00:00', people=((ALI, 'Ali Veli', True),
                                                           (AYSE, 'Ayşe Yılmaz', True)))
        self.assertEqual(day_row(result, ALI, '2026-03-02')['card_seconds'], 3600)
        other = [row for row in result['rows'] if row['uid'] == AYSE]
        self.assertEqual(other, [], 'coverage before the first journal event must not claim absence')
        self.assertEqual(person_summary(result, AYSE)['absent_days'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
