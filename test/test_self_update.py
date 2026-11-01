"""Self-update staging and rollback tests; no real process replacement happens."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from windows_app import self_update
from windows_app.update_contract import ReleaseInfo, UpdateError, verify_signature

TEMP = Path(tempfile.gettempdir())/'opencode'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
OLD, NEW = b'MZ-old-build', b'MZ-new-build'
VERSION, SEQUENCE = '1.6.0', 10600


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sign(manifest):
    raw = json.dumps(manifest, sort_keys=True).encode('utf-8')
    return raw, KEY.sign(raw, padding.PKCS1v15(), hashes.SHA256())


def manifest_for(payload, version=VERSION, sequence=SEQUENCE):
    return {'version': version, 'sequence': sequence, 'windows':
        {'target': 'windows-x64', 'name': 'PDKS_Yonetim.exe', 'size': len(payload),
         'sha256': sha(payload)}, 'firmware':
        {'target': 'nodemcuv2_sd_soft_tx', 'name': 'PDKS_Firmware_TX_SD.signed.bin',
         'size': 300, 'sha256': sha(b'x'*300)}}


def verified(raw, signature, public_key=None):
    """Real RSA verification with the ephemeral test key, then normal parsing."""
    verify_signature(raw, signature, public_key or PUBLIC)
    return ReleaseInfo(manifest=json.loads(raw), raw=raw, signature=signature)


class SelfUpdateTest(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='pdks-self-update-', dir=TEMP))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def make_job(self, old=OLD, new=NEW, version=VERSION, sequence=SEQUENCE):
        job = self.folder/'job'
        job.mkdir()
        raw, signature = sign(manifest_for(new, version, sequence))
        (job/'update-manifest.json').write_bytes(raw)
        (job/'update-manifest.sig').write_bytes(signature)
        (job/'PDKS_Yonetim.exe').write_bytes(new)
        app = self.folder/'app'
        app.mkdir()
        target = app/'PDKS_Yonetim.exe'
        target.write_bytes(old)
        descriptor = job/'descriptor.json'
        descriptor.write_text(json.dumps({'schema': 1, 'target': str(target), 'pid': 4242,
            'old_sha256': sha(old), 'version': version, 'sequence': sequence,
            'manifestpath': str(job/'update-manifest.json'),
            'signaturepath': str(job/'update-manifest.sig'),
            'artifactpath': str(job/'PDKS_Yonetim.exe'),
            'logfilepath': str(job/'update.log')}), encoding='utf-8')
        return job, descriptor, target

    def apply_with(self, descriptor, frozen=None, health=True, wait=None, start=None):
        helper = frozen or Path('C:/PDKS/PDKS_UpdateHelper.exe')
        patches = [mock.patch.object(self_update, '_frozen_exe', return_value=helper),
                   mock.patch.object(self_update, 'verify_manifest', side_effect=verified),
                   mock.patch.object(self_update, '_wait_for_exit',
                                     wait if wait is not None else mock.Mock()),
                   mock.patch.object(self_update, '_run_health', return_value=health),
                   mock.patch.object(self_update, '_start', start if start is not None
                                     else mock.Mock())]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return self_update.apply_update(descriptor)

    def backups(self, target):
        return list(target.parent.glob('PDKS_Yonetim.previous.*.exe'))

    def test_prepare_source_mode_refused(self):
        with self.assertRaisesRegex(UpdateError, 'paketlenmiş'):
            self_update.prepare_self_update(ReleaseInfo({}, b'', b''), Path('missing.exe'))

    def test_prepare_stages_and_launches_helper(self):
        exe = self.folder/'build'/'PDKS_Yonetim.exe'
        exe.parent.mkdir()
        exe.write_bytes(OLD)
        artifact = self.folder/'download.exe'
        artifact.write_bytes(NEW)
        raw, signature = sign(manifest_for(NEW))
        root = self.folder/'updates'
        with mock.patch.object(self_update, '_frozen_exe', return_value=exe), \
                mock.patch.object(self_update, '_staging_root', return_value=root), \
                mock.patch.object(self_update, 'verify_manifest', side_effect=verified), \
                mock.patch.object(self_update, '_spawn', return_value=777) as spawn:
            descriptor = self_update.prepare_self_update(
                ReleaseInfo(json.loads(raw), raw, signature), artifact)
        self.assertEqual(descriptor.name, 'descriptor.json')
        self.assertEqual(descriptor.parent.parent, root)
        data = json.loads(descriptor.read_text(encoding='utf-8'))
        self.assertEqual(data['target'], str(exe))
        self.assertEqual(data['pid'], os.getpid())
        self.assertEqual(data['old_sha256'], sha(OLD))
        self.assertEqual((descriptor.parent/'PDKS_Yonetim.exe').read_bytes(), NEW)
        self.assertEqual((descriptor.parent/'PDKS_UpdateHelper.exe').read_bytes(), OLD)
        self.assertEqual(spawn.call_args.args[0][1:], ['--apply-update', str(descriptor)])

    def test_prepare_rejects_stale_sequence(self):
        exe = self.folder/'PDKS_Yonetim.exe'
        exe.write_bytes(OLD)
        artifact = self.folder/'download.exe'
        artifact.write_bytes(NEW)
        raw, signature = sign(manifest_for(NEW, '1.5.1', 10501))
        root = self.folder/'updates'
        with mock.patch.object(self_update, '_frozen_exe', return_value=exe), \
                mock.patch.object(self_update, '_staging_root', return_value=root), \
                mock.patch.object(self_update, 'verify_manifest', side_effect=verified):
            with self.assertRaises(UpdateError):
                self_update.prepare_self_update(
                    ReleaseInfo(json.loads(raw), raw, signature), artifact)
        self.assertFalse(root.exists() and any(root.iterdir()))

    def test_prepare_rejects_old_hash_and_magic(self):
        exe = self.folder/'PDKS_Yonetim.exe'
        exe.write_bytes(OLD)
        artifact = self.folder/'download.exe'
        artifact.write_bytes(b'MZ-tampered-build')
        raw, signature = sign(manifest_for(NEW))
        root = self.folder/'updates'
        with mock.patch.object(self_update, '_frozen_exe', return_value=exe), \
                mock.patch.object(self_update, '_staging_root', return_value=root), \
                mock.patch.object(self_update, 'verify_manifest', side_effect=verified), \
                mock.patch.object(self_update, '_spawn') as spawn:
            with self.assertRaises(UpdateError):
                self_update.prepare_self_update(
                    ReleaseInfo(json.loads(raw), raw, signature), artifact)
        spawn.assert_not_called()

    def test_apply_rejects_corrupt_manifest(self):
        job, descriptor, target = self.make_job()
        raw = (job/'update-manifest.json').read_bytes()
        (job/'update-manifest.json').write_bytes(raw.replace(b'1.6.0', b'1.6.9'))
        with self.assertRaises(UpdateError):
            self.apply_with(descriptor)
        self.assertEqual(target.read_bytes(), OLD)
        self.assertEqual(self.backups(target), [])

    def test_apply_rejects_old_hash_mismatch(self):
        job, descriptor, target = self.make_job()
        target.write_bytes(b'MZ-unknown-build')
        with self.assertRaises(UpdateError):
            self.apply_with(descriptor)
        self.assertEqual(target.read_bytes(), b'MZ-unknown-build')
        self.assertEqual(self.backups(target), [])

    def test_apply_rejects_dangerous_path(self):
        job, descriptor, target = self.make_job()
        outside = self.folder/'outside'/'PDKS_Yonetim.exe'
        outside.parent.mkdir()
        outside.write_bytes(NEW)
        data = json.loads(descriptor.read_text(encoding='utf-8'))
        data['artifactpath'] = str(outside)
        descriptor.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(UpdateError):
            self.apply_with(descriptor)
        self.assertEqual(target.read_bytes(), OLD)
        self.assertEqual(self.backups(target), [])

    def test_apply_timeout_leaves_target_untouched(self):
        job, descriptor, target = self.make_job()
        wait = mock.Mock(side_effect=UpdateError('Uygulama kapanmadı; güncelleme yapılmadı.'))
        with self.assertRaises(UpdateError):
            self.apply_with(descriptor, wait=wait)
        self.assertEqual(target.read_bytes(), OLD)
        self.assertEqual(self.backups(target), [])

    def test_apply_replaces_target_and_deletes_old_backup(self):
        job, descriptor, target = self.make_job()
        start, health, wait = mock.Mock(), mock.Mock(return_value=True), mock.Mock()
        with mock.patch.object(self_update, '_run_health', health), \
                mock.patch.object(self_update, '_start', start), \
                mock.patch.object(self_update, '_wait_for_exit', wait), \
                mock.patch.object(self_update, '_frozen_exe', return_value=target), \
                mock.patch.object(self_update, 'verify_manifest', side_effect=verified):
            self.assertEqual(self_update.apply_update(descriptor), self_update.OK)
        self.assertEqual(target.read_bytes(), NEW)
        self.assertEqual(self.backups(target), [])
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(health.call_args.args[2], VERSION)
        self.assertEqual(Path(start.call_args.args[0]), target.resolve())

    def test_apply_health_failure_restores_old_build(self):
        job, descriptor, target = self.make_job()
        start = mock.Mock()
        with mock.patch.object(self_update, '_run_health', return_value=False), \
                mock.patch.object(self_update, '_start', start), \
                mock.patch.object(self_update, '_wait_for_exit', mock.Mock()), \
                mock.patch.object(self_update, '_frozen_exe', return_value=target), \
                mock.patch.object(self_update, 'verify_manifest', side_effect=verified):
            self.assertEqual(self_update.apply_update(descriptor), self_update.ROLLED_BACK)
        self.assertEqual(target.read_bytes(), OLD)
        self.assertEqual(self.backups(target), [])
        self.assertEqual(Path(start.call_args.args[0]), target.resolve())

    def test_apply_replace_failure_preserves_original(self):
        job, descriptor, target = self.make_job()
        with mock.patch.object(self_update, '_replace', side_effect=OSError('locked')), \
                mock.patch.object(self_update, '_run_health', return_value=True), \
                mock.patch.object(self_update, '_start', mock.Mock()), \
                mock.patch.object(self_update, '_wait_for_exit', mock.Mock()), \
                mock.patch.object(self_update, '_frozen_exe', return_value=target), \
                mock.patch.object(self_update, 'verify_manifest', side_effect=verified):
            self.assertEqual(self_update.apply_update(descriptor), self_update.FAILED)
        self.assertEqual(target.read_bytes(), OLD)
        self.assertEqual(self.backups(target), [])

    def test_cleanup_stale_removes_only_old_update_files(self):
        app = self.folder/'cleanup-app'
        app.mkdir()
        backup = app/('PDKS_Yonetim.previous.' + 'a' * 32 + '.exe')
        temp = app/('PDKS_Yonetim.previous.' + 'b' * 32 + '.tmp')
        keep = app/'PDKS_Yonetim.exe'
        unrelated = app/'notes.txt'
        other = app/('PDKS_Yonetim.previous.' + 'z' * 32 + '.exe')
        for path in (backup, temp, keep, unrelated, other):
            path.write_bytes(b'x')
        jobs = self.folder/'cleanup-jobs'
        job = jobs/('c' * 32)
        job.mkdir(parents=True)
        (job/'leftover.bin').write_bytes(b'x')
        self_update.cleanup_stale(app, jobs)
        self.assertFalse(backup.exists())
        self.assertFalse(temp.exists())
        self.assertFalse(job.exists())
        self.assertTrue(keep.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(other.exists())

    def test_cleanup_stale_keeps_a_failed_rollback_backup(self):
        app = self.folder/'cleanup-failed-app'
        app.mkdir()
        backup = app/('PDKS_Yonetim.previous.' + 'f' * 32 + '.exe')
        backup.write_bytes(b'old')
        jobs = self.folder/'cleanup-failed-jobs'
        job = jobs/('d' * 32)
        job.mkdir(parents=True)
        (job/self_update.LOG_NAME).write_text('rollback-failed\n', encoding='utf-8')
        self_update.cleanup_stale(app, jobs)
        self.assertTrue(backup.exists())
        self.assertTrue(job.exists())

    def test_brief_windows_image_lock_is_retried_without_killing(self):
        job, descriptor, target = self.make_job()
        source = job/'PDKS_Yonetim.exe'
        error = OSError('fixture sharing lock')
        error.winerror = 32
        replace = self_update._replace
        calls = []
        def locked_once(src, dst):
            calls.append((src, dst))
            if len(calls) == 1:
                raise error
            return replace(src, dst)
        with mock.patch.object(self_update, '_replace', side_effect=locked_once), \
                mock.patch.object(self_update.time, 'sleep') as sleep:
            self_update._replace_unlocked(source, target, sha(OLD))
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(0.2)
        self.assertEqual(target.read_bytes(), NEW)

    def test_windows_lock_wait_is_bounded(self):
        job, descriptor, target = self.make_job()
        error = OSError('fixture sharing lock')
        error.winerror = 32
        with mock.patch.object(self_update, '_replace', side_effect=error), \
                mock.patch.object(self_update.time, 'monotonic', side_effect=[0, 16]), \
                mock.patch.object(self_update.time, 'sleep') as sleep:
            with self.assertRaises(OSError):
                self_update._replace_unlocked(job/'PDKS_Yonetim.exe', target, sha(OLD))
        sleep.assert_not_called()
        self.assertEqual(target.read_bytes(), OLD)

    def test_windows_lock_retry_refuses_a_changed_target(self):
        job, descriptor, target = self.make_job()
        target.write_bytes(b'MZ-changed')
        error = OSError('fixture sharing lock')
        error.winerror = 32
        with mock.patch.object(self_update, '_replace', side_effect=error), \
                mock.patch.object(self_update.time, 'sleep') as sleep:
            with self.assertRaises(UpdateError):
                self_update._replace_unlocked(job/'PDKS_Yonetim.exe', target, sha(OLD))
        sleep.assert_not_called()
        self.assertEqual(target.read_bytes(), b'MZ-changed')

    def test_nested_staging_target_is_rejected(self):
        job, descriptor, target = self.make_job()
        nested = job/'nested/target.exe'
        nested.parent.mkdir()
        nested.write_bytes(OLD)
        with self.assertRaises(UpdateError):
            self_update._check_target(nested, job)

    def test_health_json_does_not_override_a_failed_exit_code(self):
        job, descriptor, target = self.make_job()
        report = job/'health.json'
        def run(*args, **kwargs):
            report.write_text(json.dumps(dict(ok=True, version=VERSION)), encoding='utf-8')
            return mock.Mock(returncode=7)
        with mock.patch.object(self_update.subprocess, 'run', side_effect=run):
            self.assertFalse(self_update._run_health(target, report, VERSION))


if __name__ == '__main__':
    unittest.main(verbosity=2)
