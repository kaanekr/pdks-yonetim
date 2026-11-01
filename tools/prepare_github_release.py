"""Prepare allowlisted public sources and signed assets. Never publish or contact hardware."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windows_app.update_contract import (CURRENT_VERSION, CURRENT_SEQUENCE, REPOSITORY,
    FIRMWARE_TARGET, WINDOWS_NAME, FIRMWARE_NAME, MANIFEST_NAME, SIGNATURE_NAME,
    firmware_envelope, verify_signature, verify_artifact)
from windows_app.updates import verify_manifest
from release_signing import sign

DOCS = ('PUBLIC_README.md', 'GUNCELLEME_V1_5.md', 'KULLANIM_PUBLIC.md', 'YAYINCI.md')
TOOLS = ('check.ps1', 'release_signing.py', 'prepare_github_release.py', 'publish_public.py',
         'test_core.py', 'test_card_gate.py', 'test_network.py', 'test_journal.py',
         'test_management.py', 'test_manual_attendance.py', 'test_named_core.py',
         'test_named_storage.py', 'test_named_sd.py', 'test_delete_api.py',
         'test_device_auth.py', 'test_ota_policy.py', 'test_ota_server.py',
         'test_buzzer.py', 'test_feedback.py', 'test_attendance_reset.py', 'test_console.py',
         'test_soft_sd_spi.py', 'test_panel.py', 'panel_fixture.py', 'test_display.py')
NOTES = ('İmzalı GitHub güncellemeleri: Windows programı ve aynı yerel ağdaki ESP8266 firmware’i. '
         'Windows 1.5.3, başarılı bir güncellemeden sonra eski EXE’yi siler; eski sürüm geride bırakılmaz. '
         'Eski EXE yalnız yeni sürümün sağlık kontrolü sırasında tutulur; başarısızsa geri alınır. '
         '1.5.3 firmware yalnız sürüm hizalamasıdır; işlevsel değişiklik yoktur ve cihaz 1.5.2 ile uyumludur. '
         '1.5.2, 1.5.1 firmware OTA hazırlığındaki BearSSL ikinci yığın hatasını düzeltmişti. '
         'İlk OTA kurulumu bir kez özel USB kurulumu gerektirir. '
         'Kendiliğinden indirme/yükleme ve POST tekrarı yoktur. '
         'İsimli kayıtlar, kalıcı silme, puantaj ve bağlantı/arşiv özellikleri korunur.')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_literals():
    """Read configured local credentials solely for exclusion; never emit their values."""
    result = []
    for file in (ROOT/'include/Config.h', ROOT/'include/Secrets.h'):
        if not file.exists():
            continue
        text = file.read_text(encoding='utf-8')
        for macro in ('PDKS_ADMIN_PASSWORD', 'PDKS_AP_PASSWORD'):
            matches = re.findall(r'^\s*#\s*define\s+'+macro+r'\s+"([^"\r\n]+)"', text, re.M)
            result.extend(value.encode('utf-8') for value in matches if len(value) >= 8 and
                          value not in ('TEST-ONLY-AP-CHANGE', 'TEST-ONLY-ADMIN-CHANGE'))
    return set(result)


def audit(files):
    patterns = [rb'-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----',
                rb'gh[pousr]_[A-Za-z0-9]{20,}', rb'github_pat_[A-Za-z0-9_]{20,}']
    literals = private_literals()
    for name, data in files.items():
        if any(literal in data for literal in literals) or any(re.search(p, data) for p in patterns):
            raise RuntimeError('Private material detected; publication refused: '+name)
        if any(part.lower() in {'secrets.h', 'device-auth.json', 'connection.json',
                               'workforce.sqlite3'} for part in Path(name).parts):
            raise RuntimeError('Private file detected: '+name)


def source_files():
    files = {}
    for folder, pattern in (('src', '*.cpp'), ('include', '*.h'), ('windows_app', '*.py')):
        for path in sorted((ROOT/folder).glob(pattern)):
            if path.name in ('Config.h', 'Secrets.h', 'ResetAttendance.cpp', 'ResetAttendance.h'):
                continue
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for path in sorted((ROOT/'test').rglob('*')):
        if path.is_file() and path.suffix in ('.py', '.cpp', '.h') and '__pycache__' not in path.parts:
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in TOOLS:
        files['tools/'+name] = (ROOT/'tools'/name).read_bytes()
    for name in ('windows_app_launcher.py', 'requirements-dev.txt',
                 'windows_app/requirements-build.txt', 'windows_app/DEMO_AC.bat', 'data/index.html'):
        files[name] = (ROOT/name).read_bytes()
    for name in ('NotoSans-Regular.ttf', 'OFL.txt', 'README.md', 'update_public_key.pem'):
        files['windows_app/assets/'+name] = (ROOT/'windows_app/assets'/name).read_bytes()
    for name in DOCS:
        files['docs/'+name] = (ROOT/'docs'/name).read_bytes()
    for path in sorted((ROOT/'docs/THIRD_PARTY_LICENSES').rglob('*')):
        if path.is_file() and (path.name == 'LICENSE' or path.suffix.lower() in ('.txt', '.md', '.terms', '.bsd', '.apache')):
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    files['README.md'] = (ROOT/'docs/PUBLIC_README.md').read_bytes()
    # The original Config is private input, never an upload candidate. Replace ALL credential defaults.
    config = (ROOT/'include/Config.h').read_text(encoding='utf-8')
    defaults = {'PDKS_AP_SSID': 'PDKS-Setup', 'PDKS_AP_PASSWORD': 'TEST-ONLY-AP-CHANGE',
                'PDKS_ADMIN_USER': 'admin', 'PDKS_ADMIN_PASSWORD': 'TEST-ONLY-ADMIN-CHANGE'}
    for macro, value in defaults.items():
        config, count = re.subn(r'(^\s*#define\s+'+macro+r'\s+)"[^"\r\n]*"',
                               lambda m: m[1]+'"'+value+'"', config, flags=re.M)
        if count != 1:
            raise RuntimeError('Cannot safely sanitize Config; manual review required.')
    marker = '#ifndef PDKS_AP_SSID'
    config = config.replace(marker,
        '#if defined(ARDUINO_ARCH_ESP8266) && !PDKS_PUBLIC_RELEASE && (!defined(PDKS_LOCAL_CREDENTIALS_CONFIGURED) || !defined(PDKS_ADMIN_PASSWORD) || !defined(PDKS_AP_PASSWORD) || !defined(PDKS_AP_SSID) || !defined(PDKS_ADMIN_USER))\n'
        '#error "Private bootstrap requires local Secrets.h and PDKS_LOCAL_CREDENTIALS_CONFIGURED=1"\n'
        '#endif\n\n'+marker, 1)
    files['include/Config.h'] = config.encode('utf-8')
    ini = (ROOT/'platformio.ini').read_text(encoding='utf-8')
    ini = ini.split('; EXPLICITLY DESTRUCTIVE maintenance image')[0]
    ini = ini.replace('default_envs = nodemcuv2\n', 'default_envs = nodemcuv2_sd_soft_tx_public\n', 1)
    files['platformio.ini'] = ini.encode('utf-8')
    # Keep reset implementation for its isolated native tests, not a deployable maintenance target.
    for name in ('src/ResetAttendance.cpp', 'include/ResetAttendance.h'):
        if (ROOT/name).exists():
            files[name] = (ROOT/name).read_bytes()
    files['.gitignore'] = ('.pio/\n.platformio/\n.venv/\n__pycache__/\nbuild/\ndist/\nrelease/\n'
        '*.spec\ninclude/Secrets.h\n*device-auth*json\nwifi.json\nemployees*.json\nconnection.json\n*.sqlite3*\n*.dpapi\n'
        '*.bin\n*.exe\n*.zip\n*.csv\n*.xlsx\n*.pdf\n*.key\n*.p12\n*.pfx\n*.pem\n'
        '!windows_app/assets/update_public_key.pem\n*private*bootstrap*\n').encode('ascii')
    audit(files)
    return files


def stage_sources(destination):
    destination = Path(destination)
    if destination.exists():
        raise RuntimeError('Refusing to overwrite an existing staging directory.')
    files = source_files()
    destination.mkdir(parents=True)
    for name, data in files.items():
        path = destination/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print('Sanitized public files:', len(files))
    return files


def prepare_assets(exe, firmware, destination):
    destination = Path(destination)
    if destination.exists():
        raise RuntimeError('Refusing to overwrite an existing release directory.')
    raw = Path(firmware).read_bytes()
    marker = f'PDKS-FW:{CURRENT_VERSION}:{CURRENT_SEQUENCE}UL:{FIRMWARE_TARGET}'.encode('ascii')
    if not raw or raw[0] != 0xE9 or marker not in raw:
        raise RuntimeError('Public firmware version/target marker is missing.')
    audit({'public-firmware.bin': raw})
    signed = raw+sign(raw)+struct.pack('<I', 256)
    verify_signature(signed[:-260], signed[-260:-4])
    windows = Path(exe).read_bytes()
    if not windows.startswith(b'MZ') or len(windows) > 80*1024*1024:
        raise RuntimeError('Invalid Windows executable.')
    prefix = f'https://github.com/{REPOSITORY}/releases/download/v{CURRENT_VERSION}/'
    artifact = lambda name, data, target: dict(name=name, size=len(data), sha256=sha(data), url=prefix+name, target=target)
    win = artifact(WINDOWS_NAME, windows, 'windows-x64')
    fw = artifact(FIRMWARE_NAME, signed, FIRMWARE_TARGET)
    fw.update(version=CURRENT_VERSION, sequence=CURRENT_SEQUENCE, min_api=5)
    fw['meta_signature'] = base64.b64encode(sign(firmware_envelope(fw))).decode('ascii')
    manifest = dict(schema=1, repository=REPOSITORY, version=CURRENT_VERSION, sequence=CURRENT_SEQUENCE,
                    published_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), notes=NOTES,
                    windows=win, firmware=fw)
    body = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')+b'\n'
    signature = sign(body)
    verify_manifest(body, signature)
    files = {WINDOWS_NAME: windows, FIRMWARE_NAME: signed, MANIFEST_NAME: body, SIGNATURE_NAME: signature}
    audit(files)
    destination.mkdir(parents=True)
    for name, data in files.items():
        (destination/name).write_bytes(data)
    for kind in ('windows', 'firmware'):
        verify_artifact(destination/manifest[kind]['name'], manifest[kind])
    print('Verified signed assets:', len(files), 'version', CURRENT_VERSION)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    stage = commands.add_parser('stage')
    stage.add_argument('--out', type=Path, required=True)
    assets = commands.add_parser('assets')
    assets.add_argument('--exe', type=Path, required=True)
    assets.add_argument('--firmware', type=Path, required=True)
    assets.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'stage':
        stage_sources(args.out)
    else:
        prepare_assets(args.exe, args.firmware, args.out)
