"""Historical-name report tests with synthetic in-memory journal events (no device/network)."""
import calendar
import copy
from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app import workforce_engine as engine
from windows_app.workforce_engine import NAME_FALLBACK, build_report
from windows_app.workforce_model import default_config

ALI, AYSE, UNKNOWN = 'A1B2C3D4', '11223344', 'DEADBEEF'
NOW = '2026-03-10 12:00:00'


def epoch(text):
    return calendar.timegm(datetime.strptime(text, '%Y-%m-%d %H:%M:%S').timetuple())


def event(sequence, stamp, person, kind, seconds, name=None, source='recorded', archived=False):
    item = {'sequence': sequence, 'epoch': epoch(stamp), 'uid': person, 'event': kind,
            'seconds': seconds}
    if name is not None:
        item.update(name=name, name_source=source, archived=archived)
    return item


def status(people=((ALI, 'Ali Veli', True), (AYSE, 'Ayşe Yılmaz', True))):
    return {'now': epoch(NOW), 'clock_ok': True, 'employees': [
        {'uid': uid, 'name': name, 'active': active, 'inside': False}
        for uid, name, active in people]}


def shift(sid='gunduz', start='09:00', end='18:00', overtime_after_minutes=0):
    return {'id': sid, 'name': sid, 'start': start, 'end': end,
            'weekdays': list(range(7)), 'break_minutes': 0, 'break_after_minutes': 0,
            'late_tolerance': 0, 'early_tolerance': 0,
            'overtime_after_minutes': overtime_after_minutes}


def config(person=ALI, overtime_after_minutes=0):
    data = default_config()
    data['shifts'] = [shift(overtime_after_minutes=overtime_after_minutes)]
    data['assignments'] = [{'uid': person, 'shift_id': 'gunduz',
                            'effective_from': '2024-01-01', 'effective_to': ''}]
    return data


def build(events, cfg, start='2026-03-02', end='2026-03-02', payload=None):
    payload = payload or status()
    with patch.object(engine, 'parse_journal', return_value=events):
        return build_report(payload, b'', cfg, start, end)


def day_row(result, person, date='2026-03-02'):
    found = [row for row in result['rows'] if row['uid'] == person and row['date'] == date]
    assert len(found) == 1, found
    return found[0]


def person_summary(result, person):
    return next(item for item in result['summary'] if item['uid'] == person)


class RecordedNameTests(unittest.TestCase):
    def test_rename_keeps_old_recorded_name_and_does_not_mutate_inputs(self):
        cfg = config()
        payload = status(people=((ALI, 'Ali Veli Yeni', True),))
        events = [event(1, '2026-03-02 09:00:00', ALI, 'I', 0, 'Ali Veli'),
                  event(2, '2026-03-02 17:00:00', ALI, 'O', 28800, 'Ali Veli')]
        before = (copy.deepcopy(cfg), copy.deepcopy(payload), copy.deepcopy(events))
        result = build(events, cfg, payload=payload)
        row = day_row(result, ALI)
        self.assertEqual(row['name'], 'Ali Veli')
        self.assertNotIn(NAME_FALLBACK, row['status'])
        self.assertNotIn('name_inferred', row['flags'])
        self.assertEqual(person_summary(result, ALI)['name'], 'Ali Veli Yeni')
        self.assertEqual((cfg, payload, events), before)

    def test_comma_quote_turkish_and_mixed_names_same_workday_are_visible(self):
        cfg = config()
        first = 'Ayşe, "Öz" Yılmaz'
        second = 'Ayşe Öztürk'
        events = [event(1, '2026-03-02 09:00:00', ALI, 'I', 0, first),
                  event(2, '2026-03-02 12:00:00', ALI, 'O', 10800, first),
                  event(3, '2026-03-02 13:00:00', ALI, 'I', 0, second),
                  event(4, '2026-03-02 17:00:00', ALI, 'O', 14400, second)]
        row = day_row(build(events, cfg), ALI)
        self.assertEqual(row['name'], first)
        self.assertIn('Diğer ad kayıtları: ' + second, row['status'])
        self.assertEqual(row['net_seconds'], 25200)

    def test_inferred_migration_is_flagged_as_estimate_not_recorded(self):
        cfg = config()
        events = [event(1, '2026-03-02 09:00:00', ALI, 'I', 0, 'Eski Ad', 'migrated'),
                  event(2, '2026-03-02 17:00:00', ALI, 'O', 28800, 'Eski Ad', 'migrated')]
        result = build(events, cfg)
        row = day_row(result, ALI)
        self.assertEqual(row['name'], 'Eski Ad')
        self.assertIn('name_inferred', row['flags'])
        self.assertIn('tahmin (kayıtlı değil)', row['status'])
        self.assertTrue(any('tahmin' in text for text in result['warnings']))
        self.assertEqual(person_summary(result, ALI)['name'], 'Ali Veli')


class DeletedPersonTests(unittest.TestCase):
    def test_archived_name_survives_roster_removal_without_absence_or_overtime(self):
        cfg = config(person='*', overtime_after_minutes=60)
        payload = status(people=((AYSE, 'Ayşe Yılmaz', True),))
        events = [event(1, '2026-03-02 09:00:00', ALI, 'I', 0, 'Arşiv Personel', 'recorded', True),
                  event(2, '2026-03-02 17:00:00', ALI, 'O', 28800, 'Arşiv Personel', 'recorded', True)]
        result = build(events, cfg, end='2026-03-03', payload=payload)
        row = day_row(result, ALI)
        self.assertEqual(row['name'], 'Arşiv Personel')
        self.assertIn('unknown_employee', row['flags'])
        self.assertIn('name_archived', row['flags'])
        self.assertEqual(row['net_seconds'], 28800)
        self.assertEqual(row['overtime_seconds'], 0)
        self.assertNotIn('absent', row['flags'])
        self.assertEqual(person_summary(result, ALI)['name'], 'Arşiv Personel')
        self.assertEqual(person_summary(result, ALI)['absent_days'], 0)
        self.assertEqual([item['date'] for item in result['rows'] if item['uid'] == ALI],
                         ['2026-03-02'])
        self.assertTrue(any(ALI in text and 'silinmiş personel' in text
                            for text in result['warnings']))
        self.assertFalse(any(ALI in text and 'ad yerine UID kullanıldı' in text
                             for text in result['warnings']))


class LegacyFallbackTests(unittest.TestCase):
    def test_nameless_legacy_rows_label_roster_name_as_current(self):
        cfg = config()
        events = [event(1, '2026-03-02 09:00:00', ALI, 'I', 0),
                  event(2, '2026-03-02 17:00:00', ALI, 'O', 28800)]
        result = build(events, cfg)
        row = day_row(result, ALI)
        self.assertEqual(row['name'], 'Ali Veli')
        self.assertIn(NAME_FALLBACK, row['status'])
        self.assertNotIn('name_inferred', row['flags'])
        self.assertNotIn('name_archived', row['flags'])
        self.assertTrue(any('tarihsel ad kaydı değil' in text for text in result['warnings']))
        self.assertEqual(person_summary(result, ALI)['name'], 'Ali Veli')

    def test_nameless_legacy_unknown_uid_keeps_uid_fallback_warning(self):
        cfg = config()
        events = [event(1, '2026-03-02 09:00:00', UNKNOWN, 'I', 0),
                  event(2, '2026-03-02 17:00:00', UNKNOWN, 'O', 28800)]
        result = build(events, cfg)
        row = day_row(result, UNKNOWN)
        self.assertEqual(row['name'], UNKNOWN)
        self.assertIn('unknown_employee', row['flags'])
        self.assertNotIn(NAME_FALLBACK, row['status'])
        self.assertTrue(any(text == f'{UNKNOWN}: cihaz personel listesinde yok; '
                                      'ad yerine UID kullanıldı.' for text in result['warnings']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
