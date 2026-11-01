"""Pure report-engine tests: in-memory journal, no device, no network, no files."""
import calendar
import copy
from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.workforce_engine import build_report
from windows_app.workforce_model import (DAILY_COLUMNS, SUMMARY_COLUMNS, WorkforceError,
                                          config_hash, default_config)

ALI, AYSE, UNKNOWN, RESET = 'A1B2C3D4', '11223344', 'DEADBEEF', '00000000'
NOW = '2026-03-10 12:00:00'


def epoch(text):
    return calendar.timegm(datetime.strptime(text, '%Y-%m-%d %H:%M:%S').timetuple())


def journal(*events):
    lines = ['sequence,epoch,uid,event,seconds']
    for seq, (stamp, person, kind, seconds) in enumerate(events, 1):
        lines.append(f'{seq},{epoch(stamp)},{person},{kind},{seconds}')
    return ('\n'.join(lines) + '\n').encode('utf-8')


def status(now=NOW, clock_ok=True, people=((ALI, 'Ali Veli', True), (AYSE, 'Ayşe Yılmaz', True))):
    return {'now': epoch(now), 'clock_ok': clock_ok, 'employees': [
        {'uid': uid, 'name': name, 'active': active, 'inside': False} for uid, name, active in people]}


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
    data = default_config()
    data['shifts'] = list(shifts)
    data['assignments'] = list(assignments)
    data['leaves'] = list(leaves)
    data['holidays'] = dict(holidays or {})
    data['people'] = dict(people or {})
    return data


def report(events, cfg, start, end, now=NOW, clock_ok=True, source='', synced_at='',
           people=((ALI, 'Ali Veli', True), (AYSE, 'Ayşe Yılmaz', True))):
    return build_report(status(now, clock_ok, people), journal(*events) if isinstance(events, tuple)
                        else events, cfg, start, end, source, synced_at)


def day_row(result, person, day):
    found = [row for row in result['rows'] if row['uid'] == person and row['date'] == day]
    assert len(found) == 1, (found, [row['date'] for row in result['rows']])
    return found[0]


def person_summary(result, person):
    found = [item for item in result['summary'] if item['uid'] == person]
    assert len(found) == 1, found
    return found[0]


class IntervalTests(unittest.TestCase):
    def test_sessions_sum_intervals_and_ignore_gap(self):
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 12:00:00', ALI, 'O', 10800),
                         ('2026-03-02 13:00:00', ALI, 'I', 0),
                         ('2026-03-02 17:00:00', ALI, 'O', 14400)),
                        config(), '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual(row['card_seconds'], 25200)
        self.assertEqual(row['net_seconds'], 25200)
        self.assertEqual(row['first_in'], '2026-03-02 09:00:00')
        self.assertEqual(row['last_out'], '2026-03-02 17:00:00')
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertEqual(row['flags'], ['unassigned'])
        self.assertIn('Vardiya atanmamış', row['status'])

    def test_night_shift_edges_and_early_morning_tie(self):
        cfg = config([shift('gece', '22:00', '06:00')], [assignment(ALI, 'gece')])
        result = report((('2026-03-02 21:30:00', ALI, 'I', 0),
                         ('2026-03-03 06:30:00', ALI, 'O', 32400)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual(row['net_seconds'], 32400)  # full 21:30-06:30, no lost overtime tail
        self.assertEqual(row['overtime_seconds'], 3600)
        self.assertEqual(row['first_in'], '2026-03-02 21:30:00')
        self.assertEqual(row['last_out'], '2026-03-03 06:30:00')
        self.assertEqual(row['shift'], 'gece')
        self.assertEqual(row['late_minutes'], 0)
        self.assertEqual(row['early_minutes'], 0)

    def test_previous_evening_spillover_is_not_this_period_workday(self):
        cfg = config([shift('gece', '22:00', '06:00')], [assignment(ALI, 'gece')])
        result = report((('2026-03-02 22:00:00', ALI, 'I', 0),
                         ('2026-03-03 06:00:00', ALI, 'O', 28800)), cfg, '2026-03-03', '2026-03-03',
                        people=((ALI, 'Ali Veli', False),))
        self.assertEqual(result['rows'], [])

    def test_manual_interval_is_separate_source_and_keeps_open_state(self):
        cfg = config([shift()], [assignment(ALI)])
        result = report((('2026-03-02 10:00:00', ALI, 'M', 3600),
                         ('2026-03-02 13:00:00', ALI, 'I', 0),
                         ('2026-03-02 13:30:00', ALI, 'O', 1800)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['card_seconds'], row['manual_seconds']), (1800, 3600))
        self.assertEqual(row['net_seconds'], 5400)
        self.assertNotIn('manual_only', row['flags'])  # mixed card/manual is not manual-only

    def test_manual_only_day_claims_no_badge_times(self):
        cfg = config([shift()], [assignment(ALI)])
        result = report((('2026-03-02 11:00:00', ALI, 'M', 3600),), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['first_in'], row['last_out']), ('', ''))
        self.assertIn('Manuel mesai', row['status'])
        self.assertEqual(row['overtime_seconds'], 0)

    def test_clock_recovery_cancels_open_session_without_time(self):
        cfg = config([shift()], [assignment(ALI)])
        result = report((('2026-03-02 10:00:00', ALI, 'M', 3600),
                         ('2026-03-02 13:00:00', ALI, 'I', 0),
                         ('2026-03-02 14:00:00', RESET, 'R', 0)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['card_seconds'], row['manual_seconds']), (0, 3600))
        self.assertEqual(row['net_seconds'], 3600)
        self.assertIn('clock_recovery', row['flags'])
        self.assertNotIn('open_session', row['flags'])
        self.assertIn('Saat kurtarma / iptal', row['status'])
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertTrue(any('saat kurtarma' in text for text in result['warnings']))

    def test_unclosed_session_has_zero_completed_time(self):
        cfg = config([shift()], [assignment(ALI)])
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual(row['net_seconds'], 0)
        self.assertIn('open_session', row['flags'])
        self.assertIn('Açık oturum', row['status'])
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertEqual((row['late_minutes'], row['early_minutes']), (0, 0))

    def test_duplicate_entry_and_orphan_checkout_are_flagged(self):
        cfg = config()
        result = report((('2026-03-02 08:00:00', ALI, 'I', 0),
                         ('2026-03-02 08:05:00', ALI, 'I', 0),
                         ('2026-03-02 17:00:00', ALI, 'O', 32100),
                         ('2026-03-03 10:00:00', ALI, 'O', 1800)), cfg, '2026-03-02', '2026-03-03')
        first = day_row(result, ALI, '2026-03-02')
        second = day_row(result, ALI, '2026-03-03')
        self.assertEqual(first['net_seconds'], 32100)
        self.assertIn('duplicate_entry', first['flags'])
        self.assertEqual(second['net_seconds'], 1800)
        self.assertIn('orphan_out', second['flags'])
        self.assertIn('yetim', second['status'].lower())
        self.assertTrue(any('yinelenen giriş' in text for text in result['warnings']))
        self.assertTrue(any('girişi olmayan çıkış' in text for text in result['warnings']))

    def test_mismatched_checkout_duration_is_rejected(self):
        with self.assertRaises(WorkforceError):
            report((('2026-03-02 08:00:00', ALI, 'I', 0),
                    ('2026-03-02 17:00:00', ALI, 'O', 100)), config(), '2026-03-02', '2026-03-02')

    def test_zero_duration_checkout_is_accepted(self):
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 09:00:00', ALI, 'O', 0)), config(), '2026-03-02', '2026-03-02')
        self.assertEqual(day_row(result, ALI, '2026-03-02')['net_seconds'], 0)

    def test_overlapping_manual_card_blocks_overtime(self):
        cfg = config([shift('kisa', '09:00', '11:00')], [assignment(ALI, 'kisa')])
        result = report((('2026-03-02 11:00:00', ALI, 'M', 7200),
                         ('2026-03-02 10:00:00', ALI, 'I', 0),
                         ('2026-03-02 12:00:00', ALI, 'O', 7200)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual(row['net_seconds'], 14400)
        self.assertIn('overlap', row['flags'])
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertTrue(any('çakışan kayıt' in text for text in result['warnings']))

    def test_manual_inside_clock_recovery_gap_is_flagged(self):
        cfg = config([shift()], [assignment(ALI)])
        result = report((('2026-03-02 10:00:00', ALI, 'M', 3600),
                         ('2026-03-02 12:00:00', RESET, 'R', epoch('2026-03-02 09:30:00'))),
                        cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertIn('clock_recovery', row['flags'])
        self.assertEqual(row['net_seconds'], 3600)


class PolicyTests(unittest.TestCase):
    def test_break_once_and_clamped(self):
        cfg = config([shift(break_minutes=60, break_after_minutes=240)], [assignment(ALI)])
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 11:00:00', ALI, 'O', 7200),
                         ('2026-03-02 11:30:00', ALI, 'I', 0),
                         ('2026-03-02 13:30:00', ALI, 'O', 7200)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['gross_seconds'], row['break_seconds']), (14400, 3600))
        self.assertEqual(row['net_seconds'], 10800)

    def test_break_below_threshold_and_gross_clamp(self):
        cfg = config([shift(break_minutes=120, break_after_minutes=240)], [assignment(ALI)])
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 11:00:00', ALI, 'O', 7200)), cfg, '2026-03-02', '2026-03-02')
        self.assertEqual(day_row(result, ALI, '2026-03-02')['break_seconds'], 0)
        cfg = config([shift(break_minutes=120, break_after_minutes=0)], [assignment(ALI)])
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 09:30:00', ALI, 'O', 1800)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['break_seconds'], row['net_seconds']), (1800, 0))

    def test_tolerances_affect_classification_only(self):
        cfg = config([shift(late_tolerance=10, early_tolerance=15)], [assignment(ALI)])
        result = report((('2026-03-02 09:20:00', ALI, 'I', 0),
                         ('2026-03-02 17:40:00', ALI, 'O', 30000)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['late_minutes'], row['early_minutes']), (10, 5))
        self.assertEqual(row['first_in'], '2026-03-02 09:20:00')
        self.assertIn('Geç giriş', row['status'])
        summary = person_summary(result, ALI)
        self.assertEqual((summary['late_count'], summary['early_count']), (1, 1))

    def test_tolerance_absorbs_small_deviation(self):
        cfg = config([shift(late_tolerance=10, early_tolerance=15)], [assignment(ALI)])
        result = report((('2026-03-02 09:10:00', ALI, 'I', 0),
                         ('2026-03-02 17:45:00', ALI, 'O', 30900)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['late_minutes'], row['early_minutes']), (0, 0))

    def test_overtime_threshold_and_unassigned_no_assumption(self):
        cfg = config([shift(overtime_after_minutes=60)], [assignment(ALI)])
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 12:00:00', ALI, 'O', 10800)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertEqual((row['net_seconds'], row['overtime_seconds']), (10800, 7200))
        unassigned = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                             ('2026-03-02 20:00:00', ALI, 'O', 39600)), config(),
                            '2026-03-02', '2026-03-02')
        self.assertEqual(day_row(unassigned, ALI, '2026-03-02')['overtime_seconds'], 0)

    def test_assignment_precedence_and_weekdays(self):
        cfg = config([shift('gunduz'), shift('gece', '22:00', '06:00')],
                     [assignment('*', 'gece'), assignment(ALI, 'gunduz', end='2026-03-02')])
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 11:00:00', ALI, 'O', 7200),
                         ('2026-03-03 22:00:00', ALI, 'I', 0),
                         ('2026-03-04 06:00:00', ALI, 'O', 28800)), cfg, '2026-03-02', '2026-03-03')
        self.assertEqual(day_row(result, ALI, '2026-03-02')['shift'], 'gunduz')
        self.assertEqual(day_row(result, ALI, '2026-03-03')['shift'], 'gece')
        self.assertEqual(day_row(result, ALI, '2026-03-03')['net_seconds'], 28800)
        weekday = config([shift(weekdays=[0])], [assignment(ALI)])
        result = report((('2026-03-03 09:00:00', ALI, 'I', 0),
                         ('2026-03-03 11:00:00', ALI, 'O', 7200)), weekday,
                        '2026-03-03', '2026-03-03')
        row = day_row(result, ALI, '2026-03-03')
        self.assertEqual(row['shift'], '')
        self.assertIn('unassigned', row['flags'])


class CalendarTests(unittest.TestCase):
    def test_leave_and_holiday_suppress_absence(self):
        cfg = config([shift()], [assignment(ALI)],
                     leaves=[{'uid': ALI, 'start': '2026-03-02', 'end': '2026-03-02', 'kind': 'Yıllık'}])
        result = report((), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertIn('İzin: Yıllık', row['status'])
        self.assertNotIn('absent', row['flags'])
        self.assertEqual(person_summary(result, ALI)['absent_days'], 0)

        cfg = config([shift()], [assignment(ALI)], holidays={'2026-03-02': 'Resmî Tatil'})
        result = report((('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 12:00:00', ALI, 'O', 10800)), cfg, '2026-03-02', '2026-03-02')
        row = day_row(result, ALI, '2026-03-02')
        self.assertIn('Tatil: Resmî Tatil', row['status'])
        self.assertEqual(row['net_seconds'], 10800)
        self.assertIn('holiday', row['flags'])

    def test_pending_current_day_and_future_days(self):
        cfg = config([shift()], [assignment(ALI)])
        pending = report((), cfg, '2026-03-02', '2026-03-03', now='2026-03-02 12:00:00')
        row = day_row(pending, ALI, '2026-03-02')
        self.assertIn('pending', row['flags'])
        self.assertIn('Devam ediyor', row['status'])
        self.assertNotIn('absent', row['flags'])
        self.assertEqual([item['date'] for item in pending['rows']], ['2026-03-02'])
        final = report((('2026-03-01 09:00:00', ALI, 'I', 0),
                        ('2026-03-01 10:00:00', ALI, 'O', 3600)), cfg, '2026-03-02', '2026-03-02',
                       now='2026-03-02 18:30:00')
        row = day_row(final, ALI, '2026-03-02')
        self.assertIn('absent', row['flags'])
        self.assertIn('Devamsız', row['status'])
        self.assertEqual(person_summary(final, ALI)['absent_days'], 1)

    def test_unknown_clock_and_journal_coverage_block_absence(self):
        cfg = config([shift()], [assignment(ALI)])
        unknown = report((), cfg, '2026-03-02', '2026-03-02', now='2026-03-03 12:00:00',
                         clock_ok=False)
        self.assertEqual(unknown['rows'], [])
        self.assertTrue(any('clock_ok=false' in text for text in unknown['warnings']))
        late_log = report((('2026-03-03 09:00:00', ALI, 'I', 0),
                           ('2026-03-03 11:00:00', ALI, 'O', 7200)), cfg,
                          '2026-03-02', '2026-03-03', now='2026-03-03 18:30:00')
        self.assertEqual([item['date'] for item in late_log['rows']], ['2026-03-03'])
        self.assertEqual(late_log['summary'][0]['absent_days'], 0)

    def test_inactive_employee_is_never_absent(self):
        cfg = config([shift()], [assignment(ALI)])
        result = report((), cfg, '2026-03-02', '2026-03-02', people=((ALI, 'Ali Veli', False),))
        self.assertEqual(result['rows'], [])
        self.assertEqual(person_summary(result, ALI)['absent_days'], 0)
        self.assertEqual(person_summary(result, ALI)['days'], 0)


class ShapeTests(unittest.TestCase):
    def test_columns_summary_weekly_and_metadata(self):
        cfg = config([shift()], [assignment(ALI)],
                     people={ALI: {'department': 'Üretim', 'employee_no': '1001'}})
        events = (('2026-03-02 09:00:00', ALI, 'I', 0),
                  ('2026-03-02 10:00:00', ALI, 'O', 3600),
                  ('2026-03-03 09:00:00', AYSE, 'I', 0),
                  ('2026-03-03 10:00:00', AYSE, 'O', 3600),
                  ('2026-03-09 09:00:00', ALI, 'I', 0),
                  ('2026-03-09 10:00:00', ALI, 'O', 3600))
        result = report(events, cfg, '2026-03-02', '2026-03-09', source='Cihaz A (salt okunur)',
                        synced_at='2026-03-10 12:01:00')
        self.assertEqual(set(result), {'rows', 'summary', 'weekly', 'warnings', 'start', 'end',
                                       'source', 'synced_at', 'generated_at', 'config_hash',
                                       'policy_notes'})
        for row in result['rows']:
            self.assertEqual(set(row), {key for key, _title in DAILY_COLUMNS} | {'flags'})
            self.assertIsInstance(row['flags'], list)
        for item in result['summary']:
            self.assertEqual(set(item), {key for key, _title in SUMMARY_COLUMNS} | {'days'})
        self.assertEqual({key for item in result['weekly'] for key in item},
                         {'uid', 'name', 'week_start', 'net_seconds', 'overtime_seconds'})
        self.assertEqual([(item['uid'], item['week_start']) for item in result['weekly']],
                         [(AYSE, '2026-03-02'), (ALI, '2026-03-02'), (ALI, '2026-03-09')])
        self.assertEqual(result['config_hash'], config_hash(cfg))
        self.assertEqual((result['start'], result['end']), ('2026-03-02', '2026-03-09'))
        self.assertEqual(result['source'], 'Cihaz A (salt okunur)')
        self.assertEqual(result['synced_at'], '2026-03-10 12:01:00')
        self.assertRegex(result['generated_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertTrue(result['policy_notes'])
        ali = person_summary(result, ALI)
        self.assertEqual(ali['department'], 'Üretim')
        self.assertEqual(ali['days'], 2)
        self.assertEqual(ali['net_seconds'], 7200)

    def test_unknown_employee_uses_uid_fallback(self):
        result = report((('2026-03-02 09:00:00', UNKNOWN, 'I', 0),
                         ('2026-03-02 10:00:00', UNKNOWN, 'O', 3600)), config(),
                        '2026-03-02', '2026-03-02')
        row = day_row(result, UNKNOWN, '2026-03-02')
        self.assertEqual(row['name'], UNKNOWN)
        self.assertIn('unknown_employee', row['flags'])
        self.assertTrue(any(UNKNOWN in text and 'cihaz personel listesinde yok' in text
                            for text in result['warnings']))

    def test_inputs_are_not_mutated(self):
        cfg = config([shift()], [assignment(ALI)])
        payload = status()
        events = journal(('2026-03-02 09:00:00', ALI, 'I', 0),
                         ('2026-03-02 10:00:00', ALI, 'O', 3600))
        before = (copy.deepcopy(cfg), copy.deepcopy(payload), events)
        build_report(payload, events, cfg, '2026-03-02', '2026-03-02')
        self.assertEqual((cfg, payload, events), before)

    def test_metadata_and_date_validation(self):
        cfg = config()
        good = status()
        cases = [
            (good, cfg, '2026-03-03', '2026-03-02'),
            (good, cfg, '2026-01-01', '2027-01-02'),
            (good, cfg, '2026-3-2', '2026-03-02'),
            ({**good, 'now': 'x'}, cfg, '2026-03-02', '2026-03-02'),
            ({**good, 'clock_ok': 'yes'}, cfg, '2026-03-02', '2026-03-02'),
            (good, {**cfg, 'version': 2}, '2026-03-02', '2026-03-02'),
        ]
        for payload, settings, start, end in cases:
            with self.assertRaises(WorkforceError):
                build_report(payload, journal(), settings, start, end)
        with self.assertRaises(WorkforceError):
            build_report(good, b'bad,header\n1,2\n', cfg, '2026-03-02', '2026-03-02')
        with self.assertRaises(WorkforceError):
            build_report(good, journal(), cfg, '2026-03-02', '2026-03-02', 'x' * 81)
        result = build_report(good, journal(), cfg, '2026-03-02', '2026-03-02')
        self.assertEqual(result['warnings'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
