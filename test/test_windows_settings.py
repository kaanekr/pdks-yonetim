"""Remembered Windows connection settings; fake vault, disposable files, no device."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.client import normalize_address
from windows_app.settings import ConnectionSettings, SettingsError

BASE_TEMP = Path(r'C:\Users\PC\AppData\Local\Temp\opencode')
DEFAULT_PROFILE = dict(address='192.168.4.1', username='admin', remember=True, password='')
JSON_KEYS = {'version', 'address', 'username', 'remember'}


class FakeVault:
    """In-memory Credential Manager stand-in with one credential slot per target.

    The real vault keeps a single credential whose UserName is the binding, so a
    write replaces the previous profile regardless of binding.
    """

    def __init__(self):
        self.binding = None
        self.password = ''
        self.reads = []
        self.writes = []
        self.deletes = 0
        self.fail_read = None
        self.fail_write = None
        self.fail_delete = None

    def read(self, binding):
        self.reads.append(binding)
        if self.fail_read is not None:
            raise self.fail_read('vault read failed')
        return self.password if binding == self.binding else ''

    def peek(self, binding):
        return self.password if binding == self.binding else ''

    def write(self, binding, password):
        self.writes.append((binding, password))
        if self.fail_write is not None:
            raise self.fail_write('vault write failed')
        self.binding, self.password = binding, password

    def delete(self):
        self.deletes += 1
        if self.fail_delete is not None:
            raise self.fail_delete('vault delete failed')
        self.binding, self.password = None, ''

    def seed(self, binding, password):
        self.binding, self.password = binding, password


class SettingsTest(unittest.TestCase):
    def setUp(self):
        BASE_TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='umt-settings-', dir=str(BASE_TEMP))
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.path = self.dir/'connection.json'
        self.vault = FakeVault()

    def settings(self, path=None):
        return ConnectionSettings(path or self.path, self.vault)

    def write_raw(self, text, path=None):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def write_config(self, path=None, **overrides):
        data = dict(version=1, address='192.168.4.1', username='admin', remember=False)
        data.update(overrides)
        self.write_raw(json.dumps(data), path)

    def saved_json(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def assert_no_temp_leftovers(self):
        leftovers = [p.name for p in self.dir.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [])

    def test_missing_config_returns_defaults_without_vault_read(self):
        for path in (self.path, self.dir/'missing'/'connection.json'):
            with self.subTest(path=str(path)):
                self.assertEqual(self.settings(path).load(), DEFAULT_PROFILE)
                self.assertEqual(self.vault.reads, [])
                self.assertEqual(self.vault.writes, [])

    def test_save_persists_only_nonsecret_json_and_vault_secret(self):
        self.settings().save('192.168.4.1', 'admin', 's3cret-pass', True)
        data = self.saved_json()
        self.assertEqual(set(data), JSON_KEYS)
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['address'], 'http://192.168.4.1')
        self.assertEqual(data['username'], 'admin')
        self.assertIs(data['remember'], True)
        raw = self.path.read_text(encoding='utf-8')
        self.assertNotIn('s3cret-pass', raw)
        for banned in ('password', 'secret', 'csrf', 'token', 'authorization'):
            self.assertNotIn(banned, raw.lower())
        self.assertEqual(len(self.vault.writes), 1)
        self.assertEqual(self.vault.writes[0][1], 's3cret-pass')
        self.assert_no_temp_leftovers()

    def test_roundtrip_across_instances_reuses_same_binding(self):
        self.settings().save('192.168.4.1', 'admin', 's3cret-pass', True)
        loaded = self.settings().load()
        self.assertEqual(set(loaded), set(DEFAULT_PROFILE))
        self.assertEqual(loaded, dict(address='http://192.168.4.1', username='admin',
                                      remember=True, password='s3cret-pass'))
        self.assertEqual(self.vault.reads[-1], self.vault.writes[-1][0])

    def test_load_optout_never_reads_vault(self):
        self.write_config(remember=False)
        self.vault.seed('ignored-binding', 'must-not-be-used')
        loaded = self.settings().load()
        self.assertEqual(loaded, dict(address='http://192.168.4.1', username='admin',
                                      remember=False, password=''))
        self.assertEqual(self.vault.reads, [])

    def test_save_optout_deletes_saved_secret_without_storing_password(self):
        first = self.settings()
        first.save('192.168.4.1', 'admin', 's3cret-pass', True)
        binding = self.vault.writes[-1][0]
        first.save('192.168.4.1', 'admin', 's3cret-pass', False)
        self.assertFalse(self.saved_json()['remember'])
        self.assertIsNone(self.vault.binding)
        self.assertEqual(self.vault.password, '')
        self.assertGreaterEqual(self.vault.deletes, 1)
        self.assertEqual(self.vault.peek(binding), '')
        reads_before = len(self.vault.reads)
        loaded = self.settings().load()
        self.assertEqual(loaded['password'], '')
        self.assertEqual(len(self.vault.reads), reads_before)
        self.assertEqual([pw for _, pw in self.vault.writes], ['s3cret-pass'])

    def test_empty_password_deletes_saved_secret_without_writing(self):
        first = self.settings()
        first.save('192.168.4.1', 'admin', 's3cret-pass', True)
        binding = self.vault.writes[-1][0]
        first.save('192.168.4.1', 'admin', '', True)
        self.assertIsNone(self.vault.binding)
        self.assertEqual(self.vault.password, '')
        self.assertGreaterEqual(self.vault.deletes, 1)
        self.assertEqual(self.vault.peek(binding), '')
        self.assertNotIn('', [pw for _, pw in self.vault.writes])
        loaded = self.settings().load()
        self.assertTrue(loaded['remember'])
        self.assertEqual(loaded['password'], '')

    def test_switching_username_changes_binding_and_invalidates_old_secret(self):
        first = self.settings()
        first.save('192.168.4.1', 'admin', 'admin-secret', True)
        old_binding = self.vault.writes[-1][0]
        first.save('192.168.4.1', 'operator', 'operator-secret', True)
        new_binding = self.vault.writes[-1][0]
        self.assertNotEqual(old_binding, new_binding)
        self.assertEqual(self.vault.binding, new_binding)
        self.assertEqual(self.vault.password, 'operator-secret')
        self.assertEqual(self.vault.peek(old_binding), '')
        loaded = self.settings().load()
        self.assertEqual((loaded['username'], loaded['password']), ('operator', 'operator-secret'))
        self.assertEqual(self.vault.reads[-1], new_binding)

    def test_switching_address_changes_binding_and_invalidates_old_secret(self):
        first = self.settings()
        first.save('192.168.4.1', 'admin', 'first-secret', True)
        old_binding = self.vault.writes[-1][0]
        first.save('192.168.5.1', 'admin', 'second-secret', True)
        new_binding = self.vault.writes[-1][0]
        self.assertNotEqual(old_binding, new_binding)
        self.assertEqual(self.vault.binding, new_binding)
        self.assertEqual(self.vault.password, 'second-secret')
        self.assertEqual(self.vault.peek(old_binding), '')
        loaded = self.settings().load()
        self.assertEqual(loaded['address'], 'http://192.168.5.1')
        self.assertEqual(loaded['password'], 'second-secret')

    def test_equivalent_address_spellings_share_one_binding(self):
        first = self.settings()
        first.save('192.168.4.1', 'admin', 'first-secret', True)
        first_binding = self.vault.writes[-1][0]
        first.save('http://192.168.4.1', 'admin', 'second-secret', True)
        second_binding = self.vault.writes[-1][0]
        self.assertEqual(first_binding, second_binding)
        self.assertEqual(self.vault.binding, first_binding)
        self.assertEqual(self.vault.password, 'second-secret')
        self.assertEqual(self.settings().load()['password'], 'second-secret')

    def test_load_password_only_for_matching_binding(self):
        self.write_config(remember=True)
        self.vault.seed('some-other-binding', 'stale-secret')
        loaded = self.settings().load()
        self.assertEqual(loaded['password'], '')
        self.assertEqual(len(self.vault.reads), 1)
        self.assertEqual(self.vault.peek('some-other-binding'), 'stale-secret')

    def test_load_rejects_invalid_settings_without_vault_read(self):
        cases = {
            'garbage': 'not json at all',
            'list': '[]',
            'null': 'null',
            'empty-object': '{}',
            'version-2': dict(version=2, address='192.168.4.1', username='admin', remember=True),
            'version-bool': dict(version=True, address='192.168.4.1', username='admin', remember=True),
            'version-text': dict(version='1', address='192.168.4.1', username='admin', remember=True),
            'remember-int': dict(version=1, address='192.168.4.1', username='admin', remember=1),
            'remember-text': dict(version=1, address='192.168.4.1', username='admin', remember='yes'),
            'address-int': dict(version=1, address=123, username='admin', remember=True),
            'address-file-url': dict(version=1, address='file:///etc/passwd', username='admin', remember=True),
            'address-credentials': dict(version=1, address='http://user:pass@localhost', username='admin', remember=True),
            'username-int': dict(version=1, address='192.168.4.1', username=123, remember=True),
            'username-empty': dict(version=1, address='192.168.4.1', username='', remember=True),
            'username-colon': dict(version=1, address='192.168.4.1', username='bad:name', remember=True),
            'username-control': dict(version=1, address='192.168.4.1', username='bad\nname', remember=True),
            'optout-bad-address': dict(version=1, address='http://user:pass@localhost',
                                       username='admin', remember=False),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                self.vault.reads = []
                if isinstance(case, dict):
                    self.write_config(**case)
                else:
                    self.write_raw(case)
                with self.assertRaises(SettingsError):
                    self.settings().load()
                self.assertEqual(self.vault.reads, [])

    def test_load_rejects_oversized_config_without_vault_read(self):
        padded = json.dumps(dict(version=1, address='192.168.4.1', username='admin', remember=True))
        self.write_raw(padded + ' ' * 17000)
        with self.assertRaises(SettingsError):
            self.settings().load()
        self.assertEqual(self.vault.reads, [])

    def test_load_vault_failure_raises_settings_error(self):
        for failure in (SettingsError, OSError):
            with self.subTest(failure=failure.__name__):
                self.vault = FakeVault()
                self.vault.fail_read = failure
                self.write_config(remember=True)
                with self.assertRaises(SettingsError):
                    self.settings().load()
                self.assertEqual(len(self.vault.reads), 1)
                self.assertEqual((self.vault.binding, self.vault.password), (None, ''))

    def test_save_vault_failure_raises_settings_error_without_plaintext(self):
        for failure in (SettingsError, OSError):
            with self.subTest(failure=failure.__name__):
                self.vault = FakeVault()
                self.vault.fail_write = failure
                with self.assertRaises(SettingsError):
                    self.settings().save('192.168.4.1', 'admin', 'top-secret-value', True)
                self.assertFalse(self.path.exists())
                self.assert_no_temp_leftovers()
                self.assertEqual((self.vault.binding, self.vault.password), (None, ''))

    def test_save_validation_never_writes_file_or_vault(self):
        cases = {
            'empty-address': ('', 'admin', 'secret', True),
            'file-url': ('file:///etc/passwd', 'admin', 'secret', True),
            'address-credentials': ('http://user:pass@localhost', 'admin', 'secret', True),
            'address-none': (None, 'admin', 'secret', True),
            'address-int': (123, 'admin', 'secret', True),
            'empty-username': ('192.168.4.1', '', 'secret', True),
            'username-colon': ('192.168.4.1', 'bad:name', 'secret', True),
            'username-control': ('192.168.4.1', 'bad\nname', 'secret', True),
            'username-none': ('192.168.4.1', None, 'secret', True),
            'password-control': ('192.168.4.1', 'admin', 'bad\nsecret', True),
            'password-none': ('192.168.4.1', 'admin', None, True),
            'password-too-long': ('192.168.4.1', 'admin', 'x' * 1281, True),
            'remember-int': ('192.168.4.1', 'admin', 'secret', 1),
            'remember-none': ('192.168.4.1', 'admin', 'secret', None),
        }
        for name, args in cases.items():
            with self.subTest(case=name):
                self.vault.writes = []
                self.vault.deletes = 0
                with self.assertRaises(SettingsError):
                    self.settings().save(*args)
                self.assertFalse(self.path.exists())
                self.assertEqual(self.vault.writes, [])
                self.assertEqual(self.vault.deletes, 0)
                self.assert_no_temp_leftovers()

    def test_forget_password_works_with_invalid_settings(self):
        broken = '{broken settings'
        self.write_raw(broken)
        self.vault.seed('stale-binding', 'old-secret')
        self.settings().forget_password()
        self.assertIsNone(self.vault.binding)
        self.assertEqual(self.vault.password, '')
        self.assertEqual(self.vault.deletes, 1)
        self.assertEqual(self.path.read_text(encoding='utf-8'), broken)
        with self.assertRaises(SettingsError):
            self.settings().load()

    def test_forget_password_works_without_config(self):
        self.vault.seed('stale-binding', 'old-secret')
        self.settings().forget_password()
        self.assertIsNone(self.vault.binding)
        self.assertEqual(self.vault.password, '')
        self.assertEqual(self.vault.deletes, 1)
        self.assertEqual(self.settings().load(), DEFAULT_PROFILE)
        self.assertEqual(self.vault.reads, [])

    def test_forget_password_clears_valid_saved_secret(self):
        self.settings().save('192.168.4.1', 'admin', 's3cret-pass', True)
        binding = self.vault.writes[-1][0]
        self.settings().forget_password()
        self.assertIsNone(self.vault.binding)
        self.assertEqual(self.vault.password, '')
        self.assertEqual(self.vault.peek(binding), '')
        loaded = self.settings().load()
        self.assertTrue(loaded['remember'])
        self.assertEqual(loaded['password'], '')
        self.assert_no_temp_leftovers()

    def test_forget_password_failure_raises_settings_error(self):
        self.vault.fail_delete = SettingsError
        with self.assertRaises(SettingsError):
            self.settings().forget_password()
        self.assertEqual(self.vault.deletes, 1)

    def test_save_failure_keeps_previous_profile_intact(self):
        first = self.settings()
        first.save('192.168.4.1', 'admin', 'first-secret', True)
        original = self.path.read_text(encoding='utf-8')
        self.vault.fail_write = SettingsError
        with self.assertRaises(SettingsError):
            first.save('192.168.5.1', 'operator', 'second-secret', True)
        self.vault.fail_write = None
        self.assertEqual(self.path.read_text(encoding='utf-8'), original)
        self.assert_no_temp_leftovers()
        loaded = self.settings().load()
        self.assertEqual(loaded, dict(address='http://192.168.4.1', username='admin',
                                      remember=True, password='first-secret'))

    def test_save_file_error_raises_settings_error_and_cleans_temp(self):
        self.path.mkdir()
        with self.assertRaises(SettingsError):
            self.settings().save('192.168.4.1', 'admin', 's3cret-pass', True)
        self.assertTrue(self.path.is_dir())
        self.assert_no_temp_leftovers()

    def test_replace_failure_never_exposes_new_secret_to_old_device(self):
        store = self.settings()
        store.save('192.168.4.1', 'admin', 'old-secret', True)
        original = self.path.read_bytes()
        with patch('windows_app.settings.os.replace', side_effect=OSError('test replace failure')):
            with self.assertRaises(SettingsError):
                store.save('192.168.5.1', 'operator', 'new-secret', True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(store.load()['password'], '')  # fail closed on mismatched vault binding
        self.assert_no_temp_leftovers()

    def test_load_file_error_raises_settings_error(self):
        self.path.mkdir()
        with self.assertRaises(SettingsError):
            self.settings().load()
        self.assertEqual(self.vault.reads, [])

    def test_non_ascii_profile_roundtrip(self):
        self.settings().save('192.168.4.1', 'Yönetici', 'şifre-ğüş', True)
        loaded = self.settings().load()
        self.assertEqual(loaded['username'], 'Yönetici')
        self.assertEqual(loaded['password'], 'şifre-ğüş')
        self.assertEqual(loaded['address'], normalize_address('192.168.4.1'))
        self.assertNotIn('şifre-ğüş', self.path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
