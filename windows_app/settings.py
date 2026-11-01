"""One per-user connection profile; secrets live only in Windows Credential Manager."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile

from windows_app.client import DeviceError, normalize_address


class SettingsError(Exception):
    pass


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(wintypes.BYTE)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


class WindowsVault:
    def __init__(self, target='PDKS_Yonetim/Connection/v1'):
        self.target = target
        self.api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        pointer = ctypes.POINTER(_Credential)
        self.api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        for name in ('CredWriteW', 'CredReadW', 'CredDeleteW'):
            getattr(self.api, name).restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None

    @staticmethod
    def _error():
        # Never include input, blobs, or arbitrary system exception text in UI/logs.
        return SettingsError('Windows şifre kasasına erişilemedi (kod %d).' % ctypes.get_last_error())

    def read(self, binding):
        pointer = ctypes.POINTER(_Credential)()
        if not self.api.CredReadW(self.target, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
                return ''
            raise self._error()
        try:
            item = pointer.contents
            if item.UserName != binding:
                return ''
            if item.CredentialBlobSize > 2560 or item.CredentialBlobSize % 2:
                raise SettingsError('Kayıtlı şifre biçimi geçersiz; şifreyi yeniden girin.')
            return ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode('utf-16-le')
        finally:
            self.api.CredFree(pointer)

    def write(self, binding, password):
        data = password.encode('utf-16-le')
        if len(data) > 2560:
            raise SettingsError('Şifre Windows kasası için çok uzun.')
        blob = (wintypes.BYTE * len(data)).from_buffer_copy(data)
        item = _Credential()
        item.Type, item.TargetName, item.UserName = 1, self.target, binding
        item.Persist = 2  # same Windows user, same computer, survives logoff
        item.CredentialBlobSize, item.CredentialBlob = len(data), blob
        try:
            if not self.api.CredWriteW(ctypes.byref(item), 0):
                raise self._error()
        finally:
            ctypes.memset(blob, 0, len(data))

    def delete(self):
        if not self.api.CredDeleteW(self.target, 1, 0) and ctypes.get_last_error() != 1168:
            raise self._error()


class ConnectionSettings:
    def __init__(self, path=None, vault=None):
        self.path = Path(path) if path is not None else Path(os.environ['LOCALAPPDATA'])/'PDKS_Yonetim'/'connection.json'
        self.vault = vault if vault is not None else WindowsVault()

    @staticmethod
    def _profile(address, username):
        if not isinstance(address, str) or len(address) > 2048 or not isinstance(username, str):
            raise SettingsError('Bağlantı ayarları geçersiz.')
        address = normalize_address(address)
        if not username or len(username) > 256 or ':' in username or any(ord(c) < 32 for c in username):
            raise SettingsError('Kullanıcı adı geçersiz.')
        binding = hashlib.sha256(json.dumps([address, username], ensure_ascii=True).encode('ascii')).hexdigest()
        return address, binding

    def load(self):
        defaults = dict(address='192.168.4.1', username='admin', remember=True, password='')
        try:
            try:
                with self.path.open('r', encoding='utf-8') as source:
                    raw = source.read(16385)
            except FileNotFoundError:
                return defaults
            if len(raw) > 16384:
                raise ValueError()
            data = json.loads(raw)
            if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1 or
                    type(data.get('remember')) is not bool):
                raise ValueError()
            address, binding = self._profile(data.get('address'), data.get('username'))
            password = self.vault.read(binding) if data['remember'] else ''
            return dict(address=address, username=data['username'], remember=data['remember'], password=password)
        except SettingsError:
            raise
        except (OSError, ValueError, TypeError, DeviceError):
            raise SettingsError('Kayıtlı bağlantı ayarları okunamadı. Bilgileri yeniden girin.') from None

    def forget_password(self):
        try:
            self.vault.delete()
        except SettingsError:
            raise
        except OSError:
            raise SettingsError('Kayıtlı şifre silinemedi.') from None

    def save(self, address, username, password, remember):
        temp = None
        try:
            address, binding = self._profile(address, username)
            if (type(remember) is not bool or not isinstance(password, str) or
                    any(ord(c) < 32 for c in password) or len(password.encode('utf-16-le')) > 2560):
                raise SettingsError('Şifre veya hatırlama seçimi geçersiz.')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Prepare non-secret file before touching the vault; replace atomically last.
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='connection-', suffix='.tmp', delete=False) as out:
                temp = Path(out.name)
                json.dump(dict(version=1, address=address, username=username, remember=remember), out)
                out.flush()
                os.fsync(out.fileno())
            if remember and password:
                self.vault.write(binding, password)
            else:
                self.vault.delete()
            os.replace(temp, self.path)
        except SettingsError:
            raise
        except (OSError, ValueError, TypeError, DeviceError):
            raise SettingsError('Bağlantı bilgileri kaydedilemedi; düz metin şifre kaydı yapılmadı.') from None
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass  # contains only non-secret metadata; do not mask the save error
