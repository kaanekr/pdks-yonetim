"""Local-only attendance archive (SQLite) plus verified ZIP backups.

Nothing in this module talks to a device: it never writes firmware/config/serial/
network settings, never restores a device and never synchronises several devices.
A "source" is only a local profile label (a normalised device address or ``DEMO``);
it is *not* a trusted physical device identity and this archive is not a fleet
service. Only allowlisted status fields and the raw event CSV are stored.
"""
import contextlib
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import zipfile

from windows_app.client import DeviceError, normalize_address
from windows_app.journal_actions import validate_authorized_rewrite
from windows_app.workforce_model import (WorkforceError, config_hash, default_config,
                                         parse_journal, text, uid as checked_uid,
                                         validate_config)


SCHEMA_VERSION = 1
FORMAT_NAME = 'PDKS-Workforce-Archive'
FORMAT_VERSION = 1
DB_MEMBER = 'workforce.sqlite3'
MANIFEST_MEMBER = 'MANIFEST.json'
MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_EMPLOYEES = 10000
BACKUP_NOTE = ('Yerel puantaj arşivi: yalnız cihazdan okunan durum/günlük kopyaları ve yerel '
               'puantaj politikalarını içerir. Cihaz ayarları, şifreler ve bağlantı bilgileri '
               'bu arşivde yoktur; arşiv cihaza geri yüklenemez ve cihazlar arası eşitleme yapmaz.')

REQUIRED_TABLES = frozenset({'meta', 'snapshots', 'source_state', 'policies'})

# Strict allowlists: anything else in a status payload is dropped before storage.
STATUS_BOOL_FIELDS = ('clock_ok', 'storage_ok', 'rtc_present', 'rfid_ok', 'oled_ok',
                      'sd_enabled', 'sd_ok', 'permanent_delete', 'event_names', 'ota_update')
STATUS_INT_RANGES = {'now': (0, 4102444799), 'sequence': (0, 2 ** 32 - 1),
                     'roster_version': (0, 2 ** 32 - 1), 'api_version': (0, 1000),
                     'firmware_sequence': (0, 2 ** 32 - 1),
                     'journal_revision': (0, 2 ** 32 - 1),
                     'last_event_epoch': (0, 4102444799)}
STATUS_TEXT_LIMITS = {'clock_reason': 160, 'message': 200, 'firmware_version': 64, 'firmware_target': 64}
EMPLOYEE_FIELDS = ('uid', 'card_uid', 'name', 'active', 'inside', 'entry',
                   'total_seconds', 'live_seconds')
EMPLOYEE_SECONDS = ('total_seconds', 'live_seconds')
# Values that change with every poll (clock tick) and therefore never mean "new data".
VOLATILE_STATUS_FIELDS = frozenset({'now'})

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (
           key TEXT PRIMARY KEY,
           value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS snapshots (
           snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
           source TEXT NOT NULL,
           sequence INTEGER NOT NULL,
           csv BLOB NOT NULL,
           csv_sha256 TEXT NOT NULL,
           synced_at TEXT NOT NULL,
           UNIQUE (source, csv_sha256))""",
    """CREATE TABLE IF NOT EXISTS source_state (
           source TEXT PRIMARY KEY,
           snapshot_id INTEGER NOT NULL,
           sequence INTEGER NOT NULL,
           status_json TEXT NOT NULL,
           status_sha256 TEXT NOT NULL,
           essential_sha256 TEXT NOT NULL,
           employees_count INTEGER NOT NULL,
           synced_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS policies (
           policy_id INTEGER PRIMARY KEY AUTOINCREMENT,
           source TEXT NOT NULL,
           version INTEGER NOT NULL,
           config_json TEXT NOT NULL,
           config_hash TEXT NOT NULL,
           saved_at TEXT NOT NULL,
           UNIQUE (source, version))""",
    'CREATE INDEX IF NOT EXISTS policies_source_version ON policies (source, version DESC)',
)

READ_FAILED = 'Yerel arşiv okunamadı; dosya değiştirilmedi.'
WRITE_FAILED = 'Yerel arşiv yazılamadı; mevcut arşiv verileri korundu.'
BAD_ARCHIVE = 'Yedek arşivi doğrulanamadı; dosya açılmadı ve hiçbir yere çıkarılmadı.'


def _utc_text(moment=None):
    moment = moment if moment is not None else datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def _journal_named(data):
    """True when the raw journal header is the named 8-column contract."""
    return b'name_source' in bytes(data).split(b'\n', 1)[0]


def _stored_status(status_json):
    try:
        stored = json.loads(status_json)
    except (TypeError, ValueError):
        raise WorkforceError('Kayıtlı cihaz durumu okunamadı; dosya değiştirilmedi.') from None
    if not isinstance(stored, dict):
        raise WorkforceError('Kayıtlı cihaz durumu okunamadı; dosya değiştirilmedi.')
    return stored


def _normalize_source(source):
    """Return the stored label: a device address without credentials, or DEMO."""
    if not isinstance(source, str):
        raise WorkforceError('Arşiv kaynağı metin olmalı; kayıt yapılmadı.')
    value = source.strip()
    if not value:
        raise WorkforceError('Arşiv kaynağı boş olamaz; kayıt yapılmadı.')
    if value.split()[0].upper() == 'DEMO':
        return 'DEMO'
    if '@' in value:
        raise WorkforceError('Arşiv kaynağı kimlik bilgisi içeremez; yalnız cihaz adresi veya DEMO.')
    try:
        address = normalize_address(value)
    except DeviceError:
        raise WorkforceError('Arşiv kaynağı geçersiz; yalnız cihaz adresi veya DEMO kaydedilir.') from None
    if '@' in address or len(address) > 200:
        raise WorkforceError('Arşiv kaynağı kimlik bilgisi içeremez; yalnız cihaz adresi veya DEMO.')
    return address


def _clean_employee(item):
    if not isinstance(item, dict):
        raise WorkforceError('Cihaz personel listesi geçersiz; arşiv değiştirilmedi.')
    row = {}
    try:
        row['uid'] = checked_uid(item.get('uid'))
        row['card_uid'] = checked_uid(item.get('card_uid', item.get('uid')))
    except WorkforceError:
        raise WorkforceError('Cihaz personel listesindeki UID geçersiz; arşiv değiştirilmedi.') from None
    row['name'] = text(item.get('name'), 120, empty=True)
    for key in ('active', 'inside'):
        if type(item.get(key)) is not bool:
            raise WorkforceError('Cihaz personel durumu geçersiz; arşiv değiştirilmedi.')
        row[key] = item[key]
    if type(item.get('entry')) is not int or not 0 <= item['entry'] <= 4102444799:
        raise WorkforceError('Cihaz personel giriş zamanı geçersiz; arşiv değiştirilmedi.')
    row['entry'] = item['entry']
    for key in EMPLOYEE_SECONDS:
        value = item.get(key, 0)
        if type(value) is not int or not 0 <= value <= (2 ** 64 - 1 if key == 'total_seconds' else 2 ** 32 - 1):
            raise WorkforceError('Cihaz personel süresi geçersiz; arşiv değiştirilmedi.')
        row[key] = value
    return {key: row[key] for key in EMPLOYEE_FIELDS}


def _clean_status(status):
    """Strict allowlist copy of the device status; unknown/secret fields never survive."""
    if not isinstance(status, dict):
        raise WorkforceError('Cihaz durumu okunamadı; arşiv değiştirilmedi.')
    clean = {}
    for key in STATUS_BOOL_FIELDS:
        if key not in status:
            continue
        if type(status[key]) is not bool:
            raise WorkforceError('Cihaz durum alanları geçersiz; arşiv değiştirilmedi.')
        clean[key] = status[key]
    for key, (low, high) in STATUS_INT_RANGES.items():
        if key not in status:
            continue
        value = status[key]
        if type(value) is not int or not low <= value <= high:
            raise WorkforceError('Cihaz durum alanları geçersiz; arşiv değiştirilmedi.')
        clean[key] = value
    for key, limit in STATUS_TEXT_LIMITS.items():
        if key not in status:
            continue
        clean[key] = text(status[key], limit, empty=True)
    if 'last_uid' in status:
        value = status['last_uid']
        if value in ('', None):
            clean['last_uid'] = ''
        else:
            try:
                clean['last_uid'] = checked_uid(value)
            except WorkforceError:
                raise WorkforceError('Cihaz durumundaki son UID geçersiz; arşiv değiştirilmedi.') from None
    if 'sequence' not in clean:
        raise WorkforceError('Cihaz kayıt numarası eksik; arşiv değiştirilmedi.')
    employees = status.get('employees')
    if not isinstance(employees, list) or len(employees) > MAX_EMPLOYEES:
        raise WorkforceError('Cihaz personel listesi geçersiz; arşiv değiştirilmedi.')
    rows, seen = [], set()
    for item in employees:
        row = _clean_employee(item)
        if row['uid'] in seen:
            raise WorkforceError('Cihaz personel listesinde UID yineleniyor; arşiv değiştirilmedi.')
        seen.add(row['uid'])
        rows.append(row)
    clean['employees'] = rows
    return clean


def _events(rows):
    return [(row['sequence'], row['epoch'], row['uid'], row['event'], row['seconds'])
            for row in rows]


def _essential_hash(clean):
    essential = {key: value for key, value in clean.items()
                 if key not in VOLATILE_STATUS_FIELDS}
    # Live seconds is a clock tick for open sessions, not new attendance data.
    essential['employees'] = [dict(row, live_seconds=0) for row in clean['employees']]
    return hashlib.sha256(_canonical(essential).encode('ascii')).hexdigest()


def _inspect_database(data):
    """Integrity/tables/schema check of an archive image held in memory. No extraction."""
    if not data:
        raise ValueError()
    connection = sqlite3.connect(':memory:')
    try:
        connection.deserialize(data)
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError()
        tables = {row[0] for row in
                  connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not REQUIRED_TABLES <= tables:
            raise ValueError()
        stored = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if stored is None or stored[0] != str(SCHEMA_VERSION):
            raise ValueError()
        return connection.execute('SELECT COUNT(*) FROM source_state').fetchone()[0]
    finally:
        connection.close()


def verify_backup(path):
    """Check a ZIP archive without extracting it.

    Exactly two members are allowed (the database image and MANIFEST.json); the
    manifest schema/timestamp/SHA256 values must match, the ZIP CRCs must pass and
    the embedded database must open, be intact and carry this archive schema.
    """
    try:
        with zipfile.ZipFile(str(path)) as archive:
            names = archive.namelist()
            if len(names) != 2 or set(names) != {MANIFEST_MEMBER, DB_MEMBER}:
                raise ValueError()
            if archive.testzip() is not None:  # CRC check of every member
                raise ValueError()
            raw_manifest = archive.read(MANIFEST_MEMBER)
            data = archive.read(DB_MEMBER)
        if len(raw_manifest) > MAX_MANIFEST_BYTES:
            raise ValueError()
        manifest = json.loads(raw_manifest.decode('utf-8'))
        if not isinstance(manifest, dict):
            raise ValueError()
        schema = manifest.get('schema_version')
        if (manifest.get('format') != FORMAT_NAME or manifest.get('format_version') != FORMAT_VERSION or
                type(schema) is not int or schema != SCHEMA_VERSION):
            raise ValueError()
        created_at = manifest.get('created_at')
        if not isinstance(created_at, str) or len(created_at) != 20:
            raise ValueError()
        datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%SZ')
        if manifest.get('database') != DB_MEMBER or manifest.get('kind') not in ('manual', 'automatic'):
            raise ValueError()
        if manifest.get('device_restore') is not False or manifest.get('fleet_sync') is not False:
            raise ValueError()
        digest = hashlib.sha256(data).hexdigest()
        if manifest.get('database_sha256') != digest or manifest.get('database_size') != len(data):
            raise ValueError()
        sources = _inspect_database(data)
        return {'ok': True, 'manifest': manifest, 'schema_version': schema, 'created_at': created_at,
                'database_sha256': digest, 'database_size': len(data), 'sources': sources,
                'device_restore': False}
    except WorkforceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError,
            sqlite3.Error, zipfile.BadZipFile):
        raise WorkforceError(BAD_ARCHIVE) from None


class WorkforceStore:
    """Thread-safe local archive. File-backed by default, ``':memory:'`` for DEMO."""

    def __init__(self, path=None):
        self._lock = threading.RLock()
        self._conn = None
        if path is None:
            try:
                base = Path(os.environ['LOCALAPPDATA'])
            except KeyError:
                raise WorkforceError('Windows yerel veri klasörü bulunamadı; arşiv açılmadı.') from None
            self.path = base / 'PDKS_Yonetim' / 'workforce.sqlite3'
        else:
            self.path = None if path == ':memory:' else Path(path)
        self._memory = self.path is None
        try:
            if self._memory:
                self._conn = sqlite3.connect(':memory:', check_same_thread=False)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute('PRAGMA foreign_keys = ON')
            self._conn.execute('PRAGMA synchronous = FULL')
            self._init_schema()
        except WorkforceError:
            self._discard()
            raise
        except (OSError, sqlite3.Error):
            self._discard()
            raise WorkforceError('Yerel arşiv açılamadı; mevcut dosyalar korundu.') from None

    # -- plumbing ---------------------------------------------------------
    def _discard(self):
        connection, self._conn = self._conn, None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _ensure_open(self):
        if self._conn is None:
            raise WorkforceError('Yerel arşiv kapalı; işlem yapılamadı.')

    def _tables(self):
        return {row[0] for row in
                self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def _init_schema(self):
        tables = self._tables()
        if 'meta' not in tables:
            if tables - {'sqlite_sequence'}:
                raise WorkforceError('Bu dosya puantaj arşivi değil; veriler korunmak için değiştirilmedi.')
            with self._write() as conn:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                             (str(SCHEMA_VERSION),))
            return
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise WorkforceError('Arşiv şema sürümü eksik; veriler korunmak için değiştirilmedi.')
        if row[0] != str(SCHEMA_VERSION):
            raise WorkforceError('Yerel arşiv şeması bu sürümle uyumlu değil; veriler silinmedi.')
        if not REQUIRED_TABLES <= tables:
            raise WorkforceError('Yerel arşiv tabloları eksik; veriler sıfırlanmadı.')

    @contextlib.contextmanager
    def _read(self):
        with self._lock:
            self._ensure_open()
            try:
                yield self._conn
            except sqlite3.Error:
                raise WorkforceError(READ_FAILED) from None

    @contextlib.contextmanager
    def _write(self):
        with self._lock:
            self._ensure_open()
            try:
                self._conn.execute('BEGIN IMMEDIATE')
            except sqlite3.Error:
                raise WorkforceError(WRITE_FAILED) from None
            try:
                yield self._conn
            except BaseException as exc:
                try:
                    self._conn.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise WorkforceError(WRITE_FAILED) from None
                raise
            else:
                try:
                    self._conn.execute('COMMIT')
                except sqlite3.Error:
                    try:
                        self._conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise WorkforceError(WRITE_FAILED) from None

    def _timestamp(self):
        return _utc_text()

    def _today(self):
        return date.today().isoformat()

    @staticmethod
    def _meta_get(conn, key):
        row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row[0] if row else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        with self._lock:
            if self._conn is None:
                return
            connection, self._conn = self._conn, None
            try:
                connection.close()
            except sqlite3.Error:
                raise WorkforceError('Yerel arşiv kapatılamadı.') from None

    # -- snapshots --------------------------------------------------------
    def save_snapshot(self, source, status, csv_data):
        """Store one polled status + full raw event log for a local source label."""
        with self._lock:
            self._ensure_open()
            label = _normalize_source(source)
            clean = _clean_status(status)
            if not isinstance(csv_data, (bytes, bytearray)) or len(csv_data) > MAX_CSV_BYTES:
                raise WorkforceError('Kayıt CSV verisi eksik veya çok büyük; arşiv değiştirilmedi.')
            raw = bytes(csv_data)  # original bytes are stored unchanged
            events = parse_journal(raw)
            if clean['sequence'] != len(events):
                raise WorkforceError('Cihaz kayıt numarası CSV satır sayısıyla uyuşmuyor; '
                                     'arşiv değiştirilmedi.')
            digest = hashlib.sha256(raw).hexdigest()
            status_json = _canonical(clean)
            essential_sha = _essential_hash(clean)
            with self._write() as conn:
                previous = conn.execute(
                    'SELECT t.snapshot_id, t.essential_sha256, t.status_json, s.csv, s.csv_sha256'
                    ' FROM source_state AS t JOIN snapshots AS s ON s.snapshot_id = t.snapshot_id'
                    ' WHERE t.source = ?', (label,)).fetchone()
                if previous is not None:
                    self._require_prefix(previous['csv'], raw, events)
                    stored_revision = _stored_status(previous['status_json']).get('journal_revision')
                    if stored_revision is not None and stored_revision != clean.get('journal_revision'):
                        raise WorkforceError('Cihaz günlük sürümü arşivdeki sürümden farklı; yalnız '
                                             'onaylanmış kalıcı silme bunu değiştirebilir ve arşiv '
                                             'değiştirilmedi.')
                same_raw = previous is not None and previous['csv_sha256'] == digest
                changed = (previous is None or not same_raw or
                           previous['essential_sha256'] != essential_sha)
                if same_raw:
                    snapshot_id = previous['snapshot_id']
                elif previous is not None:
                    # The verified new full log contains every preceding raw row.
                    # Extend the cached image instead of retaining O(n²) prefix copies.
                    snapshot_id = previous['snapshot_id']
                    conn.execute('UPDATE snapshots SET sequence = ?, csv = ?, csv_sha256 = ?, synced_at = ?'
                                 ' WHERE snapshot_id = ?',
                                 (clean['sequence'], raw, digest, self._timestamp(), snapshot_id))
                else:
                    known = conn.execute('SELECT snapshot_id FROM snapshots'
                                         ' WHERE source = ? AND csv_sha256 = ?',
                                         (label, digest)).fetchone()
                    if known is not None:
                        snapshot_id = known['snapshot_id']
                    else:
                        stamp = self._timestamp()
                        cursor = conn.execute(
                            'INSERT INTO snapshots (source, sequence, csv, csv_sha256, synced_at)'
                            ' VALUES (?, ?, ?, ?, ?)',
                            (label, clean['sequence'], raw, digest, stamp))
                        snapshot_id = cursor.lastrowid
                stamp = self._timestamp()
                conn.execute(
                    'INSERT INTO source_state (source, snapshot_id, sequence, status_json,'
                    ' status_sha256, essential_sha256, employees_count, synced_at)'
                    ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)'
                    ' ON CONFLICT(source) DO UPDATE SET snapshot_id = excluded.snapshot_id,'
                    ' sequence = excluded.sequence, status_json = excluded.status_json,'
                    ' status_sha256 = excluded.status_sha256,'
                    ' essential_sha256 = excluded.essential_sha256,'
                    ' employees_count = excluded.employees_count, synced_at = excluded.synced_at',
                    (label, snapshot_id, clean['sequence'], status_json,
                     hashlib.sha256(status_json.encode('ascii')).hexdigest(),
                     essential_sha, len(clean['employees']), stamp))
            return {'changed': bool(changed), 'sequence': clean['sequence'], 'source': label,
                    'synced_at': stamp}

    @staticmethod
    def _require_prefix(previous_csv, new_csv, events):
        """The five device fields must always extend; known named metadata is immutable."""
        try:
            old = parse_journal(bytes(previous_csv))
        except WorkforceError:
            raise WorkforceError('Arşivdeki önceki günlük okunamadı; yeni veri yazılmadı.') from None
        old_rows, new_rows = _events(old), _events(events)
        if len(new_rows) < len(old_rows) or new_rows[:len(old_rows)] != old_rows:
            raise WorkforceError('Cihaz günlüğü önceki arşivin üzerine eklenmiyor (kısaldı veya '
                                 'eski satırlar değişti); arşiv değiştirilmedi ve sıfırlanmadı.')
        if not _journal_named(previous_csv):
            return  # a legacy image carries no names: a named8 poll is pure enrichment
        if not _journal_named(new_csv):
            raise WorkforceError('Cihaz günlüğü adlandırılmış biçimden eski biçime düşürülemez; '
                                 'arşiv değiştirilmedi.')
        for before, after in zip(old, events):
            if after['archived'] != before['archived']:
                raise WorkforceError('Arşivdeki personel silme işareti istenmeyen şekilde değişti; '
                                     'arşiv değiştirilmedi.')
            if before['name_source'] in ('recorded', 'migrated'):
                if (after['name'], after['name_source']) != (before['name'], before['name_source']):
                    raise WorkforceError('Kayıtlı personel adı kayboldu veya değişti; arşiv '
                                         'değiştirilmedi.')
            elif (before['name_source'] == 'current' and
                  (after['name_source'] == 'unknown' or not after['name'])):
                raise WorkforceError('Rosterden türetilen personel adı kayboldu; arşiv değiştirilmedi.')

    def accept_authorized_snapshot(self, source, before_status, before_csv, after_status,
                                   after_csv, kind, target):
        """Apply one explicitly authorised device rewrite to the stored snapshot.

        The approved transformation is proved by the pure validator before any local
        work, the stored image must still be byte-identical to ``before_csv`` with the
        same journal revision, and only then is that single snapshot row replaced.
        There is no force/reset path; other sources, policies and backups are untouched.
        """
        with self._lock:
            self._ensure_open()
            label = _normalize_source(source)
            try:
                validate_authorized_rewrite(before_status, before_csv, after_status,
                                            after_csv, kind, target)
            except WorkforceError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError):
                raise WorkforceError('Onaylanan silme dönüşümü doğrulanamadı; arşiv değiştirilmedi.') from None
            clean_before = _clean_status(before_status)
            clean_after = _clean_status(after_status)
            revision = clean_before.get('journal_revision')
            if type(revision) is not int:
                raise WorkforceError('Onaylanan silme için günlük sürümü eksik; arşiv değiştirilmedi.')
            before_raw = bytes(before_csv)
            raw = bytes(after_csv)
            digest = hashlib.sha256(raw).hexdigest()
            status_json = _canonical(clean_after)
            essential_sha = _essential_hash(clean_after)
            self._conn.execute('PRAGMA secure_delete = ON')  # wiped cells while replacing the image
            with self._write() as conn:
                previous = conn.execute(
                    'SELECT t.snapshot_id, t.status_json, s.csv, s.csv_sha256'
                    ' FROM source_state AS t JOIN snapshots AS s ON s.snapshot_id = t.snapshot_id'
                    ' WHERE t.source = ?', (label,)).fetchone()
                if previous is None:
                    raise WorkforceError('Arşivde bu kaynak için doğrulanmış kayıt yok; silme onayı '
                                         'uygulanmadı.')
                stored = bytes(previous['csv'])
                if (stored != before_raw or
                        hashlib.sha256(before_raw).hexdigest() != previous['csv_sha256'] or
                        hashlib.sha256(stored).hexdigest() != previous['csv_sha256']):
                    raise WorkforceError('Arşivdeki günlük onaylanan silme öncesi kopyayla aynı değil; '
                                         'arşiv değiştirilmedi.')
                if _stored_status(previous['status_json']).get('journal_revision') != revision:
                    raise WorkforceError('Arşivdeki günlük sürümü onaylanan silme öncesi sürümle '
                                         'eşleşmiyor; arşiv değiştirilmedi.')
                stamp = self._timestamp()
                conn.execute('UPDATE snapshots SET sequence = ?, csv = ?, csv_sha256 = ?, synced_at = ?'
                             ' WHERE snapshot_id = ?',
                             (clean_after['sequence'], raw, digest, stamp, previous['snapshot_id']))
                conn.execute(
                    'UPDATE source_state SET sequence = ?, status_json = ?, status_sha256 = ?,'
                    ' essential_sha256 = ?, employees_count = ?, synced_at = ? WHERE source = ?',
                    (clean_after['sequence'], status_json,
                     hashlib.sha256(status_json.encode('ascii')).hexdigest(), essential_sha,
                      len(clean_after['employees']), stamp, label))
                conn.execute('DELETE FROM meta WHERE key = ?', (self._delete_intent_key(label),))
            result = {'changed': True, 'source': label, 'sequence': clean_after['sequence'],
                      'synced_at': stamp}
            if not self._memory:
                try:
                    self._compact_deleted_pages()
                except sqlite3.Error:
                    # The new snapshot is committed; never pretend it was rolled back.
                    result['cleanup_warning'] = ('Arşiv güncellendi ancak eski günlük kalıntısı '
                                                 'dosyadan tamamen temizlenemedi; arşiv geri '
                                                 'alınmadı.')
            return result

    @staticmethod
    def _delete_intent_key(source):
        return 'delete-intent:' + hashlib.sha256(source.encode('utf-8')).hexdigest()

    def remember_delete_intent(self, source, before_status, before_csv, kind, target):
        """Persist only an approved request description, never credentials or a second log copy."""
        with self._lock:
            self._ensure_open()
            label = _normalize_source(source)
            clean = _clean_status(before_status)
            if kind not in ('employee', 'session') or clean.get('permanent_delete') is not True:
                raise WorkforceError('Silme isteği türü/cihaz desteği geçersiz.')
            revision = clean.get('journal_revision')
            if type(revision) is not int:
                raise WorkforceError('Silme öncesi günlük sürümü eksik.')
            current = self.load_snapshot(label)
            if (current is None or current['csv'] != before_csv or
                    current['status'].get('journal_revision') != revision):
                raise WorkforceError('Silme öncesi görüntü arşivle eşleşmiyor; önce Yenile deyin.')
            intent = dict(source=label, kind=kind, target=target, before_status=clean,
                          csv_sha256=hashlib.sha256(before_csv).hexdigest())
            with self._write() as conn:
                key = self._delete_intent_key(label)
                if self._meta_get(conn, key) is not None:
                    raise WorkforceError('Önceki silme isteğinin sonucu kontrol edilmeli; önce Yenile deyin.')
                conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)'
                             ' ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, _canonical(intent)))

    def load_delete_intent(self, source):
        with self._lock:
            self._ensure_open()
            label = _normalize_source(source)
            with self._read() as conn:
                raw = self._meta_get(conn, self._delete_intent_key(label))
            if raw is None:
                return None
            try:
                intent = json.loads(raw)
                current = self.load_snapshot(label)
                if (intent['source'] != label or intent['kind'] not in ('employee', 'session') or
                        current is None or hashlib.sha256(current['csv']).hexdigest() != intent['csv_sha256']):
                    raise ValueError()
                intent['before_status'] = _clean_status(intent['before_status'])
                intent['before_csv'] = current['csv']
                return intent
            except (TypeError, KeyError, ValueError):
                raise WorkforceError('Bekleyen silme onayı arşivle uyuşmuyor; otomatik üzerine yazılmadı.') from None

    def clear_delete_intent(self, source):
        with self._lock:
            self._ensure_open()
            label = _normalize_source(source)
            with self._write() as conn:
                conn.execute('DELETE FROM meta WHERE key = ?', (self._delete_intent_key(label),))

    def _compact_deleted_pages(self):
        """Rebuild the file after commit so freed cells do not retain deleted text."""
        self._conn.execute('VACUUM')

    def load_snapshot(self, source):
        with self._lock:
            self._ensure_open()
            label = _normalize_source(source)
            with self._read() as conn:
                row = conn.execute(
                    'SELECT t.sequence, t.status_json, t.status_sha256, t.synced_at, s.csv, s.csv_sha256'
                    ' FROM source_state AS t JOIN snapshots AS s ON s.snapshot_id = t.snapshot_id'
                    ' WHERE t.source = ?', (label,)).fetchone()
            if row is None:
                return None
            raw = bytes(row['csv'])
            if (hashlib.sha256(raw).hexdigest() != row['csv_sha256'] or
                    hashlib.sha256(row['status_json'].encode('ascii')).hexdigest() != row['status_sha256']):
                raise WorkforceError('Arşiv kayıt özeti uyuşmuyor; bozuk kopya raporlanmadı.')
            try:
                status = json.loads(row['status_json'])
            except (TypeError, ValueError):
                raise WorkforceError('Kayıtlı cihaz durumu okunamadı; dosya değiştirilmedi.') from None
            return {'source': label, 'status': status, 'csv': raw,
                    'synced_at': row['synced_at'], 'sequence': row['sequence']}

    def list_sources(self):
        with self._lock:
            self._ensure_open()
            with self._read() as conn:
                rows = conn.execute('SELECT source, synced_at, sequence, employees_count'
                                    ' FROM source_state ORDER BY source').fetchall()
            return [{'source': row['source'], 'synced_at': row['synced_at'],
                     'sequence': row['sequence'], 'employees_count': row['employees_count']}
                    for row in rows]

    # -- local policy -----------------------------------------------------
    def load_config(self, source):
        with self._lock:
            self._ensure_open()
            label = _normalize_source(source)
            with self._read() as conn:
                row = conn.execute('SELECT config_json, config_hash FROM policies WHERE source = ?'
                                   ' ORDER BY version DESC LIMIT 1', (label,)).fetchone()
            if row is None:
                return validate_config(default_config())
            try:
                data = validate_config(json.loads(row['config_json']))
                if config_hash(data) != row['config_hash']:
                    raise WorkforceError('Yerel ayar özeti uyuşmuyor; bozuk politika kullanılmadı.')
                return data
            except (TypeError, ValueError):
                raise WorkforceError('Kayıtlı yerel ayarlar okunamadı; dosya değiştirilmedi.') from None

    def save_config(self, source, config):
        with self._lock:
            self._ensure_open()
            label = _normalize_source(source)
            data = validate_config(config)  # detached, allowlisted policy
            payload = _canonical(data)
            digest = config_hash(data)
            with self._write() as conn:
                current = conn.execute('SELECT version, config_hash FROM policies'
                                       ' WHERE source = ? ORDER BY version DESC LIMIT 1',
                                       (label,)).fetchone()
                if current is not None and current['config_hash'] == digest:
                    return None  # unchanged policy: keep history, add no duplicate version
                version = 1 if current is None else current['version'] + 1
                conn.execute('INSERT INTO policies (source, version, config_json, config_hash,'
                             ' saved_at) VALUES (?, ?, ?, ?, ?)',
                             (label, version, payload, digest, self._timestamp()))
            return None

    # -- backups ----------------------------------------------------------
    def backup(self, destination=None):
        """Write a verified ZIP archive; on failure an existing file is left untouched."""
        with self._lock:
            self._ensure_open()
            if self._memory and destination is None:
                raise WorkforceError('Bellek içi DEMO arşivi için hedef konum gerekli.')
            if destination is None:
                destination = self.path.parent / ('workforce-backup-%s.zip'
                                                  % self._timestamp().replace(':', '').replace('-', ''))
            path = Path(destination)
            if self.path is not None and path.resolve() == self.path.resolve():
                raise WorkforceError('Arşiv veritabanının üzerine yedek yazılamaz; ZIP konumu seçin.')
            self._write_archive(path, 'manual')
            return path

    def auto_backup(self):
        """At most one automatic backup per local day; disabled for DEMO memory stores."""
        with self._lock:
            self._ensure_open()
            if self._memory:
                return None
            today = self._today()
            with self._read() as conn:
                if self._meta_get(conn, 'auto_backup_date') == today:
                    return None
            path = self.path.parent / 'backups' / ('workforce-%s.zip' % today)
            self._write_archive(path, 'automatic')
            with self._write() as conn:
                conn.execute("INSERT INTO meta (key, value) VALUES ('auto_backup_date', ?)"
                             ' ON CONFLICT(key) DO UPDATE SET value = excluded.value', (today,))
                conn.execute("INSERT INTO meta (key, value) VALUES ('auto_backup_path', ?)"
                             ' ON CONFLICT(key) DO UPDATE SET value = excluded.value', (str(path),))
            return path

    def _write_archive(self, destination, kind):
        """Consistent copy via the SQLite backup API, then verify before replacing."""
        self._ensure_open()
        db_path = zip_path = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            handle, db_path = tempfile.mkstemp(prefix='workforce-image-', suffix='.sqlite3',
                                               dir=str(destination.parent))
            os.close(handle)
            handle, zip_path = tempfile.mkstemp(prefix='workforce-archive-', suffix='.tmp',
                                                dir=str(destination.parent))
            os.close(handle)
        except OSError:
            for leftover in (db_path, zip_path):
                if leftover is not None:
                    try:
                        Path(leftover).unlink(missing_ok=True)
                    except OSError:
                        pass
            raise WorkforceError('Yedek konumu hazırlanamadı; mevcut yedek dosyası korundu.') from None
        try:
            target = sqlite3.connect(db_path)
            try:
                self._conn.backup(target)
                target.commit()
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise WorkforceError('Arşiv veritabanı sağlamlık denetiminden geçemedi; '
                                         'yedek yazılmadı.')
                tables = {row[0] for row in
                          target.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if not REQUIRED_TABLES <= tables:
                    raise WorkforceError('Arşiv veritabanı beklenen tabloları içermiyor; yedek yazılmadı.')
            finally:
                target.close()
            data = Path(db_path).read_bytes()
            if not data:
                raise WorkforceError('Arşiv görüntüsü boş; yedek yazılmadı.')
            digest = hashlib.sha256(data).hexdigest()
            manifest = {'format': FORMAT_NAME, 'format_version': FORMAT_VERSION,
                        'schema_version': SCHEMA_VERSION, 'created_at': self._timestamp(),
                        'kind': kind, 'database': DB_MEMBER, 'database_sha256': digest,
                        'database_size': len(data), 'device_restore': False,
                        'fleet_sync': False, 'note': BACKUP_NOTE}
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(DB_MEMBER, data)
                archive.writestr(MANIFEST_MEMBER,
                                 json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
                                 .encode('utf-8'))
            verify_backup(zip_path)
            # Temporary file is verified first; only then the destination is replaced.
            os.replace(zip_path, str(destination))
            zip_path = None
        except WorkforceError:
            raise
        except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError):
            raise WorkforceError('Yedek yazılamadı; mevcut yedek dosyası değiştirilmedi.') from None
        finally:
            for leftover in (db_path, zip_path):
                if leftover is not None:
                    try:
                        Path(leftover).unlink(missing_ok=True)
                    except OSError:
                        pass
