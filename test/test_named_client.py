"""In-memory contract tests for named/legacy client parsing and bounded delete calls."""
import csv
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.client import (DeviceClient, DeviceError, LEGACY_JOURNAL_FIELDS,
                                NAMED_JOURNAL_FIELDS, parse_events)

BASE = 1800000000


def legacy_csv(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(LEGACY_JOURNAL_FIELDS)
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8')


def named_csv(rows, header=NAMED_JOURNAL_FIELDS):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8')


def named_row(sequence, epoch, uid, event, seconds, name='', source='unknown', archived='0'):
    return [sequence, epoch, uid, event, seconds, name, source, archived]


def make_client():
    return DeviceClient('127.0.0.1', 'admin', 'test-pass', timeout=1)


def make_state(**overrides):
    state = {'csrf': 'csrf-token', 'sequence': 5, 'roster_version': 2,
             'api_version': 4, 'journal_revision': 7, 'permanent_delete': True,
             'clock_recovery': True, 'personnel_management': True,
             'employees': [
                 {'uid': 'AABBCCDD', 'card_uid': 'AABBCCDD', 'name': 'Ayşe, "Gül"',
                  'active': True, 'inside': False, 'entry': 0,
                  'total_seconds': 0, 'live_seconds': 0},
                 {'uid': '11223344', 'card_uid': '11223344', 'name': 'İsmail',
                  'active': True, 'inside': True, 'entry': BASE,
                  'total_seconds': 60, 'live_seconds': 0}]}
    state.update(overrides)
    return state


class NamedClientTest(unittest.TestCase):
    def test_named8_quotes_and_turkish_metadata(self):
        name = 'Ayşe, "Gül" Öz'
        data = named_csv([named_row(1, BASE, '11223344', 'I', 0, name, 'recorded', '0'),
                          named_row(2, BASE + 3600, '11223344', 'O', 3600, name, 'recorded', '1'),
                          named_row(3, BASE + 4000, '00000000', 'R', 0)])
        rows = parse_events(data)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(rows[0]), 8)
        self.assertEqual(rows[0][5], name)
        self.assertEqual(rows[0][6], 'recorded')
        self.assertIs(rows[0][7], False)
        self.assertIs(rows[1][7], True)
        self.assertEqual(rows[2][5], '')
        self.assertEqual(rows[2][6], 'unknown')
        self.assertIs(rows[2][7], False)

    def test_legacy5_header_and_tuple_unchanged(self):
        rows = parse_events(legacy_csv([[5, BASE, '11223344', 'I', 0]]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 5)
        self.assertEqual(rows[0][0], 5)
        manual = parse_events(legacy_csv([[5, BASE + 7200, '11223344', 'M', 3600]]))
        self.assertEqual(len(manual[0]), 5)
        self.assertIn('Manuel mesai', manual[0][3])
        self.assertEqual(manual[0][4], '1:00:00')

    def test_bad_metadata_extra_fields_and_controls_rejected(self):
        good = named_row(1, BASE, '11223344', 'I', 0, 'Ali', 'recorded', '0')
        bad_rows = [
            named_row(1, BASE, '11223344', 'I', 0, 'Ali', 'unknown', '0'),
            named_row(1, BASE, '11223344', 'I', 0, '', 'recorded', '0'),
            named_row(1, BASE, '11223344', 'I', 0, 'Ali', 'guess', '0'),
            named_row(1, BASE, '11223344', 'I', 0, 'Ali', 'recorded', '2'),
            named_row(1, BASE, '00000000', 'R', 0, 'Ali', 'recorded', '0'),
            named_row(1, BASE, '11223344', 'I', 0, 'Ali\x01', 'recorded', '0'),
            named_row(1, BASE, '11223344', 'I', 0, 'Ali\x00', 'recorded', '0'),
            good + ['extra'],
            good[:-1],
        ]
        for row in bad_rows:
            with self.subTest(row=row):
                with self.assertRaises(DeviceError):
                    parse_events(named_csv([row]))
        with self.assertRaises(DeviceError):
            parse_events(named_csv([good], header=NAMED_JOURNAL_FIELDS[:-1]))
        with self.assertRaises(DeviceError):
            parse_events(legacy_csv([[1, BASE, '11223344', 'I', 0, 'extra']]))

    def test_expected_old_device_vs_api4_revision(self):
        old = DeviceClient.expected(make_state(api_version=3))
        self.assertEqual(old, {'expected_sequence': 5, 'roster_version': 2})
        new = DeviceClient.expected(make_state(api_version=4, journal_revision=9))
        self.assertEqual(new, {'expected_sequence': 5, 'roster_version': 2,
                               'journal_revision': 9})
        for revision in (-1, 2 ** 32, True, '7', None):
            with self.subTest(revision=revision):
                with self.assertRaises(DeviceError):
                    DeviceClient.expected(make_state(journal_revision=revision))
        missing = make_state()
        missing.pop('journal_revision')
        with self.assertRaises(DeviceError):
            DeviceClient.expected(missing)
        with self.assertRaises(DeviceError):
            DeviceClient.expected(make_state(api_version='4'))

    def test_permanent_capabilities_blocked(self):
        client = make_client()
        client._request = Mock(return_value={'ok': True})
        states = [make_state(api_version=3), make_state(permanent_delete=False),
                  make_state(journal_revision=-1), make_state(csrf='')]
        missing = make_state()
        missing.pop('permanent_delete')
        states.append(missing)
        no_gate = make_state()
        no_gate.pop('personnel_management')
        states.append(no_gate)
        for state in states:
            with self.subTest(state=state):
                with self.assertRaises(DeviceError):
                    client.delete_employee(state, 'AABBCCDD')
                with self.assertRaises(DeviceError):
                    client.delete_session(state, 1)
        client._request.assert_not_called()

    def test_delete_employee_single_request_payload_and_inside_block(self):
        state = make_state()
        client = make_client()
        client._request = Mock(return_value={'ok': True})
        client.delete_employee(state, 'aabbccdd')
        payload = {'expected_sequence': 5, 'roster_version': 2, 'journal_revision': 7,
                   'uid': 'AABBCCDD', 'confirm': 'PERSONELI KALICI SIL'}
        client._request.assert_called_once_with('/api/employee/delete', payload, 'csrf-token')
        client._request.reset_mock()
        with self.assertRaises(DeviceError):
            client.delete_employee(state, '11223344')
        with self.assertRaises(DeviceError):
            client.delete_employee(state, 'DEADBEEF')
        client._request.assert_not_called()

    def test_delete_session_single_request_and_bad_target_block(self):
        state = make_state()
        client = make_client()
        client._request = Mock(return_value={'ok': True})
        client.delete_session(state, 3)
        payload = {'expected_sequence': 5, 'roster_version': 2, 'journal_revision': 7,
                   'target_sequence': 3, 'confirm': 'OTURUMU KALICI SIL'}
        client._request.assert_called_once_with('/api/attendance/delete', payload, 'csrf-token')
        for target in (True, '3', 3.0, 0, -1, 6, None):
            with self.subTest(target=target):
                client._request.reset_mock()
                with self.assertRaises(DeviceError):
                    client.delete_session(state, target)
                client._request.assert_not_called()

    def test_request_timeout_post_is_not_retried(self):
        client = make_client()
        opener = Mock()
        opener.open.side_effect = URLError('down')
        client._opener = opener
        with self.assertRaisesRegex(DeviceError, 'otomatik tekrar yapılmadı') as caught:
            client._request('/api/attendance/delete', {'target_sequence': 3}, 'csrf-token')
        self.assertIn('sonucu belirsiz', str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
