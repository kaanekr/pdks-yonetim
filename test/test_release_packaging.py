"""Offline publisher contract and sanitization checks; never publish or use a private key."""
import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tools'))
import prepare_github_release as publisher
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from windows_app import update_contract as contract
from windows_app.updates import verify_manifest


class PackagingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pdks-publisher-', dir=Path(tempfile.gettempdir())/'opencode')
        self.root = Path(self.temp.name)
        self.public = self.root/'public.pem'
        self.public.write_bytes(self.key.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo))
        self.patchers = [patch.object(contract, 'PUBLIC_KEY', self.public),
            patch.object(publisher, 'sign', side_effect=lambda b: self.key.sign(b, padding.PKCS1v15(), hashes.SHA256()))]
        for item in self.patchers:
            item.start()
        self.exe = self.root/'fixture.exe'
        self.exe.write_bytes(b'MZ'+b'fixture-only'*60)
        self.fw = self.root/'public.bin'
        marker = f'PDKS-FW:{contract.CURRENT_VERSION}:{contract.CURRENT_SEQUENCE}UL:{contract.FIRMWARE_TARGET}'.encode()
        self.fw.write_bytes(b'\xe9'+b'x'*300+marker)
        self.out = self.root/'assets'

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def test_assets_match_real_verifier_and_native_envelope(self):
        publisher.prepare_assets(self.exe, self.fw, self.out)
        release = verify_manifest((self.out/contract.MANIFEST_NAME).read_bytes(), (self.out/contract.SIGNATURE_NAME).read_bytes())
        for kind in ('windows', 'firmware'):
            artifact = release.manifest[kind]
            contract.verify_artifact(self.out/artifact['name'], artifact)
        signed = (self.out/contract.FIRMWARE_NAME).read_bytes()
        self.assertEqual(signed[-4:], b'\x00\x01\x00\x00')
        contract.verify_signature(signed[:-260], signed[-260:-4])
        artifact = release.manifest['firmware']
        contract.verify_signature(contract.firmware_envelope(artifact), base64.b64decode(artifact['meta_signature']))
        self.assertEqual(set(p.name for p in self.out.iterdir()),
            {contract.WINDOWS_NAME, contract.FIRMWARE_NAME, contract.MANIFEST_NAME, contract.SIGNATURE_NAME})

    def test_wrong_build_rejected_before_signing(self):
        self.fw.write_bytes(b'\xe9'+b'old firmware'*50)
        with self.assertRaisesRegex(RuntimeError, 'marker'):
            publisher.prepare_assets(self.exe, self.fw, self.out)
        publisher.sign.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_secret_material_refused_without_emitting_value(self):
        secret = b'fixture-private-password-123'
        with patch.object(publisher, 'private_literals', return_value={secret}):
            for data in (secret, b'-----BEGIN '+b'PRIVATE KEY-----', b'ghp_'+b'A'*35):
                with self.assertRaisesRegex(RuntimeError, 'Private material') as result:
                    publisher.audit({'fixture.txt': data})
                self.assertNotIn(data.decode(), str(result.exception))

    def test_public_firmware_secret_rejected(self):
        secret = b'fixture-private-password-123'
        self.fw.write_bytes(self.fw.read_bytes()+secret)
        with patch.object(publisher, 'private_literals', return_value={secret}):
            with self.assertRaisesRegex(RuntimeError, 'Private material'):
                publisher.prepare_assets(self.exe, self.fw, self.out)
        self.assertFalse(self.out.exists())

    def test_existing_delivery_is_preserved(self):
        self.out.mkdir()
        sentinel = self.out/'keep'
        sentinel.write_bytes(b'unchanged')
        with self.assertRaises(RuntimeError):
            publisher.prepare_assets(self.exe, self.fw, self.out)
        self.assertEqual(sentinel.read_bytes(), b'unchanged')
        publisher.sign.assert_not_called()

    def test_source_allowlist_and_sanitized_defaults(self):
        files = publisher.source_files()
        self.assertNotEqual(files['include/Config.h'], (ROOT/'include/Config.h').read_bytes())
        self.assertIn(b'PDKS_LOCAL_CREDENTIALS_CONFIGURED', files['include/Config.h'])
        self.assertIn(b'default_envs = nodemcuv2_sd_soft_tx_public', files['platformio.ini'])
        self.assertNotIn(b'sd_soft_tx_reset]', files['platformio.ini'])
        for name in files:
            self.assertFalse(name.startswith(('release/', '.pio/', '.venv/', '.platformio/')))
            self.assertNotIn('Secrets.h', name)
            self.assertNotEqual(name, 'docs/DOGRULAMA.md')
        publisher.audit(files)
        with self.assertRaises(RuntimeError):
            publisher.audit({'include/Secrets.h': b'private'})


if __name__ == '__main__':
    unittest.main()
