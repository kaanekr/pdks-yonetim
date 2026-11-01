"""Shared reporting input contracts. No disk/network/device access."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.client import encode_local_time
from windows_app.workforce_model import (WorkforceError, checked_date, default_config,
                                        config_hash, parse_journal, validate_config)


def shift_config():
    config = default_config()
    config['shifts'] = [dict(id='day', name='Gündüz', start='09:00', end='18:00',
        weekdays=[0, 1, 2, 3, 4], break_minutes=0, break_after_minutes=0,
        late_tolerance=0, early_tolerance=0, overtime_after_minutes=0)]
    config['assignments'] = [dict(uid='*', shift_id='day', effective_from='2026-01-01', effective_to='')]
    return config


class ModelTest(unittest.TestCase):
    def test_default_is_detached(self):
        first = default_config()
        first['people']['11223344'] = dict(department='Test', employee_no='1')
        self.assertEqual(default_config()['people'], {})
        self.assertEqual(validate_config(default_config()), default_config())

    def test_hash_and_no_mutation(self):
        config = shift_config()
        old = copy.deepcopy(config)
        result = validate_config(config)
        result['shifts'][0]['name'] = 'Başka'
        self.assertEqual(config, old)
        self.assertNotEqual(config_hash(config), config_hash(result))

    def test_bad_schema_and_secret_fields_rejected(self):
        for change in ({'csrf': 'forbidden'}, {'password': 'forbidden'}, {'version': True}, {'shifts': 'bad'}):
            config = default_config()
            config.update(change)
            with self.assertRaises(WorkforceError):
                validate_config(config)

    def test_assignment_overlap_rejected_but_person_overrides_global(self):
        config = shift_config()
        specific = dict(uid='11223344', shift_id='day', effective_from='2026-01-01', effective_to='2026-12-31')
        config['assignments'].append(specific)
        validate_config(config)
        config['assignments'].append(copy.deepcopy(specific))
        with self.assertRaises(WorkforceError):
            validate_config(config)

    def test_unknown_shift_bad_days_and_overnight(self):
        config = shift_config()
        config['shifts'][0].update(start='22:00', end='06:00')
        validate_config(config)
        for field, value in (('start', '6:00'), ('end', '22:00'), ('weekdays', [0, 0]), ('break_minutes', -1)):
            bad = copy.deepcopy(config)
            bad['shifts'][0][field] = value
            with self.assertRaises(WorkforceError):
                validate_config(bad)
        config['assignments'][0]['shift_id'] = 'missing'
        with self.assertRaises(WorkforceError):
            validate_config(config)

    def test_calendar_validation(self):
        for value in ('2026-02-30', '2026-1-1', '2023-12-31', None):
            with self.assertRaises(WorkforceError):
                checked_date(value)
        config = default_config()
        config['holidays']['2026-11-01'] = 'Tatil'
        config['leaves'].append(dict(uid='11223344', start='2026-11-02', end='2026-11-03', kind='Yıllık izin'))
        validate_config(config)
        config['leaves'][0]['end'] = '2026-11-01'
        with self.assertRaises(WorkforceError):
            validate_config(config)

    def test_journal_sequence_and_recovery_without_prior_clock(self):
        epoch = encode_local_time('2026-11-01 09:00:00')
        header = 'sequence,epoch,uid,event,seconds\n'
        data = f'{header}1,{epoch},00000000,R,0\n'.encode()
        self.assertEqual(parse_journal(data)[0]['seconds'], 0)
        self.assertEqual(parse_journal(header.encode()), [])
        for bad in (data.replace(b'1,', b'2,', 1), data + f'1,{epoch},11223344,I,0\n'.encode(),
                    f'{header}1,{epoch},11223344,M,0\n'.encode(),
                    f'{header}1,{epoch},11223344,I,1\n'.encode(),
                    f'{header}1,{epoch},11223344,X,0\n'.encode()):
            with self.assertRaises(WorkforceError):
                parse_journal(bad)

    def test_overlapping_leave_and_nontext_assignment_end_rejected(self):
        config = shift_config()
        config['assignments'][0]['effective_to'] = False
        with self.assertRaises(WorkforceError):
            validate_config(config)
        config = default_config()
        config['leaves'] = [dict(uid='11223344', start='2026-11-01', end='2026-11-02', kind='Rapor'),
                            dict(uid='11223344', start='2026-11-02', end='2026-11-03', kind='Yıllık izin')]
        with self.assertRaises(WorkforceError):
            validate_config(config)


if __name__ == '__main__':
    unittest.main(verbosity=2)
