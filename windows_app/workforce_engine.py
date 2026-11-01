"""Pure attendance report builder (local-only, read-only, never writes a device)."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import re

from windows_app.workforce_model import (DAILY_COLUMNS, SUMMARY_COLUMNS, WorkforceError,
                                         checked_date, config_hash, epoch_text, local_epoch,
                                         parse_journal, validate_config)

DAY = 86400
MAX_DAYS = 366
MIN_EPOCH, MAX_EPOCH = 1704067200, 4102444799
UID_RE = re.compile(r'^(?:[0-9A-F]{8}|[0-9A-F]{14}|[0-9A-F]{20})$')
# Flags that make the net duration advisory instead of reliable.
REVIEW_FLAGS = ('clock_recovery', 'duplicate_entry', 'open_session', 'orphan_out', 'overlap',
                'unknown_employee', 'long_session', 'after_snapshot')
# Flags that block overtime and late/early classification for the day.
BLOCKING_FLAGS = REVIEW_FLAGS
# Visible provenance when only the current device roster supplies a name.
NAME_FALLBACK = 'Ad: mevcut personel kaydı (tarihsel ad bilinmiyor)'
NAME_SOURCES = ('recorded', 'migrated', 'current', 'unknown')
POLICY_NOTES = [
    'Mola, vardiya tanımındaki sabit dakikadır; iş günü başına bir kez ve ham toplamı aşmayacak '
    'biçimde düşülür (0 dakika düşüm yok demektir).',
    'Fazla mesai adayı yalnızca atanmış vardiya gününde üretilir: vardiyada açık eşik varsa o, '
    'yoksa planlanan süre eksi sabit mola sınırıdır; vardiya atanmayan günde varsayım yapılmaz.',
    'Saat kurtarma (R) kaydı süre üretmez; açık oturumları görünür "Saat kurtarma / iptal" '
    'istisnasına çevirir ve R üzerindeki eski saat değeri hiç sayılmaz.',
    'Manuel mesai ayrı kaynaktır; yalnız manuel olan günde fiziksel ilk giriş/son çıkış alanları boş kalır.',
    'Sabit mola düşümü, kart oturumları arasındaki boşluklara ek bir düşümdür; mola zaten kartla '
    'çıkış/giriş yapılarak ayrılıyorsa çifte düşümü önlemek için sabit molayı 0 bırakın.',
    'Devamsızlık yalnızca güvenilir cihaz saati, bilinen günlük kapsamı, aktif personel ve atanmış '
    'vardiya günü için üretilir; izin/tatil günü devamsızlık sayılmaz.',
    'Gece vardiyasının ertesi güne taşan erken saatleri önceki iş gününe bağlanır; atanmamış '
    'aralıklar gece yarısından bölünür ve yalnızca aralık toplamı sayılır, aradaki boşluk sayılmaz.',
    'Giriş/çıkış toleransı yalnızca sınıflandırmayı etkiler; ham saatler ve süreler yuvarlanmaz.',
    'Süresi 0 olan çıkış kaydı sıfır süreli oturum kabul edilir; süre uydurulmaz.',
    'Açık oturum, yetim çıkış veya çakışan kayıt varsa net süre/fazla mesai kesin kabul edilmez.',
]


def _label(value, name, limit=120, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise WorkforceError(f'{name} alanı geçersiz.')
    return value


def _uid(value):
    if not isinstance(value, str) or not UID_RE.match(value):
        raise WorkforceError('Sabit personel UID geçersiz.')
    return value


def _minute(value):
    try:
        hour, minute = value.split(':')
        if len(hour) != 2 or len(minute) != 2:
            raise ValueError()
        hour, minute = int(hour), int(minute)
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError()
        return hour * 60 + minute
    except (AttributeError, TypeError, ValueError):
        raise WorkforceError('Vardiya saati SS:DD biçiminde olmalı.') from None


def _day(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).date()


def _period(start, end):
    first, last = checked_date(start), checked_date(end)
    if last < first:
        raise WorkforceError('Bitiş tarihi başlangıçtan önce olamaz.')
    if (last - first).days + 1 > MAX_DAYS:
        raise WorkforceError('Rapor dönemi en fazla 366 gün olabilir.')
    return first, last


def _snapshot(status):
    if not isinstance(status, dict):
        raise WorkforceError('Cihaz durumu geçersiz.')
    clock_ok = status.get('clock_ok')
    if type(clock_ok) is not bool:
        raise WorkforceError('Cihaz saat durumu (clock_ok) eksik veya geçersiz.')
    now = status.get('now')
    if type(now) is not int or (not MIN_EPOCH <= now <= MAX_EPOCH and not (now == 0 and not clock_ok)):
        raise WorkforceError('Cihaz anlık zamanı geçersiz.')
    raw = status.get('employees', [])
    if not isinstance(raw, list) or len(raw) > 256:
        raise WorkforceError('Cihaz personel listesi geçersiz.')
    people, duplicated = {}, False
    for item in raw:
        if not isinstance(item, dict):
            raise WorkforceError('Cihaz personel kaydı geçersiz.')
        person = _uid(item.get('uid'))
        if person in people:
            duplicated = True
            continue
        if type(item.get('active')) is not bool or type(item.get('inside')) is not bool:
            raise WorkforceError('Personel etkinlik/oturum alanı geçersiz.')
        people[person] = {'name': _label(item.get('name'), 'Personel adı'),
                          'active': item['active'], 'inside': item['inside']}
    return now, clock_ok, people, duplicated


def _name_record(event):
    """Optional recorded-name snapshot from a journal event; legacy rows carry none."""
    name = event.get('name')
    if (not isinstance(name, str) or not name.strip() or len(name) > 120 or
            any(ord(c) < 32 or ord(c) == 127 for c in name)):
        return None
    source = event.get('name_source')
    if source not in NAME_SOURCES:
        source = 'unknown'
    return {'name': name, 'source': source, 'archived': event.get('archived') is True,
            'epoch': event['epoch'], 'order': event.get('sequence', event['epoch'])}


def _reconstruct(events):
    cards, manual = defaultdict(list), defaultdict(list)
    marks, duplicate, orphan, cancelled = defaultdict(set), defaultdict(int), defaultdict(int), defaultdict(int)
    opens, recovery = {}, []
    for event in events:
        kind, person, epoch, seconds = event['event'], event['uid'], event['epoch'], event['seconds']
        record = _name_record(event)
        if kind == 'I':
            span = opens.get(person)
            if span is None:
                opens[person] = {'start': epoch, 'last': epoch, 'dups': [],
                                 'names': [record] if record else []}
            else:
                span['dups'].append(epoch)
                span['last'] = epoch
                duplicate[person] += 1
                marks[(person, epoch)].add('duplicate_entry')
                if record:
                    span['names'].append(record)
        elif kind == 'O':
            span = opens.pop(person, None)
            flags, start = set(), epoch
            names = list(span['names']) if span else []
            if record:
                names.append(record)
            if span is None:
                orphan[person] += 1
                flags.add('orphan_out')
                start = epoch - seconds
            else:
                start, last = span['start'], span['last']
                if seconds:
                    if epoch - start != seconds:
                        if span['dups'] and epoch - last == seconds:
                            start = last
                            flags.add('duplicate_entry')
                        else:
                            raise WorkforceError(
                                'Çıkış kaydının süresi giriş zamanıyla eşleşmiyor; günlük tutarsız.')
                else:
                    if epoch != start:
                        if span['dups'] and epoch == last:
                            flags.add('duplicate_entry')
                        else:
                            raise WorkforceError('Sıfır süreli çıkış giriş zamanıyla eşleşmiyor; günlük tutarsız.')
                    start = epoch
            cards[person].append({'start': start, 'end': epoch, 'flags': flags, 'manual': False,
                                  'names': names})
        elif kind == 'M':
            manual[person].append({'start': epoch - seconds, 'end': epoch, 'flags': set(), 'manual': True,
                                   'names': [record] if record else []})
        else:  # R never carries worked time; it cancels every open session.
            for open_uid, span in list(opens.items()):
                cancelled[open_uid] += 1
                marks[(open_uid, span['start'])].add('clock_recovery')
            opens.clear()
            if seconds:
                recovery.append((min(seconds, epoch), max(seconds, epoch)))
    for person, span in opens.items():
        marks[(person, span['start'])].add('open_session')
    return cards, manual, marks, duplicate, orphan, cancelled, recovery


def _resolve_name(person, roster, records, worked):
    """Pick the visible name for one workday from event snapshots, never claiming more.

    Recorded snapshots win over inferred or current data. Legacy journals without names
    keep the roster name but are labelled as current, not historical.
    """
    ordered = sorted(records, key=lambda item: item['order'])
    recorded = [item for item in ordered if item['source'] == 'recorded']
    inferred = [item for item in ordered if item['source'] in ('migrated', 'current')]
    archived = [item for item in ordered if item['archived']]
    chosen, flags, notes = None, set(), []
    if recorded:
        chosen = recorded[0]
    elif inferred:
        chosen = inferred[0]
        flags.add('name_inferred')
        notes.append('Ad: taşınmış kayıttan tahmin (kayıtlı değil)' if chosen['source'] == 'migrated'
                     else 'Ad: güncel kayıttan tahmin (kayıtlı değil)')
    elif archived and person not in roster:
        chosen = archived[0]
    if chosen is None:
        current = roster[person]['name'] if person in roster else None
        if current and worked:
            return current, NAME_FALLBACK, flags, True
        return (current or person), '', flags, False
    if person not in roster and archived:
        flags.add('name_archived')
        notes.append('Ad: arşiv kaydı (cihaz listesinde silinmiş personel)')
    others = []
    for item in ordered:
        if item['name'] != chosen['name'] and item['name'] not in others:
            others.append(item['name'])
    if others:
        notes.append('Diğer ad kayıtları: ' + ', '.join(others))
    return chosen['name'], '; '.join(notes), flags, False


def _mark_overlaps(cards, manual, recovery, warnings):
    """Flag overlapping intervals and manual records inside a clock-recovery gap."""
    overlap_people, recovery_people = set(), set()
    for person in sorted(set(cards) | set(manual)):
        items = sorted(cards.get(person, []) + manual.get(person, []),
                       key=lambda item: (item['start'], item['end']))
        previous = None
        for item in items:
            if item['end'] <= item['start']:
                continue
            if previous is not None and item['start'] < previous['end']:
                item['flags'].add('overlap')
                previous['flags'].add('overlap')
                overlap_people.add(person)
            if previous is None or item['end'] > previous['end']:
                previous = item
        for item in manual.get(person, []):
            if any(item['start'] < high and low < item['end'] for low, high in recovery):
                item['flags'].add('clock_recovery')
                recovery_people.add(person)
    for person in sorted(overlap_people):
        warnings.append(f'{person}: çakışan kayıtlar var; net süre ve fazla mesai kesin değil.')
    for person in sorted(recovery_people):
        warnings.append(f'{person}: saat kurtarma aralığıyla çakışan manuel kayıt işaretlendi.')


def _policy(data):
    shifts = {shift['id']: shift for shift in data['shifts']}
    specific, wildcard = defaultdict(list), []
    for item in data['assignments']:
        start = checked_date(item['effective_from'])
        end = checked_date(item['effective_to']) if item['effective_to'] else date(2099, 12, 31)
        target = specific[item['uid']] if item['uid'] != '*' else wildcard
        target.append((start, end, shifts[item['shift_id']]))
    leaves = defaultdict(list)
    for item in data['leaves']:
        leaves[item['uid']].append((item['start'], item['end'], item['kind']))
    return specific, wildcard, leaves


def _shift_for(person, day, specific, wildcard):
    for start, end, shift in specific.get(person, ()):
        if start <= day <= end:
            return shift
    for start, end, shift in wildcard:
        if start <= day <= end:
            return shift
    return None


def _windows(person, days, specific, wildcard):
    found = []
    for day in days:
        shift = _shift_for(person, day, specific, wildcard)
        if shift is None or day.weekday() not in shift['weekdays']:
            continue
        begin = local_epoch(day, _minute(shift['start']))
        finish = local_epoch(day, _minute(shift['end']))
        if finish <= begin:
            finish += DAY
        found.append((day, begin, finish, shift))
    return found


def _owner(epoch, windows):
    for day, begin, finish, _shift in windows:
        if begin <= epoch < finish:
            return day
    return _day(epoch)


def _allocate(start, end, windows):
    """A completed session belongs to its assigned start workday, including overtime.

    Unassigned sessions split at midnight. Sessions over 24h are marked separately
    and split for display, never silently truncated or treated as reliable overtime.
    """
    owner = _owner(start, windows)
    if end - start <= DAY and any(day == owner for day, _a, _b, _s in windows):
        return [(owner, start, end)]
    if end <= start:
        return [(_day(start), start, end)]
    points = {start, end}
    midnight = (start // DAY + 1) * DAY
    while midnight < end:
        points.add(midnight)
        midnight += DAY
    for _day_, begin, finish, _shift in windows:
        if start < finish and begin < end:
            if start < begin < end:
                points.add(begin)
            if start < finish < end:
                points.add(finish)
    ordered = sorted(points)
    return [(_owner(a, windows), a, b) for a, b in zip(ordered, ordered[1:]) if b > a]


def build_report(status, csv_data, config, start, end, source='', synced_at=''):
    """Return a detached report dict; raises WorkforceError for invalid metadata or journal."""
    data = validate_config(config)
    digest = config_hash(data)
    source = _label(source, 'Kaynak profili', 80, empty=True)
    synced_at = _label(synced_at, 'Eşitleme zamanı', 40, empty=True)
    first, last = _period(start, end)
    now, clock_ok, roster, duplicated = _snapshot(status)
    events = parse_journal(csv_data)
    cards, manual, marks, duplicate, orphan, cancelled, recovery = _reconstruct(events)
    specific, wildcard, leaves = _policy(data)
    latest_names, event_names = {}, defaultdict(list)
    for event in events:
        record = _name_record(event)
        if record is not None and event['event'] in ('I', 'O', 'M'):
            event_names[event['uid']].append(record)
            if event['uid'] not in latest_names or record['order'] >= latest_names[event['uid']]['order']:
                latest_names[event['uid']] = record

    warnings = []
    if duplicated:
        warnings.append('Cihaz personel listesindeki yinelenen UID yok sayıldı.')
    _mark_overlaps(cards, manual, recovery, warnings)
    for person in sorted(duplicate):
        warnings.append(f'{person}: {duplicate[person]} yinelenen giriş kaydı yok sayıldı.')
    for person in sorted(orphan):
        warnings.append(f'{person}: girişi olmayan çıkış kaydı; süre kesin değil.')
    for person in sorted(cancelled):
        warnings.append(f'{person}: saat kurtarma {cancelled[person]} açık oturumu iptal etti; '
                        'süre sayılmadı.')
    for (person, _day_), flags in sorted(marks.items()):
        if 'open_session' in flags:
            warnings.append(f'{person}: çıkış kaydı yok; açık oturum süresi sayılmadı.')
    snapshot = _day(now)
    if clock_ok and last > snapshot:
        warnings.append('Rapor dönemi son alınan cihaz zamanını aşıyor; sonraki günler için devamsızlık üretilmedi.')
    coverage_start = next((event['epoch'] for event in events if event['event'] != 'M'), None)
    coverage = _day(coverage_start) if coverage_start is not None else None
    if data['assignments'] and (coverage is None or first < coverage):
        warnings.append('Dönemin başında günlük kapsamı bilinmiyor; kayıt kapsamından önce devamsızlık üretilmedi.')
    if not clock_ok:
        warnings.append('Cihaz saati güvenilir değil (clock_ok=false); devamsızlık ve fazla mesai '
                        'kesinleştirilmedi.')

    people = set(roster) | set(cards) | set(manual) | set(specific) | set(leaves)
    people |= set(data['people']) | {person for person, _day_ in marks}
    people.discard('*')
    for person in sorted(people - set(roster)):
        known = latest_names.get(person)
        if known is None:
            warnings.append(f'{person}: cihaz personel listesinde yok; ad yerine UID kullanıldı.')
        elif known['archived']:
            warnings.append(f'{person}: cihaz personel listesinde yok (silinmiş personel); '
                            f'ad arşiv kaydından korundu: {known["name"]}.')
        else:
            warnings.append(f'{person}: cihaz personel listesinde yok; ad kayıtlı geçmişten: '
                            f'{known["name"]}.')

    window_days = [first - timedelta(days=1) + timedelta(days=offset)
                   for offset in range((last - first).days + 2)]
    allocated = defaultdict(lambda: {'card': [], 'manual': []})
    windows = {}
    work_marks = defaultdict(set)
    event_name_days = defaultdict(list)
    for person in sorted(people):
        windows[person] = _windows(person, window_days, specific, wildcard)
        for (mark_person, epoch), flags in marks.items():
            if mark_person == person:
                work_marks[(person, _owner(epoch, windows[person]))] |= flags
        for record in event_names.get(person, ()):
            event_name_days[(person, _owner(record['epoch'], windows[person]))].append(record)
        for item in cards.get(person, []) + manual.get(person, []):
            if item['end'] - item['start'] > DAY:
                item['flags'].add('long_session')
            bucket = 'manual' if item['manual'] else 'card'
            for owner, begin, finish in _allocate(item['start'], item['end'], windows[person]):
                allocated[(person, owner)][bucket].append(
                    (begin, finish, frozenset(item['flags']), tuple(item['names'])))

    rows, totals = [], {}
    inferred_people, legacy_names_used = set(), False
    for person in sorted(people):
        active = roster[person]['active'] if person in roster else None
        meta = data['people'].get(person, {})
        by_day = {entry[0]: entry for entry in windows[person]}
        totals[person] = {'net_seconds': 0, 'card_seconds': 0, 'manual_seconds': 0,
                          'overtime_seconds': 0, 'late_count': 0, 'early_count': 0,
                          'absent_days': 0, 'exception_days': 0, 'days': 0}
        day = first
        while day <= last:
            slot = allocated.get((person, day), {'card': [], 'manual': []})
            day_marks = work_marks.get((person, day), set())
            shift_window = by_day.get(day)
            shift = shift_window[3] if shift_window else None
            kind = _leave(leaves, person, day)
            holiday = data['holidays'].get(day.isoformat())
            pending = bool(shift_window) and day <= snapshot and now < shift_window[2]
            card_pieces, manual_pieces = slot['card'], slot['manual']
            card_seconds = sum(b - a for a, b, _f, _n in card_pieces)
            manual_seconds = sum(b - a for a, b, _f, _n in manual_pieces)
            gross = card_seconds + manual_seconds
            flags = set(day_marks)
            for _a, _b, extra, _n in card_pieces + manual_pieces:
                flags |= extra
            records = [record for _a, _b, _f, names in card_pieces + manual_pieces for record in names]
            if not records:
                records = event_name_days.get((person, day), ())
            day_name, name_note, name_flags, legacy_fallback = _resolve_name(
                person, roster, records, gross > 0)
            flags |= name_flags
            if 'name_inferred' in name_flags:
                inferred_people.add(person)
            legacy_names_used = legacy_names_used or legacy_fallback
            if kind:
                flags.add('leave')
            if holiday:
                flags.add('holiday')
            if pending:
                flags.add('pending')
            if not clock_ok:
                flags.add('unknown_clock')
            if manual_pieces and not card_pieces:
                flags.add('manual_only')
            if person not in roster:
                flags.add('unknown_employee')
            range_start = shift_window[1] if shift_window else local_epoch(day)
            range_end = shift_window[2] if shift_window else range_start + DAY
            clock_gap = any(range_start < high and low < range_end for low, high in recovery)
            if clock_gap:
                flags.add('clock_recovery')
            if clock_ok and any(finish > now for _begin, finish, _flags, _names in card_pieces + manual_pieces):
                flags.add('after_snapshot')
            covered = (clock_ok and coverage_start is not None and coverage_start <= range_start and
                       day <= snapshot and not clock_gap)
            reach = clock_ok and day <= snapshot
            absent = (bool(shift_window) and active is True and covered and not pending and
                      gross == 0 and not card_pieces and not manual_pieces and
                      not (flags & set(REVIEW_FLAGS)) and kind is None and holiday is None)
            if absent:
                flags.add('absent')
            if (shift_window is None and (card_pieces or manual_pieces)):
                flags.add('unassigned')
            emit = bool(card_pieces or manual_pieces or day_marks or absent)
            if not emit and shift_window and active is True and reach and (kind or holiday or pending or clock_gap):
                emit = True
            if not emit:
                day += timedelta(days=1)
                continue
            break_seconds = 0
            if shift and shift['break_minutes'] and gross // 60 >= shift['break_after_minutes']:
                break_seconds = min(shift['break_minutes'] * 60, gross)
            net = gross - break_seconds
            classify = clock_ok and not pending and not (flags & set(BLOCKING_FLAGS))
            late = early = overtime = 0
            if classify and shift_window:
                if card_pieces:
                    if not kind and not holiday:
                        late = max(0, (min(a for a, _b, _f, _n in card_pieces) - shift_window[1] -
                                       shift['late_tolerance'] * 60 + 59) // 60)
                        early = max(0, (shift_window[2] - max(b for _a, b, _f, _n in card_pieces) -
                                        shift['early_tolerance'] * 60 + 59) // 60)
                if shift['overtime_after_minutes']:
                    threshold = shift['overtime_after_minutes'] * 60
                else:
                    threshold = max(0, (shift_window[2] - shift_window[1]) - shift['break_minutes'] * 60)
                overtime = max(0, net - threshold)
            if late:
                flags.add('late')
            if early:
                flags.add('early')
            labels = _status_labels(flags, pending, clock_ok, kind, holiday, absent, late, early,
                                    bool(card_pieces or manual_pieces), shift_window is not None,
                                    name_note)
            values = {
                'date': day.isoformat(), 'name': day_name, 'uid': person,
                'employee_no': meta.get('employee_no', ''), 'department': meta.get('department', ''),
                'shift': shift['name'] if shift else '',
                'first_in': epoch_text(min(a for a, _b, _f, _n in card_pieces)) if card_pieces else '',
                'last_out': epoch_text(max(b for _a, b, _f, _n in card_pieces)) if card_pieces else '',
                'card_seconds': card_seconds, 'manual_seconds': manual_seconds,
                'gross_seconds': gross, 'break_seconds': break_seconds, 'net_seconds': net,
                'overtime_seconds': overtime, 'late_minutes': late, 'early_minutes': early,
                'status': labels,
            }
            row = {key: values[key] for key, _title in DAILY_COLUMNS}
            row['flags'] = sorted(flags)
            rows.append(row)
            total = totals[person]
            total['net_seconds'] += net
            total['card_seconds'] += card_seconds
            total['manual_seconds'] += manual_seconds
            total['overtime_seconds'] += overtime
            total['late_count'] += 1 if late else 0
            total['early_count'] += 1 if early else 0
            total['absent_days'] += 1 if absent else 0
            total['exception_days'] += 1 if flags & (set(REVIEW_FLAGS) | {'late', 'early', 'absent'}) else 0
            total['days'] += 1 if gross > 0 else 0
            day += timedelta(days=1)

    if legacy_names_used:
        warnings.append(NAME_FALLBACK + '; adlar güncel personel listesinden, tarihsel ad kaydı değil.')
    for person in sorted(inferred_people):
        warnings.append(f'{person}: ad kaydı kayıtlı değil; taşınmış/güncel kayıttan tahmin edildi.')

    groups = {}
    for row in rows:
        day = date.fromisoformat(row['date'])
        week = (day - timedelta(days=day.weekday())).isoformat()
        slot = groups.setdefault((row['uid'], week),
                                 {'uid': row['uid'], 'name': row['name'], 'week_start': week,
                                  'net_seconds': 0, 'overtime_seconds': 0})
        slot['net_seconds'] += row['net_seconds']
        slot['overtime_seconds'] += row['overtime_seconds']
    weekly = [groups[key] for key in sorted(groups)]

    summary = []
    summary_names = {}
    for person in people:
        if person in roster:
            summary_names[person] = roster[person]['name']
        elif person in latest_names:
            summary_names[person] = latest_names[person]['name']
        else:
            summary_names[person] = person
    for person in sorted(people, key=lambda item: (summary_names[item], item)):
        meta = data['people'].get(person, {})
        values = {'name': summary_names[person], 'uid': person,
                  'department': meta.get('department', '')}
        values.update(totals[person])
        item = {key: values[key] for key, _title in SUMMARY_COLUMNS}
        item['days'] = totals[person]['days']
        summary.append(item)

    policy_notes = list(POLICY_NOTES)
    used_shifts = {row['shift'] for row in rows if row['shift']}
    for shift in data['shifts']:
        if shift['name'] in used_shifts:
            policy_notes.append(f'{shift["name"]}: {shift["start"]}–{shift["end"]}; mola {shift["break_minutes"]} dk '
                f'(ham süre eşiği {shift["break_after_minutes"]} dk); geç/erken tolerans '
                f'{shift["late_tolerance"]}/{shift["early_tolerance"]} dk; fazla mesai eşiği '
                f'{shift["overtime_after_minutes"]} dk (0=vardiya net planı).')

    return {'rows': rows, 'summary': summary, 'weekly': weekly, 'warnings': warnings,
            'start': first.isoformat(), 'end': last.isoformat(), 'source': source,
            'synced_at': synced_at,
            'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'config_hash': digest, 'policy_notes': policy_notes}


def _leave(leaves, person, day):
    stamp = day.isoformat()
    for start, end, kind in leaves.get(person, ()):
        if start <= stamp <= end:
            return kind
    return None


def _status_labels(flags, pending, clock_ok, kind, holiday, absent, late, early, has_time,
                   assigned, name_note=''):
    labels = []
    if pending:
        labels.append('Devam ediyor (kesinleşmedi)')
    if not clock_ok:
        labels.append('Saat kapsamı belirsiz')
    if 'clock_recovery' in flags:
        labels.append('Saat kurtarma / iptal')
    if 'open_session' in flags:
        labels.append('Açık oturum – çıkış yok')
    if 'orphan_out' in flags:
        labels.append('Eksik giriş (yetim çıkış)')
    if 'overlap' in flags:
        labels.append('Çakışan kayıt')
    if 'duplicate_entry' in flags:
        labels.append('Yinelenen giriş')
    if 'long_session' in flags:
        labels.append('24 saati aşan oturum — inceleyin')
    if 'after_snapshot' in flags:
        labels.append('Son doğrulanmış cihaz zamanından ileri kayıt')
    if kind:
        labels.append('İzin: ' + kind)
    if holiday:
        labels.append('Tatil: ' + holiday)
    if absent:
        labels.append('Devamsız')
    if 'manual_only' in flags:
        labels.append('Manuel mesai (kart kaydı yok)')
    if late:
        labels.append('Geç giriş')
    if early:
        labels.append('Erken çıkış')
    if not labels and has_time:
        labels.append('Vardiya atanmamış' if not assigned else 'Tamamlandı')
    if not labels:
        labels.append('Kayıt yok')
    if name_note:
        labels.append(name_note)
    return '; '.join(labels)
