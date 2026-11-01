"""A-REPORT-UI: read-only local Tk reporting panel over saved attendance snapshots.

Only the supplied store's local archive is read; this panel never contacts the
device/LAN, never writes settings, and never repairs or infers raw logs.
Exports/backups write solely to a path the user explicitly picks.
"""
import copy
from datetime import date, timedelta
from pathlib import Path
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from windows_app.client import duration, turkey_now
from windows_app.tree_state import row_key, sync_rows
from windows_app.workforce_model import DAILY_COLUMNS, SUMMARY_COLUMNS, WorkforceError, checked_date

ALL = 'Tümü'
NO_DEPT = '(Bölüm belirtilmemiş)'
WEEKLY_COLUMNS = [('week_start', 'Hafta'), ('name', 'Personel'), ('uid', 'Sabit UID'),
                  ('net_seconds', 'Net süre'), ('overtime_seconds', 'Fazla mesai adayı')]
EXCEPTION_COLUMNS = [('date', 'Tarih'), ('name', 'Personel'), ('uid', 'Sabit UID'),
                     ('department', 'Bölüm'), ('status', 'Durum'), ('flags', 'Bayraklar')]
DAILY_KEYS = {key for key, _ in DAILY_COLUMNS}
FLAG_KEYS = {'flags', 'days', 'summary_days'}


def _load_engine():
    """Imported lazily so the panel can land before the reporting engine does."""
    from windows_app import workforce_engine
    return workforce_engine


def _load_export():
    from windows_app import workforce_export
    return workforce_export


class ReportsPanel(ttk.Frame):
    def __init__(self, parent, store, on_source_change=None):
        super().__init__(parent, padding=12)
        self.store, self.on_source_change = store, on_source_change
        self.source, self._snapshot, self._report = None, None, None
        self._shown = {'rows': [], 'summary': [], 'weekly': []}
        self._people, self._depts = {}, {}
        self._trees = {}
        self._build()
        self._clear()

    # -- construction -----------------------------------------------------
    def _build(self):
        ttk.Label(self, text='Puantaj raporları', style='Section.TLabel').pack(anchor='w')
        ttk.Label(self, wraplength=1040, style='Muted.TLabel', justify='left', text=(
            'Yalnızca bu bilgisayardaki arşiv okunur; cihaza bağlanılmaz. Sonuçlar puantaj/ücret için '
            'tek kaynak değildir; eksik günler işaretlenir, kayıtlar tahmin edilmez.')).pack(anchor='w', pady=(2, 8))
        filter_box = ttk.LabelFrame(self, text='Rapor aralığı', style='Card.TLabelframe',
                                    padding=(10, 6))
        filter_box.pack(fill='x', pady=(0, 8))
        archive = ttk.Frame(filter_box)
        archive.pack(fill='x', pady=(0, 6))
        ttk.Label(archive, text='PC arşivi').pack(side='left', padx=(0, 6))
        self.source_var = tk.StringVar()
        self.source_combo = ttk.Combobox(archive, textvariable=self.source_var, state='readonly', width=34)
        self.source_combo.pack(side='left', padx=(0, 6))
        ttk.Button(archive, text='PC arşivini aç', command=self.open_archive).pack(side='left', padx=(0, 6))
        ttk.Button(archive, text='Kaynakları yenile', command=self.update_sources).pack(side='left', padx=(0, 10))
        self.meta_var = tk.StringVar(value='Henüz yerel arşiv seçilmedi.')
        ttk.Label(archive, textvariable=self.meta_var, foreground='#17324d').pack(side='left')
        dates = ttk.Frame(filter_box)
        dates.pack(fill='x', pady=(0, 6))
        self.start_var = tk.StringVar(value=turkey_now()[:10])
        self.end_var = tk.StringVar(value=turkey_now()[:10])
        for label, variable in (('Başlangıç', self.start_var), ('Bitiş', self.end_var)):
            ttk.Label(dates, text=label).pack(side='left', padx=(0, 4))
            ttk.Entry(dates, textvariable=variable, width=12).pack(side='left', padx=(0, 8))
        ttk.Button(dates, text='Bugün', command=self.preset_today).pack(side='left', padx=(0, 4))
        ttk.Button(dates, text='Bu hafta', command=self.preset_week).pack(side='left', padx=(0, 4))
        ttk.Button(dates, text='Bu ay', command=self.preset_month).pack(side='left', padx=(0, 4))
        ttk.Button(dates, text='Raporu yenile', command=self.refresh_report).pack(side='left', padx=(10, 0))
        filters = ttk.Frame(filter_box)
        filters.pack(fill='x')
        self.person_var, self.dept_var = tk.StringVar(value=ALL), tk.StringVar(value=ALL)
        ttk.Label(filters, text='Personel').pack(side='left', padx=(0, 4))
        self.person_combo = ttk.Combobox(filters, textvariable=self.person_var, state='readonly', width=30)
        self.person_combo.pack(side='left', padx=(0, 10))
        ttk.Label(filters, text='Bölüm').pack(side='left', padx=(0, 4))
        self.dept_combo = ttk.Combobox(filters, textvariable=self.dept_var, state='readonly', width=26)
        self.dept_combo.pack(side='left')
        for combo in (self.person_combo, self.dept_combo):
            combo.bind('<<ComboboxSelected>>', lambda event: self._render())
        self.info_var, self.warn_var = tk.StringVar(), tk.StringVar()
        ttk.Label(self, textvariable=self.info_var, wraplength=1040, justify='left',
                  foreground='#17324d').pack(fill='x')
        ttk.Label(self, textvariable=self.warn_var, wraplength=1040, justify='left',
                  foreground='#a54200').pack(fill='x', pady=(0, 6))
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill='both', expand=True)
        specs = (('daily', 'Günlük', DAILY_COLUMNS),
                 ('summary', 'Kişi Özeti', SUMMARY_COLUMNS + [('days', 'Gün')]),
                 ('weekly', 'Haftalık', WEEKLY_COLUMNS),
                 ('exceptions', 'İnceleme', EXCEPTION_COLUMNS))
        for key, title, columns in specs:
            frame = ttk.Frame(self.tabs, padding=8)
            self.tabs.add(frame, text='  ' + title + '  ')
            self._trees[key] = self._table(frame, columns)
        actions = ttk.Frame(self)
        actions.pack(side='bottom', fill='x', pady=(8, 0), before=self.tabs)
        ttk.Button(actions, text='Excel (.xlsx)', command=self.export_excel).pack(side='left', padx=(0, 6))
        ttk.Button(actions, text='PDF / Yazdır', command=self.export_pdf).pack(side='left', padx=(0, 6))
        ttk.Button(actions, text='Arşiv yedeği (ZIP)', command=self.backup_archive).pack(side='left', padx=(0, 10))
        ttk.Label(actions, wraplength=560, foreground='#304c64', text=(
            'Yazdırma, oluşturulan PDF dosyası açılıp yazdırılarak yapılır; otomatik yazdırma yoktur. '
            'ZIP yedeği tam cihaz geri yükleme veya rapor dışa aktarımı değildir.')).pack(side='left')

    @staticmethod
    def _width(key):
        fixed = {'date': 95, 'name': 150, 'uid': 95, 'employee_no': 70, 'department': 110, 'shift': 90,
                 'status': 220, 'flags': 300, 'week_start': 95, 'first_in': 175, 'last_out': 175}
        return fixed.get(key, 100 if key.endswith('_seconds') else 80)

    @classmethod
    def _table(cls, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=[key for key, _ in columns], show='headings',
                            selectmode='browse', height=6)
        tree.configure(displaycolumns=[key for key, _ in columns if key != 'uid'])
        for key, label in columns:
            tree.heading(key, text=label)
            tree.column(key, width=cls._width(key), minwidth=60, anchor='w')
        yscroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        xscroll = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky='nsew')
        yscroll.grid(row=0, column=1, sticky='ns')
        xscroll.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    # -- public API -------------------------------------------------------
    @property
    def current_report(self):
        return self._report

    @property
    def displayed_report(self):
        report = self._report
        if not isinstance(report, dict):
            return None
        shown = dict(report)
        for key in ('rows', 'summary', 'weekly'):
            shown[key] = [dict(row) for row in self._shown[key]]
        return shown

    def set_snapshot(self, source, status, csv_data, synced_at):
        """Cache detached live inputs; rebuild only when they belong to the current source."""
        if self.source not in (None, source):
            return  # do not overwrite an explicitly selected offline source with live data
        self._snapshot = {'source': source, 'status': copy.deepcopy(status), 'config': None,
                          'csv': csv_data if isinstance(csv_data, bytes) else bytes(csv_data or b''),
                          'synced_at': str(synced_at or '')}
        if self.source in (None, source):
            self.source = source
            self.source_var.set(source)
            self._rebuild()

    def select_source(self, source):
        """Open one saved archive snapshot offline (never touches the device)."""
        previous = self.source, self._snapshot
        try:
            if not isinstance(source, str) or not source:
                raise WorkforceError('Yerel arşiv kaynağı geçersiz.')
            snapshot = self.store.load_snapshot(source)
            if not snapshot:
                raise WorkforceError('Yerel arşiv anlık görüntüsü bulunamadı: ' + source)
            payload = snapshot.get('csv')
            self._snapshot = {'source': source, 'status': copy.deepcopy(snapshot.get('status')),
                              'csv': payload if isinstance(payload, bytes) else bytes(payload or b''),
                              'synced_at': str(snapshot.get('synced_at') or ''), 'config': None}
            self.source = source
        except Exception as exc:
            messagebox.showerror('Yerel arşiv açılamadı', str(exc), parent=self)
            return
        self.source_var.set(source)
        self._rebuild()
        if self._report is not None and self.on_source_change:
            try:
                if self.on_source_change(source, self._snapshot['status']) is False:
                    self.source, self._snapshot = previous
                    self.source_var.set(self.source or '')
                    self._rebuild()
            except Exception as exc:
                messagebox.showwarning('Planlama güncellenemedi', str(exc), parent=self)

    def refresh_report(self):
        """Reload the current policy from the archive and rebuild."""
        if self.source is not None:
            if self._snapshot is not None:
                self._snapshot['config'] = None
            self._rebuild()
            return
        self.update_sources()
        source = self.source_var.get()
        if not source:
            messagebox.showinfo('Rapor', 'Önce bir PC arşivi kaynağı seçin.', parent=self)
            return
        self.select_source(source)

    def update_sources(self):
        try:
            items = self.store.list_sources() or []
        except Exception as exc:
            messagebox.showerror('Arşiv kaynakları okunamadı', str(exc), parent=self)
            return
        items = sorted((item for item in items if isinstance(item, dict) and isinstance(item.get('source'), str)),
                       key=lambda item: (item.get('synced_at') or '', item['source']), reverse=True)
        values = [item['source'] for item in items]
        self.source_combo['values'] = values
        if self.source_var.get() not in values:
            self.source_var.set(values[0] if values else '')

    def open_archive(self):
        if not self.source_var.get().strip():
            self.update_sources()
        source = self.source_var.get().strip()
        if source:
            self.select_source(source)
            return
        messagebox.showinfo('Yerel arşiv', 'Kullanılabilir yerel arşiv kaynağı bulunamadı.', parent=self)

    # -- dates ------------------------------------------------------------
    def preset_today(self):
        self._preset(turkey_now()[:10], turkey_now()[:10])

    def preset_week(self):
        today = date.fromisoformat(turkey_now()[:10])
        self._preset((today - timedelta(days=today.weekday())).isoformat(), today.isoformat())

    def preset_month(self):
        today = date.fromisoformat(turkey_now()[:10])
        self._preset(today.replace(day=1).isoformat(), today.isoformat())

    def _preset(self, start, end):
        self.start_var.set(start)
        self.end_var.set(end)
        self._rebuild()

    def _checked_range(self):
        start = checked_date(self.start_var.get().strip())
        end = checked_date(self.end_var.get().strip())
        if end < start:
            raise WorkforceError('Bitiş tarihi başlangıç tarihinden önce olamaz.')
        return start.isoformat(), end.isoformat()

    # -- build/render -----------------------------------------------------
    def _clear(self):
        self._report = None
        self._shown = {'rows': [], 'summary': [], 'weekly': []}
        for tree in self._trees.values():
            tree.delete(*tree.get_children())

    def _rebuild(self):
        if self.source is None or self._snapshot is None or self._snapshot.get('source') != self.source:
            self._clear()
            self.info_var.set('Yerel arşiv kaynağı seçilmedi; canlı cihaz bağlantısı kurulmaz.')
            self.warn_var.set('')
            return
        try:
            start, end = self._checked_range()
            snapshot = self._snapshot
            config = snapshot.get('config')
            if config is None:
                config = self.store.load_config(self.source)
                snapshot['config'] = config
            report = _load_engine().build_report(
                snapshot['status'], snapshot['csv'], config, start, end,
                source=self.source, synced_at=snapshot.get('synced_at', ''))
            if not isinstance(report, dict):
                raise WorkforceError('Puantaj motoru geçersiz rapor döndürdü.')
        except Exception as exc:
            self._clear()
            self.info_var.set('Rapor oluşturulamadı; eski sonuç dışa aktarılamaz.')
            messagebox.showerror('Rapor oluşturulamadı', str(exc), parent=self)
            return
        self._report = report
        self._render()
        self._show_meta(report)

    def _render(self):
        report = self._report
        if not isinstance(report, dict):
            return
        rows = [row for row in report.get('rows') or [] if isinstance(row, dict)]
        summary = [row for row in report.get('summary') or [] if isinstance(row, dict)]
        weekly = [row for row in report.get('weekly') or [] if isinstance(row, dict)]
        self._sync_filters(rows)
        person, department = self._filters()

        def keep(row):
            return ((person is None or row.get('uid') == person) and
                    (department is None or (row.get('department') or '') == department))
        shown_rows = [row for row in rows if keep(row)]
        shown_summary = [row for row in summary if keep(row)]
        allowed = None if (person is None and department is None) else {row.get('uid') for row in shown_rows}
        shown_weekly = [row for row in weekly if allowed is None or row.get('uid') in allowed]
        self._shown = {'rows': shown_rows, 'summary': shown_summary, 'weekly': shown_weekly}
        self._fill('daily', shown_rows)
        self._fill('summary', shown_summary)
        self._fill('weekly', shown_weekly)
        self._fill('exceptions', [row for row in shown_rows if self._flags(row)])

    def _sync_filters(self, rows):
        selected_uid = self._people.get(self.person_var.get())
        people, depts = {}, set()
        for row in rows:
            uid_value = row.get('uid')
            if isinstance(uid_value, str) and uid_value:
                base = (row.get('name') or '').strip() or 'Personel'
                label, number = base, 2
                while label in people and people[label] != uid_value:
                    label, number = f'{base} ({number})', number + 1
                people[label] = uid_value
            depts.add(row.get('department') or '')
        self._people = people
        if selected_uid is not None:
            self.person_var.set(next((label for label, uid in people.items() if uid == selected_uid), ALL))
        self._depts = {(NO_DEPT if not value else value): value for value in depts}
        for combo, variable, mapping in ((self.person_combo, self.person_var, self._people),
                                         (self.dept_combo, self.dept_var, self._depts)):
            values = [ALL] + sorted(mapping)
            combo['values'] = values
            if variable.get() not in values:
                variable.set(ALL)

    def _filters(self):
        person = self.person_var.get()
        department = self.dept_var.get()
        return (None if person == ALL else self._people.get(person),
                None if department == ALL else self._depts.get(department))

    @staticmethod
    def _cell(key, value):
        if key.endswith('_seconds'):
            return duration(value or 0)
        if key in ('first_in', 'last_out'):
            return str(value) if value else '—'
        return '' if value is None else str(value)

    @staticmethod
    def _flags(row):
        value = row.get('flags')
        if isinstance(value, dict):
            return [str(name) for name, active in value.items() if active]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value]
        if isinstance(value, str):
            return [value] if value.strip() else []
        extra = []
        for key, item in row.items():
            if key not in DAILY_KEYS and key not in FLAG_KEYS and item not in (None, False, '', 0, 0.0):
                extra.append(str(key))
        return extra

    def _fill(self, key, rows):
        tree = self._trees[key]
        columns = tuple(tree['columns'])
        items = []
        for row in rows:
            values = [self._cell(column, row.get(column)) for column in columns]
            if key == 'exceptions':
                values[-1] = ', '.join(self._flags(row))
            identity = row_key(self.source, row.get('uid'), row.get('week_start') if key == 'weekly' else
                               row.get('date') if key != 'summary' else None)
            items.append((identity, values))
        sync_rows(tree, items)

    def _show_meta(self, report):
        source = report.get('source') or self.source or '—'
        self.meta_var.set('Kaynak: ' + str(source) + ' · yerel arşiv')
        stamp = str(report.get('synced_at') or '—')
        if len(stamp) >= 19 and stamp[10] in ' T':
            stamp = stamp[:10] + ' ' + stamp[11:19]
        self.info_var.set('Son arşiv: ' + stamp +
                          ' · aralık: ' + str(report.get('start') or '—') + ' → ' +
                          str(report.get('end') or '—'))
        warnings = report.get('warnings') or []
        # Keep raw card IDs out of the user-facing warning line.
        friendly = [re.sub(r'^[0-9A-Fa-f]{8,20}: ', 'Personel: ', str(item)) for item in warnings]
        self.warn_var.set('Uyarılar: ' + (' • '.join(friendly) if friendly else 'yok'))

    # -- exports ----------------------------------------------------------
    def _save_path(self, title, suffix, kind, initial):
        return filedialog.asksaveasfilename(parent=self, title=title, defaultextension=suffix,
                                            initialfile=initial, filetypes=[(kind, '*' + suffix)])

    def export_excel(self):
        self._export('.xlsx', 'Excel', 'puantaj.xlsx', 'write_xlsx', 'Excel raporu kaydet',
                     'Seçili görünüm kaydedildi:')

    def export_pdf(self):
        self._export('.pdf', 'PDF', 'puantaj.pdf', 'write_pdf', 'PDF raporu kaydet',
                     'Yazdırmak için dosyayı açıp yazdırın (otomatik yazdırma yok):')

    def _export(self, suffix, kind, initial, writer, title, done):
        if self.source is not None:
            self._rebuild()  # honour edited dates/current saved policy, not a stale previous view
        report = self.displayed_report
        if report is None:
            messagebox.showinfo('Dışa aktarma', 'Önce yerel arşivden bir rapor oluşturun.', parent=self)
            return
        path = self._save_path(title, suffix, kind, initial)
        if not path:
            return
        try:
            database = getattr(self.store, 'path', None)
            if database is not None and Path(path).resolve() == Path(database).resolve():
                raise WorkforceError('Arşiv veritabanının üzerine rapor yazılamaz; başka konum seçin.')
            getattr(_load_export(), writer)(Path(path), report)
        except Exception as exc:
            messagebox.showerror(kind + ' yazılamadı', str(exc), parent=self)
            return
        messagebox.showinfo(kind + ' kaydedildi', done + '\n' + str(path), parent=self)

    def backup_archive(self):
        path = self._save_path('Arşiv yedeğini kaydet', '.zip', 'ZIP arşivi', 'puantaj-yedek.zip')
        if not path:
            return
        try:
            saved = self.store.backup(Path(path))
        except Exception as exc:
            messagebox.showerror('Arşiv yedeği alınamadı', str(exc), parent=self)
            return
        messagebox.showinfo('Arşiv yedeği alındı', str(saved or path) + '\n\n'
                            'Bu ZIP yalnızca yerel arşiv kopyasıdır; tam cihaz geri yükleme veya '
                            'geçerli rapor dışa aktarımı değildir.', parent=self)
