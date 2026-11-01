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
        root.title('PDKS Yönetim v' + CURRENT_VERSION + ' — Güncelleme' + (' — DEMO' if demo else ''))
        height = max(680, min(850, root.winfo_screenheight() - 100))
        root.geometry(f'1280x{height}')
        root.minsize(1100, 680)
        root.configure(bg='#eef2f6')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10))
        style.configure('TFrame', background='#eef2f6')
        style.configure('TLabel', background='#eef2f6')
        style.configure('Title.TLabel', font=('Segoe UI', 20, 'bold'), foreground='#17324d')
        style.configure('Treeview', rowheight=29, background='white', fieldbackground='white')
        style.configure('Treeview.Heading', font=('Segoe UI', 10, 'bold'))
        style.configure('TButton', padding=(12, 7))
        base = ttk.Frame(root, padding=18)
        base.pack(fill='both', expand=True)
        ttk.Label(base, text='PDKS Yönetim', style='Title.TLabel').pack(anchor='w')
        ttk.Label(base, text='Puantaj • vardiya ve izinler • raporlar • yerel arşiv • cihaz yönetimi').pack(anchor='w', pady=(2, 12))
        if demo:
            ttk.Label(base, text='DEMO: yalnız geçici örnek veriler; fiziksel cihaza bağlantı YOK.', foreground='#a54200').pack(anchor='w')
        connect = ttk.Frame(base)
        connect.pack(fill='x', pady=(4, 10))
        self.address = tk.StringVar(value='192.168.4.1')
        self.username = tk.StringVar(value='admin')
        self.password = tk.StringVar()
        self.remember = tk.BooleanVar(value=True)
        self.connection_entries = []
        for label, variable, width, show in (('Cihaz IP', self.address, 24, ''), ('Kullanıcı', self.username, 12, ''), ('Şifre', self.password, 20, '•')):
            ttk.Label(connect, text=label).pack(side='left', padx=(0, 5))
            entry = ttk.Entry(connect, textvariable=variable, width=width, show=show)
            entry.pack(side='left', padx=(0, 10))
            self.connection_entries.append(entry)
        self.connect_btn = ttk.Button(connect, text='Bağlan', command=self.connect)
        self.connect_btn.pack(side='left')
        self._button(connect, 'Yenile', self.refresh).pack(side='left', padx=6)
        memory = ttk.Frame(base)
        memory.pack(fill='x', pady=(0, 8))
        self.remember_check = ttk.Checkbutton(memory, text='Şifreyi hatırla', variable=self.remember, command=self._remember_changed)
        self.remember_check.pack(side='left')
        ttk.Label(memory, text='IP ve kullanıcı hatırlanır. Şifre Windows kasasında saklanır; otomatik bağlantı yok.').pack(side='left', padx=10)
        self.status_text = tk.StringVar(value='Bağlı değil. HTTP yalnız güvenilen yerel ağ içindir.')
        ttk.Label(base, textvariable=self.status_text, wraplength=1020, foreground='#304c64').pack(fill='x', pady=(0, 10))
        tabs = ttk.Notebook(base)
        tabs.pack(fill='both', expand=True)
        overview, reports, planning = (ttk.Frame(tabs, padding=10) for _ in range(3))
        tabs.add(overview, text='Genel Bakış')
        tabs.add(reports, text='Puantaj / Rapor')
        tabs.add(planning, text='Vardiya / Takvim')
        self.planning_panel = PlanningPanel(planning, self.archive, on_change=self._policy_changed)
        self.planning_panel.pack(fill='both', expand=True)
        self.reports_panel = ReportsPanel(reports, self.archive, on_source_change=self._offline_source_changed)
        self.reports_panel.pack(fill='both', expand=True)
        self.reports_panel.update_sources()
        self._build_overview(overview)
        people, records, manual, clock = (ttk.Frame(tabs, padding=12) for _ in range(4))
        tabs.add(people, text='  Personel / Kartlar  ')
        tabs.add(records, text='  Giriş / Çıkış Kayıtları  ')
        tabs.add(manual, text='  Manuel Mesai  ')
        tabs.add(clock, text='  Saat ve Kurtarma  ')
        self.people = self._tree(people, ('Ad', 'Kart UID', 'Durum', 'İçeride', 'Tamamlanan süre'), (230, 180, 90, 80, 130))
        self.people.bind('<<TreeviewSelect>>', self.on_select)
        editor = ttk.Frame(people)
        editor.pack(side='bottom', fill='x', before=self.people.master)
        form = ttk.Frame(editor)
        form.pack(fill='x', pady=12)
        self.name, self.card, self.active = tk.StringVar(), tk.StringVar(), tk.BooleanVar(value=True)
        ttk.Label(form, text='Ad soyad').grid(row=0, column=0, sticky='w')
        ttk.Label(form, text='Kart UID').grid(row=0, column=1, sticky='w', padx=12)
        ttk.Entry(form, textvariable=self.name, width=32).grid(row=1, column=0, sticky='ew')
        ttk.Entry(form, textvariable=self.card, width=27).grid(row=1, column=1, padx=12)
        self.active_check = ttk.Checkbutton(form, text='Aktif', variable=self.active)
        self.active_check.grid(row=1, column=2)
        self._button(form, 'Yeni personel', self.new_person).grid(row=1, column=3, padx=8)
        self._button(form, 'Kaydet', self.save_person).grid(row=1, column=4)
        self.identity_text = tk.StringVar(value='Yeni personel: kart UID alanını doldurun veya listeden düzenlenecek kişiyi seçin.')
        ttk.Label(editor, textvariable=self.identity_text, wraplength=960).pack(anchor='w')
        self._button(editor, 'Son okutulan UID’yi forma al', self.use_last_card).pack(anchor='w', pady=8)
        self.delete_person_button = self._button(editor, 'Seçili personeli kalıcı sil…', self.delete_person)
        self.delete_person_button.pack(anchor='w', pady=4)
        ttk.Label(editor, text='Personeli silmek kartı/personel kaydını kaldırır; isimli geçmiş korunur. Açık oturum önce kapatılmalı/kurtarılmalı.', wraplength=960).pack(anchor='w')
        bar = ttk.Frame(records)
        bar.pack(fill='x', pady=(0, 10))
        self._button(bar, 'Kayıtları getir', self.load_events).pack(side='left')
        self._button(bar, 'Ham CSV indir / yedekle', self.export_csv).pack(side='left', padx=8)
        self.delete_session_button = self._button(bar, 'Seçili oturumu kalıcı sil…', self.delete_session)
        self.delete_session_button.pack(side='left', padx=8)
        self.events_tree = self._tree(records, ('Sıra', 'Cihaz yerel tarih/saat', 'Sabit personel UID', 'İşlem', 'Süre / açıklama',
                'Kayıttaki ad', 'Adın kaynağı', 'Silinmiş personel'), (55, 175, 155, 270, 250, 220, 190, 140))
        self.events_tree.configure(displaycolumns=('Sıra', 'Cihaz yerel tarih/saat', 'Kayıttaki ad', 'İşlem',
                'Süre / açıklama', 'Sabit personel UID', 'Adın kaynağı', 'Silinmiş personel'))
        ttk.Label(records, text='M = manuel mesai. R = saat kurtarma, mesai DEĞİLDİR. Eski kayıtlarda ad sonradan eşleştirilir; tarihsel ad olduğu varsayılmaz. Adın kaynağı sütununu kontrol edin.', wraplength=1100).pack(anchor='w', pady=8)
        ttk.Label(manual,text='Geçmiş tarihli tamamlanmış mesai ekle',font=('Segoe UI',13,'bold')).pack(anchor='w',pady=(0,12))
        ttk.Label(manual,text='Personel (sabit geçmiş kimliği)').pack(anchor='w')
        self.manual_choice=ttk.Combobox(manual,state='readonly',width=62)
        self.manual_choice.pack(anchor='w',pady=(3,12))
        self.manual_uids=[]
        now=turkey_now()
        self.manual_start=tk.StringVar(value=format_local_time(encode_local_time(now)-3600))
        self.manual_end=tk.StringVar(value=now)
        for label,variable in (('Başlangıç — Türkiye yerel tarih/saat',self.manual_start),('Bitiş — Türkiye yerel tarih/saat',self.manual_end)):
            ttk.Label(manual,text=label).pack(anchor='w')
            ttk.Entry(manual,textvariable=variable,width=28,font=('Segoe UI',12)).pack(anchor='w',pady=(3,12))
        self.manual_button=self._button(manual,'Manuel mesaiyi ekle…',self.add_manual)
        self.manual_button.pack(anchor='w',pady=8)
        ttk.Label(manual,text='Biçim: YYYY-AA-GG SS:DD:SN. Süre başlangıç/bitişten hesaplanır. Cihaz saati doğru olmalı; gelecekteki bitiş ve açık oturumu olan kişi reddedilir.',wraplength=880).pack(anchor='w',pady=8)
        ttk.Label(manual,text='Çakışan mesai tekrar eklenmez. Yanlış tamamlanmış M kaydı, yeni cihaz yazılımıyla Giriş / Çıkış Kayıtları bölümünden açık onayla silinebilir.',foreground='#a23e20',wraplength=880).pack(anchor='w',pady=8)
        ttk.Label(manual,text='Manuel mesai için cihazda v1.1 firmware gerekir. Program cihaza otomatik yazılım yüklemez.',wraplength=880).pack(anchor='w')
        ttk.Label(clock, text='Türkiye yerel tarih/saat (UTC dönüşümü uygulanmaz)', font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        self.time = tk.StringVar(value=turkey_now())
        timebar = ttk.Frame(clock)
        timebar.pack(fill='x', pady=12)
        ttk.Entry(timebar, textvariable=self.time, width=26, font=('Segoe UI', 13)).pack(side='left')
        ttk.Button(timebar, text='Şu anki Türkiye saati', command=lambda: self.time.set(turkey_now())).pack(side='left', padx=10)
        self._button(clock, 'Normal saat ayarla', lambda: self.set_time(False)).pack(anchor='w')
        ttk.Label(clock, text='Normal işlem açık oturumda ve geriye saat almada reddedilir. Saat bozulup kilitlendiğinde aşağıdaki yönetici kurtarmasını kullanın.', wraplength=880).pack(anchor='w', pady=12)
        self._button(clock, 'Açık oturumları iptal et ve saati kurtar…', lambda: self.set_time(True)).pack(anchor='w')
        ttk.Label(clock, text='Kurtarma: önce CSV yedeği alır; geçmiş satırlarını ve tamamlanan toplamları korur. TÜM açık oturumları süre eklemeden iptal eder, günlüğe R yönetici olayı ekler. Yeniden onay ister; otomatik tekrarlanmaz.', foreground='#a23e20', wraplength=880).pack(anchor='w', pady=12)
        ttk.Label(clock, text='Yeni yönetim firmware’i gerekir. Onaylı imzalı firmware yükleme Güncelleme sekmesindedir; format/uploadfs yapılmaz. Yeni kayıt neslinden sonra eski firmware’e dönmeyin.', wraplength=880).pack(anchor='w', pady=8)
        update_tab = ttk.Frame(tabs, padding=12)
        tabs.add(update_tab, text='Güncelleme')
        self._build_updates(update_tab)
        self.footer = tk.StringVar(value='Bağlan demeden cihaz isteği yoktur. Otomatik yenileme isteğe bağlıdır; otomatik cihaz yazma işlemi yoktur.')
        ttk.Label(base, textvariable=self.footer, wraplength=1150).pack(side='bottom', fill='x', pady=(8, 0), before=tabs)
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
        self.dashboard_text = tk.StringVar(value='Canlı cihaz bağlantısı yok. Puantaj / Rapor bölümünden bilgisayar arşivini açabilirsiniz.')
        self.health_text = tk.StringVar(value='RTC / okuyucu / depolama: henüz okunmadı.')
        bar = ttk.Frame(parent)
        bar.pack(fill='x', pady=(0, 10))
        ttk.Checkbutton(bar, text='Bağlantıdan sonra 30 saniyede bir otomatik oku',
                        variable=self.auto_refresh, command=self._auto_refresh_changed).pack(side='left')
        ttk.Label(bar, text='Yalnız okuma; program açık kalmalı.').pack(side='left', padx=12)
        ttk.Label(parent, textvariable=self.dashboard_text, wraplength=1150, font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=5)
        ttk.Label(parent, textvariable=self.health_text, wraplength=1150).pack(anchor='w', pady=5)
        ttk.Label(parent, textvariable=self.archive_warning, foreground='#a23e20', wraplength=1150).pack(anchor='w', pady=5)
        self.dashboard_tree = self._tree(parent, ('Personel', 'Sabit UID', 'Durum', 'Giriş zamanı', 'Tamamlanan', 'Açık süre (tahmini)'),
                                          (220, 150, 90, 185, 130, 170))
        ttk.Label(parent, text='Açık süre tamamlanan mesaiye eklenmez. Eksik çıkışa otomatik bitiş yazılmaz. '
                  'Devamsızlık için önce vardiya atayın; raporlar son alınan veri ve seçili kurallara göredir.',
                  wraplength=1150).pack(anchor='w', pady=10)

    def _auto_refresh_changed(self):
        self.next_refresh = time.monotonic() + 30

    def _build_updates(self, parent):
        ttk.Label(parent, text='GitHub üzerinden imzalı güncelleme', font=('Segoe UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(parent, text='Kaynak: github.com/kaanekr/pdks-yonetim • Program: v' + CURRENT_VERSION).pack(anchor='w', pady=8)
        self.update_text = tk.StringVar(value='Kendiliğinden indirme/yükleme yok. Önce sürümleri kontrol edin.')
        self.device_update_text = tk.StringVar(value='Cihaz bağlı değil. İlk OTA kurulumu bir kez USB gerektirir.')
        ttk.Label(parent, textvariable=self.update_text, wraplength=1000).pack(anchor='w', pady=8)
        ttk.Label(parent, textvariable=self.device_update_text, wraplength=1000).pack(anchor='w', pady=8)
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=12)
        self.updates_check_btn = ttk.Button(row, text='Sürümleri kontrol et', command=self.check_updates)
        self.updates_check_btn.pack(side='left')
        self.updates_download_btn = ttk.Button(row, text='Dosyaları indir / hazırla', command=self.download_updates)
        self.updates_download_btn.pack(side='left', padx=8)
        self.updates_app_btn = ttk.Button(parent, text='Windows programını güncelle…', command=self.install_app_update)
        self.updates_app_btn.pack(anchor='w', pady=8)
        self.updates_firmware_btn = ttk.Button(parent, text='Cihaz firmware’ini kablosuz güncelle…', command=self.install_firmware_update)
        self.updates_firmware_btn.pack(anchor='w', pady=8)
        ttk.Label(parent, text='İmza + SHA256 + hedef/sürüm kontrolü zorunlu. Eski sürüme otomatik dönüş yok. '
            'Program güncellemesinde eski EXE yalnız sağlık kontrolü sırasında tutulur; yeni sürüm doğrulanınca silinir. Yeni sürüm açılmazsa geri alınır. '
            'Kayıt/ayar dosyaları değiştirilmez. Firmware yalnız aynı ağdaki, OTA destekli ve açık oturumu olmayan cihaza gönderilir.',
            wraplength=1000).pack(anchor='w', pady=10)
        ttk.Label(parent, text='Cihazın kurulum Wi-Fi ağında internet olmayabilir: dosyaları önce internet varken indirin, '
            'programı kapatmadan cihaz ağına geçip bağlanın. İlk özel USB kurulumu ve her yükleme ayrı onay ister. '
            'DEMO gerçek güncelleme indirmez/yüklemez.', wraplength=1000, foreground='#a23e20').pack(anchor='w', pady=8)

    def check_updates(self):
        if self.demo or self.busy:
            return
        from windows_app.updates import check_release
        self.update_text.set('GitHub sürümü ve yayıncı imzası kontrol ediliyor…')
        def done(release):
            self.update_release, self.update_downloads = release, {}
            manifest = release.manifest
            self.update_text.set('Doğrulanan sürüm: v' + manifest['version'] +
                (' — programınız güncel.' if manifest['sequence'] <= CURRENT_SEQUENCE else ' — yeni Windows sürümü var.') +
                '\n' + manifest['notes'][:1500])
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
        self.update_text.set('İmzalı Windows ve firmware dosyaları indiriliyor; henüz yükleme yapılmıyor…')
        def done(paths):
            self.update_downloads = paths
            self.update_text.set('Dosyalar doğrulandı. Cihaz ağına geçebilirsiniz; programı açık tutun. Yükleme için ayrıca düğmeye basıp onaylayın.')
        self.job(lambda: {kind: self._update_path(release, kind) for kind in ('windows', 'firmware')}, done)

    def install_app_update(self):
        if self.demo or self.busy or self.update_release is None:
            return
        release = self.update_release
        if release.manifest['sequence'] <= CURRENT_SEQUENCE or not getattr(sys, 'frozen', False):
            messagebox.showinfo('Program güncelleme', 'Yeni sürüm yok veya paketli EXE kullanılmıyor.', parent=self.root)
            return
        if not messagebox.askyesno('Programı güncelle', 'v' + release.manifest['version'] +
                ' doğrulanıp yüklenecek. Program kapanıp yeniden açılır; eski EXE sağlık kontrolünden sonra silinir. Yeni sürüm açılmazsa geri alınır. Kaydedilmemiş formlarınızı tamamladınız mı?', parent=self.root):
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
        self.update_text.set('Yeni Windows EXE doğrulanıyor…')
        self.job(lambda: self._update_path(release, 'windows'), done)

    def install_firmware_update(self):
        if self.demo or self.busy or self.update_release is None or not self.client or not self.state:
            return
        release, client = self.update_release, self.client
        if (self.state.get('ota_update') is not True or self.state.get('firmware_target') != FIRMWARE_TARGET or
                self.state.get('storage_ok') is not True):
            messagebox.showerror('Firmware güncelleme', 'Bu cihazda önce özel USB / OTA kurulumu gerekiyor.', parent=self.root)
            return
        if not messagebox.askyesno('İmzalı firmware yükle', 'Cihaz aynı ağda olmalı ve açık oturum bulunmamalı. '
                'Önce PC arşivi ZIP yedeği alınır, sonra imzalı v' + release.manifest['firmware']['version'] +
                ' yüklenir. Cihaz yeniden başlar; elektrik kesilmemeli. Günlük/Wi-Fi/şifre dosyaları korunur. Onaylıyor musunuz?', parent=self.root):
            return
        backup = filedialog.asksaveasfilename(parent=self.root, title='Firmware öncesi PC arşivi yedeği',
            defaultextension='.zip', initialfile='PDKS_OTA_oncesi_arsiv.zip', filetypes=[('ZIP arşivi', '*.zip')])
        if not backup:
            return
        self.update_text.set('Firmware doğrulanıyor, arşiv yedekleniyor ve onaylı OTA gönderiliyor…')
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
            self.update_text.set('İmzalı yükleme cihaz tarafından onaylandı. Yeniden başladıktan sonra Yenile ile sürümü kontrol edin; otomatik tekrar yapılmaz.')
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
        link = 'DEMO' if self.demo else ('Bağlı' if self.online else 'Bağlantı yok / eski veri')
        stamp = self.last_success_at or 'henüz tutarlı kayıt görüntüsü alınmadı'
        self.dashboard_text.set(f'{link} | İçeride: {inside} / {len(people)} | Son veri: {stamp} | {elapsed} sn önce')
        def health(key):
            return 'OK' if self.state.get(key) else 'HATA / bilinmiyor'
        drift = ''
        if self.state.get('clock_ok') and self.state.get('now'):
            difference = int(self.state['now']) + (elapsed if self.online else 0) - encode_local_time(turkey_now())
            if abs(difference) > 120:
                drift = f' | PC Türkiye saatiyle fark: {difference:+d} sn — iki saati kontrol edin'
        self.health_text.set('Saat: ' + health('clock_ok') + ' | Depo: ' + health('storage_ok') +
                             ' | RFID: ' + health('rfid_ok') + ' | SD ayna: ' +
                             (health('sd_ok') if self.state.get('sd_enabled') else 'kapalı') +
                             drift + ' | Saat kurtarma kaydı mesai değildir.')
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

    def _button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
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
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
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
        self.device_update_text.set('Cihaz firmware: ' + str(state.get('firmware_version', 'eski sürüm')) +
            (' — imzalı OTA destekleniyor.' if state.get('ota_update') else ' — ilk OTA desteği için bir kez özel USB kurulumu gerekli.'))
        index=self.manual_choice.current()
        manual_uid=self.manual_uids[index] if 0<=index<len(self.manual_uids) else None
        self.manual_uids=[e['uid'] for e in state['employees']]
        self.manual_choice['values']=[e['name']+'  ['+e['uid']+']'+(' — pasif' if not e.get('active',True) else '') for e in state['employees']]
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
        clock = format_local_time(state.get('now')) if state.get('clock_ok') else 'HATALI — ' + str(state.get('clock_reason', ''))
        self.status_text.set(f"{self.client.address}  |  Saat: {clock}  |  İçeride: {inside}  |  Depo: {'OK' if state.get('storage_ok') else 'HATA'}")
        if not state.get('personnel_management'):
            self.identity_text.set('Eski firmware: ad ekle/düzenle çalışır; kart değiştirme, pasife alma ve saat kurtarma için güncelleme gerekli.')
        self._enable()
        self._render_dashboard()

    def new_person(self):
        self.selected_uid = None
        self.name.set('')
        self.card.set('')
        self.active.set(True)
        self.people.selection_remove(self.people.selection())
        self.identity_text.set('Yeni personel. Kaydetmeden önce kart UID ve adı kontrol edin.')

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
            self.identity_text.set('Sabit geçmiş/personel UID: ' + e['uid'] + ' — kart değişse de geçmiş bu kimlikte kalır.')

    def use_last_card(self):
        try:
            self.card.set(canonical_uid(self.state.get('last_uid', '')))
        except DeviceError as exc:
            messagebox.showerror('Kart', str(exc), parent=self.root)

    def save_person(self):
        try:
            card, name = canonical_uid(self.card.get()), checked_name(self.name.get())
        except DeviceError as exc:
            messagebox.showerror('Personel', str(exc), parent=self.root)
            return
        uid = self.selected_uid or card
        create = self.selected_uid is None
        active, state, client = self.active.get(), copy.deepcopy(self.state), self.client
        if not messagebox.askyesno('Personeli kaydet', f"{'Yeni personel' if create else 'Personel güncelleme'}\n{name}\nKart: {card}\n{'Aktif' if active else 'PASİF'}\n\nGeçmiş silinmez. Onaylıyor musunuz?", parent=self.root):
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
                detail = f'{person["name"]}\nSabit UID: {target}\nPersonel/kart kaldırılır; isimli geçmiş kayıtlar KALIR.'
            else:
                sequences = deletion_sequences(events, target)
                selected = [event for event in events if event['sequence'] in sequences]
                person = selected[0]
                name = person.get('name') or next((p['name'] for p in state['employees'] if p['uid'] == person['uid']), person['uid'])
                detail = (f'{name} · {person["uid"]}\nSilinecek sıra: {", ".join(map(str, sorted(sequences)))}\n' +
                    '\n'.join(f'{format_local_time(event["epoch"])} · {event["event"]}' for event in selected) +
                    '\nGiriş/çıkış birlikte, M tek kayıt olarak kalıcı kaldırılır. Toplamlar yeniden hesaplanır.')
        except (DeviceError, WorkforceError, ValueError) as exc:
            messagebox.showerror('Kalıcı silme', str(exc), parent=self.root)
            return
        if not messagebox.askyesno('GERİ ALINAMAZ — kalıcı silme', detail +
                '\n\nCihaz, SD aynası ve bu PC arşivi güncellenir. Eski ZIP/CSV/PDF/Excel yedekleri kendiliğinden silinmez. Devam?', parent=self.root):
            return
        phrase = f'SIL {target}'
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
                raise DeviceError('Manuel mesai için cihazı v1.1 firmware’e güncellemek gerekiyor.')
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
        prompt=(employee['name']+' ['+uid+']\nBaşlangıç: '+start_text+'\nBitiş: '+end_text+
                '\nEklenecek süre: '+duration(end-start)+'\n\nGeçmiş korunur, M yönetici kaydı eklenir.'+
                '\nBu sürümde kaydı geri alma/silme yoktur. Kontrol edip onaylıyor musunuz?')
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
        path = filedialog.asksaveasfilename(parent=self.root, title='Ham olay günlüğünü yedekle', defaultextension='.csv', initialfile='pdks-kayitlar.csv', filetypes=[('CSV', '*.csv')])
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
                      '\n\nTamamlanan süreler ve geçmiş korunur. Açık oturumlara süre eklenmez. R yönetici kaydı yazılır.\n' +
                      'Yeni günlük/personel biçiminden sonra eski firmware’e dönülmez.\n\nOnaylamak için KURTAR yazın:')
            if simpledialog.askstring('Yönetici saat kurtarma', prompt, parent=self.root) != 'KURTAR':
                return
            backup = filedialog.asksaveasfilename(parent=self.root, title='Kurtarmadan ÖNCE zorunlu CSV yedeği', defaultextension='.csv', initialfile='pdks-kurtarma-oncesi.csv', filetypes=[('CSV', '*.csv')])
            if not backup:
                return
        elif not messagebox.askyesno('Saat ayarı', f'Türkiye yerel saatini {text} olarak ayarla?\nAçık oturum/geriye saat koruması devam eder.', parent=self.root):
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
