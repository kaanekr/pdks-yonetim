"""Offline tests for signed update discovery; no network, no real publisher key."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app import updates
from windows_app.update_contract import (CURRENT_SEQUENCE, CURRENT_VERSION, FIRMWARE_NAME,
                                         MANIFEST_NAME, REPOSITORY, SIGNATURE_NAME,
                                         WINDOWS_NAME, UpdateError, firmware_envelope,
                                         version_sequence)

WINDOWS_BYTES = b'MZ' + bytes(range(256))*2
FIRMWARE_BYTES = b'\xa5'*300
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def prefix(version):
    return f'https://github.com/{REPOSITORY}/releases/download/v{version}'


def manifest(version=CURRENT_VERSION, windows=WINDOWS_BYTES, firmware=FIRMWARE_BYTES):
    sequence = version_sequence(version)
    base = prefix(version)
    item = {'name': FIRMWARE_NAME, 'size': len(firmware), 'sha256': sha256(firmware),
            'url': f'{base}/{FIRMWARE_NAME}', 'version': version, 'sequence': sequence,
            'target': 'nodemcuv2_sd_soft_tx', 'min_api': 5}
    item['meta_signature'] = base64.b64encode(
        KEY.sign(firmware_envelope(item), padding.PKCS1v15(), hashes.SHA256())).decode('ascii')
    return {'schema': 1, 'repository': REPOSITORY, 'version': version, 'sequence': sequence,
            'published_at': '2026-10-01T09:00:00Z',
            'notes': 'Sürüm notları: kararlı sürüm.',
            'windows': {'name': WINDOWS_NAME, 'size': len(windows), 'sha256': sha256(windows),
                        'url': f'{base}/{WINDOWS_NAME}', 'target': 'windows-x64'},
            'firmware': item}


def sign(document):
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return raw, KEY.sign(raw, padding.PKCS1v15(), hashes.SHA256())


def signed(**kwargs):
    return sign(manifest(**kwargs))


def release_document(version, raw, signature):
    base = prefix(version)
    return {'tag_name': f'v{version}', 'draft': False, 'prerelease': False,
            'assets': [{'name': MANIFEST_NAME,
                        'browser_download_url': f'{base}/{MANIFEST_NAME}'},
                       {'name': SIGNATURE_NAME,
                        'browser_download_url': f'{base}/{SIGNATURE_NAME}'},
                       {'name': WINDOWS_NAME,
                        'browser_download_url': f'{base}/{WINDOWS_NAME}'}]}


class FakeResponse:
    def __init__(self, body, status=200, length=None):
        self.body, self.status, self.position = body, status, 0
        self.headers = {} if length is None else {'Content-Length': str(length)}

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self.body) - self.position
        chunk = self.body[self.position:self.position + size]
        self.position += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url):
        self.calls.append(url)
        body = self.routes.get(url)
        if body is None:
            raise OSError('test route missing')
        return body if isinstance(body, FakeResponse) else FakeResponse(body)


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, updates, '_open', updates._open)
        self.addCleanup(setattr, updates, '_key_override', updates._key_override)
        updates._key_override = PUBLIC_PEM
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def serve(self, routes):
        http = FakeHTTP(routes)
        updates._open = http
        return http

    def release(self, **kwargs):
        raw, signature = signed(**kwargs)
        return updates.verify_manifest(raw, signature, PUBLIC_PEM)


class ManifestTests(OfflineTest):
    def test_verifies_signed_manifest_and_older_catalog(self):
        raw, signature = signed()
        release = updates.verify_manifest(raw, signature, PUBLIC_PEM)
        self.assertEqual(release.raw, raw)
        self.assertEqual(release.signature, signature)
        self.assertEqual(release.manifest['version'], CURRENT_VERSION)
        self.assertEqual(release.manifest['sequence'], CURRENT_SEQUENCE)
        older = self.release(version='1.5.0')
        self.assertLess(older.manifest['sequence'], CURRENT_SEQUENCE)

    def test_rejects_tampered_raw_and_signature(self):
        raw, signature = signed()
        with self.assertRaises(UpdateError):
            updates.verify_manifest(raw.replace(b'1.5.1', b'1.5.2'), signature, PUBLIC_PEM)
        with self.assertRaises(UpdateError):
            updates.verify_manifest(raw, signature[:-1], PUBLIC_PEM)
        with self.assertRaises(UpdateError):
            updates.verify_manifest(raw, bytes(256), PUBLIC_PEM)
        with self.assertRaises(UpdateError):
            updates.verify_manifest('metin', signature, PUBLIC_PEM)

    def test_rejects_duplicate_keys_and_boolean_numbers(self):
        raw, _ = signed()
        duplicated = raw.replace(b'"schema":1', b'"schema":1,"schema":1', 1)
        with self.assertRaises(UpdateError):
            updates.verify_manifest(duplicated,
                                    KEY.sign(duplicated, padding.PKCS1v15(), hashes.SHA256()),
                                    PUBLIC_PEM)
        for field, value in (('size', True), ('size', 512.0), ('size', 0),
                             ('size', 80*1024*1024 + 1)):
            document = manifest()
            document['windows'][field] = value
            with self.assertRaises(UpdateError):
                updates.verify_manifest(*sign(document), PUBLIC_PEM)

    def test_rejects_schema_repository_version_and_sequence(self):
        for change in ({'schema': 2}, {'schema': True}, {'repository': 'evil/repo'},
                       {'version': '1.5'}, {'version': '01.5.1'},
                       {'published_at': 'dun'}, {'notes': ''}, {'sequence': 10500}):
            document = manifest()
            document.update(change)
            with self.assertRaises(UpdateError):
                updates.verify_manifest(*sign(document), PUBLIC_PEM)

    def test_rejects_asset_url_target_fields_and_firmware_meta(self):
        documents = []
        document = manifest()
        document['windows']['url'] = 'https://evil.example/PDKS_Yonetim.exe'
        documents.append(document)
        document = manifest()
        document['windows']['target'] = 'linux-x64'
        documents.append(document)
        document = manifest()
        document['windows']['name'] = 'private-key.pem'
        documents.append(document)
        document = manifest()
        document['windows']['token'] = 'secret'
        documents.append(document)
        document = manifest()
        document['firmware']['target'] = 'esp8266'
        documents.append(document)
        document = manifest()
        document['firmware']['size'] = 1024*1024 + 1
        documents.append(document)
        document = manifest()
        document['firmware']['meta_signature'] = base64.b64encode(bytes(256)).decode('ascii')
        documents.append(document)
        document = manifest()
        document['firmware']['sequence'] = 10500
        documents.append(document)
        for document in documents:
            with self.assertRaises(UpdateError):
                updates.verify_manifest(*sign(document), PUBLIC_PEM)


class ReleaseTests(OfflineTest):
    def test_check_release_fetches_only_manifest_assets(self):
        raw, signature = signed()
        document = release_document(CURRENT_VERSION, raw, signature)
        http = self.serve({updates.API_LATEST: json.dumps(document).encode(),
                           f'{prefix(CURRENT_VERSION)}/{MANIFEST_NAME}': raw,
                           f'{prefix(CURRENT_VERSION)}/{SIGNATURE_NAME}': signature})
        release = updates.check_release()
        self.assertEqual(release.manifest['version'], CURRENT_VERSION)
        self.assertEqual(http.calls, [updates.API_LATEST,
                                      f'{prefix(CURRENT_VERSION)}/{MANIFEST_NAME}',
                                      f'{prefix(CURRENT_VERSION)}/{SIGNATURE_NAME}'])

    def test_check_release_rejects_draft_prerelease_tag_and_assets(self):
        raw, signature = signed()
        base = release_document(CURRENT_VERSION, raw, signature)
        variants = []
        for change in ({'draft': True}, {'prerelease': True}, {'tag_name': 'v1.5.2'},
                       {'tag_name': 'release-1.5.1'}, {'assets': []}):
            document = json.loads(json.dumps(base))
            document.update(change)
            variants.append(document)
        evil = json.loads(json.dumps(base))
        evil['assets'][0]['browser_download_url'] = 'https://evil.example/update-manifest.json'
        variants.append(evil)
        duplicate = json.loads(json.dumps(base))
        duplicate['assets'].append(dict(duplicate['assets'][0]))
        variants.append(duplicate)
        for document in variants:
            self.serve({updates.API_LATEST: json.dumps(document).encode(),
                        f'{prefix(CURRENT_VERSION)}/{MANIFEST_NAME}': raw,
                        f'{prefix(CURRENT_VERSION)}/{SIGNATURE_NAME}': signature})
            with self.assertRaises(UpdateError):
                updates.check_release()

    def test_redirect_allowlist_and_credential_free_requests(self):
        for url in ('https://evil.example/x', 'http://github.com/x',
                    'https://github.com.evil.example/x', 'https://github.com/../etc',
                    'https://github.com:8443/x'):
            with self.assertRaises(UpdateError):
                updates._safe_url(url)
        allowed = 'https://release-assets.githubusercontent.com/a/b'
        self.assertEqual(updates._safe_url(allowed), allowed)
        with self.assertRaises(UpdateError):
            updates._SafeRedirect().redirect_request(None, None, 302, 'Found', {},
                                                     'https://evil.example/x')
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(b'{}')
        with mock.patch.object(updates.urllib.request, 'build_opener', lambda *a: opener):
            updates._fetch(updates.API_LATEST, 1024)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.type, 'https')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertIsNone(request.get_header('Cookie'))
        self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 30)


class DownloadTests(OfflineTest):
    def test_download_windows_verifies_reports_progress_and_renames(self):
        release = self.release()
        url = release.manifest['windows']['url']
        http = self.serve({url: WINDOWS_BYTES})
        seen = []
        path = updates.download_artifact(release, 'windows', directory=self.temp.name,
                                         progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(http.calls, [url])
        self.assertEqual(path, Path(self.temp.name)/WINDOWS_NAME)
        self.assertEqual(path.read_bytes(), WINDOWS_BYTES)
        self.assertEqual(seen[-1], (len(WINDOWS_BYTES), len(WINDOWS_BYTES)))
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), [WINDOWS_NAME])
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': self.temp.name}):
            default = updates.download_artifact(release, 'windows')
        self.assertEqual(default.name, WINDOWS_NAME)
        self.assertEqual(default.read_bytes(), WINDOWS_BYTES)
        self.assertEqual(default.parent.parent.parent,
                         Path(self.temp.name)/'PDKS_Yonetim')
        self.assertRegex(default.parent.name, '^[0-9a-f]{32}$')

    def test_download_rejects_truncation_hash_mismatch_and_preserves_file(self):
        release = self.release()
        url = release.manifest['windows']['url']
        target = Path(self.temp.name)/WINDOWS_NAME
        for response in (FakeResponse(WINDOWS_BYTES[:-1], length=len(WINDOWS_BYTES)),
                         FakeResponse(b'X'*len(WINDOWS_BYTES)),
                         FakeResponse(WINDOWS_BYTES, length=len(WINDOWS_BYTES) + 1)):
            self.serve({url: response})
            with self.assertRaises(UpdateError):
                updates.download_artifact(release, 'windows', directory=self.temp.name)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        target.write_bytes(b'eski-kurulum')
        self.serve({url: WINDOWS_BYTES})
        with self.assertRaises(UpdateError):
            updates.download_artifact(release, 'windows', directory=self.temp.name)
        self.assertEqual(target.read_bytes(), b'eski-kurulum')
        target.write_bytes(WINDOWS_BYTES)
        http = self.serve({})
        self.assertEqual(updates.download_artifact(release, 'windows',
                                                   directory=self.temp.name), target)
        self.assertEqual(http.calls, [])

    def test_download_ignores_mutated_manifest_mapping(self):
        release = self.release()
        url = release.manifest['windows']['url']
        release.manifest['windows']['url'] = 'https://evil.example/payload.exe'
        release.manifest['windows']['sha256'] = 'a'*64
        release.manifest['windows']['size'] = 3
        http = self.serve({url: WINDOWS_BYTES})
        path = updates.download_artifact(release, 'windows', directory=self.temp.name)
        self.assertEqual(http.calls, [url])
        self.assertEqual(path.read_bytes(), WINDOWS_BYTES)
        with self.assertRaises(UpdateError):
            updates.download_artifact(release, 'firmware', directory=self.temp.name)


if __name__ == '__main__':
    unittest.main()
