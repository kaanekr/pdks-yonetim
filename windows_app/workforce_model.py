"""Shared, versioned inputs for local-only attendance reporting (never device writes)."""
import calendar
import copy
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json

from windows_app.client import (DeviceError, canonical_uid, has_control,
                                LEGACY_JOURNAL_FIELDS, NAMED_JOURNAL_FIELDS,
                                journal_metadata)


class WorkforceError(Exception):
    pass


DAILY_COLUMNS = [('date', 'Tarih'), ('name', 'Personel'), ('uid', 'Sabit UID'),
                 ('employee_no', 'Sicil'), ('department', 'Bölüm'), ('shift', 'Vardiya'),
                 ('first_in', 'İlk giriş'), ('last_out', 'Son çıkış'),
                 ('card_seconds', 'Kart süresi'), ('manual_seconds', 'Manuel süre'),
                 ('gross_seconds', 'Ham toplam'), ('break_seconds', 'Mola düşümü'),
                 ('net_seconds', 'Net süre'), ('overtime_seconds', 'Fazla mesai adayı'),
                 ('late_minutes', 'Geç dakika'), ('early_minutes', 'Erken dakika'), ('status', 'Durum')]
SUMMARY_COLUMNS = [('name', 'Personel'), ('uid', 'Sabit UID'), ('department', 'Bölüm'),
                   ('net_seconds', 'Net süre'), ('card_seconds', 'Kart süresi'),
                   ('manual_seconds', 'Manuel süre'), ('overtime_seconds', 'Fazla mesai adayı'),
                   ('late_count', 'Geç gün'), ('early_count', 'Erken gün'),
                   ('absent_days', 'Devamsız gün'), ('exception_days', 'İncelenecek gün')]


def default_config():
    return {'version': 1, 'shifts': [], 'assignments': [], 'holidays': {}, 'leaves': [], 'people': {}}


def checked_date(value):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value or not 2024 <= result.year <= 2099:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise WorkforceError('Tarih YYYY-AA-GG biçiminde, 2024–2099 arasında olmalı.') from None


def minute_of_day(value):
    try:
        parts = value.split(':')
        if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
            raise ValueError()
        hour, minute = map(int, parts)
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError()
        return hour * 60 + minute
    except (ValueError, TypeError, AttributeError):
        raise WorkforceError('Vardiya saati SS:DD biçiminde olmalı.') from None


def text(value, limit=120, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise WorkforceError('Metin alanı boş, çok uzun veya geçersiz karakter içeriyor.')
    return value.strip()


def uid(value):
    try:
        if canonical_uid(value) != value:
            raise ValueError()
        return value
    except (DeviceError, ValueError, TypeError, AttributeError):
        raise WorkforceError('Sabit personel UID geçersiz.') from None


def validate_config(config):
    """Return a detached, allowlisted policy. No passwords, tokens or device config."""
    try:
        if not isinstance(config, dict) or type(config.get('version')) is not int or config['version'] != 1:
            raise WorkforceError('Yerel puantaj ayarları sürümü desteklenmiyor.')
        if set(config) != set(default_config()):
            raise WorkforceError('Yerel puantaj ayarları alanları geçersiz.')
        data = copy.deepcopy(config)
        for key in ('shifts', 'assignments', 'leaves'):
            if not isinstance(data[key], list) or len(data[key]) > 10000:
                raise WorkforceError('Yerel ayar listesi geçersiz veya çok büyük.')
        if not isinstance(data['holidays'], dict) or not isinstance(data['people'], dict):
            raise WorkforceError('Takvim/personel ayarları geçersiz.')
        shift_ids = set()
        for shift in data['shifts']:
            fields = {'id', 'name', 'start', 'end', 'weekdays', 'break_minutes', 'break_after_minutes',
                      'late_tolerance', 'early_tolerance', 'overtime_after_minutes'}
            if set(shift) != fields:
                raise WorkforceError('Vardiya alanları geçersiz.')
            shift['id'] = text(shift['id'], 64)
            shift['name'] = text(shift['name'])
            if shift['id'] in shift_ids:
                raise WorkforceError('Vardiya kimliği yineleniyor.')
            shift_ids.add(shift['id'])
            if minute_of_day(shift['start']) == minute_of_day(shift['end']):
                raise WorkforceError('Vardiya başlangıç ve bitişi aynı olamaz.')
            days = shift['weekdays']
            if (not isinstance(days, list) or not days or len(days) != len(set(days)) or
                    any(type(day) is not int or not 0 <= day <= 6 for day in days)):
                raise WorkforceError('Vardiya günleri 0=Pazartesi … 6=Pazar olmalı.')
            for key in fields - {'id', 'name', 'start', 'end', 'weekdays'}:
                if type(shift[key]) is not int or not 0 <= shift[key] <= 1440:
                    raise WorkforceError('Vardiya dakika ayarları 0–1440 arasında olmalı.')
        for assignment in data['assignments']:
            if set(assignment) != {'uid', 'shift_id', 'effective_from', 'effective_to'}:
                raise WorkforceError('Vardiya ataması alanları geçersiz.')
            if not isinstance(assignment['effective_to'], str):
                raise WorkforceError('Vardiya bitiş tarihi metin veya boş metin olmalı.')
            if assignment['uid'] != '*':
                uid(assignment['uid'])
            if assignment['shift_id'] not in shift_ids:
                raise WorkforceError('Atanan vardiya bulunamadı.')
            start = checked_date(assignment['effective_from'])
            end = checked_date(assignment['effective_to']) if assignment['effective_to'] else date(2099, 12, 31)
            if end < start:
                raise WorkforceError('Vardiya ataması tarihleri ters.')
        # An individual assignment overrides *, but same-target date ranges may not overlap.
        ranges = {}
        for item in data['assignments']:
            start, end = item['effective_from'], item['effective_to'] or '2099-12-31'
            for old_start, old_end in ranges.setdefault(item['uid'], []):
                if start <= old_end and old_start <= end:
                    raise WorkforceError('Aynı personel/grup için vardiya tarihleri çakışıyor.')
            ranges[item['uid']].append((start, end))
        for day, label in data['holidays'].items():
            checked_date(day)
            text(label)
        leave_ranges = {}
        for leave in data['leaves']:
            if set(leave) != {'uid', 'start', 'end', 'kind'}:
                raise WorkforceError('İzin alanları geçersiz.')
            uid(leave['uid'])
            if checked_date(leave['end']) < checked_date(leave['start']):
                raise WorkforceError('İzin bitişi başlangıçtan önce olamaz.')
            text(leave['kind'])
            for old_start, old_end in leave_ranges.setdefault(leave['uid'], []):
                if leave['start'] <= old_end and old_start <= leave['end']:
                    raise WorkforceError('Aynı personelin izin tarihleri çakışıyor.')
            leave_ranges[leave['uid']].append((leave['start'], leave['end']))
        for person, meta in data['people'].items():
            uid(person)
            if set(meta) != {'department', 'employee_no'}:
                raise WorkforceError('Yerel personel alanları geçersiz.')
            text(meta['department'], empty=True)
            text(meta['employee_no'], 64, empty=True)
        return data
    except WorkforceError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise WorkforceError('Yerel puantaj ayarları geçersiz.') from None


def config_hash(config):
    return hashlib.sha256(json.dumps(validate_config(config), sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode('ascii')).hexdigest()


def parse_journal(data):
    """Raw chronological-sequence events; M may be historical, R rebases clock."""
    try:
        if not isinstance(data, bytes) or len(data) > 8 * 1024 * 1024:
            raise ValueError()
        reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
        if tuple(reader.fieldnames or ()) not in (LEGACY_JOURNAL_FIELDS, NAMED_JOURNAL_FIELDS):
            raise ValueError()
        events = []
        minimum, maximum = 1704067200, 4102444799
        for row in reader:
            if (set(row) != set(reader.fieldnames) or any(value is None for value in row.values()) or
                    any(has_control(value) for value in row.values())):
                raise ValueError()
            sequence, epoch, seconds = (int(row[key]) for key in ('sequence', 'epoch', 'seconds'))
            if sequence != len(events) + 1 or not minimum <= epoch <= maximum or seconds < 0:
                raise ValueError()
            kind = row['event']
            uid(row['uid'])
            if kind not in ('I', 'O', 'R', 'M'):
                raise ValueError()
            if kind == 'I' and seconds != 0:
                raise ValueError()
            if kind in ('O', 'M') and (seconds >= epoch or epoch - seconds < minimum or (kind == 'M' and seconds == 0)):
                raise ValueError()
            if kind == 'R' and (row['uid'] != '00000000' or not (seconds == 0 or minimum <= seconds <= maximum)):
                raise ValueError()
            event = dict(sequence=sequence, epoch=epoch, uid=row['uid'], event=kind, seconds=seconds)
            if tuple(reader.fieldnames) == NAMED_JOURNAL_FIELDS:
                name, source, archived = journal_metadata(row['name'], row['name_source'], row['archived'], kind)
                event.update(name=name, name_source=source, archived=archived)
            events.append(event)
        return events
    except (ValueError, TypeError, KeyError, UnicodeError, csv.Error, WorkforceError, DeviceError):
        raise WorkforceError('Olay günlüğü eksik, sırası bozuk veya biçimi desteklenmiyor; arşiv değiştirilmedi.') from None


def local_epoch(day, minute=0):
    value = checked_date(day) if isinstance(day, str) else day
    return calendar.timegm(datetime(value.year, value.month, value.day).timetuple()) + minute * 60


def epoch_text(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%d %H:%M:%S') if epoch else ''
