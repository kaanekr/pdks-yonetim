"""Local attendance archive: SQLite, ZIP backups, no device and no network.

Every store lives under a disposable directory in the approved temp root; the real
%LOCALAPPDATA% profile is only used with a patched environment variable.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.workforce_model import WorkforceError, config_hash, default_config
from windows_app.workforce_store import (DB_MEMBER, MANIFEST_MEMBER, SCHEMA_VERSION,
                                         WorkforceStore, verify_backup)

BASE_TEMP = Path(r'C:\Users\PC\AppData\Local\Temp\opencode')
SOURCE = 'http://192.168.4.1'
HEADER = b'sequence,epoch,uid,event,seconds\n'
UID = '11223344'
OTHER_UID = 'AABBCCDD'
STATUS_FIELDS = {'now', 'clock_ok', 'clock_reason', 'storage_ok', 'rtc_present', 'rfid_ok',
                 'oled_ok', 'sd_enabled', 'sd_ok', 'sequence', 'roster_version',
                 'api_version', 'last_event_epoch', 'last_uid', 'message', 'employees'}
EMPLOYEE_FIELDS = {'uid', 'card_uid', 'name', 'active', 'inside', 'entry', 'total_seconds',
                   'live_seconds'}

E1 = (1, 1793520000, UID, 'I', 0)
E2 = (2, 1793620000, UID, 'O', 3000)
E3 = (3, 1793700000, OTHER_UID, 'I', 0)
E4 = (4, 1793710000, UID, 'O', 60)


def journal(*events):
    return (HEADER + ''.join('%d,%d,%s,%s,%d\n' % event for event in events).encode('utf-8'))


LOG3 = journal(E1, E2, E3)
LOG4 = journal(E1, E2, E3, E4)


def employee(uid=UID, **extra):
    row = {'uid': uid, 'card_uid': uid, 'name': 'Deneme Personeli', 'active': True,
           'inside': False, 'entry': 0, 'total_seconds': 0, 'live_seconds': 0}
    row.update(extra)
    return row


def status(sequence, employees=None, **extra):
    data = {'now': 1793520000, 'clock_ok': True, 'clock_reason': 'OK', 'storage_ok': True,
            'rtc_present': True, 'rfid_ok': True, 'oled_ok': True, 'sd_enabled': True,
            'sd_ok': True, 'last_uid': UID, 'message': 'HAZIR', 'sequence': sequence,
            'api_version': 3, 'roster_version': 1, 'last_event_epoch': 1793520000,
            'employees': [employee()] if employees is None else employees}
    data.update(extra)
    return data


class StoreTest(unittest.TestCase):
    def setUp(self):
        BASE_TEMP.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix='workforce-store-', dir=str(BASE_TEMP)))
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

    # -- opening / schema -------------------------------------------------
    def test_default_path_uses_local_profile_only_when_patched(self):
        profile = self.dir / 'profil'
        profile.mkdir()
        with patch.dict(os.environ, {'LOCALAPPDATA': str(profile)}):
            store = WorkforceStore()
        self.addCleanup(store.close)
        self.assertEqual(store.path, profile / 'PDKS_Yonetim' / 'workforce.sqlite3')
        store.save_snapshot('DEMO', status(0), HEADER)
        self.assertTrue(store.path.exists())

    def test_reopen_roundtrip(self):
        result = self.store.save_snapshot(SOURCE, status(3), LOG3)
        self.assertTrue(result['changed'])
        self.assertEqual(result['source'], SOURCE)
        self.assertEqual(result['sequence'], 3)
        self.store.save_config(SOURCE, default_config())
        self.reopen()
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['source'], SOURCE)
        self.assertEqual(snapshot['csv'], LOG3)
        self.assertEqual(snapshot['sequence'], 3)
        self.assertEqual(snapshot['synced_at'], result['synced_at'])
        self.assertEqual(snapshot['status']['employees'][0]['name'], 'Deneme Personeli')
        self.assertIsNone(self.store.load_snapshot('DEMO'))
        self.assertEqual(self.store.load_config(SOURCE), default_config())
        sources = self.store.list_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['source'], SOURCE)
        self.assertEqual(sources[0]['employees_count'], 1)
        self.assertEqual(sources[0]['sequence'], 3)

    def test_unknown_schema_is_not_reset(self):
        other = self.dir / 'yabanci.sqlite3'
        connection = sqlite3.connect(str(other))
        connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute("INSERT INTO meta VALUES ('schema_version', '99')")
        connection.execute('CREATE TABLE korunacak (value TEXT)')
        connection.execute("INSERT INTO korunacak VALUES ('KORU')")
        connection.commit()
        connection.close()
        with self.assertRaises(WorkforceError):
            WorkforceStore(other)
        connection = sqlite3.connect(str(other))
        self.assertEqual(connection.execute('SELECT value FROM korunacak').fetchone()[0], 'KORU')
        self.assertEqual(connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], '99')
        connection.close()

    def test_foreign_database_is_not_reset(self):
        foreign = self.dir / 'baska.sqlite3'
        connection = sqlite3.connect(str(foreign))
        connection.execute('CREATE TABLE rastgele (value TEXT)')
        connection.execute("INSERT INTO rastgele VALUES ('KORU')")
        connection.commit()
        connection.close()
        with self.assertRaises(WorkforceError):
            WorkforceStore(foreign)
        connection = sqlite3.connect(str(foreign))
        self.assertEqual(connection.execute('SELECT value FROM rastgele').fetchone()[0], 'KORU')
        connection.close()

    def test_corrupt_file_is_reported_and_kept(self):
        corrupt = self.dir / 'bozuk.sqlite3'
        original = b'bozuk veri' * 32
        corrupt.write_bytes(original)
        with self.assertRaises(WorkforceError):
            WorkforceStore(corrupt)
        self.assertEqual(corrupt.read_bytes(), original)

    def test_close_blocks_further_use(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        self.store.close()
        self.store.close()
        with self.assertRaises(WorkforceError):
            self.store.load_snapshot(SOURCE)
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(SOURCE, status(3), LOG3)
        with self.assertRaises(WorkforceError):
            self.store.backup(self.dir / 'kapali.zip')

    # -- snapshots --------------------------------------------------------
    def test_repeated_ingestion_is_idempotent(self):
        first = self.store.save_snapshot(SOURCE, status(3), LOG3)
        second = self.store.save_snapshot(SOURCE, status(3), LOG3)
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])
        self.assertEqual(self.snapshot_rows(), 1)
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], LOG3)
        # a pure clock tick is not new attendance data and must not clone the raw log
        tick = self.store.save_snapshot(SOURCE, status(3, now=1793529999, message='HAZIR'), LOG3)
        self.assertFalse(tick['changed'])
        self.assertEqual(self.snapshot_rows(), 1)

    def test_same_sequence_roster_update(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        result = self.store.save_snapshot(
            SOURCE, status(3, employees=[employee(name='Yeni Personel'), employee(OTHER_UID)],
                           roster_version=2), LOG3)
        self.assertTrue(result['changed'])
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['csv'], LOG3)
        self.assertEqual(snapshot['sequence'], 3)
        self.assertEqual(snapshot['status']['roster_version'], 2)
        self.assertEqual([row['uid'] for row in snapshot['status']['employees']],
                         [UID, OTHER_UID])
        self.assertEqual(snapshot['status']['employees'][0]['name'], 'Yeni Personel')
        self.assertEqual(self.snapshot_rows(), 1)
        self.assertEqual(self.store.list_sources()[0]['employees_count'], 2)

    def test_appended_log_is_accepted(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        result = self.store.save_snapshot(SOURCE, status(4), LOG4)
        self.assertTrue(result['changed'])
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], LOG4)
        self.assertEqual(self.snapshot_rows(), 1)  # one complete, append-verified cache; no quadratic prefixes

    def test_shorter_log_rejected_and_data_preserved(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(SOURCE, status(2), journal(E1, E2))
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['csv'], LOG3)
        self.assertEqual(snapshot['sequence'], 3)
        self.assertEqual(self.snapshot_rows(), 1)

    def test_edited_prefix_rejected_and_data_preserved(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        edited = journal((1, 1793520001, UID, 'I', 0), E2, E3)
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(SOURCE, status(3), edited)
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], LOG3)
        self.assertEqual(self.snapshot_rows(), 1)

    def test_sequence_status_mismatch_rejected(self):
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(SOURCE, status(2), journal(E1))
        self.assertIsNone(self.store.load_snapshot(SOURCE))
        self.assertEqual(self.snapshot_rows(), 0)
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        with self.assertRaises(WorkforceError):
            self.store.save_snapshot(SOURCE, status(3), journal(E1, E2))
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], LOG3)

    def test_zero_event_log_is_allowed(self):
        result = self.store.save_snapshot('DEMO', status(0), HEADER)
        self.assertTrue(result['changed'])
        self.assertEqual(result['sequence'], 0)
        self.assertEqual(self.store.load_snapshot('DEMO')['csv'], HEADER)

    def test_malformed_csv_rejected(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        for bad in (b'', b'sequence,epoch,uid,event\n', b'1,1793520000,11223344,I,0\n',
                    b'sequence,epoch,uid,event,seconds\n2,1793520000,11223344,I,0\n', 'metin'):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkforceError):
                    self.store.save_snapshot(SOURCE, status(1), bad)
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], LOG3)

    def test_invalid_status_rejected(self):
        cases = {
            'string bool': {'sequence': 3, 'employees': [employee()], 'clock_ok': 'evet'},
            'text too long': {'sequence': 3, 'employees': [employee()], 'message': 'x' * 500},
            'bad last uid': {'sequence': 3, 'employees': [employee()], 'last_uid': 'ZZZ'},
            'bad employee uid': {'sequence': 3, 'employees': [employee(uid='ZZZ')]},
            'missing sequence': {'employees': [employee()]},
            'missing employees': {'sequence': 3},
            'duplicate uid': {'sequence': 3, 'employees': [employee(), employee()]},
            'negative entry': {'sequence': 3, 'employees': [employee(entry=-5)]},
            'wrong entry type': {'sequence': 3, 'employees': [employee(entry='0')]},
        }
        for name, payload in cases.items():
            with self.subTest(name):
                with self.assertRaises(WorkforceError):
                    self.store.save_snapshot(SOURCE, payload, LOG3)
        self.assertIsNone(self.store.load_snapshot(SOURCE))

    def test_no_secrets_are_persisted(self):
        secrets = ['GIZLI-CSRF-TOKEN', 'GIZLI-PAROLA-123', 'Basic R0laTE6R0laMQ==',
                   'WPA-GIZLI-ANAHTAR', 'GIZLI-SSID', 'admin:gizli@', 'GIZLI-KIMLIK']
        row = employee()
        row.update({'csrf': secrets[0], 'password': secrets[1], 'Authorization': secrets[2],
                    '_auth': secrets[3], 'wifi': {'ssid': secrets[4], 'psk': secrets[3]},
                    'settings': {'password': secrets[1]}, 'nested': [{'token': secrets[0]}]})
        payload = status(3, employees=[row])
        payload.update({'csrf': secrets[0], 'password': secrets[1], 'Authorization': secrets[2],
                        '_auth': secrets[3], 'wifi': {'ssid': secrets[4], 'psk': secrets[3]},
                        'connection': {'address': 'http://admin:gizli@192.168.4.1'},
                        'settings': {'username': secrets[6]}})
        self.store.save_snapshot(SOURCE, payload, LOG3)
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(set(snapshot['status']), STATUS_FIELDS)
        self.assertEqual(set(snapshot['status']['employees'][0]), EMPLOYEE_FIELDS)
        database_bytes = self.path.read_bytes()
        for secret in secrets:
            self.assertNotIn(secret.encode('utf-8'), database_bytes)
        archive = self.store.backup(self.dir / 'sizinti.zip')
        with zipfile.ZipFile(archive) as source:
            members = {name: source.read(name) for name in source.namelist()}
        self.assertEqual(set(members), {DB_MEMBER, MANIFEST_MEMBER})
        manifest = json.loads(members[MANIFEST_MEMBER].decode('utf-8'))
        self.assertFalse(manifest['device_restore'])
        self.assertFalse(manifest['fleet_sync'])
        for secret in secrets:
            self.assertNotIn(secret.encode('utf-8'), members[DB_MEMBER])
            self.assertNotIn(secret.encode('utf-8'), members[MANIFEST_MEMBER])

    def test_concurrent_writes_are_serialised(self):
        errors = []

        def worker(index):
            try:
                self.store.save_snapshot('http://192.168.4.%d' % (10 + index), status(0), HEADER)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.list_sources()), 6)

    # -- source labels ----------------------------------------------------
    def test_source_normalisation(self):
        self.store.save_snapshot('192.168.4.1', status(0), HEADER)
        self.assertEqual(self.store.list_sources()[0]['source'], SOURCE)
        self.store.save_snapshot('DEMO - fiziksel cihaza bağlı değil', status(0), HEADER)
        self.assertEqual([row['source'] for row in self.store.list_sources()], ['DEMO', SOURCE])
        for bad in ('', 'http://admin:gizli@192.168.4.1', 'http://192.168.4.1/api/status',
                    'ftp://192.168.4.1'):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkforceError):
                    self.store.save_snapshot(bad, status(0), HEADER)
        self.assertEqual(len(self.store.list_sources()), 2)

    # -- policy -----------------------------------------------------------
    def test_config_history_is_retained(self):
        first = default_config()
        shift = {'id': 'varsayilan', 'name': 'Gündüz', 'start': '08:00', 'end': '17:00',
                 'weekdays': [0, 1, 2, 3, 4], 'break_minutes': 60, 'break_after_minutes': 240,
                 'late_tolerance': 5, 'early_tolerance': 5, 'overtime_after_minutes': 480}
        second = dict(default_config(), shifts=[shift])
        untouched_a, untouched_b = json.dumps(first), json.dumps(second)
        self.store.save_config(SOURCE, first)
        self.store.save_config(SOURCE, second)
        self.store.save_config(SOURCE, second)  # unchanged policy: no duplicate version
        self.assertEqual(json.dumps(first), untouched_a)
        self.assertEqual(json.dumps(second), untouched_b)
        self.assertEqual(self.store.load_config(SOURCE)['shifts'][0]['id'], 'varsayilan')
        connection = sqlite3.connect(str(self.path))
        try:
            rows = connection.execute('SELECT version, config_json, config_hash, saved_at'
                                      ' FROM policies ORDER BY version').fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual(rows[0][2], config_hash(first))
        self.assertEqual(rows[1][2], config_hash(second))
        self.assertEqual(json.loads(rows[0][1]), first)
        self.assertEqual(json.loads(rows[1][1]), second)
        self.assertTrue(all(row[3].endswith('Z') for row in rows))

    def test_invalid_policy_rejected(self):
        self.store.save_config(SOURCE, default_config())
        for bad in (dict(default_config(), version=2), dict(default_config(), gizli={}),
                    {'version': 1}, [], 'metin'):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkforceError):
                    self.store.save_config(SOURCE, bad)
        self.assertEqual(self.store.load_config(SOURCE), default_config())
        connection = sqlite3.connect(str(self.path))
        try:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM policies').fetchone()[0], 1)
        finally:
            connection.close()

    def test_config_default_and_isolation(self):
        self.assertEqual(self.store.load_config('DEMO'), default_config())
        self.store.save_config('DEMO', dict(default_config(), holidays={'2026-01-01': 'Yılbaşı'}))
        self.assertEqual(self.store.load_config(SOURCE), default_config())
        self.assertEqual(self.store.load_config('DEMO')['holidays'], {'2026-01-01': 'Yılbaşı'})

    # -- backups ----------------------------------------------------------
    def test_archive_can_be_read_offline(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        self.store.save_config(SOURCE, default_config())
        destination = self.dir / 'elle.zip'
        self.assertEqual(self.store.backup(destination), destination)
        self.store.close()
        info = verify_backup(destination)
        self.assertTrue(info['ok'])
        self.assertEqual(info['schema_version'], SCHEMA_VERSION)
        self.assertEqual(info['sources'], 1)
        self.assertFalse(info['device_restore'])
        self.assertEqual(info['manifest']['kind'], 'manual')
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()), {DB_MEMBER, MANIFEST_MEMBER})
            database = sqlite3.connect(':memory:')
            try:
                database.deserialize(archive.read(DB_MEMBER))
                self.assertEqual(bytes(database.execute('SELECT csv FROM snapshots')
                                       .fetchone()[0]), LOG3)
                self.assertEqual(tuple(database.execute(
                    'SELECT source, sequence FROM source_state').fetchone()), (SOURCE, 3))
            finally:
                database.close()

    def test_default_backup_destination(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        path = self.store.backup()
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.dir)
        self.assertTrue(path.name.startswith('workforce-backup-'))
        self.assertTrue(verify_backup(path)['ok'])

    def test_backup_verification_and_tamper(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        good = self.dir / 'iyi.zip'
        self.store.backup(good)
        self.assertTrue(verify_backup(good)['ok'])
        with zipfile.ZipFile(good) as archive:
            manifest = json.loads(archive.read(MANIFEST_MEMBER).decode('utf-8'))
            database = archive.read(DB_MEMBER)
        self.assertEqual(manifest['database_sha256'], hashlib.sha256(database).hexdigest())

        def rewrite(name, members, compression=zipfile.ZIP_DEFLATED):
            target = self.dir / name
            with zipfile.ZipFile(target, 'w', compression) as archive:
                for member, data in members:
                    archive.writestr(member, data)
            return target

        changed = rewrite('degistirilmis.zip', [
            (DB_MEMBER, database + b'\x00'),
            (MANIFEST_MEMBER, json.dumps(manifest).encode('utf-8'))])
        with self.assertRaises(WorkforceError):
            verify_backup(changed)
        extra_member = rewrite('fazla.zip', [
            (DB_MEMBER, database),
            (MANIFEST_MEMBER, json.dumps(manifest).encode('utf-8')),
            ('ek.txt', b'x')])
        with self.assertRaises(WorkforceError):
            verify_backup(extra_member)
        traversal = rewrite('kacis.zip', [
            ('../' + DB_MEMBER, database),
            (MANIFEST_MEMBER, json.dumps(manifest).encode('utf-8'))])
        with self.assertRaises(WorkforceError):
            verify_backup(traversal)
        claimed = rewrite('geri_yukleme.zip', [
            (DB_MEMBER, database),
            (MANIFEST_MEMBER, json.dumps(dict(manifest, device_restore=True)).encode('utf-8'))])
        with self.assertRaises(WorkforceError):
            verify_backup(claimed)
        # CRC corruption inside a stored manifest member that still parses as JSON
        stored = rewrite('crc.zip', [
            (DB_MEMBER, database),
            (MANIFEST_MEMBER, json.dumps(manifest, indent=2).encode('utf-8'))],
            compression=zipfile.ZIP_STORED)
        raw = stored.read_bytes()
        marker = b'"kind": "manual"'
        offset = raw.index(marker) + len(b'"kind"')
        stored.write_bytes(raw[:offset] + b'\t' + raw[offset + 1:])
        with self.assertRaises(WorkforceError):
            verify_backup(stored)
        junk = self.dir / 'cop.zip'
        junk.write_bytes(b'bu bir zip degil')
        with self.assertRaises(WorkforceError):
            verify_backup(junk)
        with self.assertRaises(WorkforceError):
            verify_backup(self.dir / 'yok.zip')
        # nothing is ever extracted next to the archives
        self.assertEqual([item.name for item in self.dir.iterdir() if item.is_dir()], [])
        self.assertTrue(all(item.suffix in ('.zip', '.sqlite3')
                            for item in self.dir.iterdir()))

    def test_backup_failure_keeps_previous_file(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        destination = self.dir / 'yedek.zip'
        self.store.backup(destination)
        before = destination.read_bytes()
        digest = verify_backup(destination)['database_sha256']
        self.store.save_snapshot(SOURCE, status(4), LOG4)
        with patch('windows_app.workforce_store.verify_backup',
                   side_effect=WorkforceError('doğrulanamadı')):
            with self.assertRaises(WorkforceError):
                self.store.backup(destination)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(verify_backup(destination)['database_sha256'], digest)
        blocker = self.dir / 'dosya'
        blocker.write_bytes(b'x')
        with self.assertRaises(WorkforceError):
            self.store.backup(blocker / 'alt' / 'yedek.zip')
        self.assertEqual(sorted(item.name for item in self.dir.iterdir()),
                         ['dosya', 'workforce.sqlite3', 'yedek.zip'])
        # the store keeps working and can write a new archive afterwards
        self.assertTrue(verify_backup(self.store.backup(self.dir / 'yeni.zip'))['ok'])

    def test_auto_backup_at_most_once_per_day(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        self.store._today = lambda: '2026-01-02'
        first = self.store.auto_backup()
        self.assertEqual(first, self.dir / 'backups' / 'workforce-2026-01-02.zip')
        self.assertTrue(first.exists())
        self.assertEqual(verify_backup(first)['manifest']['kind'], 'automatic')
        self.assertIsNone(self.store.auto_backup())
        self.reopen()
        self.store._today = lambda: '2026-01-02'
        self.assertIsNone(self.store.auto_backup())
        self.store._today = lambda: '2026-01-03'
        second = self.store.auto_backup()
        self.assertEqual(second, self.dir / 'backups' / 'workforce-2026-01-03.zip')
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(verify_backup(second)['ok'], True)

    def test_memory_store_isolation(self):
        empty = self.dir / 'bellek'
        empty.mkdir()
        store = WorkforceStore(':memory:')
        self.addCleanup(store.close)
        self.assertIsNone(store.path)
        self.assertTrue(store.save_snapshot('DEMO', status(3), LOG3)['changed'])
        self.assertEqual(store.load_snapshot('DEMO')['csv'], LOG3)
        self.assertIsNone(store.auto_backup())
        with self.assertRaises(WorkforceError):
            store.backup()
        archive = store.backup(empty / 'demo.zip')
        self.assertTrue(archive.exists())
        self.assertEqual(verify_backup(archive)['sources'], 1)
        self.assertEqual([item.name for item in empty.iterdir()], ['demo.zip'])

    def test_write_failure_preserves_known_good_data(self):
        self.store.save_snapshot(SOURCE, status(3), LOG3)
        self.store._conn.execute('PRAGMA busy_timeout = 100')
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            blocker.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(WorkforceError):
                self.store.save_snapshot(SOURCE, status(4), LOG4)
        finally:
            blocker.execute('ROLLBACK')
            blocker.close()
        snapshot = self.store.load_snapshot(SOURCE)
        self.assertEqual(snapshot['sequence'], 3)
        self.assertEqual(snapshot['csv'], LOG3)
        self.assertEqual(self.snapshot_rows(), 1)
        self.assertTrue(self.store.save_snapshot(SOURCE, status(4), LOG4)['changed'])
        self.assertEqual(self.store.load_snapshot(SOURCE)['csv'], LOG4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
