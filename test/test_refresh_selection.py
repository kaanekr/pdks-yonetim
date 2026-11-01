"""Withdrawn Tk regression tests: live refreshes keep selected rows, focus and drafts.

Every test runs the DEMO manager with its in-memory archive only; no real profile,
network request, device or serial port is touched, and rows are located by their
displayed VALUES so internal widget id formats are never assumed.
"""
import copy
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.gui import Manager
from windows_app.workforce_model import default_config
from windows_app.workforce_smoke import sample_snapshot
from windows_app.tree_state import sync_rows

FIRST, SECOND = '11223344', '55AA7788'
ALL = 'Tümü'
REPORT_START, REPORT_END = '2026-10-28', '2026-10-31'


def row_with_values(tree, expected):
    """Locate a row by {column: displayed text}; widget ids and their format are not assumed."""
    for iid in tree.get_children():
        values = tree.item(iid, 'values')
        if all(str(values[column]) == str(text) for column, text in expected.items()):
            return iid
    return None


def values_of(tree, iid):
    return tree.item(iid, 'values') if iid else None


def selected_values(tree):
    selection = tree.selection()
    return values_of(tree, selection[0]) if selection else None


class RefreshSelectionTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Manager(self.root, demo=True)
        self.assertIsNone(self.app.archive.path)  # isolated in-memory archive only
        self.status, self.data, self.config = sample_snapshot()
        self.app.archive.save_config('DEMO', self.config)
        self.app._apply_snapshot((self.status, self.data))
        self.root.update()

    def tearDown(self):
        if not self.app.closing:
            plan = self.app.planning_panel
            plan._config = plan.saved_config() or default_config()  # never prompt on close
            self.app.close()

    def prepare_report(self):
        panel = self.app.reports_panel
        panel.start_var.set(REPORT_START)
        panel.end_var.set(REPORT_END)
        panel.refresh_report()
        self.root.update()
        return panel

    def test_dashboard_live_renders_keep_selected_uid_and_focus(self):
        tree = self.app.dashboard_tree
        row = row_with_values(tree, {1: FIRST})
        self.assertIsNotNone(row)
        tree.selection_set(row)
        tree.focus(row)
        self.root.update()
        self.app.state['employees'][0]['total_seconds'] = 68400
        for _ in range(3):
            self.app._render_dashboard()
            self.root.update()
        self.assertEqual(len(tree.get_children()), 2)  # in-place sync, no duplicate rows
        values = selected_values(tree)
        self.assertIsNotNone(values)
        self.assertEqual(values[1], FIRST)
        self.assertEqual(values[4], '19:00:00')  # values refreshed under the selection
        focus = values_of(tree, tree.focus())
        self.assertIsNotNone(focus)
        self.assertEqual(focus[1], FIRST)

    def test_personnel_refresh_keeps_selection_and_edited_draft(self):
        tree = self.app.people
        row = row_with_values(tree, {1: 'AABBCCDD'})
        self.assertIsNotNone(row)
        tree.selection_set(row)
        self.root.update()
        self.assertEqual(self.app.selected_uid, FIRST)
        self.app.name.set('Ayşe Yılmaz')
        self.app.card.set('DEADBEEF')
        self.app.active.set(False)
        for _ in range(2):
            self.app._apply_snapshot((self.status, self.data))
            self.root.update()
        values = selected_values(tree)
        self.assertIsNotNone(values)
        self.assertEqual(self.app.selected_uid, FIRST)
        self.assertEqual(values[0], 'Ayşe Şahin')  # row still shows the stored snapshot value
        self.assertEqual(self.app.name.get(), 'Ayşe Yılmaz')
        self.assertEqual(self.app.card.get(), 'DEADBEEF')
        self.assertFalse(self.app.active.get())

    def test_events_keep_selected_sequence_row_on_same_snapshot(self):
        tree = self.app.events_tree
        row = row_with_values(tree, {0: '3'})
        self.assertIsNotNone(row)
        tree.selection_set(row)
        tree.focus(row)
        self.root.update()
        for _ in range(2):
            self.app._apply_snapshot((self.status, self.data))
            self.root.update()
        self.assertEqual(len(tree.get_children()), 6)
        values = selected_values(tree)
        self.assertIsNotNone(values)
        self.assertEqual(values[0], '3')
        self.assertEqual(values[2], FIRST)

    def test_report_tabs_keep_selected_semantic_row_across_refresh(self):
        panel = self.prepare_report()
        self.assertTrue(panel._trees['daily'].get_children())
        identities = {'daily': (0, 2), 'summary': (1,), 'weekly': (0, 2), 'exceptions': (0, 2)}
        checked = 0
        for key, columns in identities.items():
            tree = panel._trees[key]
            rows = tree.get_children()
            if not rows:  # a tab may be empty for the current range/policy
                continue
            iid = rows[-1]
            before = tuple(str(tree.item(iid, 'values')[column]) for column in columns)
            tree.selection_set(iid)
            tree.focus(iid)
            self.root.update()
            panel.refresh_report()
            self.root.update()
            values = selected_values(tree)
            self.assertIsNotNone(values, key)
            self.assertEqual(tuple(str(values[column]) for column in columns), before, key)
            checked += 1
        self.assertGreaterEqual(checked, 3)

    def test_report_person_filter_keeps_uid_after_employee_rename(self):
        panel = self.prepare_report()
        label = next(value for value in panel.person_combo['values'] if FIRST in value)
        panel.person_var.set(label)
        panel.refresh_report()
        self.root.update()
        shown = panel._trees['daily'].get_children()
        self.assertTrue(shown)
        self.assertTrue(all(panel._trees['daily'].item(iid, 'values')[2] == FIRST for iid in shown))
        renamed = copy.deepcopy(self.status)
        renamed['employees'][0]['name'] = 'Ayşe Yılmaz'
        self.app._apply_snapshot((renamed, self.data))
        self.root.update()
        panel.refresh_report()
        self.root.update()
        self.assertNotEqual(panel.person_var.get(), ALL)
        self.assertIn(FIRST, panel.person_var.get())
        shown = panel._trees['daily'].get_children()
        self.assertTrue(shown)
        self.assertTrue(all(panel._trees['daily'].item(iid, 'values')[2] == FIRST for iid in shown))
        summary = panel._trees['summary'].get_children()
        self.assertTrue(summary)
        self.assertTrue(all(panel._trees['summary'].item(iid, 'values')[1] == FIRST for iid in summary))

    def test_planning_shift_selection_and_draft_survive_status_updates(self):
        plan = self.app.planning_panel
        self.assertTrue(plan.enabled)
        self.assertTrue(plan.select_shift('day'))
        plan.shift_name.set('Gündüz 2')
        for _ in range(2):
            plan.update_status(self.status)
            plan.refresh()
            self.root.update()
        values = selected_values(plan.shift_list)
        self.assertIsNotNone(values)
        self.assertEqual(values[0], 'Gündüz')
        self.assertEqual(values[1], '09:00')
        self.assertEqual(plan.shift_name.get(), 'Gündüz 2')
        self.assertFalse(plan.has_unsaved_changes())

    def test_manual_choice_keeps_uid_when_roster_reorders(self):
        self.assertEqual(self.app.manual_uids, [FIRST, SECOND])
        self.app.manual_choice.current(self.app.manual_uids.index(SECOND))
        reordered = copy.deepcopy(self.status)
        reordered['employees'].reverse()
        reordered['employees'][0]['name'] = 'Mehmet Yeni'
        self.app._apply_status(reordered)
        self.root.update()
        self.assertEqual(self.app.manual_uids, [SECOND, FIRST])
        chosen = self.app.manual_choice.get()
        self.assertIn(SECOND, chosen)
        self.assertNotIn(FIRST, chosen)
        self.assertIn('Mehmet Yeni', chosen)

    def test_removed_selected_rows_clear_selection_instead_of_switching(self):
        tree = self.app.dashboard_tree
        row = row_with_values(tree, {1: SECOND})
        self.assertIsNotNone(row)
        tree.selection_set(row)
        tree.focus(row)
        self.root.update()
        shrunk = copy.deepcopy(self.status)
        shrunk['employees'] = [person for person in shrunk['employees'] if person['uid'] != SECOND]
        self.app._apply_snapshot((shrunk, self.data))
        self.root.update()
        self.assertEqual(tree.selection(), ())
        self.assertEqual(tree.focus(), '')
        self.assertIsNone(row_with_values(tree, {1: SECOND}))
        self.assertIsNotNone(row_with_values(tree, {1: FIRST}))  # another row exists, unselected
        people = self.app.people
        row = row_with_values(people, {1: 'AABBCCDD'})
        self.assertIsNotNone(row)
        people.selection_set(row)
        self.root.update()
        self.assertEqual(self.app.selected_uid, FIRST)
        removed = copy.deepcopy(self.status)
        removed['employees'] = [person for person in removed['employees'] if person['uid'] != FIRST]
        self.app._apply_snapshot((removed, self.data))
        self.root.update()
        self.assertEqual(people.selection(), ())
        self.assertIsNone(self.app.selected_uid)
        self.assertIsNotNone(row_with_values(people, {1: SECOND}))

    def test_assignment_identity_survives_earlier_row_removal(self):
        plan = self.app.planning_panel
        tree = plan.assignment_list
        selected = tree.get_children()[1]
        values = tree.item(selected, 'values')
        tree.selection_set(selected)
        plan._config['assignments'].pop(0)
        plan.refresh()
        self.root.update()
        self.assertEqual(tree.selection(), (selected,))
        self.assertEqual(tree.item(selected, 'values'), values)
        self.assertEqual(plan._selected_index(tree), 0)
        plan._config['assignments'].pop(0)
        plan.refresh()
        self.root.update()
        self.assertEqual(tree.selection(), ())

    def test_report_source_switch_does_not_reuse_other_sources_selection(self):
        panel = self.prepare_report()
        tree = panel._trees['daily']
        tree.selection_set(tree.get_children()[0])
        other = 'http://192.0.2.5'
        self.app.archive.save_config(other, self.config)
        self.app.archive.save_snapshot(other, self.status, self.data)
        panel.select_source(other)
        self.root.update()
        self.assertTrue(tree.get_children())
        self.assertEqual(tree.selection(), ())


class TreeSyncTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry('320x180')
        self.tree = ttk.Treeview(self.root, columns=('text',), show='headings', height=5)
        self.tree.column('text', width=1200, stretch=False)
        self.tree.pack(fill='both', expand=True)
        self.rows = [(str(n), (f'row {n}',)) for n in range(80)]
        sync_rows(self.tree, self.rows)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_refresh_keeps_both_scroll_axes_and_selection(self):
        tree = self.tree
        tree.selection_set('45')
        tree.focus('45')
        tree.xview_moveto(.4)
        tree.yview_moveto(.5)
        self.root.update()
        before = tree.xview(), tree.yview()
        sync_rows(tree, [(key, (values[0] + ' updated',)) for key, values in self.rows])
        self.root.update()
        self.assertEqual((tree.xview(), tree.yview()), before)
        self.assertEqual(tree.selection(), ('45',))
        self.assertEqual(tree.focus(), '45')

    def test_unchanged_refresh_does_not_emit_selection_events(self):
        self.tree.selection_set('5')
        self.root.update()
        events = []
        self.tree.bind('<<TreeviewSelect>>', lambda _event: events.append(True))
        for _ in range(4):
            sync_rows(self.tree, self.rows)
            self.root.update()
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
