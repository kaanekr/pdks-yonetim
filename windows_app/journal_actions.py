"""Exact, explicitly authorised journal transformations; no I/O or force-reset path."""
import copy
import csv
import io

from windows_app.client import NAMED_JOURNAL_FIELDS
from windows_app.workforce_model import WorkforceError, parse_journal


def deletion_sequences(events, target_sequence):
    if type(target_sequence) is not int or target_sequence < 1:
        raise WorkforceError('Silinecek kayıt sırası geçersiz.')
    selected = next((event for event in events if event['sequence'] == target_sequence), None)
    if selected is None or selected['event'] == 'R':
        raise WorkforceError('Saat kurtarma kaydı silinemez; tamamlanmış oturum seçin.')
    if selected['event'] == 'M':
        return {target_sequence}
    opened, result = None, None
    for event in events:
        if event['event'] == 'R':
            opened = None
        elif event['uid'] == selected['uid']:
            if event['event'] == 'I':
                if opened is not None:
                    raise WorkforceError('Yinelenen giriş nedeniyle oturum güvenle eşleştirilemedi.')
                opened = event
            elif event['event'] == 'O':
                if event['sequence'] == target_sequence or (opened and opened['sequence'] == target_sequence):
                    if opened is None or event['epoch']-opened['epoch'] != event['seconds']:
                        raise WorkforceError('Giriş/çıkış eşleşmesi geçersiz; kayıt silinmedi.')
                    result = {opened['sequence'], event['sequence']}
                opened = None
    if result is None:
        raise WorkforceError('Açık/iptal edilmiş oturum silinemez; tamamlanmış giriş/çıkış veya M seçin.')
    return result


def rewrite_events(events, removed, employees, removed_uid=None):
    names = {employee['uid']: employee['name'] for employee in employees}
    out = io.StringIO(newline='')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(NAMED_JOURNAL_FIELDS)
    sequence, baseline = 0, 0
    for original in events:
        if original['sequence'] in removed:
            continue
        event = copy.deepcopy(original)
        sequence += 1
        event['sequence'] = sequence
        if event['event'] == 'R':
            event.update(seconds=baseline, name='', name_source='unknown', archived=False)
        else:
            if not event.get('name'):
                name = names.get(event['uid'], '')
                event.update(name=name, name_source='migrated' if name else 'unknown')
            elif event.get('name_source') == 'current':
                event['name_source'] = 'migrated'
            event['archived'] = bool(event.get('archived')) or event['uid'] == removed_uid
            if event['archived'] and not event['name']:
                raise WorkforceError('Silinen personelin geçmiş adı çözülemedi; geçmiş korunamadı.')
        if event['event'] != 'M' or event['epoch'] > baseline:
            baseline = event['epoch']
        writer.writerow([event['sequence'], event['epoch'], event['uid'], event['event'], event['seconds'],
                         event['name'], event['name_source'], int(event['archived'])])
    data = out.getvalue().encode('utf-8')
    parse_journal(data)  # validate the produced contract before it can replace anything
    return data


def _revision(status):
    value = status.get('journal_revision')
    if (status.get('api_version', 0) < 4 or status.get('permanent_delete') is not True or
            type(value) is not int or not 0 <= value <= 0xffffffff):
        raise WorkforceError('Kalıcı silme için uyumlu cihaz ve günlük sürümü gerekli.')
    return value


def validate_authorized_rewrite(before_status, before_csv, after_status, after_csv, kind, target):
    old_revision, new_revision = _revision(before_status), _revision(after_status)
    if new_revision != old_revision+1:
        raise WorkforceError('Günlük sürümü beklenen silme işlemiyle eşleşmiyor; arşiv değiştirilmedi.')
    before, after = parse_journal(before_csv), parse_journal(after_csv)
    if before_status.get('sequence') != len(before) or after_status.get('sequence') != len(after):
        raise WorkforceError('Silme öncesi/sonrası sıra doğrulaması başarısız.')
    people = before_status['employees']
    if kind == 'employee':
        selected = next((person for person in people if person['uid'] == target), None)
        if selected is None or selected.get('inside') is not False:
            raise WorkforceError('Personel bulunamadı veya açık oturumu var.')
        removed = set()
        remaining = [person for person in people if person['uid'] != target]
        expected = rewrite_events(before, removed, people, removed_uid=target)
    elif kind == 'session':
        remaining = people
        expected = rewrite_events(before, deletion_sequences(before, target), people)
    else:
        raise WorkforceError('Silme işlemi türü geçersiz.')
    def identities(items):
        return sorted((p['uid'], p['name'], p.get('card_uid', p['uid']), p['active']) for p in items)
    if identities(remaining) != identities(after_status['employees']):
        raise WorkforceError('Beklenmeyen personel değişikliği var; arşiv değiştirilmedi.')
    expected_events = parse_journal(expected)
    if after[:len(expected_events)] != expected_events or len(after) < len(expected_events):
        raise WorkforceError('Cihaz yalnız onaylanan kayıtları silmedi; arşiv değiştirilmedi.')
