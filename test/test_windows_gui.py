"""Native Tk confirmation/cancellation tests in DEMO; no device/network."""
from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.gui import Manager


class GuiTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Manager(self.root, demo=True)

    def tearDown(self):
        self.app.close()

    def test_cancel_recovery_confirmation_never_starts_job(self):
        with patch('windows_app.gui.simpledialog.askstring', return_value=None), patch.object(self.app, 'job') as job:
            self.app.set_time(True)
            job.assert_not_called()
        self.assertEqual(self.app.client.state['sequence'], 1)

    def test_cancel_backup_never_starts_recovery(self):
        with patch('windows_app.gui.simpledialog.askstring', return_value='KURTAR'), patch('windows_app.gui.filedialog.asksaveasfilename', return_value=''), patch.object(self.app, 'job') as job:
            self.app.set_time(True)
            job.assert_not_called()

    def test_backup_write_failure_prevents_recovery_post(self):
        def run(work, done):
            with self.assertRaises(PermissionError):
                work()
        with patch('windows_app.gui.simpledialog.askstring', return_value='KURTAR'), patch('windows_app.gui.filedialog.asksaveasfilename', return_value='fixture.csv'), patch('windows_app.gui.Path.write_bytes', side_effect=PermissionError()), patch.object(self.app, 'job', side_effect=run), patch.object(self.app.client, 'set_time') as change:
            self.app.set_time(True)
            change.assert_not_called()

    def test_normal_time_cancel_never_starts_job(self):
        with patch('windows_app.gui.messagebox.askyesno', return_value=False), patch.object(self.app, 'job') as job:
            self.app.set_time(False)
            job.assert_not_called()

    def test_cancel_personnel_edit_does_not_write(self):
        self.app.people.selection_set('11223344')
        self.app.on_select()
        self.app.name.set('Yeni ad')
        with patch('windows_app.gui.messagebox.askyesno', return_value=False), patch.object(self.app, 'job') as job:
            self.app.save_person()
            job.assert_not_called()
        self.assertEqual(self.app.client.state['employees'][0]['name'], 'Deneme Personeli')

    def test_legacy_recovery_reports_requirement_without_job(self):
        self.app.state.pop('clock_recovery')
        with patch('windows_app.gui.messagebox.showerror') as error, patch.object(self.app, 'job') as job:
            self.app.set_time(True)
            error.assert_called_once()
            job.assert_not_called()

    def prepare_manual(self):
        self.app.client.state.update(clock_ok=True,now=1800000000)
        self.app.client.state['employees'][0]['inside']=False
        self.app._apply_status(self.app.client.status())
        self.app.manual_start.set('2026-10-31 09:00:00')
        self.app.manual_end.set('2026-10-31 10:00:00')

    def test_manual_cancel_does_not_start_job(self):
        self.prepare_manual()
        with patch('windows_app.gui.messagebox.askyesno',return_value=False),patch.object(self.app,'job') as job:
            self.app.add_manual()
            job.assert_not_called()
        self.assertEqual(self.app.client.state['sequence'],1)

    def test_manual_bad_range_does_not_start_job(self):
        self.prepare_manual()
        self.app.manual_end.set('2026-10-31 08:00:00')
        with patch('windows_app.gui.messagebox.showerror') as error,patch.object(self.app,'job') as job:
            self.app.add_manual()
            error.assert_called_once()
            job.assert_not_called()

    def test_legacy_manual_button_is_disabled(self):
        self.prepare_manual()
        self.app.state.pop('manual_attendance')
        self.app._enable()
        self.assertEqual(str(self.app.manual_button['state']),'disabled')


if __name__ == '__main__':
    unittest.main(verbosity=2)
