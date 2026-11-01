"""Disposable A-package acceptance fixtures; never use a real profile, vault or device."""
from pathlib import Path
import tempfile
import tkinter as tk
import zipfile

from windows_app.client import encode_local_time
from windows_app.workforce_export import write_pdf, write_xlsx
from windows_app.workforce_model import default_config
from windows_app.workforce_store import WorkforceStore, verify_backup


def sample_snapshot():
    first, second = '11223344', '55AA7788'
    items = [('2026-10-29 09:00:00', first, 'I', 0),
             ('2026-10-29 17:00:00', first, 'O', 28800),
             ('2026-10-30 22:00:00', first, 'I', 0),
             ('2026-10-31 06:30:00', first, 'O', 30600),
             ('2026-10-28 11:00:00', first, 'M', 3600),
             ('2026-11-01 12:00:00', '00000000', 'R', encode_local_time('2026-10-31 06:30:00'))]
    lines = ['sequence,epoch,uid,event,seconds']
    lines += [f'{index},{encode_local_time(stamp)},{person},{kind},{seconds}'
              for index, (stamp, person, kind, seconds) in enumerate(items, 1)]
    status = dict(csrf='SYNTHETIC-DO-NOT-ARCHIVE', sequence=len(items), roster_version=0xDEADBEEF,
        api_version=3, clock_recovery=True, personnel_management=True, manual_attendance=True,
        clock_ok=True, now=encode_local_time('2026-11-01 12:00:00'),
        clock_reason='OK', storage_ok=True, rfid_ok=True, oled_ok=True, rtc_present=True,
        sd_enabled=True, sd_ok=True, last_uid=first,
        last_event_epoch=encode_local_time('2026-11-01 12:00:00'), message='DEMO',
        employees=[dict(uid=first, card_uid='AABBCCDD', name='Ayşe Şahin', active=True, inside=False,
                        entry=0, total_seconds=63000, live_seconds=0),
                   dict(uid=second, card_uid=second, name='Mehmet Öztürk', active=True, inside=False,
                        entry=0, total_seconds=0, live_seconds=0)])
    config = default_config()
    base = dict(weekdays=list(range(7)), break_after_minutes=240, late_tolerance=0,
                early_tolerance=0, overtime_after_minutes=0)
    config['shifts'] = [dict(base, id='day', name='Gündüz', start='09:00', end='18:00', break_minutes=60),
                        dict(base, id='night', name='Gece', start='22:00', end='06:00', break_minutes=30)]
    config['assignments'] = [dict(uid=first, shift_id='day', effective_from='2026-01-01', effective_to='2026-10-29'),
        dict(uid=first, shift_id='night', effective_from='2026-10-30', effective_to=''),
        dict(uid=second, shift_id='day', effective_from='2026-01-01', effective_to='')]
    config['holidays'] = {'2026-10-29': 'Cumhuriyet Bayramı — DEMO'}
    config['leaves'] = [dict(uid=second, start='2026-10-30', end='2026-10-30', kind='Rapor')]
    config['people'] = {first: dict(department='Üretim', employee_no='001'),
                        second: dict(department='Depo', employee_no='002')}
    return status, ('\n'.join(lines) + '\n').encode('utf-8'), config


def prepare_workforce_demo(app):
    """Show the A package with explicitly synthetic, memory-only demo data."""
    import copy
    if not app.demo or app.archive.path is not None:
        raise ValueError('A demo must use an isolated in-memory archive.')
    status, csv_data, config = sample_snapshot()
    app.client.state = copy.deepcopy(status)
    app.client.rows = csv_data.decode('utf-8').splitlines()
    app.client.enable_named_journal()
    app.archive.save_config('DEMO', config)
    app._apply_snapshot((app.client.status(), app.client.events()))
    app.reports_panel.start_var.set('2026-10-28')
    app.reports_panel.end_var.set('2026-10-31')
    app.reports_panel.refresh_report()


def run_workforce_smoke(report_path):
    from windows_app.gui import Manager
    report_path = Path(report_path)
    app = None
    with tempfile.TemporaryDirectory(prefix='pdks-workforce-', dir=Path(tempfile.gettempdir())/'opencode') as folder:
        db = Path(folder)/'workforce.sqlite3'
        try:
            store = WorkforceStore(db)
            status, csv_data, config = sample_snapshot()
            store.save_config('DEMO', config)
            root = tk.Tk()
            root.withdraw()
            app = Manager(root, demo=True, archive_store=store)
            app._apply_snapshot((status, csv_data))
            assert app.archive_warning.get() == '', app.archive_warning.get()
            panel = app.reports_panel
            panel.start_var.set('2026-10-28')
            panel.end_var.set('2026-10-31')
            panel.refresh_report()
            report = panel.current_report
            summary = next(row for row in report['summary'] if row['uid'] == '11223344')
            assert summary['net_seconds'] == 57600
            assert summary['manual_seconds'] == 3600 and summary['overtime_seconds'] == 1800
            night = next(row for row in report['rows'] if row['uid'] == '11223344' and row['date'] == '2026-10-30')
            assert night['last_out'] == '2026-10-31 06:30:00' and night['card_seconds'] == 30600
            assert any('İzin' in row['status'] for row in report['rows'])
            digest = report['config_hash']
            # Exercise queued Tk selection events too, not only the immediate render state.
            root.update()
            app.dashboard_tree.selection_set('11223344')
            app.dashboard_tree.focus('11223344')
            app.people.selection_set('11223344')
            app.planning_panel.select_shift('day')
            root.update()
            app.name.set('Kaydedilmemiş personel taslağı')
            app.planning_panel.shift_name.set('Kaydedilmemiş vardiya taslağı')
            daily_tree = panel._trees['daily']
            daily_id = daily_tree.get_children()[0]
            daily_tree.selection_set(daily_id)
            daily_tree.focus(daily_id)
            app.events_tree.selection_set('1')
            for _ in range(3):
                app._render_dashboard()
                app._apply_snapshot((status, csv_data))
                app.planning_panel.refresh()
                root.update()
            assert app.dashboard_tree.selection() == ('11223344',)
            assert app.dashboard_tree.focus() == '11223344'
            assert app.people.selection() == ('11223344',)
            assert app.name.get() == 'Kaydedilmemiş personel taslağı'
            assert app.events_tree.selection() == ('1',)
            assert daily_tree.selection() == (daily_id,) and daily_tree.focus() == daily_id
            assert app.planning_panel.shift_list.selection() == ('day',)
            assert app.planning_panel.shift_name.get() == 'Kaydedilmemiş vardiya taslağı'
            pdf, xlsx = report_path.with_suffix('.pdf'), report_path.with_suffix('.xlsx')
            write_pdf(pdf, panel.displayed_report)
            write_xlsx(xlsx, panel.displayed_report)
            assert pdf.read_bytes().startswith(b'%PDF-')
            with zipfile.ZipFile(xlsx) as archive:
                assert archive.testzip() is None and 'xl/worksheets/sheet4.xml' in archive.namelist()
            backup = store.backup(Path(folder)/'verified-backup.zip')
            assert verify_backup(backup)['ok']
            assert b'SYNTHETIC-DO-NOT-ARCHIVE' not in db.read_bytes()
            app.close()
            root = tk.Tk()
            root.withdraw()
            app = Manager(root, demo=True, archive_store=WorkforceStore(db))
            app.reports_panel.start_var.set('2026-10-28')
            app.reports_panel.end_var.set('2026-10-31')
            app.reports_panel.select_source('DEMO')
            assert app.reports_panel.current_report['config_hash'] == digest
            assert app.archive.load_snapshot('DEMO')['csv'] == csv_data
            assert app.planning_panel.enabled and len(app.planning_panel.config['shifts']) == 2
            assert not app.auto_refresh.get()
            return dict(ok=True, demo_only=True, refresh_selection_preserved=True,
                night_overtime_preserved=True, manual_separate=True,
                leave_holiday=True, pdf_font_bundled=True, xlsx_four_sheets=True,
                archive_reopened=True, backup_verified=True, csrf_not_cached=True,
                automatic_connection=False, firmware_operation=False)
        finally:
            if app is not None and not app.closing:
                app.close()
