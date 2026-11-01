"""Signed update discovery and download; nothing is trusted before RSA verification."""
import base64
from datetime import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request
import uuid

from windows_app.update_contract import (CURRENT_VERSION, FIRMWARE_NAME, FIRMWARE_TARGET,
                                         MANIFEST_NAME, REPOSITORY, SIGNATURE_NAME,
                                         ReleaseInfo, UpdateError, WINDOWS_NAME,
                                         firmware_envelope, verify_artifact, verify_signature,
                                         version_sequence)

API_LATEST = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
DOWNLOAD_PREFIX = f'https://github.com/{REPOSITORY}/releases/download'
MAX_RELEASE_JSON = 128 * 1024
MAX_MANIFEST = 256 * 1024
MAX_WINDOWS_SIZE = 80 * 1024 * 1024
MIN_FIRMWARE_SIZE = 261
MAX_FIRMWARE_SIZE = 1024 * 1024
MAX_REDIRECTS = 4
TIMEOUT = 15
CHUNK = 64 * 1024
USER_AGENT = f'PDKS-Yonetim-Updater/{CURRENT_VERSION}'
ALLOWED_HOSTS = frozenset({'github.com', 'api.github.com',
                           'release-assets.githubusercontent.com',
                           'objects.githubusercontent.com'})
TOP_KEYS = frozenset({'schema', 'repository', 'version', 'sequence', 'published_at',
                      'notes', 'windows', 'firmware'})
WINDOWS_KEYS = frozenset({'name', 'size', 'sha256', 'url', 'target'})
FIRMWARE_KEYS = frozenset({'name', 'size', 'sha256', 'url', 'version', 'sequence',
                           'target', 'min_api', 'meta_signature'})
HEX64 = re.compile('[0-9a-f]{64}')
STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')
_key_override = None


def _publisher_key():
    """Bundled publisher key, unless a test injected verified key material."""
    return _key_override


def _safe_url(url):
    if not isinstance(url, str) or not 0 < len(url) <= 2048:
        raise UpdateError('Güncelleme adresi geçersiz.')
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or '').lower()
        port = parts.port
    except ValueError:
        raise UpdateError('Güncelleme adresi güvenli değil.') from None
    path = urllib.parse.unquote(parts.path)
    if (parts.scheme != 'https' or parts.username or parts.password or parts.fragment
            or port not in (None, 443) or host not in ALLOWED_HOSTS
            or not path.startswith('/') or '..' in path.split('/')):
        raise UpdateError('Güncelleme adresi güvenli değil.')
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_repetitions = 3
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    _safe_url(url)
    opener = urllib.request.build_opener(_SafeRedirect())
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                   'User-Agent': USER_AGENT})
    return opener.open(request, timeout=TIMEOUT)


def _read_bounded(response, limit):
    chunks, total = [], 0
    while True:
        chunk = response.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UpdateError('Güncelleme yanıtı izin verilenden büyük.')
        chunks.append(chunk)
    return b''.join(chunks)


def _fetch(url, limit):
    try:
        with _open(url) as response:
            if getattr(response, 'status', 200) != 200:
                raise UpdateError('Güncelleme sunucusu beklenmeyen yanıt verdi.')
            headers = getattr(response, 'headers', None)
            length = headers.get('Content-Length') if headers is not None else None
            if length is not None:
                try:
                    declared = int(length)
                except (TypeError, ValueError):
                    raise UpdateError('Güncelleme yanıtı geçersiz.') from None
                if declared < 0 or declared > limit:
                    raise UpdateError('Güncelleme yanıtı izin verilenden büyük.')
            return _read_bounded(response, limit)
    except UpdateError:
        raise
    except (OSError, http.client.HTTPException):
        raise UpdateError('Güncelleme sunucusuna ulaşılamadı.') from None


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise UpdateError('Güncelleme belgesinde yinelenen alan var.')
        data[key] = value
    return data


def _constant(name):
    raise UpdateError('Güncelleme belgesi geçersiz.')


def _loads(raw, limit):
    if not isinstance(raw, (bytes, bytearray)) or not raw or len(raw) > limit:
        raise UpdateError('Güncelleme belgesi geçersiz.')
    try:
        data = json.loads(bytes(raw).decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=_constant)
    except UpdateError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise UpdateError('Güncelleme belgesi okunamadı.') from None
    if not isinstance(data, dict):
        raise UpdateError('Güncelleme belgesi geçersiz.')
    return data


def _text(value, size=2000):
    if (not isinstance(value, str) or not 0 < len(value) <= size
            or any(ord(char) < 32 and char not in '\n\r\t' for char in value)):
        raise UpdateError('Güncelleme metin alanı geçersiz.')
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise UpdateError('Güncelleme sayısal alanı geçersiz.')
    return value


def _digest(value):
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise UpdateError('Güncelleme SHA256 özeti geçersiz.')
    return value


def _fields(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        raise UpdateError('Güncelleme alanları beklenen küme değil.')
    return value


def _artifact(version, item, keys, name, low, high):
    _fields(item, keys)
    if item['name'] != name:
        raise UpdateError('Güncelleme dosya adı beklenenle uyuşmuyor.')
    _integer(item['size'], low, high)
    _digest(item['sha256'])
    if item['url'] != f'{DOWNLOAD_PREFIX}/v{version}/{name}':
        raise UpdateError('Güncelleme dosya adresi beklenenle uyuşmuyor.')
    return item


def _validate(manifest, public_key):
    _fields(manifest, TOP_KEYS)
    if type(manifest['schema']) is not int or manifest['schema'] != 1:
        raise UpdateError('Güncelleme şeması desteklenmiyor.')
    if manifest['repository'] != REPOSITORY:
        raise UpdateError('Güncelleme deposu beklenenle uyuşmuyor.')
    version = manifest['version']
    sequence = version_sequence(version)
    if type(manifest['sequence']) is not int or manifest['sequence'] != sequence:
        raise UpdateError('Güncelleme sırası sürümle uyuşmuyor.')
    stamp = manifest['published_at']
    if not isinstance(stamp, str) or not STAMP.fullmatch(stamp):
        raise UpdateError('Güncelleme yayın zamanı geçersiz.')
    try:
        datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise UpdateError('Güncelleme yayın zamanı geçersiz.') from None
    _text(manifest['notes'], 8000)
    windows = _artifact(version, manifest['windows'], WINDOWS_KEYS, WINDOWS_NAME,
                        1, MAX_WINDOWS_SIZE)
    if windows['target'] != 'windows-x64':
        raise UpdateError('Windows hedefi beklenenle uyuşmuyor.')
    firmware = _artifact(version, manifest['firmware'], FIRMWARE_KEYS, FIRMWARE_NAME,
                         MIN_FIRMWARE_SIZE, MAX_FIRMWARE_SIZE)
    if firmware['target'] != FIRMWARE_TARGET:
        raise UpdateError('Firmware hedefi beklenenle uyuşmuyor.')
    if firmware['version'] != version or firmware['sequence'] != sequence:
        raise UpdateError('Firmware sürümü katalogla uyuşmuyor.')
    if type(firmware['min_api']) is not int or firmware['min_api'] != 5:
        raise UpdateError('Firmware API sürümü desteklenmiyor.')
    signature = firmware['meta_signature']
    if not isinstance(signature, str):
        raise UpdateError('Firmware üst imzası geçersiz.')
    try:
        signature = base64.b64decode(signature.encode('ascii'), validate=True)
    except (UnicodeEncodeError, ValueError):
        raise UpdateError('Firmware üst imzası geçersiz.') from None
    if len(signature) != 256:
        raise UpdateError('Firmware üst imzası geçersiz.')
    verify_signature(firmware_envelope(firmware), signature, public_key)


def verify_manifest(raw, signature, public_key=None):
    if not isinstance(raw, (bytes, bytearray)) or not isinstance(signature, (bytes, bytearray)):
        raise UpdateError('Güncelleme imzası doğrulanamadı.')
    verify_signature(bytes(raw), bytes(signature), public_key)
    manifest = _loads(raw, MAX_MANIFEST)
    _validate(manifest, public_key)
    return ReleaseInfo(manifest=manifest, raw=bytes(raw), signature=bytes(signature))


def _release_asset_url(version, name):
    return f'{DOWNLOAD_PREFIX}/v{version}/{name}'


def _release_assets(data, version):
    assets = data.get('assets')
    if not isinstance(assets, list):
        raise UpdateError('Güncelleme varlık listesi geçersiz.')
    found = {MANIFEST_NAME: [], SIGNATURE_NAME: []}
    for asset in assets:
        if not isinstance(asset, dict):
            raise UpdateError('Güncelleme varlığı geçersiz.')
        name = asset.get('name')
        if name in found:
            found[name].append(asset.get('browser_download_url'))
    urls = {}
    for name, entries in found.items():
        expected = _release_asset_url(version, name)
        if len(entries) != 1 or entries[0] != expected:
            raise UpdateError('Yayın varlıkları beklenenle uyuşmuyor.')
        urls[name] = expected
    return urls


def check_release():
    data = _loads(_fetch(API_LATEST, MAX_RELEASE_JSON), MAX_RELEASE_JSON)
    if data.get('draft') is not False or data.get('prerelease') is not False:
        raise UpdateError('Yayın taslak veya ön sürüm; güncelleme yok sayıldı.')
    tag = data.get('tag_name')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise UpdateError('Yayın etiketi geçersiz.')
    version = tag[1:]
    version_sequence(version)
    urls = _release_assets(data, version)
    raw = _fetch(urls[MANIFEST_NAME], MAX_MANIFEST)
    signature = _fetch(urls[SIGNATURE_NAME], 256)
    if len(signature) != 256:
        raise UpdateError('Yayın imzası geçersiz.')
    release = verify_manifest(raw, signature, _publisher_key())
    if release.manifest['version'] != version:
        raise UpdateError('Yayın etiketi imzalı sürümle uyuşmuyor.')
    return release


def _safe_directory(directory):
    if directory is None:
        base = os.environ.get('LOCALAPPDATA') or str(Path.home()/'AppData'/'Local')
        directory = Path(base)/'PDKS_Yonetim'/'updates'/uuid.uuid4().hex
    else:
        directory = Path(directory)
    if not directory.is_absolute() or '..' in directory.parts:
        raise UpdateError('Güncelleme klasörü güvenli değil.')
    if directory.drive.startswith('\\\\'):
        raise UpdateError('Güncelleme klasörü güvenli değil.')
    current = directory
    while current != current.parent:
        if current.is_symlink():
            raise UpdateError('Güncelleme klasörü güvenli değil.')
        current = current.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise UpdateError('Güncelleme klasörü oluşturulamadı.') from None
    if directory.is_symlink() or not directory.is_dir():
        raise UpdateError('Güncelleme klasörü güvenli değil.')
    return directory


def _discard(path):
    try:
        if path.exists() and not path.is_dir():
            path.unlink()
    except OSError:
        pass


def download_artifact(release, kind, directory=None, progress=None):
    if not isinstance(release, ReleaseInfo):
        raise UpdateError('Güncelleme bilgisi geçersiz.')
    if kind not in ('windows', 'firmware'):
        raise UpdateError('Güncelleme dosya türü geçersiz.')
    verified = verify_manifest(release.raw, release.signature, _publisher_key())
    artifact = verified.manifest['windows' if kind == 'windows' else 'firmware']
    folder = _safe_directory(directory)
    target = folder/artifact['name']
    if target.is_symlink() or target.exists():
        if target.is_symlink() or not target.is_file():
            raise UpdateError('Güncelleme hedefi güvenli değil.')
        try:
            return verify_artifact(target, artifact)
        except UpdateError:
            raise UpdateError('Klasörde farklı bir güncelleme dosyası var; '
                              'üzerine yazılmadı.') from None
    part = folder/f'.{artifact["name"]}.{uuid.uuid4().hex}.part'
    if part.exists() or part.is_symlink():
        raise UpdateError('Güncelleme geçici dosyası güvenli değil.')
    size, expected = artifact['size'], artifact['sha256']
    digest, done, renamed = hashlib.sha256(), 0, False
    try:
        with _open(artifact['url']) as response, part.open('xb') as file:
            if getattr(response, 'status', 200) != 200:
                raise UpdateError('Güncelleme dosyası sunucudan alınamadı.')
            headers = getattr(response, 'headers', None)
            length = headers.get('Content-Length') if headers is not None else None
            if length is not None:
                try:
                    declared = int(length)
                except (TypeError, ValueError):
                    raise UpdateError('Güncelleme dosyası boyutu geçersiz.') from None
                if declared != size:
                    raise UpdateError('Güncelleme dosyası boyutu uyuşmuyor.')
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                done += len(chunk)
                if done > size:
                    raise UpdateError('Güncelleme dosyası beklenenden büyük.')
                digest.update(chunk)
                file.write(chunk)
                if progress is not None:
                    progress(done, size)
            file.flush()
            os.fsync(file.fileno())
        if done != size:
            raise UpdateError('Güncelleme dosyası eksik indirildi.')
        if digest.hexdigest() != expected:
            raise UpdateError('Güncelleme dosyasının SHA256 özeti uyuşmuyor.')
        os.replace(part, target)
        renamed = True
    except (OSError, http.client.HTTPException):
        raise UpdateError('Güncelleme dosyası indirilemedi.') from None
    finally:
        if not renamed:
            _discard(part)
    return target
