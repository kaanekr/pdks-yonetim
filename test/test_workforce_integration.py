"""End-to-end local reporting + main window integration. No real device/profile."""
import copy
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.gui import Manager
from windows_app.workforce_model import WorkforceError, default_config
from windows_app.workforce_smoke import sample_snapshot, run_workforce_smoke
from windows_app.workforce_store import WorkforceStore, verify_backup

TEMP = r'C:\Users\PC\AppData\Local\Temp\opencode'


class MainIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Manager(self.root, demo=True)
        self.status, self.data, self.config = sample_snapshot()

    def tearDown(self):
        if not self.app.closing:
            self.app.planning_panel._config = self.app.planning_panel.saved_config() or default_config()
            self.app.close()

    def snapshot(self):
        self.app.archive.save_config('DEMO', self.config)
        self.app._apply_snapshot((self.status, self.data))

    def test_all_panels_are_packed_and_no_automatic_network(self):
        self.assertEqual(self.app.planning_panel.winfo_manager(), 'pack')
        self.assertEqual(self.app.reports_panel.winfo_manager(), 'pack')
        self.assertIsNone(self.app.archive.path)
        self.assertFalse(self.app.auto_refresh.get())
        self.assertEqual(self.app.archive.list_sources(), [])

    def test_full_u32_roster_and_pipeline_cache(self):
        self.snapshot()
        saved = self.app.archive.load_snapshot('DEMO')
        self.assertEqual(saved['status']['roster_version'], 0xDEADBEEF)
        self.assertNotIn('csrf', saved['status'])
        self.assertEqual(saved['csv'], self.data)
        self.assertTrue(self.app.planning_panel.enabled)
        self.assertEqual(self.app.archive_warning.get(), '')
        self.assertEqual(len(self.app.dashboard_tree.get_children()), 2)

    def test_refresh_preserves_staged_plan(self):
        self.snapshot()
        plan = self.app.planning_panel
        plan._config['holidays']['2026-12-31'] = 'Taslak'
        self.app._apply_snapshot((self.status, self.data))
        self.assertTrue(plan.has_unsaved_changes())
        self.assertEqual(plan.config['holidays']['2026-12-31'], 'Taslak')
        self.assertNotIn('2026-12-31', self.app.archive.load_config('DEMO')['holidays'])

    def test_auto_refresh_is_opt_in_and_read_only(self):
        self.snapshot()
        self.app.next_refresh = time.monotonic() - 1
        with patch.object(self.app, 'refresh') as refresh:
            self.app._live_tick()
            refresh.assert_not_called()
            self.app.auto_refresh.set(True)
            self.app._live_tick()
            refresh.assert_called_once_with(quiet=True)

    def test_consistent_get_only_snapshot(self):
        client = Mock()
        client.status.return_value = self.status
        client.events.return_value = self.data
        state, data = Manager._read_snapshot(client)
        self.assertEqual(state['sequence'], 6)
        self.assertEqual(data, self.data)
        self.assertEqual(client.status.call_count, 2)
        self.assertEqual(client.events.call_count, 1)
        self.assertEqual([call[0] for call in client.method_calls], ['status', 'events', 'status'])

    def test_read_race_bounded_retry_never_post(self):
        client = Mock()
        client.status.side_effect = [dict(self.status, sequence=5), self.status, self.status, self.status]
        client.events.return_value = self.data
        Manager._read_snapshot(client)
        self.assertEqual(client.events.call_count, 2)
        self.assertTrue(all(call[0] in ('status', 'events') for call in client.method_calls))
        client.status.side_effect = [dict(self.status, sequence=5), self.status] * 2
        with self.assertRaisesRegex(Exception, 'tutarlı'):
            Manager._read_snapshot(client)

    def test_bad_prefix_keeps_previous_archive(self):
        self.snapshot()
        altered = self.data.replace(b',11223344,I,0', b',55AA7788,I,0', 1)
        self.app._apply_snapshot((self.status, altered))
        self.assertIn('önceki arşiv korundu', self.app.archive_warning.get())
        self.assertEqual(self.app.archive.load_snapshot('DEMO')['csv'], self.data)

    def test_offline_selection_does_not_replace_live_client(self):
        self.snapshot()
        source = 'http://192.0.2.2'
        self.app.archive.save_snapshot(source, self.status, self.data)
        client, state = self.app.client, copy.deepcopy(self.app.state)
        with patch('socket.socket', side_effect=AssertionError('network')):
            self.app.reports_panel.select_source(source)
        self.assertIs(self.app.client, client)
        self.assertEqual(self.app.state, state)
        self.assertEqual(self.app.planning_panel.source, source)

    def test_unsaved_close_can_be_cancelled(self):
        self.snapshot()
        self.app.planning_panel._config['holidays']['2026-12-31'] = 'Taslak'
        with patch('windows_app.gui.messagebox.askyesno', return_value=False):
            self.app.close()
        self.assertFalse(self.app.closing)

    def test_department_assignment_is_atomic_local_draft(self):
        self.snapshot()
        plan = self.app.planning_panel
        plan.assign_department.set('Depo')
        plan.choose_shift('day')
        plan.assign_from.set('2027-01-01')
        plan.assign_to.set('2027-12-31')
        # Existing B assignment is open-ended: conflicting batch must not alter anything.
        before = plan.config
        with patch('windows_app.workforce_planning.messagebox.showerror'):
            self.assertFalse(plan.add_department_assignment())
        self.assertEqual(plan.config, before)

    def test_department_assignment_success_still_needs_save(self):
        self.snapshot()
        plan = self.app.planning_panel
        for item in plan._config['assignments']:
            if item['uid'] == '55AA7788':
                item['effective_to'] = '2026-12-31'
        plan.assign_department.set('Depo')
        plan.choose_shift('day')
        plan.assign_from.set('2027-01-01')
        plan.assign_to.set('2027-12-31')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(plan.add_department_assignment())
        self.assertEqual(len(plan.config['assignments']), 4)
        self.assertEqual(len(self.app.archive.load_config('DEMO')['assignments']), 3)
        self.assertTrue(plan.has_unsaved_changes())
        self.assertEqual(self.app.state['sequence'], 6)

    def test_cancel_source_switch_preserves_plan_and_report_source(self):
        self.snapshot()
        self.app.archive.save_snapshot('http://192.0.2.2', self.status, self.data)
        self.app.planning_panel._config['holidays']['2026-12-31'] = 'Taslak'
        with patch('windows_app.gui.messagebox.askyesno', return_value=False):
            self.app.reports_panel.select_source('http://192.0.2.2')
        self.assertEqual(self.app.reports_panel.source, 'DEMO')
        self.assertEqual(self.app.planning_panel.source, 'DEMO')
        self.assertTrue(self.app.planning_panel.has_unsaved_changes())


class DurablePipelineTest(unittest.TestCase):
    def test_reopen_exports_backup_smoke(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as folder:
            report = Path(folder)/'smoke.json'
            result = run_workforce_smoke(report)
            self.assertTrue(result['ok'])
            self.assertTrue(result['archive_reopened'])
            self.assertTrue(report.with_suffix('.pdf').is_file())
            self.assertTrue(report.with_suffix('.xlsx').is_file())

    def test_backup_cannot_overwrite_database_and_saved_hash_checked(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as folder:
            path = Path(folder)/'workforce.sqlite3'
            with WorkforceStore(path) as store:
                status, data, config = sample_snapshot()
                store.save_snapshot('DEMO', status, data)
                with self.assertRaises(WorkforceError):
                    store.backup(path)
                self.assertEqual(store.load_snapshot('DEMO')['csv'], data)
                store._conn.execute("UPDATE snapshots SET csv_sha256 = 'tampered'")
                store._conn.commit()
                with self.assertRaises(WorkforceError):
                    store.load_snapshot('DEMO')


if __name__ == '__main__':
    unittest.main(verbosity=2)
