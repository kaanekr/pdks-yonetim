"""Named event/delete/reopen/export acceptance using disposable DEMO data only."""
import copy
from pathlib import Path
import tempfile
import tkinter as tk

from windows_app.gui import Manager
from windows_app.workforce_export import write_pdf, write_xlsx
from windows_app.workforce_model import parse_journal
from windows_app.workforce_smoke import sample_snapshot
from windows_app.workforce_store import WorkforceStore, verify_backup


def run_deletion_smoke(report_path):
    report_path = Path(report_path)
    app = None
    with tempfile.TemporaryDirectory(prefix='pdks-named-delete-',
            dir=Path(tempfile.gettempdir())/'opencode') as folder:
        path = Path(folder)/'archive.sqlite3'
        try:
            root = tk.Tk()
            root.withdraw()
            app = Manager(root, demo=True, archive_store=WorkforceStore(path))
            state, data, config = sample_snapshot()
            state['employees'][0].update(name='Ayşe, "Şahin"', card_uid='11223344')
            client = app.client
            client.state, client.rows = copy.deepcopy(state), data.decode('utf-8').splitlines()
            client.enable_named_journal()
            app.archive.save_config('DEMO', config)
            app._apply_snapshot((client.status(), client.events()))
            assert not app.archive_warning.get(), app.archive_warning.get()
            client.add_manual(client.status(), '11223344', '2026-10-27 08:00:00', '2026-10-27 09:00:00')
            client.save_employee(client.status(), '11223344', 'Ayşe Yeni Soyad', '11223344')
            app._apply_snapshot((client.status(), client.events()))
            latest = parse_journal(client.events())[-1]
            assert latest['name'] == 'Ayşe, "Şahin"' and latest['name_source'] == 'recorded'
            assert app.events_tree.item(str(latest['sequence']), 'values')[5] == 'Ayşe, "Şahin"'
            before, before_csv = client.status(), client.events()
            app.archive.remember_delete_intent('DEMO', before, before_csv, 'session', 1)
            client.delete_session(before, 1)
            # Simulate a successful device operation whose reply was lost, then PC restart.
            app.close()
            root = tk.Tk()
            root.withdraw()
            app = Manager(root, demo=True, archive_store=WorkforceStore(path))
            app.client = client
            app._apply_snapshot((client.status(), client.events()))
            assert not app.archive_warning.get(), app.archive_warning.get()
            assert app.archive.load_delete_intent('DEMO') is None
            assert app.archive.load_snapshot('DEMO')['csv'] == client.events()
            rows = parse_journal(client.events())
            assert not any(e['epoch'] == parse_journal(before_csv)[0]['epoch'] and e['event'] == 'I' for e in rows)
            person = next(p for p in client.state['employees'] if p['uid'] == '11223344')
            assert person['total_seconds'] == 37800  # remaining night + two manual intervals
            before, before_csv = client.status(), client.events()
            app.events_tree.selection_set('1')
            app.events_tree.focus('1')
            app.archive.remember_delete_intent('DEMO', before, before_csv, 'employee', '11223344')
            client.delete_employee(before, '11223344')
            app._apply_snapshot((client.status(), client.events()))
            assert not app.archive_warning.get(), app.archive_warning.get()
            assert app.events_tree.selection() == () and app.events_tree.focus() == ''
            assert all(p['uid'] != '11223344' for p in client.state['employees'])
            history = [e for e in parse_journal(client.events()) if e['uid'] == '11223344']
            assert history and all(e['archived'] and e['name'] == 'Ayşe, "Şahin"' for e in history)
            client.save_employee(client.status(), '11223344', 'Yeni Personel', '11223344', create=True)
            app._apply_snapshot((client.status(), client.events()))
            new = next(p for p in client.state['employees'] if p['card_uid'] == '11223344')
            assert new['uid'] != '11223344' and new['total_seconds'] == 0
            panel = app.reports_panel
            panel.start_var.set('2026-10-27')
            panel.end_var.set('2026-10-31')
            panel.refresh_report()
            assert panel.current_report is not None
            historic = [row for row in panel.current_report['rows'] if row['uid'] == '11223344']
            assert historic and any(row['name'] == 'Ayşe, "Şahin"' for row in historic)
            write_pdf(report_path.with_suffix('.pdf'), panel.displayed_report)
            write_xlsx(report_path.with_suffix('.xlsx'), panel.displayed_report)
            assert verify_backup(app.archive.backup(Path(folder)/'verified.zip'))['ok']
            root.update()
            assert not app.archive_warning.get(), app.archive_warning.get()
            return dict(ok=True, demo_only=True, immutable_event_names=True,
                paired_session_deleted=True, person_removed_history_named=True,
                reissued_card_new_identity=True, pending_reply_reopened=True,
                local_archive_reconciled=True, rewrite_selection_safe=True, pdf_xlsx_named_history=True,
                backup_verified=True, device_contacted=False, firmware_uploaded=False)
        finally:
            if app is not None and not app.closing:
                app.close()
