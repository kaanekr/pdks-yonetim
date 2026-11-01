"""Disposable in-memory GUI demo. Never opens a network connection."""
import copy
import csv
import io
import json
import uuid
from windows_app.client import DeviceClient, DeviceError, encode_local_time


class DemoClient:
    address = 'DEMO — fiziksel cihaza bağlı değil'

    def __init__(self):
        self.state = {'csrf': 'demo', 'sequence': 1, 'roster_version': 1,
                      'api_version': 3, 'clock_recovery': True, 'personnel_management': True,'manual_attendance':True,
                      'now': 0, 'clock_ok': False, 'clock_reason': 'TEST: SAAT HATALI',
                      'storage_ok': True, 'last_uid': 'AABBCCDD', 'last_event_epoch': 1793520000,
                      'employees': [{'uid': '11223344', 'card_uid': '11223344', 'name': 'Deneme Personeli',
                                     'active': True, 'inside': True, 'entry': 1793520000,
                                     'total_seconds': 3600, 'live_seconds': 0}]}
        self.rows = ['sequence,epoch,uid,event,seconds', '1,1793520000,11223344,I,0']

    def status(self):
        return copy.deepcopy(self.state)

    def events(self):
        return ('\n'.join(self.rows) + '\n').encode('utf-8')

    def enable_named_journal(self):
        if self.state.get('event_names'):
            return
        from windows_app.workforce_model import parse_journal
        from windows_app.journal_actions import rewrite_events
        data = rewrite_events(parse_journal(self.events()), set(), self.state['employees'])
        self.rows = data.decode('utf-8').splitlines()
        self.state.update(api_version=4, journal_revision=0, permanent_delete=True, event_names=True)

    def _append(self, epoch, uid, kind, seconds):
        values = [self.state['sequence'], epoch, uid, kind, seconds]
        if self.state.get('event_names'):
            employee = next((p for p in self.state['employees'] if p['uid'] == uid), None)
            values += [employee['name'] if employee and kind != 'R' else '',
                       'recorded' if employee and kind != 'R' else 'unknown', 0]
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator='\n').writerow(values)
        self.rows.append(buffer.getvalue().rstrip('\n'))

    def _check(self, state):
        if DeviceClient.expected(state) != DeviceClient.expected(self.state):
            raise DeviceError('DEMO: durum değişti; yenileyin.')

    def save_employee(self, state, uid, name, card_uid, active=True, create=False):
        self._check(state)
        employee = next((e for e in self.state['employees'] if e['uid'] == uid), None)
        if create and employee:
            raise DeviceError('DEMO: personel zaten var.')
        if any(e['uid'] != uid and card_uid in (e['uid'], e['card_uid']) for e in self.state['employees']):
            raise DeviceError('DEMO: kart başka personele ait.')
        if employee and employee['inside'] and (not active or employee['card_uid'] != card_uid):
            raise DeviceError('DEMO: önce açık oturumu kurtarın.')
        if employee is None:
            if len(self.state['employees']) >= 32:
                raise DeviceError('DEMO: personel kapasitesi dolu.')
            from windows_app.workforce_model import parse_journal
            history = {event['uid'] for event in parse_journal(self.events())}
            if uid in history:
                used = history | {p['uid'] for p in self.state['employees']} | {p['card_uid'] for p in self.state['employees']}
                while uid in used:
                    uid = uuid.uuid4().hex[:8].upper()
            employee = {'uid': uid, 'inside': False, 'entry': 0, 'total_seconds': 0, 'live_seconds': 0}
            self.state['employees'].append(employee)
        employee.update(name=name, card_uid=card_uid, active=active)
        self.state['roster_version'] += 1
        return {'ok': True, 'message': 'DEMO: yalnız geçici belleğe kaydedildi.'}

    def set_time(self, state, text, recover=False):
        epoch = encode_local_time(text)
        if recover:
            self._check(state)
            self.state['sequence'] += 1
            self._append(epoch, '00000000', 'R', self.state['last_event_epoch'])
            self.state['last_event_epoch'] = epoch
            for e in self.state['employees']:
                e.update(inside=False, entry=0, live_seconds=0)
        elif any(e['inside'] for e in self.state['employees']) or epoch < self.state['last_event_epoch']:
            raise DeviceError('DEMO: açık oturum / geriye saat engeli. Kurtarma işlemini kullanın.')
        self.state.update(now=epoch, clock_ok=True, clock_reason='OK')
        return {'ok': True, 'message': 'DEMO: saat güncellendi; fiziksel cihaz değiştirilmedi.'}

    def add_manual(self,state,uid,start_text,end_text):
        self._check(state)
        start,end=encode_local_time(start_text),encode_local_time(end_text)
        employee=next((e for e in self.state['employees'] if e['uid']==uid),None)
        if not employee or employee['inside'] or not self.state['clock_ok'] or end<=start or end>self.state['now']:
            raise DeviceError('DEMO: kişi/açık oturum/saat/aralık uygun değil.')
        from windows_app.workforce_model import parse_journal
        for event in parse_journal(self.events()):
            if event['uid']==uid and event['event'] in ('O','M') and start<event['epoch'] and event['epoch']-event['seconds']<end:
                raise DeviceError('DEMO: mevcut mesai ile çakışıyor; eklenmedi.')
        self.state['sequence']+=1
        self._append(end, uid, 'M', end-start)
        employee['total_seconds']+=end-start
        self.state['last_event_epoch']=max(self.state['last_event_epoch'],end)
        return {'ok':True,'message':'DEMO: manuel mesai yalnız geçici belleğe eklendi.'}

    def _delete(self, state, kind, target):
        from windows_app.journal_actions import deletion_sequences, rewrite_events, validate_authorized_rewrite
        from windows_app.workforce_model import parse_journal, WorkforceError
        DeviceClient._permanent_state(state)
        self._check(state)
        before, events = self.events(), parse_journal(self.events())
        after = copy.deepcopy(self.state)
        try:
            removed = deletion_sequences(events, target) if kind == 'session' else set()
            data = rewrite_events(events, removed, self.state['employees'], target if kind == 'employee' else None)
            after['journal_revision'] += 1
            if kind == 'employee':
                after['employees'] = [p for p in after['employees'] if p['uid'] != target]
                after['roster_version'] += 1
            rows = parse_journal(data)
            after['sequence'] = len(rows)
            validate_authorized_rewrite(self.state, before, after, data, kind, target)
        except WorkforceError as exc:
            raise DeviceError(str(exc)) from None
        people = {p['uid']: p for p in after['employees']}
        for person in people.values():
            person.update(total_seconds=0, inside=False, entry=0, live_seconds=0)
        baseline = 0
        for event in rows:
            if event['event'] == 'R':
                for person in people.values():
                    person.update(inside=False, entry=0)
            elif event['uid'] in people:
                person = people[event['uid']]
                if event['event'] == 'I':
                    person.update(inside=True, entry=event['epoch'])
                elif event['event'] == 'O':
                    person.update(inside=False, entry=0)
                    person['total_seconds'] += event['seconds']
                elif event['event'] == 'M':
                    person['total_seconds'] += event['seconds']
            if event['event'] != 'M' or event['epoch'] > baseline:
                baseline = event['epoch']
        after['last_event_epoch'] = baseline
        self.state, self.rows = after, data.decode('utf-8').splitlines()
        return {'ok': True, 'message': 'DEMO: yalnız geçici bellekte kalıcı silme uygulandı.'}

    def delete_employee(self, state, uid):
        return self._delete(state, 'employee', uid)

    def delete_session(self, state, sequence):
        return self._delete(state, 'session', sequence)
