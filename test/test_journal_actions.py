"""Pure transformation and durable approval recovery; never contact a device."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.demo import DemoClient
from windows_app.journal_actions import deletion_sequences, rewrite_events, validate_authorized_rewrite
from windows_app.workforce_model import WorkforceError, parse_journal
from windows_app.workforce_smoke import sample_snapshot
from windows_app.workforce_store import WorkforceStore

TEMP = r'C:\Users\PC\AppData\Local\Temp\opencode'


def fixture():
    client = DemoClient()
    state, data, _config = sample_snapshot()
    client.state, client.rows = copy.deepcopy(state), data.decode().splitlines()
    client.enable_named_journal()
    return client


class JournalActionsTest(unittest.TestCase):
    def test_pair_selected_from_either_endpoint_and_manual(self):
        client = fixture()
        events = parse_journal(client.events())
        self.assertEqual(deletion_sequences(events, 1), {1, 2})
        self.assertEqual(deletion_sequences(events, 2), {1, 2})
        self.assertEqual(deletion_sequences(events, 5), {5})
        for invalid in (None, True, '1', 0, -1, 6, 99):
            with self.assertRaises(WorkforceError):
                deletion_sequences(events, invalid)

    def test_open_and_cancelled_entry_are_not_completed_sessions(self):
        events = parse_journal(fixture().events())
        with self.assertRaises(WorkforceError):
            deletion_sequences(events[:1], 1)
        cancelled = [events[0], dict(events[-1], sequence=2)]
        with self.assertRaises(WorkforceError):
            deletion_sequences(cancelled, 1)

    def test_rewrite_preserves_other_rows_and_fixes_recovery_baseline(self):
        client = fixture()
        events = parse_journal(client.events())
        original = copy.deepcopy(events)
        data = rewrite_events(events, {3, 4}, client.state['employees'])
        rows = parse_journal(data)
        self.assertEqual(events, original)
        self.assertEqual([e['sequence'] for e in rows], [1, 2, 3, 4])
        self.assertEqual(rows[-1]['event'], 'R')
        self.assertEqual(rows[-1]['seconds'], rows[1]['epoch'])
        self.assertEqual(rows[2]['event'], 'M')

    def test_person_removal_keeps_names_then_reissue_uses_new_identity(self):
        client = fixture()
        before, data = client.status(), client.events()
        client.delete_employee(before, '11223344')
        after = client.status()
        validate_authorized_rewrite(before, data, after, client.events(), 'employee', '11223344')
        self.assertEqual(len(after['employees']), 1)
        rows = [e for e in parse_journal(client.events()) if e['uid'] == '11223344']
        self.assertTrue(rows)
        self.assertTrue(all(e['archived'] and e['name'] == 'Ayşe Şahin' for e in rows))
        client.save_employee(after, '11223344', 'Yeni Kişi', '11223344', create=True)
        new = next(p for p in client.state['employees'] if p['card_uid'] == '11223344')
        self.assertNotEqual(new['uid'], '11223344')
        self.assertEqual(new['total_seconds'], 0)

    def test_wrong_target_or_extra_deletion_is_rejected(self):
        client = fixture()
        before, data = client.status(), client.events()
        client.delete_session(before, 1)
        with self.assertRaises(WorkforceError):
            validate_authorized_rewrite(before, data, client.status(), client.events(), 'session', 3)
        wrong = rewrite_events(parse_journal(data), {1, 2, 3, 4}, before['employees'])
        after = dict(client.status(), sequence=len(parse_journal(wrong)))
        with self.assertRaises(WorkforceError):
            validate_authorized_rewrite(before, data, after, wrong, 'session', 1)

    def test_recorded_name_survives_rename(self):
        client = fixture()
        # Add a new event after enabling the named format: it carries a true snapshot.
        client.add_manual(client.status(), '11223344', '2026-10-27 08:00:00', '2026-10-27 09:00:00')
        client.save_employee(client.status(), '11223344', 'Yeni Soyad', 'AABBCCDD')
        row = parse_journal(client.events())[-1]
        self.assertEqual((row['name'], row['name_source']), ('Ayşe Şahin', 'recorded'))

    def test_intent_survives_reopen_without_credentials_or_duplicate_csv(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as folder:
            path = Path(folder)/'archive.sqlite3'
            client = fixture()
            before, data = client.status(), client.events()
            before['csrf'] = 'DO-NOT-CACHE-THIS-CSRF'
            with WorkforceStore(path) as store:
                store.save_snapshot('DEMO', before, data)
                store.remember_delete_intent('DEMO', before, data, 'session', 1)
                with self.assertRaises(WorkforceError):
                    store.remember_delete_intent('DEMO', before, data, 'session', 3)
            self.assertNotIn(b'DO-NOT-CACHE-THIS-CSRF', path.read_bytes())
            client.delete_session(client.status(), 1)  # lost successful reply; no POST is repeated
            with WorkforceStore(path) as store:
                intent = store.load_delete_intent('DEMO')
                self.assertEqual(intent['before_csv'], data)
                self.assertNotIn('csrf', intent['before_status'])
                store.accept_authorized_snapshot('DEMO', intent['before_status'], intent['before_csv'],
                    client.status(), client.events(), intent['kind'], intent['target'])
                self.assertIsNone(store.load_delete_intent('DEMO'))
                self.assertEqual(store.load_snapshot('DEMO')['csv'], client.events())
            with WorkforceStore(path) as store:
                self.assertIsNone(store.load_delete_intent('DEMO'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
