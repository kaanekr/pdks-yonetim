"""Offline client + GUI update integration tests.

Every fixture is synthetic: an ephemeral RSA key signs a real firmware image and
manifest, the public key is patched into ``windows_app.update_contract.PUBLIC_KEY``
so genuine RSA verification runs, and a fake opener stands in for the network.
No real profile, vault, archive, credentials or device is touched.
"""
import base64
import copy
import hashlib
import json
import shutil
import struct
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app import update_contract, updates
from windows_app.client import DeviceClient, DeviceError
from windows_app.gui import Manager
from windows_app.update_contract import (CURRENT_SEQUENCE, FIRMWARE_NAME, FIRMWARE_TARGET,
                                         REPOSITORY, WINDOWS_NAME, firmware_envelope,
                                         version_sequence)
from windows_app.workforce_model import WorkforceError
from windows_app.workforce_smoke import sample_snapshot
from windows_app.workforce_store import verify_backup

TEMP = Path(tempfile.gettempdir())/'opencode'
VERSION, SEQUENCE = '1.6.0', 10600
PREFIX = f'https://github.com/{REPOSITORY}/releases/download/v{VERSION}'
TOKEN = 'a1b2c3d4'*4
WINDOWS_BYTES = b'MZ' + bytes(range(256))*2
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def firmware_package(payload=None, sign_over=None):
    """Real signed image: payload + PKCS1v15 signature + LE uint32 signature length."""
    payload = b'\xe9' + bytes(range(256)) if payload is None else payload
    signature = KEY.sign(payload if sign_over is None else sign_over,
                         padding.PKCS1v15(), hashes.SHA256())
    return payload + signature + struct.pack('<I', 256)


def firmware_artifact(blob, version=VERSION, sequence=SEQUENCE, target=FIRMWARE_TARGET):
    item = {'name': FIRMWARE_NAME, 'size': len(blob), 'sha256': sha(blob),
            'url': f'{PREFIX}/{FIRMWARE_NAME}', 'version': version, 'sequence': sequence,
            'target': target, 'min_api': 5}
    item['meta_signature'] = base64.b64encode(
        KEY.sign(firmware_envelope(item), padding.PKCS1v15(), hashes.SHA256())).decode('ascii')
    return item


def manifest_bytes(firmware):
    document = {'schema': 1, 'repository': REPOSITORY, 'version': VERSION,
                'sequence': version_sequence(VERSION),
                'published_at': '2026-10-02T08:00:00Z',
                'notes': 'Kararlı sürüm; imzalı test yayını.',
                'windows': {'name': WINDOWS_NAME, 'size': len(WINDOWS_BYTES),
                            'sha256': sha(WINDOWS_BYTES),
                            'url': f'{PREFIX}/{WINDOWS_NAME}', 'target': 'windows-x64'},
                'firmware': firmware}
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return raw, KEY.sign(raw, padding.PKCS1v15(), hashes.SHA256())


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status, self.position = body, status, 0
        self.headers = {}

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self.body) - self.position
        chunk = self.body[self.position:self.position + size]
        self.position += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def json_response(payload):
    return FakeResponse(json.dumps(payload).encode('utf-8'))


class FakeOpener:
    """Records every request; returns/raises the scripted response in order."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, BaseException):
            raise response
        return response


class UpdateFixture(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='pdks-update-', dir=TEMP))
        self.addCleanup(shutil.rmtree, self.folder, True)
        pem = self.folder/'update_public_key.pem'
        pem.write_bytes(PUBLIC_PEM)
        for started in (patch.object(update_contract, 'PUBLIC_KEY', pem),
                        patch.object(updates, '_key_override', None)):
            started.start()
            self.addCleanup(started.stop)
        self.firmware = firmware_package()
        self.signed_path = self.folder/FIRMWARE_NAME
        self.signed_path.write_bytes(self.firmware)
        status, self.csv_data, self.config = sample_snapshot()
        self.state = dict(copy.deepcopy(status), api_version=5, journal_revision=0,
                          ota_update=True, firmware_target=FIRMWARE_TARGET,
                          firmware_sequence=10500, storage_ok=True)
        raw, signature = manifest_bytes(firmware_artifact(self.firmware))
        self.release_info = updates.verify_manifest(raw, signature)

    def client(self, opener):
        client = DeviceClient('http://192.0.2.10', 'admin', 'pw')
        client._opener = opener
        return client


class ClientFirmwareUpdateTest(UpdateFixture):
    def test_valid_transfer_prepares_then_uploads_once(self):
        opener = FakeOpener([json_response({'ok': True, 'token': TOKEN}),
                             json_response({'ok': True, 'message': 'kabul edildi'})])
        client = self.client(opener)
        result = client.update_firmware(self.state, self.release_info.manifest['firmware'],
                                        self.signed_path)
        self.assertEqual(result['message'], 'kabul edildi')
        self.assertEqual(len(opener.calls), 2)
        auth = 'Basic ' + base64.b64encode(b'admin:pw').decode('ascii')
        prepare, prepare_timeout = opener.calls[0]
        upload, upload_timeout = opener.calls[1]
        self.assertEqual(prepare.full_url, 'http://192.0.2.10/api/update/prepare')
        self.assertEqual(prepare.get_method(), 'POST')
        self.assertEqual(dict(prepare.header_items()),
                         {'Authorization': auth, 'Accept': 'application/json',
                          'Content-type': 'application/json',
                          'X-csrf-token': self.state['csrf']})
        self.assertEqual(json.loads(prepare.data), {
            'expected_sequence': 6, 'roster_version': 0xDEADBEEF, 'journal_revision': 0,
            'confirm': 'FIRMWARE GUNCELLE', 'target': FIRMWARE_TARGET, 'version': VERSION,
            'sequence': SEQUENCE, 'size': len(self.firmware), 'sha256': sha(self.firmware),
            'meta_signature': self.release_info.manifest['firmware']['meta_signature']})
        self.assertEqual(prepare_timeout, 5)
        self.assertEqual(upload.full_url, 'http://192.0.2.10/api/update/firmware')
        headers = dict(upload.header_items())
        content_type = headers.pop('Content-type')
        self.assertTrue(content_type.startswith('multipart/form-data; boundary=PDKS'))
        self.assertEqual(headers, {'Authorization': auth, 'X-csrf-token': self.state['csrf'],
                                   'X-ota-token': TOKEN, 'Accept': 'application/json'})
        boundary = content_type.split('boundary=', 1)[1]
        body = upload.data
        self.assertTrue(body.startswith(
            f'--{boundary}\r\nContent-Disposition: form-data; name="firmware"; '
            'filename="firmware.signed.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
            .encode('ascii')))
        self.assertTrue(body.endswith(f'\r\n--{boundary}--\r\n'.encode('ascii')))
        self.assertEqual(body.count(self.firmware), 1)
        self.assertEqual(upload_timeout, 90)

    def test_rejects_unverified_meta_signature_without_network(self):
        opener = FakeOpener([])
        client = self.client(opener)
        good = self.release_info.manifest['firmware']
        for signature in (base64.b64encode(KEY.sign(b'PDKS-FW1\nbaska', padding.PKCS1v15(),
                                                    hashes.SHA256())).decode('ascii'),
                          base64.b64encode(bytes(256)).decode('ascii'), 'gecersiz!'):
            with self.subTest(signature=signature[:8]):
                with self.assertRaises(DeviceError):
                    client.update_firmware(self.state, dict(good, meta_signature=signature),
                                           self.signed_path)
        self.assertEqual(opener.calls, [])

    def test_rejects_file_signature_hash_size_and_magic_without_network(self):
        opener = FakeOpener([])
        client = self.client(opener)
        valid = self.release_info.manifest['firmware']
        tampered = bytearray(self.firmware)
        tampered[10] ^= 0xFF
        wrong_signature = firmware_package(sign_over=b'baska bir yuk')
        wrong_magic = firmware_package(payload=b'\x00' + bytes(range(256)))
        cases = [(valid, bytes(tampered)), (valid, self.firmware[:400]),
                 (firmware_artifact(wrong_signature), wrong_signature),
                 (firmware_artifact(wrong_magic), wrong_magic),
                 (valid, None)]
        for index, (artifact, blob) in enumerate(cases):
            path = self.folder/f'payload-{index}.bin'
            if blob is not None:
                path.write_bytes(blob)
            with self.subTest(index=index):
                with self.assertRaises(DeviceError):
                    client.update_firmware(self.state, artifact, path)
        self.assertEqual(opener.calls, [])
        self.assertFalse(self.folder.joinpath('payload-4.bin').exists())

    def test_rejects_stale_sequence_and_wrong_target_without_network(self):
        opener = FakeOpener([])
        client = self.client(opener)
        artifact = self.release_info.manifest['firmware']
        states = [dict(self.state, firmware_sequence=SEQUENCE),
                  dict(self.state, firmware_sequence=SEQUENCE + 1),
                  dict(self.state, firmware_sequence=True),
                  dict(self.state, firmware_target='esp8266')]
        for index, state in enumerate(states):
            with self.subTest(index=index):
                with self.assertRaises(DeviceError):
                    client.update_firmware(state, artifact, self.signed_path)
        self.assertEqual(opener.calls, [])

    def test_rejects_open_session_missing_ota_and_legacy_device_without_network(self):
        opener = FakeOpener([])
        client = self.client(opener)
        artifact = self.release_info.manifest['firmware']
        open_state = copy.deepcopy(self.state)
        open_state['employees'][0]['inside'] = True
        missing_ota = {key: value for key, value in self.state.items() if key != 'ota_update'}
        states = [open_state, missing_ota, dict(self.state, api_version=3),
                  dict(self.state, storage_ok=False)]
        for index, state in enumerate(states):
            with self.subTest(index=index):
                with self.assertRaises(DeviceError):
                    client.update_firmware(state, artifact, self.signed_path)
        self.assertEqual(opener.calls, [])

    def test_prepare_failure_or_invalid_token_never_uploads(self):
        artifact = self.release_info.manifest['firmware']
        responses = [json_response({'ok': True}), json_response({'ok': True, 'token': 'zz'*16}),
                     json_response({'ok': True, 'token': 'a'*31}),
                     json_response({'ok': True, 'token': 12345}),
                     json_response({'ok': False}), URLError('offline')]
        for index, response in enumerate(responses):
            opener = FakeOpener([response, json_response({'ok': True})])
            with self.subTest(index=index):
                with self.assertRaises(DeviceError):
                    self.client(opener).update_firmware(self.state, artifact, self.signed_path)
                self.assertEqual(len(opener.calls), 1)

    def test_upload_timeout_is_reported_once_without_retry(self):
        opener = FakeOpener([json_response({'ok': True, 'token': TOKEN}),
                             TimeoutError('timed out')])
        with self.assertRaises(DeviceError) as raised:
            self.client(opener).update_firmware(self.state, self.release_info.manifest['firmware'],
                                                self.signed_path)
        self.assertIn('belirsiz', str(raised.exception))
        self.assertEqual(len(opener.calls), 2)


class GuiUpdateTest(UpdateFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Manager(self.root, demo=True)
        self.job_errors = []

    def tearDown(self):
        if not self.app.closing:
            with patch('windows_app.gui.messagebox.showwarning'), \
                    patch.object(self.app, '_save_connection', return_value=True):
                self.app.close()

    def sync_job(self, work, done, quiet=False):
        """Run the background work + completion callback inline; never a thread."""
        try:
            value = work()
        except Exception as exc:  # mirrors Manager.job: nothing is retried
            self.job_errors.append(str(exc))
            return
        done(value)

    def arm_firmware(self, responses=None, state=None):
        """Real verifying client with a scripted opener; no network, credentials fake."""
        self.app.demo, self.app.settings = False, None
        opener = FakeOpener(responses or [json_response({'ok': True, 'token': TOKEN}),
                                          json_response({'ok': True})])
        client = self.client(opener)
        state = self.state if state is None else state
        client.status = lambda: copy.deepcopy(state)
        client.events = lambda: self.csv_data
        self.app.client, self.app.state = client, state
        self.app.update_release = self.release_info
        self.app.update_downloads = {'firmware': self.signed_path}
        return client, opener

    def test_demo_mode_blocks_all_four_update_actions(self):
        self.app.update_release = self.release_info
        self.app.client = Mock()
        self.app.state = {'ota_update': True, 'storage_ok': True,
                          'firmware_target': FIRMWARE_TARGET}
        before = self.app.update_text.get()
        with patch.object(self.app, 'job') as job, \
                patch('windows_app.updates.check_release') as check, \
                patch('windows_app.updates.download_artifact') as download, \
                patch('windows_app.gui.filedialog.asksaveasfilename') as saveas, \
                patch('windows_app.gui.messagebox.askyesno') as confirm:
            for action in (self.app.check_updates, self.app.download_updates,
                           self.app.install_app_update, self.app.install_firmware_update):
                action()
        job.assert_not_called()
        check.assert_not_called()
        download.assert_not_called()
        saveas.assert_not_called()
        confirm.assert_not_called()
        self.assertEqual(self.app.update_text.get(), before)
        self.assertEqual(self.app.update_downloads, {})

    def test_check_updates_applies_verified_release(self):
        self.app.demo, self.app.settings = False, None
        with patch('windows_app.updates.check_release',
                   return_value=self.release_info) as check, \
                patch.object(self.app, 'job', new=self.sync_job):
            self.app.check_updates()
        check.assert_called_once()
        self.assertIs(self.app.update_release, self.release_info)
        self.assertEqual(self.app.update_downloads, {})
        text = self.app.update_text.get()
        self.assertIn('Yeni sürüm hazır', text)
        self.assertNotIn('1.6.0', text)

    def test_download_updates_verifies_and_records_both_files(self):
        self.app.demo, self.app.settings = False, None
        self.app.update_release = self.release_info

        def fake_download(release, kind, directory=None, progress=None):
            path = self.folder/f'indirilen-{kind}.bin'
            path.write_bytes(WINDOWS_BYTES if kind == 'windows' else self.firmware)
            return path

        with patch('windows_app.updates.download_artifact', side_effect=fake_download), \
                patch.object(self.app, 'job', new=self.sync_job):
            self.app.download_updates()
        self.assertEqual(set(self.app.update_downloads), {'windows', 'firmware'})
        for kind, path in self.app.update_downloads.items():
            self.assertTrue(Path(path).is_file(), kind)
        self.assertIn('Güncelleme hazır', self.app.update_text.get())

    def test_firmware_confirmation_cancel_never_backs_up_or_uploads(self):
        client, opener = self.arm_firmware()
        with patch('windows_app.gui.messagebox.askyesno', return_value=False) as confirm, \
                patch('windows_app.gui.filedialog.asksaveasfilename') as saveas, \
                patch.object(self.app.archive, 'save_snapshot') as snapshot, \
                patch.object(self.app.archive, 'backup') as backup, \
                patch.object(self.app.client, 'update_firmware') as upload, \
                patch.object(self.app, 'job') as job:
            self.app.install_firmware_update()
        confirm.assert_called_once()
        saveas.assert_not_called()
        snapshot.assert_not_called()
        backup.assert_not_called()
        upload.assert_not_called()
        job.assert_not_called()
        self.assertEqual(opener.calls, [])
        self.assertFalse(self.app.firmware_update_pending)

    def test_canceled_backup_dialog_starts_no_work(self):
        client, opener = self.arm_firmware()
        with patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch('windows_app.gui.filedialog.asksaveasfilename', return_value=''), \
                patch.object(self.app.archive, 'save_snapshot') as snapshot, \
                patch.object(self.app.archive, 'backup') as backup, \
                patch.object(self.app.client, 'update_firmware') as upload, \
                patch.object(self.app, 'job') as job:
            self.app.install_firmware_update()
        snapshot.assert_not_called()
        backup.assert_not_called()
        upload.assert_not_called()
        job.assert_not_called()
        self.assertEqual(opener.calls, [])

    def test_backup_failure_never_sends_the_firmware(self):
        client, opener = self.arm_firmware()
        backup_path = self.folder/'PDKS_OTA_oncesi_arsiv.zip'
        with patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch('windows_app.gui.filedialog.asksaveasfilename',
                      return_value=str(backup_path)), \
                patch.object(self.app.archive, 'backup',
                             side_effect=WorkforceError('Yedek alınamadı; yükleme yapılmadı.')), \
                patch.object(self.app.client, 'update_firmware') as upload, \
                patch.object(self.app, 'job', new=self.sync_job):
            self.app.install_firmware_update()
        upload.assert_not_called()
        self.assertEqual(opener.calls, [])
        self.assertEqual(len(self.job_errors), 1)
        self.assertIn('Yedek', self.job_errors[0])
        self.assertFalse(backup_path.exists())
        self.assertEqual(self.app.archive.load_snapshot(client.address)['csv'], self.csv_data)
        self.assertFalse(self.app.firmware_update_pending)

    def test_snapshot_and_backup_precede_the_signed_upload(self):
        client, opener = self.arm_firmware()
        order = []
        save, backup = self.app.archive.save_snapshot, self.app.archive.backup

        def record_save(*args):
            order.append('snapshot')
            return save(*args)

        def record_backup(*args):
            order.append('backup')
            return backup(*args)

        def record_upload(*args):
            order.append('upload')
            return DeviceClient.update_firmware(client, *args)

        client.update_firmware = record_upload
        backup_path = self.folder/'PDKS_OTA_oncesi_arsiv.zip'
        with patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch('windows_app.gui.filedialog.asksaveasfilename',
                      return_value=str(backup_path)), \
                patch.object(self.app.archive, 'save_snapshot', new=record_save), \
                patch.object(self.app.archive, 'backup', new=record_backup), \
                patch.object(self.app, 'job', new=self.sync_job):
            self.app.install_firmware_update()
        self.assertEqual(order[:3], ['snapshot', 'backup', 'upload'])
        self.assertEqual(self.job_errors, [])
        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(opener.calls[0][0].full_url.endswith('/api/update/prepare'))
        self.assertTrue(opener.calls[1][0].full_url.endswith('/api/update/firmware'))
        self.assertEqual(self.app.archive.load_snapshot(client.address)['csv'], self.csv_data)
        self.assertTrue(backup_path.is_file())
        self.assertTrue(verify_backup(backup_path)['ok'])
        self.assertFalse(self.app.online)
        self.assertTrue(self.app.firmware_update_pending)
        self.assertIn('onaylandı', self.app.update_text.get())
        self.assertEqual(self.app.state['sequence'], 6)

    def test_legacy_ota_device_is_rejected_without_a_job(self):
        client, opener = self.arm_firmware(state=dict(self.state, api_version=3,
                                                      ota_update=False))
        with patch('windows_app.gui.messagebox.showerror') as error, \
                patch.object(self.app, 'job') as job:
            self.app.install_firmware_update()
        error.assert_called_once()
        self.assertIn('USB', error.call_args[0][1])
        job.assert_not_called()
        self.assertEqual(opener.calls, [])
        self.assertFalse(self.app.firmware_update_pending)

    def test_app_update_needs_frozen_build_and_confirmation(self):
        self.app.demo, self.app.settings = False, None
        self.app.update_release = self.release_info
        with patch.object(sys, 'frozen', False, create=True), \
                patch('windows_app.gui.messagebox.showinfo') as info, \
                patch('windows_app.gui.messagebox.askyesno') as confirm, \
                patch.object(self.app, 'job') as job:
            self.app.install_app_update()
        info.assert_called_once()
        confirm.assert_not_called()
        job.assert_not_called()
        with patch.object(sys, 'frozen', True, create=True), \
                patch('windows_app.gui.messagebox.showinfo') as info, \
                patch('windows_app.gui.messagebox.askyesno', return_value=False) as confirm, \
                patch.object(self.app, 'job') as job:
            self.app.install_app_update()
        confirm.assert_called_once()
        info.assert_not_called()
        job.assert_not_called()
        self.assertFalse(self.app.closing)

    def test_drain_completion_close_never_reschedules_or_touches_dead_tk(self):
        after = Mock()
        seen = []

        def done(value):
            seen.append(value)
            self.app.close()

        self.app.results.put((self.app.generation, done, ('hazır',), None, False))
        with patch.object(self.root, 'after', after):
            self.app._drain()
            # a destroyed root must not be asked to schedule another drain
            self.app._drain()
        after.assert_not_called()
        self.assertTrue(self.app.closing)
        self.assertEqual(seen, [('hazır',)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
