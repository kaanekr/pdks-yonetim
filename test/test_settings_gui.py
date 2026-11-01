"""Connection persistence UI checks; fake vault and no device/network."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.gui import Manager
from windows_app.settings import ConnectionSettings, SettingsError


class FakeVault:
    def __init__(self):
        self.item = None

    def read(self, binding):
        return self.item[1] if self.item and self.item[0] == binding else ''

    def write(self, binding, password):
        self.item = binding, password

    def delete(self):
        self.item = None


class SettingsGuiTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=r'C:\Users\PC\AppData\Local\Temp\opencode')
        self.vault = FakeVault()
        self.store = ConnectionSettings(Path(self.folder.name)/'connection.json', self.vault)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Manager(self.root, settings=self.store)

    def tearDown(self):
        if not self.app.closing:
            # No extra saving or dialogs during test cleanup.
            self.app.demo = True
            self.app.close()
        self.folder.cleanup()

    def fill(self):
        self.app.address.set('192.168.1.25')
        self.app.username.set('operator')
        self.app.password.set('test-only-secret')

    def test_close_reopen_no_automatic_connection(self):
        self.fill()
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch('windows_app.gui.DeviceClient') as client:
            self.app = Manager(self.root, settings=self.store)
            client.assert_not_called()
        self.assertEqual(self.app.address.get(), 'http://192.168.1.25')
        self.assertEqual(self.app.username.get(), 'operator')
        self.assertEqual(self.app.password.get(), 'test-only-secret')
        self.assertIsNone(self.app.client)
        self.assertIsNone(self.app.state)

    def test_device_or_user_change_clears_remembered_secret(self):
        for field in ('address', 'username'):
            self.fill()
            self.assertTrue(self.app._save_connection())
            getattr(self.app, field).set('different')
            self.assertEqual(self.app.password.get(), '')

    def test_typed_new_secret_is_not_old_device_secret(self):
        self.fill()
        self.app._save_connection()
        self.app.password.set('new-explicit-secret')
        self.app.address.set('192.168.1.26')
        self.assertEqual(self.app.password.get(), 'new-explicit-secret')

    def test_optout_deletes_immediately(self):
        self.fill()
        self.app._save_connection()
        self.app.remember.set(False)
        self.app._remember_changed()
        self.assertIsNone(self.vault.item)
        self.assertFalse(self.store.load()['remember'])
        self.assertEqual(self.store.load()['password'], '')

    def test_busy_fields_locked(self):
        self.app.busy = True
        self.app._enable()
        self.assertTrue(all(str(entry['state']) == 'disabled' for entry in self.app.connection_entries))
        self.assertEqual(str(self.app.remember_check['state']), 'disabled')

    def test_connect_saves_profile_without_automatic_write_to_device(self):
        self.fill()
        with patch.object(self.app, 'job') as job:
            self.app.connect()
        job.assert_called_once()
        self.assertEqual(self.store.load()['password'], 'test-only-secret')
        self.assertEqual(self.store.load()['username'], 'operator')

    def test_save_failure_can_cancel_close(self):
        with patch.object(self.store, 'save', side_effect=SettingsError('test failure')), \
                patch('windows_app.gui.messagebox.showwarning') as warning, \
                patch('windows_app.gui.messagebox.askyesno', return_value=False):
            self.app.close()
        warning.assert_called_once()
        self.assertFalse(self.app.closing)

    def test_corrupt_settings_warn_without_connecting(self):
        self.store.path.write_text('{bad', encoding='utf-8')
        self.app.demo = True
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch('windows_app.gui.messagebox.showwarning') as warning, patch('windows_app.gui.DeviceClient') as client:
            self.app = Manager(self.root, settings=self.store)
        warning.assert_called_once()
        client.assert_not_called()
        self.assertEqual(self.app.password.get(), '')

    def test_demo_never_constructs_or_calls_settings(self):
        self.app.demo = True
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch('windows_app.gui.ConnectionSettings') as constructor:
            self.app = Manager(self.root, demo=True)
            self.app.connect()
            self.app.close()
        constructor.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
