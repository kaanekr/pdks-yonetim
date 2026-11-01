"""Native Tk Windows GUI; all network work off the UI thread, no automatic POST retries."""
import copy
import queue
import threading
import time
import tkinter as tk
import sys
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from windows_app.client import (DeviceClient, DeviceError, canonical_uid, checked_name,
                                duration, encode_local_time, format_local_time, parse_events, turkey_now)
from windows_app.demo import DemoClient
from windows_app.settings import ConnectionSettings, SettingsError
from windows_app.tree_state import sync_rows
from windows_app.journal_actions import deletion_sequences
from windows_app.workforce_model import WorkforceError, parse_journal
from windows_app.workforce_store import WorkforceStore
from windows_app.workforce_planning import PlanningPanel
from windows_app.workforce_reports import ReportsPanel
from windows_app.update_contract import (CURRENT_VERSION, CURRENT_SEQUENCE, FIRMWARE_TARGET,
                                         UpdateError, verify_artifact)


class Manager:
    def __init__(self, root, demo=False, settings=None, archive_store=None):
        self.root, self.demo = root, demo
        self.settings = None
        self.saved_password_binding = None
        self.settings_warning = None
        if not demo:
            try:
                self.settings = settings if settings is not None else ConnectionSettings()
            except (OSError, KeyError):
                self.settings_warning = 'Windows bağlantı ayarları kullanılamıyor; bilgiler hatırlanmayacak.'
        self.client, self.state, self.csv = None, None, None
        self.selected_uid = None
        self.busy, self.generation = False, 0
        self.results = queue.Queue()
        self.closing = False
        self.actions = []
        self.update_release, self.update_downloads = None, {}
        self.firmware_update_pending = False
        self._closing_for_update = False
        self.last_success_at = None
        self.last_success_tick = None
        self.online = False
        self.next_refresh = time.monotonic() + 30
        self.archive_warning = tk.StringVar()
        self.archive_open_warning = ''
        try:
            archive_path = ':memory:' if demo else (self.settings.path.parent/'workforce.sqlite3' if settings is not None and self.settings else None)
            self.archive = archive_store if archive_store is not None else WorkforceStore(archive_path)
        except (WorkforceError, OSError, KeyError):
            self.archive = WorkforceStore(':memory:')
            self.archive_open_warning = 'Yerel arşiv açılamadı. Bu oturumdaki yeni veriler geçici; eski arşiv sıfırlanmadı.'
            self.archive_warning.set(self.archive_open_warning)
        root.title('PDKS Yönetim' + (' — DEMO' if demo else ''))
        height = max(700, min(860, root.winfo_screenheight() - 100))
        root.geometry(f'1280x{height}')
        root.minsize(1120, 700)
        root.configure(bg='#eef2f5')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10), background='#eef2f5', foreground='#16232d')
        style.configure('TFrame', background='#eef2f5')
        style.configure('TLabel', background='#eef2f5', foreground='#16232d')
        style.configure('Muted.TLabel', foreground='#5b6b78')
        style.configure('Field.TLabel', font=('Segoe UI', 9), foreground='#5b6b78')
        style.configure('Title.TLabel', font=('Segoe UI', 23, 'bold'), foreground='#17324d')
        style.configure('Subtitle.TLabel', font=('Segoe UI', 10), foreground='#5b6b78')
        style.configure('Section.TLabel', font=('Segoe UI', 14, 'bold'), foreground='#17324d')
        style.configure('Card.TLabelframe', background='#eef2f5', bordercolor='#d9e1e7', relief='solid')
        style.configure('Card.TLabelframe.Label', background='#eef2f5', foreground='#17324d',
                        font=('Segoe UI', 10, 'bold'))
        style.configure('TNotebook', background='#eef2f5', borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure('TNotebook.Tab', padding=(16, 8), font=('Segoe UI', 10),
                        background='#e3eaf0', foreground='#304c64')
        style.map('TNotebook.Tab', background=[('selected', '#ffffff')],
                  foreground=[('selected', '#0b5fbf')])
        style.configure('Treeview', rowheight=32, background='#ffffff', fieldbackground='#ffffff',
                        foreground='#16232d', borderwidth=0)
        style.configure('Treeview.Heading', font=('Segoe UI', 10, 'bold'), background='#e8eef4',
                        foreground='#17324d', padding=(8, 8), relief='flat')
        style.map('Treeview.Heading', background=[('active', '#dce7f1')])
        style.map('Treeview', background=[('selected', '#cfe3ff')],
                  foreground=[('selected', '#16232d')])
        style.configure('TButton', padding=(14, 8))
        style.configure('Primary.TButton', font=('Segoe UI', 10, 'bold'), padding=(18, 9),
                        background='#0b5fbf', foreground='#ffffff')
        style.map('Primary.TButton',
                  background=[('disabled', '#a9bccd'), ('pressed', '#084f9f'), ('active', '#0a55ab')],
                  foreground=[('disabled', '#f4f8fb'), ('!disabled', '#ffffff')])
        style.configure('Danger.TButton', foreground='#a4261f')
        style.map('Danger.TButton', background=[('active', '#fdeceb')])
        style.configure('TEntry', padding=6, fieldbackground='#ffffff')
        style.configure('TCombobox', padding=4)
        style.configure('TCheckbutton', background='#eef2f5')
        base = ttk.Frame(root, padding=18)
        base.pack(fill='both', expand=True)
        header = ttk.Frame(base)
        header.pack(fill='x', pady=(0, 12))
        title_area = ttk.Frame(header)
        title_area.pack(side='left', fill='x', expand=True)
        ttk.Label(title_area, text='PDKS Yönetim', style='Title.TLabel').pack(anchor='w')
        ttk.Label(title_area, text='Personel, mesai ve puantaj yönetimi',
                  style='Subtitle.TLabel').pack(anchor='w', pady=(1, 0))
        if demo:
            ttk.Label(title_area, text='DEMO: yalnız geçici örnek veriler; fiziksel cihaza bağlantı yok.',
                      foreground='#a54200').pack(anchor='w', pady=(4, 0))
        connect_box = ttk.LabelFrame(base, text='Cihaz bağlantısı', style='Card.TLabelframe',
                                     padding=(14, 8))
        connect_box.pack(fill='x', pady=(0, 10))
        connect = ttk.Frame(connect_box)
        connect.pack(fill='x')
        self.address = tk.StringVar(value='192.168.4.1')
        self.username = tk.StringVar(value='admin')
        self.password = tk.StringVar()
        self.remember = tk.BooleanVar(value=True)
        self.connection_entries = []
        for index, (label, variable, width, show) in enumerate((
                ('Cihaz IP', self.address, 20, ''),
                ('Kullanıcı', self.username, 12, ''),
                ('Şifre', self.password, 16, '•'))):
            column = index * 2
            ttk.Label(connect, text=label, style='Field.TLabel').grid(
                row=0, column=column, sticky='w', padx=(0, 4))
            entry = ttk.Entry(connect, textvariable=variable, width=width, show=show)
            entry.grid(row=0, column=column + 1, sticky='w', padx=(0, 14))
            self.connection_entries.append(entry)
        connect.columnconfigure(7, weight=1)
        connect_actions = ttk.Frame(connect)
        connect_actions.grid(row=0, column=6, sticky='w', padx=(4, 0))
        self.connect_btn = ttk.Button(connect_actions, text='Bağlan', command=self.connect,
                                      style='Primary.TButton')
        self.connect_btn.pack(side='left')
        self._button(connect_actions, 'Yenile', self.refresh).pack(side='left', padx=(8, 0))
        ttk.Label(connect, text='Cihazla aynı yerel ağda olun.', style='Muted.TLabel').grid(
            row=0, column=7, sticky='w', padx=(16, 0))
        memory = ttk.Frame(connect_box)
        memory.pack(fill='x', pady=(6, 0))
        self.remember_check = ttk.Checkbutton(memory, text='Şifreyi hatırla', variable=self.remember,
                                              command=self._remember_changed)
        self.remember_check.pack(side='left')
        ttk.Label(memory, text='IP ve kullanıcı hatırlanır. Şifre Windows kasasında saklanır; otomatik bağlantı yok.',
                  style='Muted.TLabel').pack(side='left', padx=10)
        self.status_text = tk.StringVar(value='Cihaz bağlı değil.')
        self.status_label = tk.Label(base, textvariable=self.status_text, anchor='w', bg='#ffffff',
                                     fg='#5b6b78', padx=12, pady=8,
                                     highlightbackground='#d9e1e7', highlightthickness=1)
        self.status_label.pack(fill='x', pady=(0, 10))
        tabs = ttk.Notebook(base)
        tabs.pack(fill='both', expand=True)
        self.tabs = tabs
        overview = ttk.Frame(tabs, padding=14)
        people = ttk.Frame(tabs, padding=14)
        records = ttk.Frame(tabs, padding=14)
        manual = ttk.Frame(tabs, padding=14)
        clock = ttk.Frame(tabs, padding=14)
        reports = ttk.Frame(tabs)
        planning = ttk.Frame(tabs)
        update_tab = ttk.Frame(tabs, padding=14)
        for frame, title in ((overview, 'Genel Bakış'), (people, 'Personel'),
                             (records, 'Kayıtlar'), (manual, 'Manuel Mesai'),
                             (clock, 'Saat'), (reports, 'Puantaj'),
                             (planning, 'Vardiya'), (update_tab, 'Güncelleme')):
            tabs.add(frame, text=title)
        self.planning_panel = PlanningPanel(planning, self.archive, on_change=self._policy_changed)
        self.planning_panel.pack(fill='both', expand=True)
        self.reports_panel = ReportsPanel(reports, self.archive, on_source_change=self._offline_source_changed)
        self.reports_panel.pack(fill='both', expand=True)
        self.reports_panel.update_sources()
        self._build_overview(overview)
        people_header = ttk.Frame(people)
        people_header.pack(fill='x')
        ttk.Label(people_header, text='Personel ve kartlar', style='Section.TLabel').pack(side='left')
        self.delete_person_button = self._button(people_header, 'Seçili personeli kalıcı sil…',
                                                 self.delete_person, style='Danger.TButton')
        self.delete_person_button.pack(side='right', pady=(4, 0))
        ttk.Label(people, text='Kartı okutun, son kartı forma alın ve adı kaydedin. Geçmiş kayıtlar korunur.',
                  style='Muted.TLabel', wraplength=1050).pack(anchor='w', pady=(2, 10))
        self.people = self._tree(people, ('Ad', 'Kart UID', 'Durum', 'İçeride', 'Tamamlanan süre'), (230, 180, 90, 80, 130))
        self.people.configure(displaycolumns=('Ad', 'Durum', 'İçeride', 'Tamamlanan süre'), height=4)
        self.people.bind('<<TreeviewSelect>>', self.on_select)
        editor = ttk.LabelFrame(people, text='Personel bilgisi', style='Card.TLabelframe', padding=(12, 6))
        editor.pack(side='bottom', fill='x', before=self.people.master, pady=(8, 0))
        form = ttk.Frame(editor)
        form.pack(fill='x')
        form.columnconfigure(0, weight=1)
        self.name, self.card, self.active = tk.StringVar(), tk.StringVar(), tk.BooleanVar(value=True)
        self.card_status = tk.StringVar(value='Kart: okutun veya son kartı kullanın.')
        self.card.trace_add('write', lambda *_: self._card_changed())
        ttk.Label(form, text='Ad soyad', style='Field.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Entry(form, textvariable=self.name, width=32).grid(row=1, column=0, sticky='ew', pady=(2, 0))
        self.active_check = ttk.Checkbutton(form, text='Aktif', variable=self.active)
        self.active_check.grid(row=1, column=1, padx=12)
        self._button(form, 'Son okutulan kartı kullan', self.use_last_card).grid(
            row=1, column=2, padx=(8, 0))
        self._button(form, 'Yeni personel', self.new_person).grid(row=1, column=3, padx=8)
        self._button(form, 'Kaydet', self.save_person, style='Primary.TButton').grid(row=1, column=4)
        self.identity_text = tk.StringVar(value='Yeni personel: adı girin; kartı okutup “Son okutulan kartı kullan” seçin.')
        meta = ttk.Frame(editor)
        meta.pack(fill='x', pady=(6, 0))
        ttk.Label(meta, textvariable=self.identity_text, style='Muted.TLabel').pack(side='left')
        ttk.Label(meta, textvariable=self.card_status, foreground='#304c64').pack(side='right')
        ttk.Label(records, text='Giriş / çıkış kayıtları', style='Section.TLabel').pack(anchor='w')
        ttk.Label(records, text='Manuel mesai ve saat kurtarma kayıtları ayrı gösterilir. Eski kayıtlarda ad sonradan eşleştirilebilir.',
                  style='Muted.TLabel', wraplength=1050).pack(anchor='w', pady=(2, 10))
        bar = ttk.Frame(records)
        bar.pack(fill='x', pady=(0, 10))
        self._button(bar, 'Kayıtları getir', self.load_events, style='Primary.TButton').pack(side='left')
        self._button(bar, 'Kayıtları dışa aktar', self.export_csv).pack(side='left', padx=8)
        self.delete_session_button = self._button(bar, 'Seçili oturumu kalıcı sil…',
                                                  self.delete_session, style='Danger.TButton')
        self.delete_session_button.pack(side='right')
        self.events_tree = self._tree(records, ('Sıra', 'Cihaz yerel tarih/saat', 'Sabit personel UID', 'İşlem', 'Süre / açıklama',
                'Kayıttaki ad', 'Adın kaynağı', 'Silinmiş personel'), (55, 175, 155, 270, 250, 220, 190, 140))
        self.events_tree.configure(displaycolumns=('Cihaz yerel tarih/saat', 'Kayıttaki ad', 'İşlem',
                'Süre / açıklama'), height=4)
        ttk.Label(records, text='Tarihsel ad sonradan değiştirilmiş olabilir; kayıt anındaki adı esas alın.',
                  style='Muted.TLabel', wraplength=1100).pack(anchor='w', pady=8)
        ttk.Label(manual, text='Manuel mesai', style='Section.TLabel').pack(anchor='w')
        ttk.Label(manual, text='Geçmiş tarihli tamamlanmış mesaiyi personel adıyla ekleyin.',
                  style='Muted.TLabel').pack(anchor='w', pady=(2, 10))
        manual_form = ttk.LabelFrame(manual, text='Mesai bilgileri', style='Card.TLabelframe',
                                     padding=(12, 8))
        manual_form.pack(fill='x')
        ttk.Label(manual_form, text='Personel', style='Field.TLabel').pack(anchor='w')
        self.manual_choice = ttk.Combobox(manual_form, state='readonly', width=62)
        self.manual_choice.pack(fill='x', pady=(2, 8))
        self.manual_uids = []
        now = turkey_now()
        self.manual_start = tk.StringVar(value=format_local_time(encode_local_time(now) - 3600))
        self.manual_end = tk.StringVar(value=now)
        dates = ttk.Frame(manual_form)
        dates.pack(fill='x')
        for column, (label, variable) in enumerate((
                ('Başlangıç (Türkiye saati)', self.manual_start),
                ('Bitiş (Türkiye saati)', self.manual_end))):
            cell = ttk.Frame(dates)
            cell.grid(row=0, column=column, sticky='ew', padx=(0, 12))
            dates.columnconfigure(column, weight=1)
            ttk.Label(cell, text=label, style='Field.TLabel').pack(anchor='w')
            ttk.Entry(cell, textvariable=variable, width=28,
                      font=('Segoe UI', 11)).pack(fill='x', pady=(2, 0))
        self.manual_button = self._button(manual_form, 'Manuel mesaiyi ekle…', self.add_manual,
                                          style='Primary.TButton')
        self.manual_button.pack(anchor='w', pady=(10, 0))
        ttk.Label(manual, text='Biçim: YYYY-AA-GG SS:DD:SN. Süre başlangıç/bitişten hesaplanır. Cihaz saati doğru olmalı; gelecekteki bitiş ve açık oturumu olan kişi reddedilir.',
                  style='Muted.TLabel', wraplength=1000).pack(anchor='w', pady=(10, 4))
        ttk.Label(manual, text='Çakışan mesai tekrar eklenmez. Yanlış tamamlanmış M kaydı, Kayıtlar bölümünden açık onayla silinebilir.',
                  foreground='#a23e20', wraplength=1000).pack(anchor='w', pady=4)
        ttk.Label(manual, text='Manuel mesai için cihaz yönetim yazılımı gerekir. Program cihaza kendiliğinden yazılım yüklemez.',
                  style='Muted.TLabel', wraplength=1000).pack(anchor='w')
        ttk.Label(clock, text='Saat ve kurtarma', style='Section.TLabel').pack(anchor='w')
        ttk.Label(clock, text='Cihaz saati Türkiye yerel saatidir. Önce normal saat ayarını deneyin.',
                  style='Muted.TLabel').pack(anchor='w', pady=(2, 10))
        clock_form = ttk.LabelFrame(clock, text='Cihaz saati', style='Card.TLabelframe', padding=(12, 8))
        clock_form.pack(fill='x')
        self.time = tk.StringVar(value=turkey_now())
        timebar = ttk.Frame(clock_form)
        timebar.pack(fill='x')
        ttk.Entry(timebar, textvariable=self.time, width=26, font=('Segoe UI', 12)).pack(side='left')
        ttk.Button(timebar, text='Şu anki Türkiye saati',
                   command=lambda: self.time.set(turkey_now())).pack(side='left', padx=10)
        self._button(clock_form, 'Saati ayarla', lambda: self.set_time(False),
                     style='Primary.TButton').pack(anchor='w', pady=(10, 0))
        ttk.Label(clock, text='Normal işlem açık oturumda ve geriye saat almada reddedilir. Saat bozulup kilitlendiğinde aşağıdaki yönetici kurtarmasını kullanın.',
                  style='Muted.TLabel', wraplength=1000).pack(anchor='w', pady=10)
        self._button(clock, 'Açık oturumları iptal et ve saati kurtar…', lambda: self.set_time(True),
                     style='Danger.TButton').pack(anchor='w')
        ttk.Label(clock, text='Kurtarma: geçmişi ve tamamlanan toplamları korur; tüm açık oturumları süre eklemeden iptal eder. Yeniden onay ister; otomatik tekrarlanmaz.',
                  foreground='#a23e20', wraplength=1000).pack(anchor='w', pady=10)
        ttk.Label(clock, text='Cihaz yazılımı güncel değilse saat kurtarma için Güncelleme sekmesini kullanın. Yeni kayıtlardan sonra eski sürüme dönmeyin.',
                  style='Muted.TLabel', wraplength=1000).pack(anchor='w')
        self._build_updates(update_tab)
        self.footer = tk.StringVar(value='Cihaza bağlanmadan işlem yapılmaz. Otomatik yenileme isteğe bağlıdır.')
        ttk.Label(base, textvariable=self.footer, style='Muted.TLabel',
                  wraplength=1150).pack(side='bottom', fill='x', pady=(8, 0), before=tabs)
        self._enable()
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.timer = root.after(100, self._drain)
        self.live_timer = root.after(1000, self._live_tick)
        if demo:
            self.client = DemoClient()
            self._apply_status(self.client.status())
            self._enable()
        elif self.settings:
            try:
                saved = self.settings.load()
                self.address.set(saved['address'])
                self.username.set(saved['username'])
                self.password.set(saved['password'])
                self.remember.set(saved['remember'])
                if saved['password']:
                    self.saved_password_binding = (self.address.get(), self.username.get())
            except SettingsError as exc:
                self.settings_warning = str(exc)
        self.address.trace_add('write', self._connection_identity_changed)
        self.username.trace_add('write', self._connection_identity_changed)
        self.password.trace_add('write', self._password_edited)
        if self.settings_warning:
            messagebox.showwarning('Bağlantı ayarları', self.settings_warning, parent=root)

    def _build_overview(self, parent):
        self.auto_refresh = tk.BooleanVar(value=False)
        self.dashboard_text = tk.StringVar(value='Canlı cihaz bağlantısı yok. Puantaj bölümünden bilgisayar arşivini açabilirsiniz.')
        self.health_text = tk.StringVar(value='')
        self.metric_people = tk.StringVar(value='—')
        self.metric_inside = tk.StringVar(value='—')
        self.metric_clock = tk.StringVar(value='—')
        self.metric_link = tk.StringVar(value='Yok')
        self.metric_people_note = tk.StringVar(value='kayıtlı personel')
        self.metric_inside_note = tk.StringVar(value='açık oturum')
        self.metric_clock_note = tk.StringVar(value='Türkiye saati')
        self.metric_link_note = tk.StringVar(value='veri yok')
        header = ttk.Frame(parent)
        header.pack(fill='x', pady=(0, 6))
        ttk.Label(header, text='Genel bakış', style='Section.TLabel').pack(side='left')
        ttk.Checkbutton(header, text='30 saniyede bir otomatik yenile',
                        variable=self.auto_refresh, command=self._auto_refresh_changed).pack(side='right')
        ttk.Label(parent, text='Cihaz bağlıyken personel durumu ve açık oturumlar burada özetlenir.',
                  style='Muted.TLabel').pack(anchor='w', pady=(0, 10))
        cards = ttk.Frame(parent)
        cards.pack(fill='x', pady=(0, 10))
        for column, (title, value, note) in enumerate((
                ('Personel', self.metric_people, self.metric_people_note),
                ('İçeride', self.metric_inside, self.metric_inside_note),
                ('Cihaz saati', self.metric_clock, self.metric_clock_note),
                ('Bağlantı', self.metric_link, self.metric_link_note))):
            cards.columnconfigure(column, weight=1, uniform='metric')
            self._metric_card(cards, column, title, value, note)
        health = ttk.Frame(parent)
        health.pack(fill='x', pady=(0, 8))
        ttk.Label(health, text='Cihaz sağlığı', style='Field.TLabel').pack(side='left', padx=(0, 8))
        self.health_chips = {}
        for key, label in (('clock', 'Saat'), ('storage', 'Kayıt alanı'),
                           ('rfid', 'Kart okuyucu'), ('oled', 'Ekran'), ('sd', 'SD yedek')):
            variable = tk.StringVar(value=f'{label}: —')
            chip = tk.Label(health, textvariable=variable, bg='#eef1f3', fg='#52616c',
                            padx=9, pady=4, font=('Segoe UI', 9, 'bold'))
            chip.pack(side='left', padx=(0, 5))
            self.health_chips[key] = (variable, chip)
        ttk.Label(parent, textvariable=self.dashboard_text, style='Muted.TLabel',
                  wraplength=1150).pack(anchor='w', pady=(0, 4))
        self.health_label = ttk.Label(parent, textvariable=self.health_text, foreground='#a23e20',
                                      wraplength=1150)
        self.archive_label = ttk.Label(parent, textvariable=self.archive_warning, foreground='#a23e20',
                                       wraplength=1150)
        self.dashboard_tree = self._tree(parent, ('Personel', 'Sabit UID', 'Durum', 'Giriş zamanı', 'Tamamlanan', 'Açık süre (tahmini)'),
                                          (220, 150, 90, 185, 130, 170))
        self.dashboard_tree.configure(displaycolumns=('Personel', 'Durum', 'Giriş zamanı', 'Tamamlanan', 'Açık süre (tahmini)'),
                                      height=4)
        ttk.Label(parent, text='Açık süre tamamlanan toplama eklenmez; eksik çıkışa otomatik süre yazılmaz. '
                  'Devamsızlık için vardiya atayın.',
                  style='Muted.TLabel', wraplength=1150).pack(anchor='w', pady=(8, 0))
        self.health_text.trace_add('write', lambda *_: self._sync_overview_notes())
        self.archive_warning.trace_add('write', lambda *_: self._sync_overview_notes())
        self._sync_overview_notes()

    def _sync_overview_notes(self):
        for label, variable in ((self.health_label, self.health_text),
                                (self.archive_label, self.archive_warning)):
            if variable.get().strip():
                if not label.winfo_manager():
                    label.pack(anchor='w', pady=(2, 0), before=self.dashboard_tree.master)
            else:
                label.pack_forget()

    def _metric_card(self, parent, column, title, value, note):
        card = tk.Frame(parent, bg='#ffffff', highlightbackground='#d9e1e7', highlightthickness=1)
        card.grid(row=0, column=column, sticky='nsew', padx=(0, 8) if column < 3 else 0)
        body = tk.Frame(card, bg='#ffffff', padx=14, pady=10)
        body.pack(fill='both', expand=True)
        tk.Label(body, text=title, bg='#ffffff', fg='#5b6b78',
                 font=('Segoe UI', 9, 'bold'), anchor='w').pack(fill='x')
        tk.Label(body, textvariable=value, bg='#ffffff', fg='#16232d',
                 font=('Segoe UI', 20, 'bold'), anchor='w').pack(fill='x', pady=(1, 0))
        tk.Label(body, textvariable=note, bg='#ffffff', fg='#5b6b78',
                 font=('Segoe UI', 9), anchor='w').pack(fill='x')

    def _set_health_chip(self, key, text, state):
        variable, chip = self.health_chips[key]
        variable.set(text)
        colors = {'ok': ('#e4f5ec', '#0d6b45'), 'bad': ('#fdeceb', '#a4261f'),
                  'off': ('#eef1f3', '#52616c')}
        background, foreground = colors[state]
        chip.configure(bg=background, fg=foreground)

    def _auto_refresh_changed(self):
        self.next_refresh = time.monotonic() + 30

    def _build_updates(self, parent):
        ttk.Label(parent, text='Güncelleme', style='Section.TLabel').pack(anchor='w')
        ttk.Label(parent, text='Program ve cihaz yazılımını buradan doğrulayıp güncelleyin.',
                  style='Muted.TLabel').pack(anchor='w', pady=(2, 10))
        self.update_text = tk.StringVar(value='Kendiliğinden indirme/yükleme yok. Güncellemeleri kontrol edin.')
        self.device_update_text = tk.StringVar(value='Cihaz bağlı değil. İlk cihaz yazılımı güncellemesi bir kez USB gerektirir.')
        update_card = ttk.LabelFrame(parent, text='Durum', style='Card.TLabelframe', padding=(12, 8))
        update_card.pack(fill='x')
        ttk.Label(update_card, textvariable=self.update_text, wraplength=1000).pack(anchor='w', pady=4)
        ttk.Label(update_card, textvariable=self.device_update_text,
                  style='Muted.TLabel', wraplength=1000).pack(anchor='w', pady=4)
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=12)
        self.updates_check_btn = ttk.Button(row, text='Güncellemeleri kontrol et', command=self.check_updates,
                                           style='Primary.TButton')
        self.updates_check_btn.pack(side='left')
        self.updates_download_btn = ttk.Button(row, text='Güncellemeyi indir', command=self.download_updates)
        self.updates_download_btn.pack(side='left', padx=8)
        actions = ttk.LabelFrame(parent, text='Yükleme', style='Card.TLabelframe', padding=(12, 8))
        actions.pack(fill='x')
        self.updates_app_btn = ttk.Button(actions, text='Programı güncelle…', command=self.install_app_update)
        self.updates_app_btn.pack(side='left')
        self.updates_firmware_btn = ttk.Button(actions, text='Cihaz yazılımını güncelle…',
                                              command=self.install_firmware_update)
        self.updates_firmware_btn.pack(side='left', padx=8)
        ttk.Label(parent, text='Güncellemeler doğrulanır; eski sürüme otomatik dönülmez. '
            'Program güncellemesinde eski sürüm yalnız sağlık kontrolü sırasında tutulur; yeni sürüm doğrulanınca silinir, açılmazsa geri alınır. '
            'Kayıt ve ayarlar değiştirilmez. Cihaz yazılımı yalnız aynı ağdaki, açık oturumu olmayan cihaza gönderilir.',
            wraplength=1000).pack(anchor='w', pady=10)
        ttk.Label(parent, text='Cihazın kurulum Wi-Fi ağında internet olmayabilir: dosyaları önce internet varken indirin, '
            'programı kapatmadan cihaz ağına geçip bağlanın. İlk cihaz yazılımı kurulumu ve her yükleme ayrı onay ister. '
            'DEMO gerçek güncelleme indirmez/yüklemez.', wraplength=1000, foreground='#a23e20').pack(anchor='w', pady=8)

    def check_updates(self):
        if self.demo or self.busy:
            return
        from windows_app.updates import check_release
        self.update_text.set('Güncellemeler kontrol ediliyor…')
        def done(release):
            self.update_release, self.update_downloads = release, {}
            manifest = release.manifest
            self.update_text.set('Programınız güncel.' if manifest['sequence'] <= CURRENT_SEQUENCE else 'Yeni sürüm hazır.')
            self._enable()
        self.job(check_release, done)

    def _update_path(self, release, kind):
        from windows_app.updates import download_artifact, verify_manifest
        current = verify_manifest(release.raw, release.signature)
        cached = self.update_downloads.get(kind)
        return verify_artifact(cached, current.manifest[kind]) if cached else download_artifact(current, kind)

    def download_updates(self):
        if self.demo or self.busy or self.update_release is None:
            return
        release = self.update_release
        self.update_text.set('Güncelleme dosyaları indiriliyor; henüz yükleme yapılmıyor…')
        def done(paths):
            self.update_downloads = paths
            self.update_text.set('Güncelleme hazır. Cihaz ağına geçebilirsiniz; programı açık tutun. Yükleme için ayrıca onaylayın.')
        self.job(lambda: {kind: self._update_path(release, kind) for kind in ('windows', 'firmware')}, done)

    def install_app_update(self):
        if self.demo or self.busy or self.update_release is None:
            return
        release = self.update_release
        if release.manifest['sequence'] <= CURRENT_SEQUENCE or not getattr(sys, 'frozen', False):
            messagebox.showinfo('Program güncelleme', 'Yeni sürüm yok veya program bu kopyadan güncellenemiyor.', parent=self.root)
            return
        if not messagebox.askyesno('Programı güncelle', 'Yeni sürüm doğrulanıp yüklenecek. Program kapanıp yeniden açılır; eski sürüm sağlık kontrolünden sonra silinir. Yeni sürüm açılmazsa geri alınır. Kaydedilmemiş formlarınızı tamamladınız mı?', parent=self.root):
            return
        def done(path):
            from windows_app.self_update import prepare_self_update
            if self.planning_panel.has_unsaved_changes() and not messagebox.askyesno('Kaydedilmemiş plan',
                    'Yerel taslak kaydedilmedi. Güncelleme için kaydetmeden kapatılsın mı?', parent=self.root):
                return
            if not self._save_connection():
                return
            try:
                prepare_self_update(release, path)
            except UpdateError as exc:
                messagebox.showerror('Güncelleme başlatılmadı', str(exc), parent=self.root)
                return
            self._closing_for_update = True
            self.close()
        self.update_text.set('Yeni sürüm doğrulanıyor…')
        self.job(lambda: self._update_path(release, 'windows'), done)

    def install_firmware_update(self):
        if self.demo or self.busy or self.update_release is None or not self.client or not self.state:
            return
        release, client = self.update_release, self.client
        if (self.state.get('ota_update') is not True or self.state.get('firmware_target') != FIRMWARE_TARGET or
                self.state.get('storage_ok') is not True):
            messagebox.showerror('Cihaz yazılımı güncelleme', 'Bu cihazda ilk kurulum için bir kez USB gerekiyor.', parent=self.root)
            return
        if not messagebox.askyesno('Cihaz yazılımını yükle', 'Cihaz aynı ağda olmalı ve açık oturum bulunmamalı. '
                'Önce bu bilgisayara yedek alınır, sonra yeni cihaz yazılımı yüklenir. Cihaz yeniden başlar; elektrik kesilmemeli. '
                'Günlük, Wi-Fi ve şifre dosyaları korunur. Onaylıyor musunuz?', parent=self.root):
            return
        backup = filedialog.asksaveasfilename(parent=self.root, title='Cihaz yazılımı öncesi yedek',
            defaultextension='.zip', initialfile='PDKS_yedek.zip', filetypes=[('Yedek dosyası', '*.zip')])
        if not backup:
            return
        self.update_text.set('Cihaz yazılımı doğrulanıyor, yedek alınıyor ve güncelleme gönderiliyor…')
        def work():
            path = self._update_path(release, 'firmware')
            before = self._read_snapshot(client)
            self.archive.save_snapshot(client.address, before[0], before[1])
            self.archive.backup(backup)
            return client.update_firmware(before[0], release.manifest['firmware'], path), before
        def done(value):
            self._apply_snapshot(value[1])
            self.online = False
            self.firmware_update_pending = True
            self.update_text.set('Yeni cihaz yazılımı cihaz tarafından onaylandı. Yeniden başladıktan sonra Yenile ile sürümü kontrol edin; otomatik tekrar yapılmaz.')
            self._enable()
        self.job(work, done)

    def _live_tick(self):
        if self.closing:
            return
        self._render_dashboard()
        if (self.auto_refresh.get() and self.client and self.state and not self.busy and
                time.monotonic() >= self.next_refresh):
            self.next_refresh = time.monotonic() + 30
            self.refresh(quiet=True)
        self.live_timer = self.root.after(1000, self._live_tick)

    def _render_dashboard(self):
        if not self.state:
            return
        elapsed = max(0, int(time.monotonic() - self.last_success_tick)) if self.last_success_tick is not None else 0
        people = self.state['employees']
        inside = sum(bool(person['inside']) for person in people)
        link = 'DEMO' if self.demo else ('Bağlı' if self.online else 'Yok')
        if self.last_success_at:
            self.dashboard_text.set('Son veri: az önce' if elapsed < 2 else f'Son veri: {elapsed} sn önce')
        else:
            self.dashboard_text.set('Son veri yok.')
        self.metric_people.set(str(len(people)))
        self.metric_inside.set(str(inside))
        clock = format_local_time(self.state.get('now')) if self.state.get('clock_ok') else '—'
        self.metric_clock.set(clock[11:] if len(clock) > 11 else clock)
        self.metric_link.set(link)
        self.metric_link_note.set('son veri alındı' if self.last_success_at else 'veri yok')
        def health(key):
            return 'Tamam' if self.state.get(key) else 'Sorun'
        drift = ''
        if not self.demo and self.state.get('clock_ok') and self.state.get('now'):
            difference = int(self.state['now']) + (elapsed if self.online else 0) - encode_local_time(turkey_now())
            if abs(difference) > 120:
                drift = 'Bilgisayar ve cihaz saati farklı; saatleri kontrol edin.'
        self._set_health_chip('clock', 'Saat: ' + health('clock_ok'),
                              'ok' if self.state.get('clock_ok') else 'bad')
        self._set_health_chip('storage', 'Kayıt alanı: ' + health('storage_ok'),
                              'ok' if self.state.get('storage_ok') else 'bad')
        self._set_health_chip('rfid', 'Kart okuyucu: ' + health('rfid_ok'),
                              'ok' if self.state.get('rfid_ok') else 'bad')
        self._set_health_chip('oled', 'Ekran: ' + health('oled_ok'),
                              'ok' if self.state.get('oled_ok') else 'bad')
        if self.state.get('sd_enabled'):
            self._set_health_chip('sd', 'SD yedek: ' + health('sd_ok'),
                                  'ok' if self.state.get('sd_ok') else 'bad')
        else:
            self._set_health_chip('sd', 'SD yedek: kapalı', 'off')
        self.health_text.set(drift)
        rows = []
        for person in people:
            live = int(person.get('live_seconds', 0))
            if person['inside'] and self.state.get('clock_ok') and self.online:
                live += elapsed
            rows.append((person['uid'], (person['name'], person['uid'],
                'İçeride' if person['inside'] else 'Dışarıda',
                format_local_time(person.get('entry')) if person['inside'] else '—',
                duration(person.get('total_seconds', 0)), duration(live) if person['inside'] else '—')))
        sync_rows(self.dashboard_tree, rows)

    def _offline_source_changed(self, source, status):
        # An archive selection must never replace the live authenticated client/state.
        if self.planning_panel.source == source:
            self.planning_panel.update_status(status)
            return True
        if self.planning_panel.has_unsaved_changes() and not messagebox.askyesno(
                'Kaydedilmemiş yerel plan', 'Kaynak değişirse Kaydet denmemiş plan değişiklikleri bırakılır. Devam edilsin mi?', parent=self.root):
            return False
        return self.planning_panel.set_source(source, status)

    def _policy_changed(self):
        self.reports_panel.refresh_report()
        try:
            self.archive.auto_backup()
        except WorkforceError as exc:
            self.archive_warning.set('Yerel ayarlar kaydedildi; otomatik yedek alınamadı: ' + str(exc))

    @staticmethod
    def _read_snapshot(client):
        for _attempt in range(2):  # GET-only retry if a card is read between requests
            before = client.status()
            data = client.events()
            after = client.status()
            events = parse_journal(data)
            if (before['sequence'] == after['sequence'] == len(events) and
                    before.get('roster_version') == after.get('roster_version') and
                    before.get('journal_revision') == after.get('journal_revision')):
                return after, data
        raise DeviceError('Okuma sırasında kayıt değişti; tutarlı görüntü alınamadı. Yenile deyin. Cihaza yazma yapılmadı.')

    def _apply_snapshot(self, value):
        self.online = True
        self.last_success_tick = time.monotonic()
        self.last_success_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self._apply_status(value[0])
        self._apply_events(value[1])

    def _archive_current(self):
        if self.csv is None or self.state is None or self.client is None:
            return
        try:
            warning = ''
            intent = self.archive.load_delete_intent(self.client.address)
            if intent:
                if self.state.get('journal_revision') == intent['before_status'].get('journal_revision'):
                    # A successful GET confirms no destructive generation was committed.
                    # Never resend the POST; the operator must select and approve again.
                    self.archive.clear_delete_intent(self.client.address)
                else:
                    applied = self.archive.accept_authorized_snapshot(self.client.address,
                        intent['before_status'], intent['before_csv'], self.state, self.csv,
                        intent['kind'], intent['target'])
                    warning = applied.get('cleanup_warning', '')
            result = self.archive.save_snapshot(self.client.address, self.state, self.csv)
            source = result['source']
            saved = self.archive.load_snapshot(source)
            self.reports_panel.set_snapshot(source, saved['status'], saved['csv'], saved['synced_at'])
            self.reports_panel.update_sources()
            if self.reports_panel.source == source:
                if self.planning_panel.source != source:
                    self.planning_panel.set_source(source, saved['status'])
                else:
                    self.planning_panel.update_status(saved['status'])
            self.archive.auto_backup()
            self.archive_warning.set(self.archive_open_warning or warning)
        except (WorkforceError, OSError) as exc:
            self.archive_warning.set('Arşiv güncellenemedi; önceki arşiv korundu: ' + str(exc))

    def _password_edited(self, *_args):
        self.saved_password_binding = None

    def _connection_identity_changed(self, *_args):
        if self.saved_password_binding is not None:
            self.password.set('')
            self.saved_password_binding = None

    def _save_connection(self):
        if self.demo:
            return True
        if self.settings is None:
            messagebox.showwarning('Bağlantı ayarları', self.settings_warning, parent=self.root)
            return False
        try:
            self.settings.save(self.address.get(), self.username.get(), self.password.get(), self.remember.get())
            self.saved_password_binding = (self.address.get(), self.username.get()) if self.remember.get() and self.password.get() else None
            return True
        except SettingsError as exc:
            messagebox.showwarning('Bağlantı bilgileri kaydedilemedi', str(exc), parent=self.root)
            return False

    def _remember_changed(self):
        if self.demo or self.settings is None:
            return
        if not self.remember.get():
            try:
                self.settings.forget_password()
            except SettingsError as exc:
                messagebox.showwarning('Kayıtlı şifre silinemedi', str(exc), parent=self.root)
                return
        self._save_connection()

    def _button(self, parent, text, command, style=None):
        button = ttk.Button(parent, text=text, command=command,
                            style=style) if style else ttk.Button(parent, text=text, command=command)
        self.actions.append(button)
        return button

    @staticmethod
    def _tree(parent, columns, widths):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse', height=6)
        for col, width in zip(columns, widths):
            tree.heading(col, text=col)
            tree.column(col, width=width, minwidth=50)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _enable(self):
        state = 'normal' if self.client and self.state and not self.busy else 'disabled'
        for button in self.actions:
            button.configure(state=state)
        self.connect_btn.configure(state='disabled' if self.busy else 'normal')
        for entry in self.connection_entries:
            entry.configure(state='disabled' if self.busy else 'normal')
        self.remember_check.configure(state='disabled' if self.busy or self.demo else 'normal')
        self.active_check.configure(state='normal' if state == 'normal' and self.state.get('personnel_management') else 'disabled')
        self.manual_button.configure(state='normal' if state=='normal' and self.state.get('manual_attendance') else 'disabled')
        deletion = (state == 'normal' and self.state.get('permanent_delete') is True and self.state.get('api_version', 0) >= 4)
        self.delete_person_button.configure(state='normal' if deletion else 'disabled')
        self.delete_session_button.configure(state='normal' if deletion else 'disabled')
        available = not self.busy and not self.demo
        self.updates_check_btn.configure(state='normal' if available else 'disabled')
        release = self.update_release.manifest if self.update_release else None
        self.updates_download_btn.configure(state='normal' if available and release else 'disabled')
        self.updates_app_btn.configure(state='normal' if available and release and getattr(sys, 'frozen', False) and release['sequence'] > CURRENT_SEQUENCE else 'disabled')
        ota = (available and release and self.state and self.state.get('ota_update') is True and
               self.state.get('storage_ok') is True and self.state.get('firmware_target') == FIRMWARE_TARGET and
               self.state.get('firmware_sequence', 0) < release['firmware']['sequence'] and not self.firmware_update_pending)
        self.updates_firmware_btn.configure(state='normal' if ota else 'disabled')

    def job(self, work, done, quiet=False):
        if self.busy:
            return
        self.busy = True
        self.footer.set('İşlem sürüyor… Pencere kullanılabilir; ikinci istek gönderilmiyor.')
        self._enable()
        generation = self.generation
        def run():
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, str(exc) if isinstance(exc, (DeviceError, OSError, WorkforceError, UpdateError)) else 'Beklenmeyen yanıt; durumu yenileyin. İşlem otomatik tekrarlanmadı.'
            self.results.put((generation, done, value, error, quiet))
        threading.Thread(target=run, daemon=True).start()

    def _drain(self):
        if self.closing:
            return
        try:
            while True:
                generation, done, value, error, quiet = self.results.get_nowait()
                if generation != self.generation:
                    continue
                self.busy = False
                self._enable()
                if error:
                    self.online = False
                    self.status_text.set('Cihaz bağlantısı kurulamadı veya son veri geçersiz.')
                    self.status_label.configure(fg='#a4261f')
                    self._render_dashboard()
                    self.footer.set('İşlem başarısız / sonucu belirsiz. Önce Yenile ile durumu kontrol edin.')
                    if not quiet:
                        messagebox.showerror('PDKS', error, parent=self.root)
                else:
                    try:
                        done(value)
                        if self.closing:
                            return
                    except (ValueError, KeyError, TypeError, tk.TclError, WorkforceError):
                        if self.closing:
                            return
                        messagebox.showerror('PDKS', 'Cihaz yanıtı görüntülenemedi; yeniden bağlanın.', parent=self.root)
                    self.footer.set('Tamamlandı. Sonraki değişiklikten önce gerekirse durumu yenileyin.')
        except queue.Empty:
            pass
        if not self.closing:
            self.timer = self.root.after(100, self._drain)

    def close(self):
        if self.closing:
            return
        if not self._closing_for_update and self.planning_panel.has_unsaved_changes() and not messagebox.askyesno(
                'Kaydedilmemiş yerel plan', 'Kaydet denmemiş plan değişiklikleri var. Kaydetmeden kapatılsın mı?', parent=self.root):
            return
        if not self._closing_for_update and not self._save_connection() and not messagebox.askyesno('Kaydetmeden kapat?', 'Bağlantı bilgileri kaydedilemedi. Yine de kapatılsın mı?', parent=self.root):
            return
        self.closing = True
        self.generation += 1
        self.root.after_cancel(self.timer)
        self.root.after_cancel(self.live_timer)
        self.archive.close()
        self.root.destroy()

    def connect(self):
        if self.busy:
            return
        try:
            client = DemoClient() if self.demo else DeviceClient(self.address.get(), self.username.get(), self.password.get())
        except DeviceError as exc:
            messagebox.showerror('Bağlantı', str(exc), parent=self.root)
            return
        self._save_connection()
        self.generation += 1
        self.client, self.state, self.csv = client, None, None
        self._events_revision = None
        self.online = False
        self.last_success_at = None
        self.last_success_tick = None
        self.status_text.set('Cihaz bağlanıyor…')
        self.status_label.configure(fg='#5b6b78')
        self.new_person()
        self.people.delete(*self.people.get_children())
        self.events_tree.delete(*self.events_tree.get_children())
        self.job(lambda: self._read_snapshot(client), self._apply_snapshot)

    def refresh(self, quiet=False):
        if not self.client or self.busy:
            return
        client = self.client
        self.job(lambda: self._read_snapshot(client), self._apply_snapshot, quiet=quiet)

    def _apply_status(self, state):
        self.state = state
        self.firmware_update_pending = False
        self.device_update_text.set('Cihaz yazılımı: ' + str(state.get('firmware_version', 'eski sürüm')) +
            (' — kablosuz güncelleme hazır.' if state.get('ota_update') else ' — ilk kurulum için bir kez USB gerekir.'))
        index=self.manual_choice.current()
        manual_uid=self.manual_uids[index] if 0<=index<len(self.manual_uids) else None
        self.manual_uids=[e['uid'] for e in state['employees']]
        self.manual_choice['values']=[e['name']+(' — pasif' if not e.get('active',True) else '') for e in state['employees']]
        if manual_uid in self.manual_uids:
            self.manual_choice.current(self.manual_uids.index(manual_uid))
        elif self.manual_uids:
            self.manual_choice.current(0)
        else:
            self.manual_choice.set('')
        selected = self.selected_uid
        sync_rows(self.people, [(e['uid'], (e['name'], e.get('card_uid', e['uid']),
                'Aktif' if e.get('active', True) else 'Pasif', 'Evet' if e['inside'] else 'Hayır',
                duration(e['total_seconds']))) for e in state['employees']])
        if selected and not self.people.exists(selected):
            self.new_person()
        elif selected and selected not in self.people.selection():
            self.people.selection_set(selected)
        inside = sum(bool(e['inside']) for e in state['employees'])
        clock = format_local_time(state.get('now')) if state.get('clock_ok') else 'Ayarlanmalı'
        self.status_text.set(f"Cihaz bağlı  |  Saat: {clock}  |  İçeride: {inside}")
        self.status_label.configure(fg='#0d6b45')
        if not state.get('personnel_management'):
            self.identity_text.set('Cihaz yazılımı güncel değil; ad ekle/düzenle çalışır, kart değiştirme ve saat kurtarma için güncelleme gerekir.')
        self._enable()
        self._render_dashboard()

    def _card_changed(self):
        if hasattr(self, 'card_status'):
            self.card_status.set('Kart: eşleşti' if self.card.get() else 'Kart: okutun veya son kartı kullanın.')

    def new_person(self):
        self.selected_uid = None
        self.name.set('')
        self.card.set('')
        self.active.set(True)
        self.people.selection_remove(self.people.selection())
        self.identity_text.set('Yeni personel: adı girin; kartı okutup “Son okutulan kartı kullan” seçin.')

    def on_select(self, _event=None):
        selection = self.people.selection()
        if not selection or not self.state:
            return
        if selection[0] == self.selected_uid:
            return  # queued/redundant selection events must not replace an edited form
        e = next((e for e in self.state['employees'] if e['uid'] == selection[0]), None)
        if e:
            self.selected_uid = e['uid']
            self.name.set(e['name'])
            self.card.set(e.get('card_uid', e['uid']))
            self.active.set(e.get('active', True))
            self.identity_text.set('Bu personelin geçmişi korunur; kart değişse de geçmiş bu kişide kalır.')

    def use_last_card(self):
        try:
            self.card.set(canonical_uid(self.state.get('last_uid', '')))
            self.identity_text.set('Kart hazır; adı girip personeli kaydedin.')
        except DeviceError:
            messagebox.showerror('Kart', 'Kart okunamadı. Kartı okuyucuya okutup yeniden deneyin.', parent=self.root)

    def save_person(self):
        try:
            name = checked_name(self.name.get())
        except DeviceError as exc:
            messagebox.showerror('Personel', str(exc), parent=self.root)
            return
        try:
            card = canonical_uid(self.card.get())
        except DeviceError:
            messagebox.showerror('Personel', 'Kart eşleşmedi. Kartı okutup “Son okutulan kartı kullan” seçin.', parent=self.root)
            return
        uid = self.selected_uid or card
        create = self.selected_uid is None
        active, state, client = self.active.get(), copy.deepcopy(self.state), self.client
        if not messagebox.askyesno('Personeli kaydet', f"{'Yeni personel' if create else 'Personel güncelleme'}\n{name}\nKart: eşleşti\n{'Aktif' if active else 'PASİF'}\n\nGeçmiş silinmez. Onaylıyor musunuz?", parent=self.root):
            return
        def work():
            reply = client.save_employee(state, uid, name, card, active, create)
            return reply, self._read_snapshot(client)
        def done(value):
            saved = next((e for e in value[1][0]['employees'] if e.get('card_uid', e['uid']) == card), None)
            self.selected_uid = saved['uid'] if saved else uid
            self._apply_snapshot(value[1])
            messagebox.showinfo('Personel', value[0]['message'], parent=self.root)
        self.job(work, done)

    def _apply_events(self, data):
        rows = parse_events(data)
        revision = self.state.get('journal_revision')
        previous_revision = getattr(self, '_events_revision', None)
        if previous_revision is not None and revision != previous_revision:
            # A destructive rewrite renumbers rows: an old sequence ID must not
            # silently select a different person's/session's surviving record.
            self.events_tree.selection_remove(self.events_tree.selection())
            self.events_tree.focus('')
        self._events_revision = revision
        self.csv = data
        people = {p['uid']: p['name'] for p in self.state['employees']}
        labels = {'recorded': 'Kayıt anındaki ad', 'migrated': 'Eski kayıttan eşleştirildi',
                  'current': 'Mevcut ad — tarihsel değil', 'unknown': 'Ad bilinmiyor'}
        displayed = []
        for row in rows:
            if len(row) == 5:
                name = people.get(row[2], '') if not str(row[3]).startswith('Yönetici:') else ''
                values = row + (name or '—', labels['current' if name else 'unknown'], 'Hayır')
            else:
                values = row[:5] + (row[5] or '—', labels[row[6]], 'Evet' if row[7] else 'Hayır')
            displayed.append((str(row[0]), values))
        sync_rows(self.events_tree, displayed)
        self._archive_current()

    def delete_person(self):
        self._permanent_delete('employee', self.selected_uid)

    def delete_session(self):
        selection = self.events_tree.selection()
        target = int(self.events_tree.item(selection[0], 'values')[0]) if selection else None
        self._permanent_delete('session', target)

    def _permanent_delete(self, kind, target):
        if self.busy:
            return
        try:
            if not self.client or not self.state or self.csv is None:
                raise DeviceError('Önce Bağlan / Yenile ile kayıtları okuyun.')
            DeviceClient._permanent_state(self.state)
            state, data, client = copy.deepcopy(self.state), self.csv, self.client
            events = parse_journal(data)
            if kind == 'employee':
                person = next((p for p in state['employees'] if p['uid'] == target), None)
                if person is None or person['inside']:
                    raise DeviceError('Personeli seçin; açık oturumu önce kapatın/kurtarın.')
                detail = f'{person["name"]}\nPersonel/kart kaldırılır; isimli geçmiş kayıtlar KALIR.'
            else:
                sequences = deletion_sequences(events, target)
                selected = [event for event in events if event['sequence'] in sequences]
                person = selected[0]
                name = person.get('name') or next((p['name'] for p in state['employees'] if p['uid'] == person['uid']), person['uid'])
                detail = (f'{name}\n' +
                    '\n'.join(f'{format_local_time(event["epoch"])} · {event["event"]}' for event in selected) +
                    '\nGiriş/çıkış birlikte, manuel kayıt tek kayıt olarak kalıcı kaldırılır. Toplamlar yeniden hesaplanır.')
        except (DeviceError, WorkforceError, ValueError) as exc:
            messagebox.showerror('Kalıcı silme', str(exc), parent=self.root)
            return
        if not messagebox.askyesno('GERİ ALINAMAZ — kalıcı silme', detail +
                '\n\nCihaz ve bu bilgisayardaki arşiv güncellenir. Eski dışa aktarılmış yedek dosyaları kendiliğinden silinmez. Devam?', parent=self.root):
            return
        phrase = 'KALICI SIL'
        answer = simpledialog.askstring('Son silme onayı', f'Onay için tam olarak yazın: {phrase}', parent=self.root)
        if answer != phrase or self.busy:
            return
        try:
            self.archive.save_snapshot(client.address, state, data)
            self.archive.remember_delete_intent(client.address, state, data, kind, target)
        except WorkforceError as exc:
            messagebox.showerror('Silme başlatılmadı', str(exc), parent=self.root)
            return
        def work():
            try:
                reply = client.delete_employee(state, target) if kind == 'employee' else client.delete_session(state, target)
            except DeviceError as exc:
                if str(exc).startswith(('HTTP 400:', 'HTTP 401:', 'HTTP 403:', 'HTTP 409:')):
                    self.archive.clear_delete_intent(client.address)
                raise
            return reply, self._read_snapshot(client)
        def done(value):
            self._apply_snapshot(value[1])
            messagebox.showinfo('Kalıcı silme', value[0]['message'] + '\n' +
                (self.archive_warning.get() or 'Cihaz ve yerel arşiv doğrulandı. Eski dışa aktarılmış yedekler değişmedi.'), parent=self.root)
        self.job(work, done)

    def add_manual(self):
        index=self.manual_choice.current()
        try:
            if not self.state or not self.state.get('manual_attendance'):
                raise DeviceError('Manuel mesai için cihaz yazılımı güncel değil.')
            if not 0<=index<len(self.manual_uids):
                raise DeviceError('Önce personeli seçin.')
            if not self.state.get('clock_ok'):
                raise DeviceError('Önce cihaz saatini ayarlayın/kurtarın ve Yenile deyin.')
            uid=self.manual_uids[index]
            employee=next(e for e in self.state['employees'] if e['uid']==uid)
            if employee['inside']:
                raise DeviceError('Açık oturum varken tamamlanmış manuel mesai eklenmez.')
            start_text,end_text=self.manual_start.get(),self.manual_end.get()
            start,end=encode_local_time(start_text),encode_local_time(end_text)
            if end<=start:
                raise DeviceError('Bitiş, başlangıçtan sonra olmalı.')
        except DeviceError as exc:
            messagebox.showerror('Manuel mesai',str(exc),parent=self.root)
            return
        state,client=copy.deepcopy(self.state),self.client
        prompt=(employee['name']+'\nBaşlangıç: '+start_text+'\nBitiş: '+end_text+
                '\nEklenecek süre: '+duration(end-start)+'\n\nGeçmiş korunur; manuel mesai kaydı eklenir.'+
                '\nKontrol edip onaylıyor musunuz?')
        if not messagebox.askyesno('Manuel mesaiyi onayla',prompt,parent=self.root):
            return
        def work():
            reply=client.add_manual(state,uid,start_text,end_text)
            return reply,self._read_snapshot(client)
        def done(value):
            self._apply_snapshot(value[1])
            messagebox.showinfo('Manuel mesai',value[0]['message'],parent=self.root)
        self.job(work,done)

    def load_events(self):
        self.refresh()

    def export_csv(self):
        path = filedialog.asksaveasfilename(parent=self.root, title='Kayıtları dışa aktar', defaultextension='.csv', initialfile='pdks-kayitlar.csv', filetypes=[('Kayıt dosyası', '*.csv')])
        if not path:
            return
        def work():
            value = self._read_snapshot(self.client)
            Path(path).write_bytes(value[1])
            return value
        self.job(work, self._apply_snapshot)

    def set_time(self, recover):
        text = self.time.get()
        try:
            encode_local_time(text)
            if recover:
                DeviceClient.expected(self.state)
        except DeviceError as exc:
            messagebox.showerror('Saat', str(exc), parent=self.root)
            return
        state, client = copy.deepcopy(self.state), self.client
        if recover:
            inside = [e['name'] for e in state['employees'] if e['inside']]
            prompt = ('Saat: ' + text + '\nİptal edilecek açık oturumlar: ' + (', '.join(inside) or 'Yok') +
                      '\n\nTamamlanan süreler ve geçmiş korunur. Açık oturumlara süre eklenmez. Saat kurtarma kaydı yazılır.\n' +
                      'Yeni kayıtlardan sonra eski yazılıma dönülmez.\n\nOnaylamak için KURTAR yazın:')
            if simpledialog.askstring('Yönetici saat kurtarma', prompt, parent=self.root) != 'KURTAR':
                return
            backup = filedialog.asksaveasfilename(parent=self.root, title='Kurtarmadan önce yedek', defaultextension='.csv', initialfile='pdks-kurtarma-oncesi.csv', filetypes=[('Kayıt dosyası', '*.csv')])
            if not backup:
                return
        elif not messagebox.askyesno('Saat ayarı', f'Türkiye saatini {text} olarak ayarla?\nAçık oturum/geriye saat koruması devam eder.', parent=self.root):
            return
        def work():
            if recover:
                Path(backup).write_bytes(client.events())  # if backup fails, never send POST
            reply = client.set_time(state, text, recover)
            return reply, self._read_snapshot(client)
        def done(value):
            self._apply_snapshot(value[1])
            messagebox.showinfo('Saat', value[0]['message'], parent=self.root)
        self.job(work, done)
