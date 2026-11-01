"""Bounded, non-retrying HTTP client; optional GUI persistence is in settings.py."""
import base64
import calendar
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class DeviceError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward credentials/CSRF to a different host


def normalize_address(address):
    address = address.strip()
    if not address or any(ord(c) < 33 for c in address):
        raise DeviceError('Cihaz IP adresini girin; boşluk/kontrol karakteri kullanmayın.')
    if '://' not in address:
        address = 'http://' + address
    try:
        p = urlsplit(address)
        port = p.port
    except ValueError:
        raise DeviceError('Geçersiz cihaz adresi.') from None
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username is not None or
            p.password is not None or p.path not in ('', '/') or p.query or p.fragment or
            (port is not None and not 1 <= port <= 65535)):
        raise DeviceError('Yalnız cihaz adresini girin; örnek: http://192.168.4.1')
    return p.scheme + '://' + p.netloc


def canonical_uid(value):
    value = value.strip().upper()
    if len(value) not in (8, 14, 20) or any(c not in '0123456789ABCDEF' for c in value):
        raise DeviceError('Kart UID: 8, 14 veya 20 adet onaltılık karakter olmalı.')
    return value


def checked_name(value):
    value = value.strip(' ')
    if not value or len(value.encode('utf-8')) > 48 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DeviceError('Ad boş olamaz; en fazla 48 UTF-8 bayt ve kontrol karakteri içermemeli.')
    return value


def encode_local_time(text):
    try:
        dt = datetime.strptime(text.strip(), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        raise DeviceError('Tarih biçimi: YYYY-AA-GG SS:DD:SN') from None
    if not 2024 <= dt.year <= 2099:
        raise DeviceError('Tarih 2024–2099 arasında olmalı.')
    return calendar.timegm(dt.timetuple())  # device encodes Turkey wall time as UTC-like epoch


def format_local_time(epoch):
    if not epoch:
        return '—'
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OverflowError, OSError):
        return 'Geçersiz tarih'


def turkey_now():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')


def duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


LEGACY_JOURNAL_FIELDS = ('sequence', 'epoch', 'uid', 'event', 'seconds')
NAMED_JOURNAL_FIELDS = LEGACY_JOURNAL_FIELDS + ('name', 'name_source', 'archived')
JOURNAL_NAME_SOURCES = ('recorded', 'migrated', 'current', 'unknown')
MIN_JOURNAL_EPOCH, MAX_JOURNAL_EPOCH = 1704067200, 4102444799


def has_control(value):
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def journal_name(value):
    """Validate a stored journal name without silently trimming it."""
    if not isinstance(value, str) or not value.strip(' '):
        raise ValueError()
    try:
        too_long = len(value.encode('utf-8')) > 48
    except UnicodeError:
        raise ValueError() from None
    if too_long:
        raise ValueError()
    return value


def journal_metadata(name, source, archived, event):
    """Validate named-journal metadata; returns (name, source, archived_bool)."""
    if not all(isinstance(value, str) for value in (name, source, archived)):
        raise ValueError()
    if any(has_control(value) for value in (name, source, archived)):
        raise ValueError()
    if source not in JOURNAL_NAME_SOURCES or archived not in ('0', '1'):
        raise ValueError()
    flag = archived == '1'
    if event == 'R':
        if name or source != 'unknown' or flag:
            raise ValueError()
    elif source == 'unknown':
        if name or flag:
            raise ValueError()
    else:
        journal_name(name)
    return name, source, flag


def parse_events(data):
    try:
        reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
        fields = reader.fieldnames
        if fields == list(LEGACY_JOURNAL_FIELDS):
            named = False
        elif fields == list(NAMED_JOURNAL_FIELDS):
            named = True
        else:
            raise ValueError()
        rows = []
        for raw in reader:
            if set(raw) != set(fields) or any(value is None for value in raw.values()):
                raise ValueError()
            if any(has_control(value) for value in raw.values()):
                raise ValueError()
            seq, epoch, seconds = (int(raw[k]) for k in ('sequence', 'epoch', 'seconds'))
            if min(seq, epoch, seconds) < 0 or raw['event'] not in ('I', 'O', 'R', 'M'):
                raise ValueError()
            canonical_uid(raw['uid'])
            note = {'I': 'Giriş', 'O': 'Çıkış', 'R': 'Yönetici: saat kurtarma / açık oturumları iptal', 'M': 'Manuel mesai'}[raw['event']]
            if raw['event']=='M':
                if not seconds or seconds>=epoch:
                    raise ValueError()
                note += ' · başlangıç: ' + format_local_time(epoch-seconds)
            value = ('Önceki saat: ' + format_local_time(seconds)) if raw['event'] == 'R' else (duration(seconds) if raw['event'] in ('O','M') else '—')
            row = (seq, format_local_time(epoch), raw['uid'], note, value)
            if named:
                name, source, archived = journal_metadata(raw['name'], raw['name_source'], raw['archived'], raw['event'])
                row += (name, source, archived)
            rows.append(row)
        return rows
    except (UnicodeError, ValueError, KeyError, TypeError, csv.Error):
        raise DeviceError('Kayıt CSV biçimi desteklenmiyor; dosya değiştirilmedi.') from None


class DeviceClient:
    def __init__(self, address, username, password, timeout=5):
        self.address = normalize_address(address)
        if not username or ':' in username or any(ord(c) < 32 for c in username + password):
            raise DeviceError('Kullanıcı adı/şifre geçersiz.')
        self._auth = 'Basic ' + base64.b64encode((username + ':' + password).encode('utf-8')).decode('ascii')
        self._timeout = timeout
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    def _request(self, path, payload=None, csrf=None, csv_mode=False):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = {'Authorization': self._auth, 'Accept': 'text/csv' if csv_mode else 'application/json'}
        if data is not None:
            if not csrf:
                raise DeviceError('CSRF yok; cihaz durumunu yeniden okuyun.')
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': csrf})
        req = Request(self.address + path, data=data, headers=headers, method='GET' if data is None else 'POST')
        limit = 8 * 1024 * 1024 if csv_mode else 256 * 1024
        try:
            with self._opener.open(req, timeout=self._timeout) as response:
                body = response.read(limit + 1)
        except HTTPError as exc:
            if exc.code == 401:
                message = 'Kullanıcı adı veya şifre yanlış.'
            elif 300 <= exc.code < 400:
                message = 'Yönlendirme reddedildi; doğru cihaz IP adresini girin.'
            else:
                try:
                    message = str(json.loads(exc.read(8192).decode('utf-8')).get('message', 'İstek reddedildi.'))[:500]
                except (ValueError, UnicodeError, AttributeError):
                    message = 'Cihaz isteği reddetti.'
            code = exc.code
            exc.close()
            raise DeviceError(f'HTTP {code}: {message}') from None
        except (URLError, TimeoutError, OSError):
            suffix = ' İşlemin sonucu belirsiz olabilir; DURUMU YENİLEYİN, otomatik tekrar yapılmadı.' if payload is not None else ''
            raise DeviceError('Cihaza ulaşılamadı. Aynı Wi-Fi ağını ve IP adresini kontrol edin.' + suffix) from None
        if len(body) > limit:
            raise DeviceError('Yanıt boyut sınırını aştı.')
        if csv_mode:
            return body
        try:
            result = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise DeviceError('Cihaz geçerli JSON yanıtı vermedi; otomatik tekrar yapılmadı.') from None
        if not isinstance(result, dict) or (payload is not None and result.get('ok') is not True):
            raise DeviceError('Cihaz işlemi onaylamadı; durumu yenileyin.')
        return result

    def status(self):
        result = self._request('/api/status')
        if (not isinstance(result.get('employees'), list) or len(result['employees']) > 32 or
                not isinstance(result.get('csrf'), str) or not result['csrf']):
            raise DeviceError('Bu adres desteklenen bir PDKS cihazı değil.')
        return result

    def events(self):
        data = self._request('/api/events.csv', csv_mode=True)
        parse_events(data)
        return data

    @staticmethod
    def expected(state):
        if not state.get('clock_recovery') or not state.get('personnel_management'):
            raise DeviceError('Bu işlem için yeni yönetim firmware’i gerekli. Eski cihazda koruma aşılmaz.')
        payload = {'expected_sequence': state['sequence'], 'roster_version': state['roster_version']}
        version = state.get('api_version')
        if version is not None:
            if type(version) is not int:
                raise DeviceError('Cihaz sürüm bilgisi geçersiz; durumu yenileyin.')
            if version >= 4:
                revision = state.get('journal_revision')
                if type(revision) is not int or not 0 <= revision <= 0xFFFFFFFF:
                    raise DeviceError('Cihaz günlük sürümü eksik veya geçersiz; durumu yenileyin.')
                payload['journal_revision'] = revision
        return payload

    @staticmethod
    def _permanent_state(state):
        if not isinstance(state, dict):
            raise DeviceError('Cihaz durumu geçersiz; durumu yenileyin.')
        version = state.get('api_version')
        if type(version) is not int or version < 4:
            raise DeviceError('Kalıcı silme için yeni yönetim firmware’i gerekli; işlem yapılmadı.')
        if state.get('permanent_delete') is not True:
            raise DeviceError('Bu cihaz kalıcı silmeyi desteklemiyor; işlem yapılmadı.')
        revision = state.get('journal_revision')
        if type(revision) is not int or not 0 <= revision <= 0xFFFFFFFF:
            raise DeviceError('Cihaz günlük sürümü eksik veya geçersiz; durumu yenileyin.')
        if not isinstance(state.get('csrf'), str) or not state['csrf']:
            raise DeviceError('CSRF yok; cihaz durumunu yeniden okuyun.')

    def delete_employee(self, state, uid):
        uid = canonical_uid(uid)
        self._permanent_state(state)
        employees = state.get('employees')
        if not isinstance(employees, list):
            raise DeviceError('Cihaz personel listesi geçersiz; durumu yenileyin.')
        person = next((item for item in employees
                       if isinstance(item, dict) and item.get('uid') == uid), None)
        if person is None:
            raise DeviceError('Personel bulunamadı; listeyi yenileyin.')
        if person.get('inside') is not False:
            raise DeviceError('Açık oturumu olan personel silinemez; önce oturumu kapatın/kurtarın.')
        payload = dict(self.expected(state), uid=uid, confirm='PERSONELI KALICI SIL')
        return self._request('/api/employee/delete', payload, state['csrf'])

    def delete_session(self, state, sequence):
        self._permanent_state(state)
        current = state.get('sequence')
        if (type(current) is not int or type(sequence) is not int or not 1 <= sequence <= current):
            raise DeviceError('Silinecek oturum sırası geçersiz; listeyi yenileyin.')
        payload = dict(self.expected(state), target_sequence=sequence, confirm='OTURUMU KALICI SIL')
        return self._request('/api/attendance/delete', payload, state['csrf'])

    def save_employee(self, state, uid, name, card_uid, active=True, create=False):
        uid, card_uid, name = canonical_uid(uid), canonical_uid(card_uid), checked_name(name)
        if create and any(e['uid'] == uid or e.get('card_uid', e['uid']) == card_uid for e in state['employees']):
            raise DeviceError('Bu personel/kart zaten var; listeden seçip düzenleyin.')
        if not state.get('personnel_management'):
            old = next((e for e in state['employees'] if e['uid'] == uid), None)
            if not active or card_uid != uid or (old is None and not create):
                raise DeviceError('Kart değiştirme/pasife alma için yeni firmware gerekli.')
            return self._request('/api/employee', {'uid': uid, 'name': name}, state['csrf'])
        payload = dict(self.expected(state), uid=uid, name=name, card_uid=card_uid, active=bool(active), create=bool(create))
        return self._request('/api/employee/update', payload, state['csrf'])

    def set_time(self, state, text, recover=False):
        payload = {'epoch': encode_local_time(text)}
        if recover:
            payload.update(self.expected(state), confirm='IPTAL VE SAAT KURTAR')
        return self._request('/api/time/recover' if recover else '/api/time', payload, state['csrf'])

    def add_manual(self, state, uid, start_text, end_text):
        if not state.get('manual_attendance'):
            raise DeviceError('Manuel mesai için v1.1 veya daha yeni cihaz firmware’i gerekli.')
        if not state.get('clock_ok'):
            raise DeviceError('Önce cihazın saatini ayarlayın/kurtarın ve Yenile deyin.')
        uid=canonical_uid(uid)
        start,end=encode_local_time(start_text),encode_local_time(end_text)
        if end<=start:
            raise DeviceError('Bitiş, başlangıçtan sonra olmalı.')
        employee=next((e for e in state['employees'] if e['uid']==uid),None)
        if employee is None or employee['inside']:
            raise DeviceError('Personeli seçin; açık oturumu varsa önce kapatın/kurtarın.')
        payload=dict(self.expected(state),uid=uid,start_epoch=start,end_epoch=end,confirm='MANUEL MESAI EKLE')
        return self._request('/api/attendance/manual',payload,state['csrf'])

    def update_firmware(self, state, artifact, signed_path):
        """One authorised signed OTA transfer. No retry and no filesystem image support."""
        import hashlib
        from pathlib import Path
        import struct
        import uuid
        from windows_app.update_contract import (FIRMWARE_TARGET, UpdateError, firmware_envelope,
                                                 verify_signature)
        try:
            if (state.get('ota_update') is not True or state.get('api_version', 0) < 5 or
                    state.get('firmware_target') != FIRMWARE_TARGET or state.get('storage_ok') is not True):
                raise DeviceError('Önce OTA destekli özel USB kurulumu gerekli; cihaz/depo hazır değil.')
            if any(person['inside'] for person in state['employees']):
                raise DeviceError('Açık oturum varken firmware güncellenmez.')
            sequence = state.get('firmware_sequence')
            if type(sequence) is not int or artifact['sequence'] <= sequence:
                raise DeviceError('Bu firmware cihazdakinden yeni değil; eski sürüme dönülmedi.')
            verify_signature(firmware_envelope(artifact), base64.b64decode(artifact['meta_signature'], validate=True))
            path = Path(signed_path)
            if path.is_symlink() or path.stat().st_size != artifact['size']:
                raise DeviceError('Firmware dosyası boyutu uyuşmuyor.')
            data = path.read_bytes()
            if (not 260 < len(data) <= 1024*1024 or hashlib.sha256(data).hexdigest() != artifact['sha256'] or
                    data[0] != 0xE9 or struct.unpack('<I', data[-4:])[0] != 256):
                raise DeviceError('Firmware dosyası/özeti/imza biçimi geçersiz.')
            verify_signature(data[:-260], data[-260:-4])
        except UpdateError as exc:
            raise DeviceError(str(exc)) from None
        except (KeyError, TypeError, ValueError, OSError):
            raise DeviceError('Firmware güncelleme paketi doğrulanamadı; istek gönderilmedi.') from None
        payload = dict(self.expected(state), confirm='FIRMWARE GUNCELLE',
            **{key: artifact[key] for key in ('target', 'version', 'sequence', 'size', 'sha256', 'meta_signature')})
        prepared = self._request('/api/update/prepare', payload, state['csrf'])
        token = prepared.get('token')
        if not isinstance(token, str) or len(token) != 32 or any(char not in '0123456789abcdefABCDEF' for char in token):
            raise DeviceError('Cihaz güvenli OTA oturumu vermedi; dosya gönderilmedi.')
        boundary = 'PDKS' + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="firmware"; filename="firmware.signed.bin"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode('ascii') + data + f'\r\n--{boundary}--\r\n'.encode('ascii')
        request = Request(self.address+'/api/update/firmware', data=body, method='POST', headers={
            'Authorization': self._auth, 'X-CSRF-Token': state['csrf'], 'X-OTA-Token': token,
            'Content-Type': 'multipart/form-data; boundary='+boundary, 'Accept': 'application/json'})
        try:
            with self._opener.open(request, timeout=90) as response:
                raw = response.read(8193)
            if len(raw) > 8192:
                raise ValueError()
            result = json.loads(raw.decode('utf-8'))
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError()
            return result
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise DeviceError(f'HTTP {code}: firmware yüklemesi reddedildi. Otomatik tekrar yapılmadı.') from None
        except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
            raise DeviceError('Firmware yükleme sonucu belirsiz. Otomatik tekrar yapılmadı; cihaz yeniden başladıktan sonra Yenile ile sürümü kontrol edin.') from None
