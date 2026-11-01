"""Permanent-delete GUI tests: confirmations, capability guards, archive recovery.

All fixtures are synthetic and in memory or in a disposable temp archive; no real
profile, network request, device or vault is touched.
"""
from pathlib import Path
import copy
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.client import DeviceError, encode_local_time
from windows_app.gui import Manager
from windows_app.journal_actions import rewrite_events
from windows_app.workforce_model import WorkforceError, parse_journal
from windows_app.workforce_smoke import prepare_workforce_demo, sample_snapshot
from windows_app.workforce_store import WorkforceStore

FIRST, SECOND = '11223344', '55AA7788'
ADDRESS_LABEL = 'DEMO'


def seed_named_demo(app):
    """Same api4 named-journal fixture as prepare_workforce_demo, but for a file archive.

    prepare_workforce_demo deliberately refuses anything except an isolated in-memory
    archive, and the recovery test must close and reopen a real archive file.
    """
    status, csv_data, config = sample_snapshot()
    app.client.state = copy.deepcopy(status)
    app.client.rows = csv_data.decode('utf-8').splitlines()
    app.client.enable_named_journal()
    app.archive.save_config(ADDRESS_LABEL, config)
    app._apply_snapshot((app.client.status(), app.client.events()))


class DeleteGuiTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Manager(self.root, demo=True)

    def tearDown(self):
        if not self.app.closing:
            self.app.close()

    # -- helpers ---------------------------------------------------------
    def prepare(self):
        prepare_workforce_demo(self.app)
        self.root.update()

    def select_person(self, uid=FIRST):
        self.app.people.selection_set(uid)
        self.app.on_select()

    def select_event(self, sequence):
        self.app.events_tree.selection_set(str(sequence))
        self.app.events_tree.focus(str(sequence))

    def sync_job(self, work, done, quiet=False):
        done(work())

    def delete_event(self, sequence, phrase=None):
        self.select_event(sequence)
        with patch('windows_app.gui.messagebox.askyesno', return_value=True) as confirm, \
                patch('windows_app.gui.simpledialog.askstring',
                      return_value='SIL %d' % sequence if phrase is None else phrase), \
                patch.object(self.app, 'job', new=self.sync_job), \
                patch('windows_app.gui.messagebox.showinfo') as info:
            self.app.delete_session()
        self.assertTrue(confirm.called)
        return info

    def delete_employee(self, uid=FIRST, phrase=None):
        self.select_person(uid)
        with patch('windows_app.gui.messagebox.askyesno', return_value=True) as confirm, \
                patch('windows_app.gui.simpledialog.askstring',
                      return_value='SIL %s' % uid if phrase is None else phrase), \
                patch.object(self.app, 'job', new=self.sync_job), \
                patch('windows_app.gui.messagebox.showinfo') as info:
            self.app.delete_person()
        self.assertTrue(confirm.called)
        return info

    def rows(self):
        return parse_journal(self.app.client.events())

    def archived_csv(self):
        return self.app.archive.load_snapshot(self.app.client.address)['csv']

    def assert_no_delete_started(self):
        self.assertEqual(self.app.client.state['journal_revision'], 0)
        self.assertEqual(self.app.client.state['sequence'], 6)
        self.assertEqual(len(self.rows()), 6)
        self.assertIsNone(self.app.archive.load_delete_intent(self.app.client.address))
        self.assertEqual(self.archived_csv(), self.app.client.events())

    def open_demo_app(self, store):
        root = tk.Tk()
        root.withdraw()
        app = Manager(root, demo=True, archive_store=store)

        def cleanup():
            if not app.closing:
                app.close()
        self.addCleanup(cleanup)
        return app

    # -- confirmations ---------------------------------------------------
    def test_cancel_at_yes_no_prompt_never_starts_a_job(self):
        self.prepare()
        with patch('windows_app.gui.messagebox.askyesno', return_value=False), \
                patch.object(self.app.client, 'delete_session') as post, \
                patch.object(self.app.client, 'delete_employee') as post_employee, \
                patch.object(self.app, 'job') as job:
            self.select_event(1)
            self.app.delete_session()
            self.select_person(FIRST)
            self.app.delete_person()
            job.assert_not_called()
        post.assert_not_called()
        post_employee.assert_not_called()
        self.assert_no_delete_started()

    def test_wrong_typed_phrase_never_starts_a_job(self):
        self.prepare()
        for phrase in ('SIL 11223345', 'sil 11223344', 'SIL'):
            with self.subTest(phrase=phrase), \
                    patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                    patch('windows_app.gui.simpledialog.askstring', return_value=phrase), \
                    patch.object(self.app, 'job') as job:
                self.select_event(1)
                self.app.delete_session()
                job.assert_not_called()
        self.assert_no_delete_started()

    def test_legacy_api3_buttons_disabled_and_direct_call_is_guarded(self):
        self.app._apply_snapshot((self.app.client.status(), self.app.client.events()))
        self.assertEqual(self.app.state['api_version'], 3)
        self.assertNotIn('permanent_delete', self.app.state)
        self.assertEqual(str(self.app.delete_person_button['state']), 'disabled')
        self.assertEqual(str(self.app.delete_session_button['state']), 'disabled')
        with patch('windows_app.gui.messagebox.showerror') as error, \
                patch.object(self.app, 'job') as job:
            self.select_event(1)
            self.app.delete_session()
            self.select_person(FIRST)
            self.app.delete_person()
            job.assert_not_called()
        self.assertEqual(error.call_count, 2)
        self.assertIn('firmware', error.call_args[0][1])
        self.assertEqual(self.app.client.state['sequence'], 1)
        self.assertNotIn('journal_revision', self.app.client.state)
        self.assertEqual(self.app.client.state['employees'][0]['name'], 'Deneme Personeli')

    # -- approved deletions ----------------------------------------------
    def test_full_pair_delete_drops_both_rows_and_resequences(self):
        self.prepare()
        info = self.delete_event(1)
        info.assert_called_once()
        self.assertEqual(self.app.events_tree.selection(), ())
        self.assertEqual(self.app.events_tree.focus(), '')
        rows = self.rows()
        self.assertEqual([row['sequence'] for row in rows], [1, 2, 3, 4])
        self.assertEqual([row['event'] for row in rows], ['I', 'O', 'M', 'R'])
        self.assertEqual(rows[0]['epoch'], encode_local_time('2026-10-30 22:00:00'))
        self.assertEqual(rows[1]['seconds'], 30600)
        self.assertEqual(rows[2]['seconds'], 3600)
        self.assertEqual(rows[3]['uid'], '00000000')
        removed = encode_local_time('2026-10-29 09:00:00')
        self.assertNotIn(removed, [row['epoch'] for row in rows])
        self.assertEqual(self.app.state['journal_revision'], 1)
        self.assertEqual(self.app.state['sequence'], 4)
        person = next(item for item in self.app.state['employees'] if item['uid'] == FIRST)
        self.assertEqual(person['total_seconds'], 34200)
        self.assertFalse(person['inside'])
        # the still-employed person's history is untouched, not anonymised
        self.assertEqual([row['name'] for row in rows[:3]], ['Ayşe Şahin'] * 3)
        self.assertTrue(all(row['archived'] is False for row in rows))
        self.assertEqual(self.archived_csv(), self.app.client.events())
        self.assertIsNone(self.app.archive.load_delete_intent(self.app.client.address))
        self.assertEqual(self.app.archive_warning.get(), '')

    def test_manual_row_delete_keeps_completed_sessions(self):
        self.prepare()
        self.delete_event(5)
        rows = self.rows()
        self.assertEqual([row['event'] for row in rows], ['I', 'O', 'I', 'O', 'R'])
        self.assertEqual([row['epoch'] for row in rows[:4]],
                         [encode_local_time(stamp) for stamp in (
                             '2026-10-29 09:00:00', '2026-10-29 17:00:00',
                             '2026-10-30 22:00:00', '2026-10-31 06:30:00')])
        person = next(item for item in self.app.state['employees'] if item['uid'] == FIRST)
        self.assertEqual(person['total_seconds'], 59400)
        self.assertEqual(self.app.state['journal_revision'], 1)
        self.assertEqual(self.app.state['sequence'], 5)
        self.assertEqual(self.archived_csv(), self.app.client.events())

    def test_employee_delete_frees_roster_but_keeps_named_history(self):
        self.prepare()
        self.delete_employee(FIRST)
        rows = self.rows()
        self.assertEqual([row['sequence'] for row in rows], [1, 2, 3, 4, 5, 6])
        history = [row for row in rows if row['uid'] == FIRST]
        self.assertEqual(len(history), 5)
        self.assertTrue(all(row['archived'] is True for row in history))
        self.assertEqual([row['name'] for row in history], ['Ayşe Şahin'] * 5)
        self.assertTrue(all(row['name_source'] != 'current' for row in history))
        self.assertEqual([item['uid'] for item in self.app.state['employees']], [SECOND])
        self.assertEqual(self.app.state['roster_version'], 0xDEADBEEF + 1)
        self.assertEqual(self.app.state['journal_revision'], 1)
        self.assertEqual(self.app.state['sequence'], 6)
        self.assertEqual(self.archived_csv(), self.app.client.events())
        self.assertIsNone(self.app.archive.load_delete_intent(self.app.client.address))

    # -- rejected targets ------------------------------------------------
    def test_recovery_row_and_open_session_are_rejected_without_a_job(self):
        self.prepare()
        with patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch('windows_app.gui.simpledialog.askstring', return_value='SIL 6'), \
                patch('windows_app.gui.messagebox.showerror') as error, \
                patch.object(self.app.client, 'delete_session') as post, \
                patch.object(self.app, 'job') as job:
            self.select_event(6)
            self.app.delete_session()
            self.app.state['employees'][0]['inside'] = True
            self.select_person(FIRST)
            self.app.delete_person()
            job.assert_not_called()
        post.assert_not_called()
        self.assertEqual(error.call_count, 2)
        self.assert_no_delete_started()

    def test_stale_target_and_stale_revision_never_post(self):
        self.prepare()
        self.delete_event(1)
        revision, sequence = self.app.state['journal_revision'], self.app.state['sequence']
        self.assertEqual((revision, sequence), (1, 4))
        # a sequence number that no longer exists after the approved resequencing
        with patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch('windows_app.gui.simpledialog.askstring', return_value='SIL 5'), \
                patch('windows_app.gui.messagebox.showerror') as error, \
                patch.object(self.app.client, 'delete_session') as post, \
                patch.object(self.app, 'job') as job:
            self.app._permanent_delete('session', 5)
            job.assert_not_called()
        post.assert_not_called()
        self.assertEqual(error.call_count, 1)
        # the GUI form still holds the pre-delete revision: the archive refuses the intent
        self.app.state['journal_revision'] = 0
        with patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch('windows_app.gui.simpledialog.askstring', return_value='SIL 1'), \
                patch('windows_app.gui.messagebox.showerror') as error, \
                patch.object(self.app.client, 'delete_session') as post, \
                patch.object(self.app, 'job') as job:
            self.app._permanent_delete('session', 1)
            job.assert_not_called()
        post.assert_not_called()
        self.assertEqual(error.call_count, 1)
        self.assertEqual(self.app.client.state['journal_revision'], revision)
        self.assertEqual(self.app.client.state['sequence'], sequence)
        self.assertEqual(len(self.rows()), 4)
        self.assertIsNone(self.app.archive.load_delete_intent(self.app.client.address))

    def test_edited_form_name_never_rewrites_stored_event_names(self):
        self.prepare()
        self.select_person(FIRST)
        self.app.name.set('Elle Değiştirilmiş Ad')
        self.delete_event(1)
        events = self.app.client.events()
        self.assertNotIn('Elle'.encode('utf-8'), events)
        rows = self.rows()
        self.assertEqual([row['name'] for row in rows if row['uid'] == FIRST],
                         ['Ayşe Şahin'] * 3)
        self.assertEqual(self.app.name.get(), 'Elle Değiştirilmiş Ad')
        self.assertEqual(self.archived_csv(), events)

    # -- archive integrity ------------------------------------------------
    def test_unapproved_rewrite_is_blocked_and_old_image_preserved(self):
        self.prepare()
        address = self.app.client.address
        before = self.app.csv
        status = copy.deepcopy(self.app.state)
        # claim one approved session deletion but hand over a truncated recovery row
        after_status = copy.deepcopy(status)
        after_status['journal_revision'] = status['journal_revision'] + 1
        truncated = rewrite_events(parse_journal(before), {6}, status['employees'])
        after_status['sequence'] = len(parse_journal(truncated))
        with self.assertRaises(WorkforceError):
            self.app.archive.accept_authorized_snapshot(
                address, status, before, after_status, truncated, 'session', 1)
        saved = self.app.archive.load_snapshot(address)
        self.assertEqual(saved['csv'], before)
        self.assertEqual(saved['status']['journal_revision'], 0)
        self.assertIsNone(self.app.archive.load_delete_intent(address))
        self.assertEqual(self.app.client.events(), before)

    def test_pending_intent_recovers_once_from_the_approved_image(self):
        parent = Path(tempfile.gettempdir()) / 'opencode'
        parent.mkdir(parents=True, exist_ok=True)
        folder = tempfile.TemporaryDirectory(prefix='pdks-delete-', dir=parent)
        self.addCleanup(folder.cleanup)
        db = Path(folder.name) / 'workforce.sqlite3'

        app1 = self.open_demo_app(WorkforceStore(db))
        seed_named_demo(app1)
        app1.root.update()
        address = app1.client.address
        before = app1.client.events()
        self.assertIsNone(app1.archive.load_delete_intent(address))

        def uncertain(work, done, quiet=False):
            with self.assertRaises(DeviceError):
                work()

        with patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch('windows_app.gui.simpledialog.askstring', return_value='SIL 1'), \
                patch.object(app1, '_read_snapshot',
                             side_effect=DeviceError('Zaman aşımı; sonuç belirsiz.')), \
                patch.object(app1, 'job', new=uncertain), \
                patch('windows_app.gui.messagebox.showerror'):
            app1.events_tree.selection_set('1')
            app1.delete_session()
        post_status, post_csv = copy.deepcopy(app1.client.state), app1.client.events()
        self.assertEqual(len(parse_journal(post_csv)), 4)
        self.assertEqual(len(parse_journal(before)), 6)
        # the device moved on, the PC archive kept the pre-delete image and the intent
        self.assertEqual(app1.archive.load_snapshot(address)['csv'], before)
        intent = app1.archive.load_delete_intent(address)
        self.assertEqual((intent['kind'], intent['target']), ('session', 1))
        self.assertEqual(intent['before_status']['journal_revision'], 0)
        self.assertNotIn(b'SYNTHETIC-DO-NOT-ARCHIVE', db.read_bytes())
        app1.close()

        app2 = self.open_demo_app(WorkforceStore(db))
        self.assertIsNotNone(app2.archive.load_delete_intent(address))
        app2.client.state = copy.deepcopy(post_status)
        app2.client.rows = post_csv.decode('utf-8').splitlines()
        with patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch.object(app2.client, 'delete_session') as post, \
                patch.object(app2.client, 'delete_employee') as post_employee:
            app2._apply_snapshot((app2.client.status(), app2.client.events()))
            post.assert_not_called()
            post_employee.assert_not_called()
        self.assertIsNone(app2.archive.load_delete_intent(address))
        self.assertEqual(app2.state['journal_revision'], 1)
        self.assertEqual(app2.state['sequence'], 4)
        person = next(item for item in app2.state['employees'] if item['uid'] == FIRST)
        self.assertEqual(person['total_seconds'], 34200)
        saved = app2.archive.load_snapshot(address)
        self.assertEqual(saved['csv'], post_csv)
        self.assertEqual(saved['status']['journal_revision'], 1)
        self.assertEqual(app2.archive_warning.get(), '')
        # replaying the same approved device image must not duplicate or re-apply anything
        app2._apply_snapshot((app2.client.status(), app2.client.events()))
        self.assertEqual(app2.archive.load_snapshot(address)['csv'], post_csv)
        self.assertEqual(len(parse_journal(post_csv)), 4)

        app3 = self.open_demo_app(WorkforceStore(db))
        self.assertEqual(app3.archive.load_snapshot(address)['csv'], post_csv)
        self.assertIsNone(app3.archive.load_delete_intent(address))

    def test_timeout_never_triggers_a_second_background_post(self):
        self.prepare()
        address = self.app.client.address
        with patch.object(self.app.client, 'delete_session',
                          side_effect=DeviceError('Zaman aşımı; sonuç belirsiz.')) as post, \
                patch('windows_app.gui.messagebox.askyesno', return_value=True), \
                patch('windows_app.gui.simpledialog.askstring', return_value='SIL 1'), \
                patch('windows_app.gui.messagebox.showerror') as error, \
                patch('windows_app.gui.messagebox.showinfo') as info:
            self.select_event(1)
            self.app.delete_session()
            deadline = time.monotonic() + 10
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertFalse(self.app.busy)
            post.assert_called_once()
            error.assert_called_once()
            info.assert_not_called()
            # UI re-render / queue drain cycles must never resend the destructive POST
            for _ in range(3):
                self.app._render_dashboard()
                self.root.update()
                time.sleep(0.12)
                self.root.update()
            post.assert_called_once()
        self.assertEqual(self.app.client.state['journal_revision'], 0)
        self.assertEqual(self.app.client.state['sequence'], 6)
        self.assertEqual(len(self.rows()), 6)
        self.assertIsNotNone(self.app.archive.load_delete_intent(address))
        self.assertEqual(self.archived_csv(), self.app.client.events())


if __name__ == '__main__':
    unittest.main(verbosity=2)
