"""Authorised archive rewrite (named journal + permanent delete): store contract only.

No device, no network: every store lives under a disposable directory in the approved
temp root, and the approved rewrite is proved by the pure ``rewrite_events`` helper.
"""
import csv
import io
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.client import NAMED_JOURNAL_FIELDS
from windows_app.journal_actions import deletion_sequences, rewrite_events
from windows_app.workforce_model import WorkforceError, parse_journal
from windows_app.workforce_store import WorkforceStore

BASE_TEMP = Path(r'C:\Users\PC\AppData\Local\Temp\opencode')
SOURCE = 'http://192.168.4.1'
OTHER = 'http://192.168.4.2'
UID_A, UID_B = '11223344', 'AABBCCDD'
BASE = 1800000000
SECRET = 'GIZLI-CSRF-TOKEN'
# This name only ever exists in the two deleted session rows, never in the roster.
OLD_NAME = 'Gizli Eski Ad'


def named_csv(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(NAMED_JOURNAL_FIELDS)
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8')


def legacy_csv(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['sequence', 'epoch', 'uid', 'event', 'seconds'])
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8')


def named_row(sequence, epoch, uid, event, seconds, name='', source='unknown', archived=0):
    return [sequence, epoch, uid, event, seconds, name, source, archived]


BEFORE_ROWS = [
    named_row(1, BASE, UID_A, 'I', 0, 'Ali Veli', 'recorded'),
    named_row(2, BASE + 3600, UID_A, 'O', 3600, 'Ali Veli', 'recorded'),
    named_row(3, BASE + 7200, UID_B, 'I', 0, OLD_NAME, 'recorded'),
    named_row(4, BASE + 10800, UID_B, 'O', 3600, OLD_NAME, 'recorded'),
    named_row(5, BASE + 14400, UID_A, 'M', 1800, 'Ali Veli', 'recorded'),
]
BEFORE_CSV = named_csv(BEFORE_ROWS)


def employee(uid, name, inside=False):
    return {'uid': uid, 'card_uid': uid, 'name': name, 'active': True, 'inside': inside,
            'entry': 0, 'total_seconds': 0, 'live_seconds': 0}


EMPLOYEES = [employee(UID_A, 'Ali Veli'), employee(UID_B, 'Ayşe Yılmaz')]


def status(sequence, employees=None, revision=7, **extra):
    data = {'now': BASE, 'clock_ok': True, 'clock_reason': 'OK', 'storage_ok': True,
            'rtc_present': True, 'rfid_ok': True, 'oled_ok': True, 'sd_enabled': True,
            'sd_ok': True, 'last_uid': UID_A, 'message': 'HAZIR', 'sequence': sequence,
            'api_version': 4, 'permanent_delete': True, 'event_names': True,
            'journal_revision': revision, 'roster_version': 3, 'last_event_epoch': BASE,
            'employees': [dict(row) for row in EMPLOYEES] if employees is None else employees}
    data.update(extra)
    return data


def legacy_status(sequence, **extra):
    data = {'now': BASE, 'clock_ok': True, 'clock_reason': 'OK', 'storage_ok': True,
            'rtc_present': True, 'rfid_ok': True, 'oled_ok': True, 'sd_enabled': True,
            'sd_ok': True, 'last_uid': UID_A, 'message': 'HAZIR', 'sequence': sequence,
            'api_version': 3, 'roster_version': 3, 'last_event_epoch': BASE,
            'employees': [dict(row) for row in EMPLOYEES]}
    data.update(extra)
    return data


class NamedStoreTest(unittest.TestCase):
    def setUp(self):
        BASE_TEMP.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix='named-store-', dir=str(BASE_TEMP)))
        self.path = self.dir / 'workforce.sqlite3'
        self.store = WorkforceStore(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.store.close()
        except WorkforceError:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def reopen(self):
        self.store.close()
        self.store = WorkforceStore(self.path)
        return self.store

    def snapshot_rows(self):
        connection = sqlite3.connect(str(self.path))
        try:
            return connection.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
        finally:
            connection.close()

    def approved(self, kind, target):
        """The exact after image a device may produce for this approved delete."""
        before = parse_journal(BEFORE_CSV)
        people = [dict(row) for row in EMPLOYEES]
        if kind == 'employee':
            removed, removed_uid = set(), target
            remaining = [person for person in people if person['uid'] != target]
        else:
            removed, removed_uid = deletion_sequences(before, target), None
            remaining = people
        after_csv = rewrite_events(before, removed, people, removed_uid=removed_uid)
        after_status = status(len(parse_journal(after_csv)), employees=remaining, revision=8)
        return after_status, after_csv

    # -- status allowlist -------------------------------------------------
    def test_new_status_fields_are_allowlisted(self):
        payload = status(5)
        payload.update({'csrf': SECRET, 'password': SECRET})
        self.store.save_snapshot(SOURCE, payload, BEFORE_CSV)
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertIs(snapshot['status']['permanent_delete'], True)
        self.assertIs(snapshot['status']['event_names'], True)
        self.assertEqual(snapshot['status']['journal_revision'], 7)
        self.assertNotIn('csrf', snapshot['status'])
        self.assertNotIn('password', snapshot['status'])
        self.assertNotIn(SECRET.encode('utf-8'), self.path.read_bytes())
        for bad in ({'permanent_delete': 'evet'}, {'event_names': 1},
                    {'journal_revision': -1}, {'journal_revision': 2 ** 32}):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkforceError):
                    self.store.save_snapshot(SOURCE, status(5, **bad), BEFORE_CSV)
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], BEFORE_CSV)

    # -- polling rules ----------------------------------------------------
    def test_polling_cannot_change_journal_revision(self):
        self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(SOURCE, status(5, revision=8), BEFORE_CSV)
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['status']['journal_revision'], 7)
        self.assertEqual(snapshot['csv'], BEFORE_CSV)
        self.assertFalse(self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)['changed'])
        self.assertEqual(self.snapshot_rows(), 1)

    def test_named_log_cannot_fall_back_to_legacy(self):
        self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)
        legacy = legacy_csv([row[:5] for row in BEFORE_ROWS])
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(SOURCE, status(5), legacy)
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], BEFORE_CSV)

    def test_recorded_name_is_immutable_and_current_name_follows_roster(self):
        self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)
        changed = [list(row) for row in BEFORE_ROWS]
        changed[0][5] = 'Başka Ad'
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(SOURCE, status(5), named_csv(changed))
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], BEFORE_CSV)
        # a roster-inferred (current) name may follow the roster...
        current = named_csv([named_row(1, BASE, UID_A, 'I', 0, 'Eski Roster', 'current'),
                             named_row(2, BASE + 3600, UID_A, 'O', 3600, 'Eski Roster', 'current')])
        self.store.save_snapshot(OTHER, status(2), current)
        newer = named_csv([named_row(1, BASE, UID_A, 'I', 0, 'Yeni Roster', 'current'),
                           named_row(2, BASE + 3600, UID_A, 'O', 3600, 'Yeni Roster', 'current')])
        self.assertTrue(self.store.save_snapshot(OTHER, status(2), newer)['changed'])
        self.assertEqual(self.store.load_snapshot(OTHER)['csv'], newer)
        # ...but it may not be lost, and a known archive flag may not change unsolicited
        lost = named_csv([named_row(1, BASE, UID_A, 'I', 0),
                          named_row(2, BASE + 3600, UID_A, 'O', 3600)])
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(OTHER, status(2), lost)
        self.assertEqual(self.store.load_snapshot(OTHER)['csv'], newer)
        archived = named_csv([named_row(1, BASE, UID_A, 'I', 0, 'Ali Veli', 'recorded', 1)])
        self.store.save_snapshot('DEMO', status(1), archived)
        unarchived = named_csv([named_row(1, BASE, UID_A, 'I', 0, 'Ali Veli', 'recorded', 0)])
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot('DEMO', status(1), unarchived)
        self.assertEqual(self.store.load_snapshot('DEMO')['csv'], archived)

    def test_legacy_image_is_enriched_by_named_poll(self):
        legacy = legacy_csv([row[:5] for row in BEFORE_ROWS])
        self.assertTrue(self.store.save_snapshot(SOURCE, legacy_status(5), legacy)['changed'])
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], legacy)
        self.assertTrue(self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)['changed'])
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['csv'], BEFORE_CSV)
        self.assertEqual(snapshot['status']['journal_revision'], 7)
        self.assertEqual(self.snapshot_rows(), 1)

    # -- approved rewrites ------------------------------------------------
    def test_authorized_session_delete_replaces_snapshot(self):
        before = status(5)
        before.update({'csrf': SECRET})
        self.store.save_snapshot(SOURCE, before, BEFORE_CSV)
        after_status, after_csv = self.approved('session', 3)
        result = self.store.accept_authorized_snapshot(SOURCE, before, BEFORE_CSV,
                                                       after_status, after_csv, 'session', 3)
        self.assertTrue(result['changed'])
        self.assertEqual(result['source'], SOURCE)
        self.assertEqual(result['sequence'], 3)
        self.assertTrue(result['synced_at'].endswith('Z'))
        self.assertNotIn('cleanup_warning', result)
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['csv'], after_csv)
        self.assertEqual(snapshot['sequence'], 3)
        self.assertEqual(snapshot['status']['journal_revision'], 8)
        self.assertEqual(len(snapshot['status']['employees']), 2)
        self.assertEqual(self.snapshot_rows(), 1)
        self.reopen()
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], after_csv)
        self.assertEqual(sorted(item.name for item in self.dir.iterdir()),
                         ['workforce.sqlite3'])
        self.assertNotIn(SECRET.encode('utf-8'), self.path.read_bytes())

    def test_authorized_manual_then_person_delete_accepted(self):
        self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)
        manual_status, manual_csv = self.approved('session', 5)
        first = self.store.accept_authorized_snapshot(SOURCE, status(5), BEFORE_CSV,
                                                      manual_status, manual_csv, 'session', 5)
        self.assertEqual(first['sequence'], 4)
        self.assertNotIn(str(BASE + 14400).encode('ascii'), manual_csv)
        before = parse_journal(manual_csv)
        people = [dict(row) for row in EMPLOYEES]
        after_csv = rewrite_events(before, set(), people, removed_uid=UID_A)
        after_status = status(len(parse_journal(after_csv)),
                              employees=[employee(UID_B, 'Ayşe Yılmaz')], revision=9)
        second = self.store.accept_authorized_snapshot(SOURCE, manual_status, manual_csv,
                                                       after_status, after_csv, 'employee', UID_A)
        self.assertEqual(second['sequence'], 4)
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['csv'], after_csv)
        self.assertEqual(snapshot['status']['journal_revision'], 9)
        self.assertEqual([row['uid'] for row in snapshot['status']['employees']], [UID_B])
        archived = [event for event in parse_journal(snapshot['csv']) if event['uid'] == UID_A]
        self.assertTrue(archived)
        self.assertTrue(all(event['archived'] and event['name'] == 'Ali Veli'
                            for event in archived))
        self.assertEqual(self.snapshot_rows(), 1)

    def test_cleanup_warning_is_reported_without_rollback(self):
        self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)
        after_status, after_csv = self.approved('session', 3)
        with patch.object(WorkforceStore, '_compact_deleted_pages',
                          side_effect=sqlite3.Error('vacuum engellendi')):
            result = self.store.accept_authorized_snapshot(
                SOURCE, status(5), BEFORE_CSV, after_status, after_csv, 'session', 3)
        self.assertTrue(result['cleanup_warning'])
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['csv'], after_csv)
        self.assertEqual(snapshot['status']['journal_revision'], 8)
        self.assertTrue(self.store.backup(self.dir / 'sonra.zip').exists())

    def test_unapproved_stale_or_foreign_rewrites_are_rejected(self):
        self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)
        self.store.save_snapshot(OTHER, status(5), BEFORE_CSV)
        after_status, after_csv = self.approved('session', 3)
        # a different target than the approved one
        with self.assertRaises(WorkforceError):
            self.store.accept_authorized_snapshot(SOURCE, status(5), BEFORE_CSV,
                                                  after_status, after_csv, 'session', 1)
        # a newer poll changed the stored image: the approved "before" is stale
        newer = named_csv(BEFORE_ROWS +
                          [named_row(6, BASE + 18000, UID_A, 'I', 0, 'Ali Veli', 'recorded')])
        self.store.save_snapshot(SOURCE, status(6), newer)
        with self.assertRaises(WorkforceError):
            self.store.accept_authorized_snapshot(SOURCE, status(5), BEFORE_CSV,
                                                  after_status, after_csv, 'session', 3)
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], newer)
        # an unexpected change to another person's roster entry
        altered = status(3, employees=[employee(UID_A, 'Ali Veli')], revision=8)
        with self.assertRaises(WorkforceError):
            self.store.accept_authorized_snapshot(OTHER, status(5), BEFORE_CSV,
                                                  altered, after_csv, 'session', 3)
        # a revision bump that skips the approved +1 step
        skipped = status(3, revision=9)
        with self.assertRaises(WorkforceError):
            self.store.accept_authorized_snapshot(OTHER, status(5), BEFORE_CSV,
                                                  skipped, after_csv, 'session', 3)
        # no stored snapshot for this label at all
        with self.assertRaises(WorkforceError):
            self.store.accept_authorized_snapshot('http://192.168.4.9', status(5), BEFORE_CSV,
                                                  after_status, after_csv, 'session', 3)
        self.assertEqual(self.store.load_snapshot(OTHER)['csv'], BEFORE_CSV)
        result = self.store.accept_authorized_snapshot(OTHER, status(5), BEFORE_CSV,
                                                       after_status, after_csv, 'session', 3)
        self.assertEqual(result['source'], OTHER)
        self.assertEqual(self.store.load_snapshot(OTHER)['csv'], after_csv)
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], newer)
        self.assertEqual(len(self.store.list_sources()), 2)

    def test_deleted_name_is_removed_from_database_bytes(self):
        self.store.save_snapshot(SOURCE, status(5), BEFORE_CSV)
        self.store.close()
        self.assertIn(OLD_NAME.encode('utf-8'), self.path.read_bytes())
        self.reopen()
        after_status, after_csv = self.approved('session', 3)
        self.assertNotIn(OLD_NAME.encode('utf-8'), after_csv)
        self.store.accept_authorized_snapshot(SOURCE, status(5), BEFORE_CSV,
                                              after_status, after_csv, 'session', 3)
        self.store.close()
        data = self.path.read_bytes()
        self.assertNotIn(OLD_NAME.encode('utf-8'), data)
        self.assertIn('Ali Veli'.encode('utf-8'), data)
        self.reopen()
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], after_csv)


if __name__ == '__main__':
    unittest.main(verbosity=2)
