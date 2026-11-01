"""Signed in-place Windows self-update without administrator rights.

The running frozen EXE starts a detached copy of itself as
``PDKS_UpdateHelper.exe``; the copy waits for the app to exit, replaces the
target EXE atomically and restores it when the new build fails its isolated
health check. This module never closes the application. Signed manifest::

    {"version": "1.6.0", "sequence": 10600, "windows":
      {"target": "windows-x64", "name": "PDKS_Yonetim.exe",
       "size": 123, "sha256": "<64 hex>"}}
"""
import ctypes, hashlib, json, os, re, shutil, subprocess, sys, time, uuid
from ctypes import wintypes
from pathlib import Path

from windows_app.update_contract import (CURRENT_SEQUENCE, CURRENT_VERSION, MANIFEST_NAME,
    SIGNATURE_NAME, WINDOWS_NAME, ReleaseInfo, UpdateError, verify_artifact, version_sequence)

WINDOWS_TARGET, HELPER_NAME = 'windows-x64', 'PDKS_UpdateHelper.exe'
DESCRIPTOR_NAME, HEALTH_NAME, LOG_NAME = 'descriptor.json', 'health.json', 'update.log'
BACKUP_PREFIX = 'PDKS_Yonetim.previous.'
WAIT_SECONDS, HEALTH_SECONDS = 60, 30
OK, ROLLED_BACK, FAILED = 0, 1, 2


def verify_manifest(raw, signature, public_key=None):
    """Delegate to the shared protocol verifier; tests replace this name."""
    try:
        from windows_app.updates import verify_manifest as shared
    except ImportError:
        raise UpdateError('Güncelleme doğrulayıcısı bulunamadı.') from None
    return shared(raw, signature, public_key)


def _sha256(path):
    with Path(path).open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def _frozen_exe():
    """Running frozen EXE; tests replace this for fixture runs."""
    exe = Path(sys.executable)
    if not getattr(sys, 'frozen', False) or exe.suffix.lower() != '.exe' or not exe.is_file():
        raise UpdateError('Güncelleme yalnızca paketlenmiş uygulama EXE ile çalışır.')
    return exe.resolve(strict=True)


def _release_artifact(manifest):
    manifest = manifest if isinstance(manifest, dict) else {}
    version, sequence = manifest.get('version'), manifest.get('sequence')
    if type(sequence) is not int or version_sequence(version) != sequence \
            or sequence <= max(CURRENT_SEQUENCE, version_sequence(CURRENT_VERSION)):
        raise UpdateError('Bu güncelleme sürümü geçersiz veya yüklü sürümden yeni değil.')
    artifact = manifest.get('windows')
    if not isinstance(artifact, dict) or artifact.get('target') != WINDOWS_TARGET or artifact.get('name') != WINDOWS_NAME:
        raise UpdateError('Windows güncelleme paketi bildirimde bulunamadı.')
    size, digest = artifact.get('size'), artifact.get('sha256')
    if type(size) is not int or size <= 0 or not re.fullmatch('[0-9a-f]{64}', str(digest)):
        raise UpdateError('Windows güncelleme boyutu/özeti geçersiz.')
    return artifact


def _verified_artifact(path, artifact):
    verify_artifact(path, artifact)
    with Path(path).open('rb') as file:
        if file.read(2) != b'MZ':
            raise UpdateError('Güncelleme dosyası Windows EXE değil.')
    return Path(path)


def _staging_root():
    base = os.environ.get('LOCALAPPDATA')
    if not base or not Path(base).is_absolute():
        raise UpdateError('Yerel uygulama verisi klasörü bulunamadı.')
    return Path(base)/'PDKS_Yonetim'/'updates'


def _discard(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _copy_verified(source, destination, digest):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise UpdateError('Güncelleme kopya hedefi zaten var.')
    try:
        shutil.copyfile(source, destination)
    except OSError:
        raise UpdateError('Güncelleme dosyası kopyalanamadı; izinleri kontrol edin.') from None
    if _sha256(destination) != digest:
        _discard(destination)
        raise UpdateError('Güncelleme kopyası doğrulanamadı; hiçbir şey değiştirilmedi.')
    return destination


def _replace(source, destination):
    os.replace(source, destination)


def _replace_unlocked(source, target, expected_sha):
    # A one-file bootloader parent can retain its image lock briefly after the
    # recorded Python child exits. Retry only Windows sharing/access locks;
    # never kill processes, change permissions or replace a changed target.
    deadline = time.monotonic()+15
    while True:
        try:
            _replace(source, target)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            if target.is_symlink() or _sha256(target) != expected_sha:
                raise UpdateError('Uygulama beklerken değişti; güncelleme yapılmadı.') from None
            time.sleep(0.2)


def _write_json(path, data):
    temp = Path(path).with_name(Path(path).name + '.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    _replace(temp, path)


def _log(job, message):
    try:
        with (Path(job)/LOG_NAME).open('a', encoding='utf-8') as file:
            file.write(message + '\n')
    except OSError:
        pass


def _flags():
    if os.name != 'nt':
        return 0
    return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def _spawn(argv):
    return subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=_flags(), cwd=str(Path(argv[0]).parent)).pid


def _start(target):
    try:
        return bool(_spawn([str(target)]))
    except OSError:
        return False


def _run_health(target, report, version, timeout=HEALTH_SECONDS):
    report = Path(report)
    try:
        if report.exists() or report.is_symlink():
            report.unlink()
        process = subprocess.run([str(target), '--update-health-check', str(report)], shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False, creationflags=_flags(), cwd=str(report.parent))
        data = json.loads(report.read_text(encoding='utf-8'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return process.returncode == 0 and isinstance(data, dict) and data.get('ok') is True and data.get('version') == version


def _wait_for_exit(pid, target, timeout=WAIT_SECONDS):
    """Wait for the recorded app PID; it must still be the target EXE."""
    if os.name != 'nt':
        raise UpdateError('Güncelleme yalnızca Windows üzerinde uygulanır.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype, kernel.OpenProcess.argtypes = wintypes.HANDLE, [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, int(pid))
    if not handle:
        if ctypes.get_last_error() == 87:
            return
        raise UpdateError('Bekleyen uygulama doğrulanamadı.')
    try:
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        size, buffer = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        image = kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
        if not image or os.path.normcase(os.path.abspath(buffer.value)) \
                != os.path.normcase(os.path.abspath(target)):
            raise UpdateError('Bekleyen süreç güncelleme hedefi değil.')
        if kernel.WaitForSingleObject(handle, int(timeout*1000)) != 0:
            raise UpdateError('Uygulama kapanmadı; güncelleme yapılmadı.')
    finally:
        kernel.CloseHandle(handle)


def _stage_path(job, value, name, exists=True):
    path = Path(value) if isinstance(value, str) else None
    if path is None or not path.is_absolute() or path.name != name:
        raise UpdateError('Güncelleme dosyası beklenen klasörde değil.')
    try:
        same = path.parent.resolve(strict=True) == Path(job).resolve(strict=True)
    except OSError:
        same = False
    if not same or path.is_symlink() or (exists and not path.is_file()):
        raise UpdateError('Güncelleme dosyası beklenen klasörde değil.')
    return path


def _read_descriptor(path):
    keys = ('target', 'version', 'manifestpath', 'signaturepath', 'artifactpath', 'logfilepath')
    if path.name != DESCRIPTOR_NAME or path.is_symlink() or not path.is_file():
        raise UpdateError('Güncelleme tanım dosyası geçersiz.')
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise UpdateError('Güncelleme tanım dosyası okunamadı.') from None
    if not isinstance(data, dict) or data.get('schema') != 1 \
            or not all(isinstance(data.get(key), str) for key in keys):
        raise UpdateError('Güncelleme tanımı eksik veya desteklenmiyor.')
    if type(data.get('pid')) is not int or data['pid'] <= 0 \
            or type(data.get('sequence')) is not int \
            or not re.fullmatch('[0-9a-f]{64}', str(data.get('old_sha256'))):
        raise UpdateError('Güncelleme süreç/sıra/özet bilgisi geçersiz.')
    return data


def _check_target(target, job):
    if not target.is_absolute() or target.suffix.lower() != '.exe' or target.is_symlink():
        raise UpdateError('Güncelleme hedefi geçersiz.')
    try:
        resolved, staged = target.resolve(strict=True), Path(job).resolve(strict=True)
    except OSError:
        raise UpdateError('Güncelleme hedefi bulunamadı.') from None
    if not resolved.is_file() or resolved.is_relative_to(staged) \
            or resolved.name.casefold() == HELPER_NAME.casefold():
        raise UpdateError('Güncelleme hedefi güvenli değil.')
    return resolved


def _restore(target, backup, digest):
    target, backup = Path(target), Path(backup)
    if target.is_file() and _sha256(target) == digest:
        return True
    temp = target.parent/(BACKUP_PREFIX + uuid.uuid4().hex + '.tmp')
    try:
        _copy_verified(backup, temp, digest)
        _replace(temp, target)
        return True
    except (OSError, UpdateError):
        return False
    finally:
        _discard(temp)


def prepare_self_update(release: ReleaseInfo, artifact_path) -> Path:
    """Verify a release, stage it and start the detached helper.

    Returns the descriptor path only after the helper started. Raises
    UpdateError without touching the installed EXE on any failure; the caller
    must not close the application unless this returns.
    """
    exe = _frozen_exe()
    raw, signature = release.raw, release.signature
    if not isinstance(raw, bytes) or not isinstance(signature, bytes):
        raise UpdateError('Güncelleme imza verisi geçersiz.')
    manifest = verify_manifest(raw, signature).manifest
    artifact = _release_artifact(manifest)
    artifact_path = Path(artifact_path)
    _verified_artifact(artifact_path, artifact)
    job = _staging_root()/uuid.uuid4().hex
    try:
        job.mkdir(parents=True)
        old_sha = _sha256(exe)
        _copy_verified(artifact_path, job/WINDOWS_NAME, artifact['sha256'])
        (job/MANIFEST_NAME).write_bytes(raw)
        (job/SIGNATURE_NAME).write_bytes(signature)
        helper = job/HELPER_NAME
        shutil.copy2(exe, helper)
        if _sha256(helper) != old_sha:
            raise UpdateError('Güncelleme yardımcısı hazırlanamadı.')
        descriptor = job/DESCRIPTOR_NAME
        _write_json(descriptor, {'schema': 1, 'target': str(exe), 'pid': os.getpid(),
            'old_sha256': old_sha, 'version': manifest['version'],
            'sequence': manifest['sequence'], 'manifestpath': str(job/MANIFEST_NAME),
            'signaturepath': str(job/SIGNATURE_NAME), 'artifactpath': str(job/WINDOWS_NAME),
            'logfilepath': str(job/LOG_NAME)})
        _spawn([str(helper), '--apply-update', str(descriptor)])
    except UpdateError:
        shutil.rmtree(job, ignore_errors=True)
        raise
    except OSError:
        shutil.rmtree(job, ignore_errors=True)
        raise UpdateError('Güncelleme başlatılamadı; uygulama kapatılmadı.') from None
    return descriptor


def apply_update(descriptor_path) -> int:
    """Helper entry: re-verify and replace the target EXE.

    Returns OK on success, ROLLED_BACK when the health check restored the old
    build, or FAILED when the replacement could not be completed.
    """
    _frozen_exe()
    descriptor_path = Path(descriptor_path)
    job = descriptor_path.parent
    data = _read_descriptor(descriptor_path)
    manifest_path = _stage_path(job, data['manifestpath'], MANIFEST_NAME)
    signature_path = _stage_path(job, data['signaturepath'], SIGNATURE_NAME)
    artifact_path = _stage_path(job, data['artifactpath'], WINDOWS_NAME)
    _stage_path(job, data['logfilepath'], LOG_NAME, exists=False)
    target = _check_target(Path(data['target']), job)
    old_sha = data['old_sha256']
    manifest = verify_manifest(manifest_path.read_bytes(), signature_path.read_bytes()).manifest
    artifact = _release_artifact(manifest)
    if manifest['version'] != data['version'] or manifest['sequence'] != data['sequence']:
        raise UpdateError('Güncelleme tanımı bildirimle uyuşmuyor.')
    _verified_artifact(artifact_path, artifact)
    _wait_for_exit(data['pid'], target)
    if _sha256(target) != old_sha:
        raise UpdateError('Mevcut uygulama dosyası beklenen sürüm değil; güncelleme yapılmadı.')
    backup = target.parent/(BACKUP_PREFIX + uuid.uuid4().hex + '.exe')
    _copy_verified(target, backup, old_sha)
    temp = target.parent/(BACKUP_PREFIX + uuid.uuid4().hex + '.tmp')
    try:
        _copy_verified(artifact_path, temp, artifact['sha256'])
        _replace_unlocked(temp, target, old_sha)
    except (OSError, UpdateError):
        _log(job, 'replace-failed')
        _discard(temp)
        if _restore(target, backup, old_sha):
            _start(target)
        else:
            _log(job, 'rollback-failed')
        return FAILED
    if not _run_health(target, job/HEALTH_NAME, manifest['version']):
        _log(job, 'health-failed')
        if not _restore(target, backup, old_sha):
            _log(job, 'rollback-failed')
            return FAILED
        _start(target)
        return ROLLED_BACK
    _log(job, 'updated')
    _start(target)
    return OK
