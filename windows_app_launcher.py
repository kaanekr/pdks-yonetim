"""Windows entry point; default launch never connects or changes a device."""
import argparse
import json
from pathlib import Path
import tkinter as tk


def settings_smoke(report):
    """Exercise the real Windows vault with a unique disposable test credential."""
    import tempfile
    import uuid
    from windows_app.gui import Manager
    from windows_app.settings import ConnectionSettings, WindowsVault
    vault = WindowsVault('PDKS_Yonetim/TEST/' + uuid.uuid4().hex)
    app = None
    try:
        with tempfile.TemporaryDirectory(prefix='pdks-settings-', dir=Path(tempfile.gettempdir())/'opencode') as folder:
            path = Path(folder)/'connection.json'
            for launch in range(2):
                root = tk.Tk()
                root.withdraw()
                app = Manager(root, settings=ConnectionSettings(path, vault))
                assert app.client is None and app.state is None
                if launch == 0:
                    app.address.set('192.0.2.25')
                    app.username.set('smoke-user')
                    app.password.set('Synthetic-only-şifre-123')
                else:
                    assert app.address.get() == 'http://192.0.2.25'
                    assert app.username.get() == 'smoke-user'
                    assert app.password.get() == 'Synthetic-only-şifre-123'
                    assert 'Synthetic-only' not in path.read_text()
                    app.remember.set(False)
                    app._remember_changed()
                    assert ConnectionSettings(path, vault).load()['password'] == ''
                app.close()
                app = None
            report.write_text(json.dumps(dict(ok=True, isolated=True, native_vault=True,
                reopen_preserved=True, optout_deleted=True, auto_connected=False)), encoding='utf-8')
    finally:
        if app is not None and not app.closing:
            app.demo = True  # cleanup must not save again
            app.close()
        vault.delete()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--smoke-test', type=Path)
    parser.add_argument('--settings-smoke-test', type=Path)
    parser.add_argument('--workforce-smoke-test', type=Path)
    parser.add_argument('--deletion-smoke-test', type=Path)
    parser.add_argument('--apply-update', type=Path)
    parser.add_argument('--update-health-check', type=Path)
    args = parser.parse_args()
    if args.apply_update:
        from windows_app.self_update import apply_update
        from windows_app.update_contract import UpdateError
        try:
            raise SystemExit(apply_update(args.apply_update))
        except (UpdateError, OSError, ValueError):
            raise SystemExit(2) from None
    if args.update_health_check:
        from windows_app.gui import Manager
        from windows_app.update_contract import CURRENT_VERSION
        root = tk.Tk()
        root.withdraw()
        app = Manager(root, demo=True)
        try:
            root.update_idletasks()
            assert app.archive.path is None and app.settings is None
            args.update_health_check.write_text(json.dumps({'ok': True, 'version': CURRENT_VERSION}), encoding='utf-8')
        finally:
            app.close()
        return
    if args.settings_smoke_test:
        settings_smoke(args.settings_smoke_test)
        return
    if args.workforce_smoke_test:
        from windows_app.workforce_smoke import run_workforce_smoke
        result = run_workforce_smoke(args.workforce_smoke_test)
        args.workforce_smoke_test.write_text(json.dumps(result), encoding='utf-8')
        return
    if args.deletion_smoke_test:
        from windows_app.deletion_smoke import run_deletion_smoke
        result = run_deletion_smoke(args.deletion_smoke_test)
        args.deletion_smoke_test.write_text(json.dumps(result), encoding='utf-8')
        return
    from windows_app.gui import Manager
    root = tk.Tk()
    app = Manager(root, demo=args.demo or args.smoke_test is not None)
    if args.demo and args.smoke_test is None:
        from windows_app.workforce_smoke import prepare_workforce_demo
        prepare_workforce_demo(app)
    if args.smoke_test:
        app._apply_events(app.client.events())
        app.people.selection_set('11223344')
        app.on_select()
        root.update_idletasks()
        assert app.name.get() == 'Deneme Personeli'
        assert len(app.events_tree.get_children()) == 1
        assert app.state['clock_recovery'] and app.state['personnel_management']
        assert len(app.actions) >= 8
        # Native widgets also render recovery + changed-card/inactive states.
        app.client.set_time(app.state, '2026-11-01 12:34:56', True)
        app._apply_status(app.client.status())
        assert not app.state['employees'][0]['inside']
        assert app.state['employees'][0]['total_seconds'] == 3600
        app.client.save_employee(app.state, '11223344', 'Düzenlenen Personel', 'AABBCCDD', False, False)
        app._apply_status(app.client.status())
        app._apply_events(app.client.events())
        assert app.people.item('11223344', 'values')[2] == 'Pasif'
        assert len(app.events_tree.get_children()) == 2
        app.client.add_manual(app.state,'11223344','2026-10-31 09:00:00','2026-10-31 10:00:00')
        app._apply_status(app.client.status())
        app._apply_events(app.client.events())
        assert app.state['employees'][0]['total_seconds']==7200
        assert len(app.events_tree.get_children())==3
        args.smoke_test.write_text(json.dumps({'ok': True, 'tk': tk.TkVersion, 'demo_only': True,
            'people': len(app.people.get_children()), 'events': len(app.events_tree.get_children()),
            'recovery_total_preserved': True, 'card_edit_and_inactive_rendered': True,'manual_added_and_rendered':True}), encoding='utf-8')
        app.close()
        return
    root.mainloop()


if __name__ == '__main__':
    main()
