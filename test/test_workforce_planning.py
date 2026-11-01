"""Local planning panel tests with a fake store; withdrawn Tk, no device/network/files."""
import copy
from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.workforce_model import default_config
from windows_app.workforce_planning import LEAVE_KINDS, PlanningPanel

EMPLOYEES = [{'uid': '11223344', 'name': 'Ali Veli'},
             {'uid': 'AABBCCDD', 'name': 'Ayşe Yılmaz'}]
SHIFT = {'id': 's1', 'name': 'Gündüz', 'start': '09:00', 'end': '18:00',
         'weekdays': [0, 1, 2, 3, 4], 'break_minutes': 0, 'break_after_minutes': 0,
         'late_tolerance': 0, 'early_tolerance': 0, 'overtime_after_minutes': 0}
ASSIGNMENT = {'uid': '11223344', 'shift_id': 's1', 'effective_from': '2026-01-01',
              'effective_to': ''}


class FakeStore:
    def __init__(self, config=None):
        self.config = copy.deepcopy(config or default_config())
        self.loaded = []
        self.saved = []
        self.fail_load = None
        self.fail_save = None

    def load_config(self, source):
        self.loaded.append(source)
        if self.fail_load is not None:
            raise self.fail_load
        return copy.deepcopy(self.config)

    def save_config(self, source, config):
        if self.fail_save is not None:
            raise self.fail_save
        self.saved.append((source, copy.deepcopy(config)))
        self.config = copy.deepcopy(config)
        return {'ok': True}


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.changes = []
        self.store = FakeStore()

    def tearDown(self):
        self.root.destroy()

    def make(self, status=None):
        panel = PlanningPanel(self.root, self.store, on_change=lambda: self.changes.append(1))
        panel.pack(fill='both', expand=True)
        panel.set_source('demo', status or {'employees': EMPLOYEES})
        return panel

    def add_shift(self, panel, **values):
        panel.shift_name.set(values.get('name', 'Gece'))
        panel.shift_start.set(values.get('start', '22:00'))
        panel.shift_end.set(values.get('end', '06:00'))
        for index, variable in enumerate(panel.shift_days):
            variable.set(values.get('days', [0, 1, 2, 3, 4]).count(index) == 1)
        panel.shift_break_minutes.set(str(values.get('break_minutes', 30)))
        panel.shift_break_after_minutes.set(str(values.get('break_after_minutes', 15)))
        panel.shift_late_tolerance.set(str(values.get('late_tolerance', 5)))
        panel.shift_early_tolerance.set(str(values.get('early_tolerance', 5)))
        panel.shift_overtime_after_minutes.set(str(values.get('overtime_after_minutes', 0)))
        return panel.add_shift()

    def prepared(self, config=None):
        store = self.store if config is None else FakeStore(config)
        panel = PlanningPanel(self.root, store, on_change=lambda: self.changes.append(1))
        panel.pack(fill='both', expand=True)
        panel.set_source('demo', {'employees': EMPLOYEES})
        return panel, store

    def test_no_source_is_disabled_and_never_saves(self):
        panel = PlanningPanel(self.root, self.store, on_change=lambda: self.changes.append(1))
        self.assertFalse(panel.enabled)
        self.assertIn('Kaynak seçilmedi', panel.status_var.get())
        with patch('windows_app.workforce_planning.messagebox.showwarning') as warning, \
                patch.object(self.store, 'save_config') as save:
            self.assertFalse(panel.save_shifts())
            save.assert_not_called()
        warning.assert_called_once()

    def test_invalid_load_disables_panel_in_turkish(self):
        self.store.fail_load = RuntimeError('bozuk ayar')
        panel = PlanningPanel(self.root, self.store)
        self.assertFalse(panel.set_source('demo', {'employees': EMPLOYEES}))
        self.assertFalse(panel.enabled)
        self.assertIn('Yerel ayarlar yüklenemedi', panel.status_var.get())

    def test_shift_overnight_add_defaults_and_save(self):
        panel = self.make()
        self.assertTrue(self.add_shift(panel))
        shift = panel.current_config()['shifts'][0]
        self.assertEqual((shift['start'], shift['end']), ('22:00', '06:00'))
        self.assertEqual(shift['weekdays'], [0, 1, 2, 3, 4])
        self.assertTrue(len(shift['id']) <= 64 and shift['id'])
        self.assertFalse(shift['overtime_after_minutes'])
        self.assertTrue(panel.config['shifts'])
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.save_shifts())
        self.assertEqual(self.store.saved[-1][0], 'demo')
        self.assertEqual(self.store.config['shifts'][0]['name'], 'Gece')
        self.assertEqual(len(self.changes), 1)

    def test_same_start_end_rejected_without_candidate_change(self):
        panel = self.make()
        with patch('windows_app.workforce_planning.messagebox.showerror') as error:
            self.assertFalse(self.add_shift(panel, start='08:00', end='08:00'))
        self.assertEqual(panel.current_config(), default_config())
        error.assert_called_once()

    def test_no_weekday_and_bad_minutes_rejected(self):
        panel = self.make()
        with patch('windows_app.workforce_planning.messagebox.showerror') as error:
            self.assertFalse(self.add_shift(panel, days=[]))
            self.assertFalse(self.add_shift(panel, break_minutes='x'))
        self.assertEqual(panel.current_config()['shifts'], [])
        self.assertEqual(error.call_count, 2)

    def test_referenced_shift_cannot_be_deleted(self):
        config = default_config()
        config['shifts'].append(copy.deepcopy(SHIFT))
        config['assignments'].append(copy.deepcopy(ASSIGNMENT))
        panel, store = self.prepared(config)
        self.assertTrue(panel.select_shift('s1'))
        with patch('windows_app.workforce_planning.messagebox.showerror') as error:
            self.assertFalse(panel.delete_shift())
        self.assertEqual(len(panel.current_config()['shifts']), 1)
        self.assertEqual(store.saved, [])
        error.assert_called_once()

    def test_assignment_wildcard_individual_and_overlap(self):
        panel = self.make()
        self.assertTrue(self.add_shift(panel))
        shift_id = panel.current_config()['shifts'][0]['id']
        self.assertTrue(panel.choose_shift(shift_id))
        self.assertIn('*', panel.assign_person_combo['values'])
        panel.assign_person.set('*')
        panel.assign_from.set('2026-01-01')
        panel.assign_to.set('')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.add_assignment())
        panel.assign_person.set('11223344')
        panel.assign_from.set('2026-01-01')
        panel.assign_to.set('2026-01-31')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.add_assignment())
        assignments = panel.current_config()['assignments']
        self.assertEqual({item['uid'] for item in assignments}, {'*', '11223344'})
        panel.assign_from.set('2026-01-15')
        panel.assign_to.set('2026-02-15')
        with patch('windows_app.workforce_planning.messagebox.showerror') as error:
            self.assertFalse(panel.add_assignment())
        self.assertEqual(len(panel.current_config()['assignments']), 2)
        error.assert_called_once()

    def test_assignment_remove_requires_confirmation(self):
        config = default_config()
        config['shifts'].append(copy.deepcopy(SHIFT))
        config['assignments'].append(copy.deepcopy(ASSIGNMENT))
        panel, _store = self.prepared(config)
        panel.assignment_list.selection_set('0')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=False):
            self.assertFalse(panel.remove_assignment())
        self.assertEqual(len(panel.current_config()['assignments']), 1)
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.remove_assignment())
        self.assertEqual(panel.current_config()['assignments'], [])

    def prepare_leave(self, panel, kind='Rapor'):
        panel.leave_person.set('11223344')
        panel.leave_start.set('2026-03-02')
        panel.leave_end.set('2026-03-03')
        panel.leave_kind.set(kind)

    def test_leave_and_holiday_add_remove_with_confirmation(self):
        panel = self.make()
        self.prepare_leave(panel, 'Geçersiz tür')
        with patch('windows_app.workforce_planning.messagebox.showerror') as error:
            self.assertFalse(panel.add_leave())
        error.assert_called_once()
        self.prepare_leave(panel)
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.add_leave())
        self.assertEqual(panel.current_config()['leaves'][0]['kind'], 'Rapor')
        panel.leave_list.selection_set('0')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=False):
            self.assertFalse(panel.remove_leave())
        self.assertEqual(len(panel.current_config()['leaves']), 1)
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.remove_leave())
        self.assertEqual(panel.current_config()['leaves'], [])

        panel.holiday_date.set('2026-01-01')
        panel.holiday_name.set('Yılbaşı')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.add_holiday())
        with patch('windows_app.workforce_planning.messagebox.showerror') as error:
            self.assertFalse(panel.add_holiday())
        error.assert_called_once()
        panel.holiday_list.selection_set('2026-01-01')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=False):
            self.assertFalse(panel.remove_holiday())
        self.assertIn('2026-01-01', panel.current_config()['holidays'])
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.remove_holiday())
        self.assertNotIn('2026-01-01', panel.current_config()['holidays'])

    def test_person_metadata_saves_locally_only(self):
        panel = self.make()
        panel.meta_person.set('AABBCCDD')
        panel.meta_department.set('Muhasebe')
        panel.meta_employee_no.set('42')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True):
            self.assertTrue(panel.save_person_metadata())
        self.assertEqual(self.store.config['people']['AABBCCDD'],
                         {'department': 'Muhasebe', 'employee_no': '42'})
        self.assertEqual(len(self.changes), 1)
        panel.meta_employee_no.set('9' * 65)
        with patch('windows_app.workforce_planning.messagebox.showerror') as error:
            self.assertFalse(panel.save_person_metadata())
        error.assert_called_once()
        self.assertEqual(self.store.config['people']['AABBCCDD']['employee_no'], '42')

    def test_cancel_save_makes_no_write_or_callback(self):
        panel = self.make()
        self.assertTrue(self.add_shift(panel))
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=False):
            self.assertFalse(panel.save_shifts())
        self.assertEqual(self.store.saved, [])
        self.assertEqual(self.changes, [])
        self.assertEqual(panel.saved_config(), default_config())
        self.assertEqual(len(panel.current_config()['shifts']), 1)

    def test_storage_failure_preserves_previous_policy(self):
        panel = self.make()
        self.assertTrue(self.add_shift(panel))
        self.store.fail_save = OSError('disk yazılamadı')
        with patch('windows_app.workforce_planning.messagebox.askyesno', return_value=True), \
                patch('windows_app.workforce_planning.messagebox.showerror') as error:
            self.assertFalse(panel.save_shifts())
        self.assertEqual(self.store.saved, [])
        self.assertEqual(panel.saved_config(), default_config())
        self.assertEqual(self.changes, [])
        self.assertIn('korunuyor', panel.status_var.get())
        error.assert_called_once()

    def test_loaded_config_is_detached_and_source_switch_reloads(self):
        config = default_config()
        config['shifts'].append(copy.deepcopy(SHIFT))
        config['people']['AABBCCDD'] = {'department': 'Depo', 'employee_no': '7'}
        panel, store = self.prepared(config)
        self.assertIn('AABBCCDD', panel.meta_person_combo['values'])
        exposed = panel.current_config()
        exposed['shifts'].clear()
        self.assertEqual(len(panel.current_config()['shifts']), 1)
        self.assertTrue(panel.set_source('demo2'))
        panel.set_source(None)
        self.assertFalse(panel.enabled)
        self.assertEqual(store.saved, [])

    def test_leave_kinds_are_the_documented_set(self):
        self.assertEqual(LEAVE_KINDS, ('Yıllık izin', 'Rapor', 'Ücretsiz izin', 'Diğer'))


if __name__ == '__main__':
    unittest.main()
