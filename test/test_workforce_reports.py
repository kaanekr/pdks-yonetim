"""Offline UI tests for ReportsPanel: fake store, mocked engine/export, no device/network."""
import copy
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app import workforce_reports as reports
from windows_app.workforce_model import default_config

UID_A, UID_B = '11111111', '22222222'
SYNCED = '2026-11-01 09:00:00'


def daily_row(**overrides):
    row = dict(date='2026-10-26', name='Ayşe', uid=UID_A, employee_no='1', department='Montaj',
               shift='Gündüz', first_in='08:00', last_out='17:00', card_seconds=32400,
               manual_seconds=0, gross_seconds=32400, break_seconds=3600, net_seconds=28800,
               overtime_seconds=0, late_minutes=0, early_minutes=0, status='Tamam', flags={})
    row.update(overrides)
    return row


class FakeStore:
    def __init__(self):
        self.snapshots = {'arsiv-1': {'source': 'arsiv-1', 'status': {'sequence': 1},
                                      'csv': b'sequence,epoch,uid,event,seconds\n',
                                      'synced_at': SYNCED, 'sequence': 1}}
        self.config = default_config()
        self.calls, self.backups = [], []

    def list_sources(self):
        self.calls.append('list_sources')
        return [{'source': key, 'synced_at': item['synced_at'], 'sequence': item['sequence']}
                for key, item in self.snapshots.items()]

    def load_snapshot(self, source):
        self.calls.append(('load_snapshot', source))
        return copy.deepcopy(self.snapshots.get(source))

    def load_config(self, source):
        self.calls.append(('load_config', source))
        return copy.deepcopy(self.config)

    def backup(self, destination=None):
        self.calls.append(('backup', str(destination)))
        self.backups.append(destination)
        return destination


class ReportsPanelTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.store = FakeStore()
        self.engine_calls, self.writes = [], []
        self.report = {
            'rows': [daily_row(),
                     daily_row(uid=UID_B, name='Mehmet', employee_no='2', department='Sevkiyat',
                               status='Eksik', net_seconds=14400, flags={'late': True})],
            'summary': [
                dict(name='Ayşe', uid=UID_A, department='Montaj', net_seconds=28800, card_seconds=32400,
                     manual_seconds=0, overtime_seconds=0, late_count=0, early_count=0,
                     absent_days=0, exception_days=0, days=1),
                dict(name='Mehmet', uid=UID_B, department='Sevkiyat', net_seconds=14400, card_seconds=14400,
                     manual_seconds=0, overtime_seconds=0, late_count=1, early_count=0,
                     absent_days=0, exception_days=1, days=1)],
            'weekly': [
                dict(week_start='2026-10-26', name='Ayşe', uid=UID_A, net_seconds=28800, overtime_seconds=0),
                dict(week_start='2026-10-26', name='Mehmet', uid=UID_B, net_seconds=14400, overtime_seconds=0)],
            'warnings': [], 'policy_notes': [], 'config_hash': 'a' * 64}

        def build_report(status, csv_data, config, start, end, source='', synced_at=''):
            self.engine_calls.append(dict(status=status, csv=csv_data, config=config, start=start,
                                          end=end, source=source, synced_at=synced_at))
            report = copy.deepcopy(self.report)
            report.update(source=source, synced_at=synced_at, start=start, end=end)
            return report
        self.engine = types.SimpleNamespace(build_report=build_report)
        self.export = types.SimpleNamespace(
            write_xlsx=lambda path, report: self.writes.append(('xlsx', path, report)),
            write_pdf=lambda path, report: self.writes.append(('pdf', path, report)))
        self.error = patch.object(reports.messagebox, 'showerror').start()
        self.info = patch.object(reports.messagebox, 'showinfo').start()
        self.warning = patch.object(reports.messagebox, 'showwarning').start()
        patch.object(reports, '_load_engine', return_value=self.engine).start()
        patch.object(reports, '_load_export', return_value=self.export).start()
        self.addCleanup(patch.stopall)

    def panel(self, store=None):
        events = []
        widget = reports.ReportsPanel(
            self.root, store or self.store,
            on_source_change=lambda source, status: events.append((source, status)))
        return widget, events

    @staticmethod
    def person_key(widget, target_uid):
        return next(key for key, value in widget._people.items() if value == target_uid)

    def test_offline_source_selection_callback_and_sources_without_network(self):
        widget, events = self.panel()
        with patch('socket.socket', side_effect=AssertionError('network used')), \
                patch('socket.create_connection', side_effect=AssertionError('network used')):
            widget.update_sources()
            widget.select_source('arsiv-1')
        self.assertEqual(list(widget.source_combo['values']), ['arsiv-1'])
        self.assertEqual(events, [('arsiv-1', {'sequence': 1})])
        call = self.engine_calls[0]
        self.assertEqual((call['source'], call['synced_at']), ('arsiv-1', SYNCED))
        self.assertEqual(call['start'], widget.start_var.get())
        self.assertEqual(call['end'], widget.end_var.get())
        self.assertEqual(call['config']['version'], 1)
        self.assertEqual(widget.current_report['source'], 'arsiv-1')
        self.assertIn('arsiv-1', widget.meta_var.get())
        self.assertIn(SYNCED, widget.info_var.get())
        self.assertIn('aralık', widget.info_var.get())
        self.assertNotIn('a' * 64, widget.info_var.get())

    def test_invalid_date_shows_error_without_engine(self):
        widget, events = self.panel()
        widget.start_var.set('2026-13-01')
        widget.select_source('arsiv-1')
        self.error.assert_called_once()
        self.assertIn('Tarih', self.error.call_args[0][1])
        self.assertEqual(self.engine_calls, [])
        self.assertIsNone(widget.current_report)
        self.assertEqual(events, [])

    def test_end_before_start_shows_error_without_engine(self):
        widget, _events = self.panel()
        widget.start_var.set('2026-11-02')
        widget.end_var.set('2026-11-01')
        widget.select_source('arsiv-1')
        self.error.assert_called_once()
        self.assertIn('Bitiş tarihi', self.error.call_args[0][1])
        self.assertEqual(self.engine_calls, [])
        self.assertIsNone(widget.current_report)

    def test_person_filter_applies_to_rows_summary_and_weekly(self):
        widget, _events = self.panel()
        widget.select_source('arsiv-1')
        self.assertEqual(len(widget._trees['daily'].get_children()), 2)
        self.assertEqual(len(widget._trees['summary'].get_children()), 2)
        self.assertEqual(len(widget._trees['weekly'].get_children()), 2)
        first = widget._trees['daily'].get_children()[0]
        self.assertEqual(widget._trees['daily'].set(first, 'net_seconds'), '8:00:00')
        self.assertEqual(widget._trees['daily'].set(first, 'first_in'), '08:00')
        widget.person_var.set(self.person_key(widget, UID_B))
        widget._render()
        self.assertEqual({row['uid'] for row in widget._shown['rows']}, {UID_B})
        self.assertEqual({row['uid'] for row in widget._shown['summary']}, {UID_B})
        self.assertEqual({row['uid'] for row in widget._shown['weekly']}, {UID_B})
        for key in ('daily', 'summary', 'weekly'):
            self.assertEqual(len(widget._trees[key].get_children()), 1, key)
            child = widget._trees[key].get_children()[0]
            self.assertEqual(widget._trees[key].set(child, 'uid'), UID_B)

    def test_department_filter_applies_to_rows_summary_and_weekly(self):
        widget, _events = self.panel()
        widget.select_source('arsiv-1')
        widget.dept_var.set('Sevkiyat')
        widget._render()
        self.assertEqual({row['uid'] for row in widget._shown['rows']}, {UID_B})
        self.assertEqual({row['uid'] for row in widget._shown['summary']}, {UID_B})
        self.assertEqual({row['uid'] for row in widget._shown['weekly']}, {UID_B})
        self.assertEqual(len(widget._trees['daily'].get_children()), 1)
        self.assertEqual(len(widget._trees['summary'].get_children()), 1)
        self.assertEqual(len(widget._trees['weekly'].get_children()), 1)

    def test_exceptions_tab_lists_only_flagged_rows(self):
        widget, _events = self.panel()
        widget.select_source('arsiv-1')
        tree = widget._trees['exceptions']
        children = tree.get_children()
        self.assertEqual(len(children), 1)
        self.assertEqual(tree.set(children[0], 'uid'), UID_B)
        self.assertEqual(tree.set(children[0], 'flags'), 'late')
        self.assertNotIn('absent', tree.set(children[0], 'flags'))

    def test_export_cancel_writes_nothing(self):
        widget, _events = self.panel()
        widget.select_source('arsiv-1')
        self.info.reset_mock()
        with patch.object(reports.filedialog, 'asksaveasfilename', return_value=''):
            widget.export_excel()
        self.assertEqual(self.writes, [])
        self.info.assert_not_called()
        self.error.assert_not_called()

    def test_filtered_export_contains_only_selected_person(self):
        widget, _events = self.panel()
        widget.select_source('arsiv-1')
        widget.person_var.set(self.person_key(widget, UID_B))
        widget._render()
        with tempfile.TemporaryDirectory(dir=r'C:\Users\PC\AppData\Local\Temp\opencode') as folder:
            xlsx, pdf = str(Path(folder, 'rapor.xlsx')), str(Path(folder, 'rapor.pdf'))
            with patch.object(reports.filedialog, 'asksaveasfilename', return_value=xlsx):
                widget.export_excel()
            with patch.object(reports.filedialog, 'asksaveasfilename', return_value=pdf):
                widget.export_pdf()
            self.assertFalse(Path(xlsx).exists() and Path(pdf).exists())
        self.assertEqual([kind for kind, _path, _report in self.writes], ['xlsx', 'pdf'])
        for _kind, path, report in self.writes:
            self.assertEqual({row['uid'] for row in report['rows']}, {UID_B})
            self.assertEqual({row['uid'] for row in report['summary']}, {UID_B})
            self.assertEqual({row['uid'] for row in report['weekly']}, {UID_B})

    def test_backup_cancel_does_not_touch_store(self):
        widget, _events = self.panel()
        self.info.reset_mock()
        with patch.object(reports.filedialog, 'asksaveasfilename', return_value=''):
            widget.backup_archive()
        self.assertEqual(self.store.backups, [])
        self.info.assert_not_called()

    def test_backup_zip_shows_not_device_restore_notice(self):
        widget, _events = self.panel()
        folder = tempfile.TemporaryDirectory(dir=r'C:\Users\PC\AppData\Local\Temp\opencode')
        self.addCleanup(folder.cleanup)
        target = str(Path(folder.name, 'yedek.zip'))
        with patch.object(reports.filedialog, 'asksaveasfilename', return_value=target):
            widget.backup_archive()
        self.assertEqual(len(self.store.backups), 1)
        text = self.info.call_args[0][1]
        self.assertIn('tam cihaz geri yükleme', text)
        self.assertIn('değildir', text)

    def test_warnings_visible_and_no_absence_inferred(self):
        self.report['warnings'] = ['Anlık görüntü eski olabilir.']
        widget, _events = self.panel()
        widget.select_source('arsiv-1')
        self.assertIn('eski olabilir', widget.warn_var.get())
        tree = widget._trees['exceptions']
        for child in tree.get_children():
            self.assertNotIn('absent', tree.set(child, 'flags'))
        self.assertNotIn('Devamsız', ' '.join(tree.set(child, 'flags') for child in tree.get_children()))

    def test_set_snapshot_ignores_unrelated_source_and_rebuilds_matching_one(self):
        widget, events = self.panel()
        widget.select_source('arsiv-1')
        self.assertEqual(len(self.engine_calls), 1)
        widget.set_snapshot('canli-1', {'sequence': 2}, b'live', '2026-11-01 10:00:00')
        self.assertEqual(widget.current_report['source'], 'arsiv-1')
        self.assertEqual(len(self.engine_calls), 1)
        fresh, fresh_events = self.panel()
        fresh.set_snapshot('canli-1', {'sequence': 2}, b'live', '2026-11-01 10:00:00')
        self.assertEqual(fresh.current_report['source'], 'canli-1')
        self.assertEqual(self.engine_calls[-1]['source'], 'canli-1')
        self.assertEqual(self.engine_calls[-1]['csv'], b'live')
        self.assertEqual(fresh_events, [])
        self.assertEqual(events, [('arsiv-1', {'sequence': 1})])


if __name__ == '__main__':
    unittest.main(verbosity=2)
