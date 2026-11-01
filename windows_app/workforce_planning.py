"""Local-only workforce planning UI: policy edits only, never device or credential access."""
import copy
import uuid
import tkinter as tk
from tkinter import messagebox, ttk

from windows_app.client import DeviceError, canonical_uid
from windows_app.tree_state import row_key, sync_rows
from windows_app.workforce_model import (WorkforceError, config_hash, default_config,
                                          validate_config)

WEEKDAYS = ('Pazartesi', 'Salı', 'Çarşamba', 'Perşembe', 'Cuma', 'Cumartesi', 'Pazar')
LEAVE_KINDS = ('Yıllık izin', 'Rapor', 'Ücretsiz izin', 'Diğer')
NUMERIC_FIELDS = ('break_minutes', 'break_after_minutes', 'late_tolerance',
                  'early_tolerance', 'overtime_after_minutes')


class PlanningPanel(ttk.Frame):
    """Tk planning panel. The injected store owns persistence; this class is local policy UI."""

    def __init__(self, parent, store, on_change=None):
        super().__init__(parent)
        self.store = store
        self.on_change = on_change
        self.source = None
        self.status = {}
        self.enabled = False
        self._saved = None
        self._config = default_config()
        self._shift_choices = {}
        self._person_names = {}
        self._person_labels = {}
        self._editing_shift_id = None
        self._build()
        self.set_source(None)

    # ----- public API -----------------------------------------------------
    @property
    def config(self):
        return self.current_config()

    def current_config(self):
        return copy.deepcopy(self._config)

    def saved_config(self):
        return copy.deepcopy(self._saved) if self._saved is not None else None

    def has_unsaved_changes(self):
        return self._saved is not None and self._config != self._saved

    def set_source(self, source, status=None):
        """Load one source's local policy. Invalid policy leaves the panel disabled."""
        for tree in (self.shift_list, self.assignment_list, self.leave_list, self.holiday_list, self.people_list):
            tree.selection_remove(tree.selection())
            tree.focus('')
        self.source = source or None
        self._editing_shift_id = None
        self.status = status if isinstance(status, dict) else {}
        self._saved = None
        self._config = default_config()
        self.enabled = False
        if not self.source:
            self.status_var.set('Kaynak seçilmedi; değişiklik yapılamaz. Önce cihaz/kaynak seçin.')
            self._apply_enabled()
            self.refresh()
            return False
        try:
            data = validate_config(self.store.load_config(self.source))
        except Exception as exc:
            self.status_var.set('Yerel ayarlar yüklenemedi: ' + self._message(exc))
            self._apply_enabled()
            self.refresh()
            return False
        self._saved = copy.deepcopy(data)
        self._config = copy.deepcopy(data)
        self.enabled = True
        self.status_var.set(f'{len(data["shifts"])} vardiya • {len(data["assignments"])} atama • '
                            'yerel ayarlar hazır')
        self._apply_enabled()
        self.refresh()
        self._load_person_meta()
        return True

    def select_shift(self, shift_id):
        if any(shift['id'] == shift_id for shift in self._config['shifts']):
            self.shift_list.selection_set(shift_id)
            self._load_shift(shift_id)
            return True
        return False

    def update_status(self, status):
        """Refresh personnel choices without discarding unsaved local policy edits."""
        self.status = copy.deepcopy(status) if isinstance(status, dict) else {}
        self._refresh_choices()

    def choose_shift(self, shift_id):
        for label, value in self._shift_choices.items():
            if value == shift_id:
                self.assign_shift.set(label)
                return True
        return False

    def add_shift(self):
        if not self._guard():
            return False
        try:
            shift = self._shift_payload(uuid.uuid4().hex)
        except WorkforceError as exc:
            messagebox.showerror('Vardiya', str(exc), parent=self)
            return False
        candidate = copy.deepcopy(self._config)
        candidate['shifts'].append(shift)
        if self._validate_or_error(candidate, 'Vardiya') is None:
            return False
        self._config = candidate
        self.refresh()
        self.select_shift(shift['id'])
        return True

    def update_shift(self):
        if not self._guard():
            return False
        shift_id = self._selected_shift()
        if not shift_id:
            messagebox.showwarning('Vardiya', 'Güncellenecek vardiyayı listeden seçin.', parent=self)
            return False
        try:
            shift = self._shift_payload(shift_id)
        except WorkforceError as exc:
            messagebox.showerror('Vardiya', str(exc), parent=self)
            return False
        candidate = copy.deepcopy(self._config)
        candidate['shifts'] = [shift if item['id'] == shift_id else item
                               for item in candidate['shifts']]
        if self._validate_or_error(candidate, 'Vardiya') is None:
            return False
        self._config = candidate
        self.refresh()
        self.select_shift(shift_id)
        return True

    def delete_shift(self):
        if not self._guard():
            return False
        shift_id = self._selected_shift()
        if not shift_id:
            messagebox.showwarning('Vardiya', 'Silinecek vardiyayı listeden seçin.', parent=self)
            return False
        if any(item['shift_id'] == shift_id for item in self._config['assignments']):
            messagebox.showerror('Vardiya silinemedi',
                                 'Bu vardiyaya bağlı atamalar var. Önce Atamalar bölümünde '
                                 'atamaları açıkça silin; otomatik kaldırma yapılmaz.', parent=self)
            return False
        if not messagebox.askyesno('Vardiyayı sil',
                                   f'{self._shift_name(shift_id)} yalnız yerel plandan kaldırılacak. '
                                   'Ham kayıtlar ve cihaz personeli değişmez. Onaylıyor musunuz?',
                                   parent=self):
            return False
        candidate = copy.deepcopy(self._config)
        candidate['shifts'] = [item for item in candidate['shifts'] if item['id'] != shift_id]
        self._config = candidate
        self.refresh()
        return True

    def add_assignment(self):
        if not self._guard():
            return False
        try:
            item = self._assignment_payload()
            candidate = copy.deepcopy(self._config)
            candidate['assignments'].append(item)
            data = self._validate_or_error(candidate, 'Atama')
        except WorkforceError as exc:
            messagebox.showerror('Atama', str(exc), parent=self)
            return False
        if data is None:
            return False
        if not messagebox.askyesno(
                'Atama ekle',
                f'{self._person_label(item["uid"])} → {self._shift_name(item["shift_id"])}\n'
                f'{item["effective_from"]} – {item["effective_to"] or "açık"}\n\n'
                'Yalnız yerel plana eklenir; cihaz personeli değişmez. Onaylıyor musunuz?',
                parent=self):
            return False
        self._config = data
        self.refresh()
        return True

    def add_department_assignment(self):
        """Stage one validated atomic assignment batch for a local department."""
        if not self._guard():
            return False
        department = self.assign_department.get()
        people = [person for person, meta in self._config['people'].items()
                  if department and meta['department'] == department and person in self._person_names]
        if not people:
            messagebox.showwarning('Bölüm ataması', 'Önce Personel Bilgisi bölümünde bölüm tanımlayın ve seçin.', parent=self)
            return False
        try:
            template = self._assignment_payload()
        except WorkforceError as exc:
            messagebox.showerror('Bölüm ataması', str(exc), parent=self)
            return False
        candidate = copy.deepcopy(self._config)
        candidate['assignments'].extend(dict(template, uid=person) for person in people)
        data = self._validate_or_error(candidate, 'Bölüm ataması')
        if data is None:
            return False
        if not messagebox.askyesno('Toplu yerel atama',
                f'{department}: {len(people)} personele {self._shift_name(template["shift_id"])} atanacak.\n'
                'Tarih aralığı formdaki gibidir. Yalnız yerel taslağa eklenir; ardından Kaydet gerekir. Onaylıyor musunuz?', parent=self):
            return False
        self._config = data
        self.refresh()
        return True

    def remove_assignment(self):
        if not self._guard():
            return False
        index = self._selected_index(self.assignment_list)
        if index is None:
            messagebox.showwarning('Atama', 'Kaldırılacak atamayı listeden seçin.', parent=self)
            return False
        item = self._config['assignments'][index]
        if not messagebox.askyesno(
                'Atamayı kaldır',
                f'{self._person_label(item["uid"])} → {self._shift_name(item["shift_id"])} '
                'yalnız yerel plandan kaldırılacak; ham kayıtlar silinmez. Onaylıyor musunuz?',
                parent=self):
            return False
        candidate = copy.deepcopy(self._config)
        candidate['assignments'].pop(index)
        data = self._validate_or_error(candidate, 'Atama')
        if data is None:
            return False
        self._config = data
        self.refresh()
        return True

    def add_leave(self):
        if not self._guard():
            return False
        kind = self.leave_kind.get().strip()
        if kind not in LEAVE_KINDS:
            messagebox.showerror('İzin', 'İzin türü Yıllık izin, Rapor, Ücretsiz izin veya Diğer '
                                 'olmalı.', parent=self)
            return False
        item = {'uid': self._person_uid(self.leave_person.get()), 'start': self.leave_start.get().strip(),
                'end': self.leave_end.get().strip(), 'kind': kind}
        candidate = copy.deepcopy(self._config)
        candidate['leaves'].append(item)
        data = self._validate_or_error(candidate, 'İzin')
        if data is None:
            return False
        if not messagebox.askyesno('İzin ekle',
                                   f'{self._person_label(item["uid"])}\n{item["start"]} – '
                                   f'{item["end"]}\n{kind}\n\nYalnız yerel plana eklenir. '
                                   'Onaylıyor musunuz?', parent=self):
            return False
        self._config = data
        self.refresh()
        return True

    def remove_leave(self):
        if not self._guard():
            return False
        index = self._selected_index(self.leave_list)
        if index is None:
            messagebox.showwarning('İzin', 'Kaldırılacak izni listeden seçin.', parent=self)
            return False
        item = self._config['leaves'][index]
        if not messagebox.askyesno('İzni kaldır',
                                   f'{self._person_label(item["uid"])} {item["start"]} – '
                                   f'{item["end"]} yalnız yerel plandan kaldırılacak; ham kayıtlar '
                                   'silinmez. Onaylıyor musunuz?', parent=self):
            return False
        candidate = copy.deepcopy(self._config)
        candidate['leaves'].pop(index)
        data = self._validate_or_error(candidate, 'İzin')
        if data is None:
            return False
        self._config = data
        self.refresh()
        return True

    def add_holiday(self):
        if not self._guard():
            return False
        day = self.holiday_date.get().strip()
        if day in self._config['holidays']:
            messagebox.showerror('Tatil', 'Bu tarih zaten tatil olarak kayıtlı; önce silin.',
                                 parent=self)
            return False
        candidate = copy.deepcopy(self._config)
        candidate['holidays'][day] = self.holiday_name.get().strip()
        data = self._validate_or_error(candidate, 'Tatil')
        if data is None:
            return False
        if not messagebox.askyesno('Tatil ekle',
                                   f'{day} — {self.holiday_name.get().strip()}\n\nYalnız yerel '
                                   'plana eklenir. Onaylıyor musunuz?', parent=self):
            return False
        self._config = data
        self.refresh()
        return True

    def remove_holiday(self):
        if not self._guard():
            return False
        selection = self.holiday_list.selection()
        if not selection:
            messagebox.showwarning('Tatil', 'Kaldırılacak tatili listeden seçin.', parent=self)
            return False
        day = selection[0]
        if not messagebox.askyesno('Tatili kaldır',
                                   f'{day} yalnız yerel plandan kaldırılacak; ham kayıtlar '
                                   'silinmez. Onaylıyor musunuz?', parent=self):
            return False
        candidate = copy.deepcopy(self._config)
        candidate['holidays'].pop(day, None)
        data = self._validate_or_error(candidate, 'Tatil')
        if data is None:
            return False
        self._config = data
        self.refresh()
        return True

    def save_person_metadata(self):
        if not self._guard():
            return False
        person = self._person_uid(self.meta_person.get())
        if not person:
            messagebox.showerror('Personel bilgisi', 'Personel seçin.', parent=self)
            return False
        candidate = copy.deepcopy(self._config)
        candidate['people'][person] = {'department': self.meta_department.get().strip(),
                                       'employee_no': self.meta_employee_no.get().strip()}
        data = self._validate_or_error(candidate, 'Personel bilgisi')
        if data is None:
            return False
        self._config = data
        return self._save('Personel Bilgisi')

    def save_shifts(self):
        return self._save('Vardiyalar')

    def save_assignments(self):
        return self._save('Atamalar')

    def save_leaves(self):
        return self._save('İzin / Tatil')

    def save_holidays(self):
        return self._save('İzin / Tatil')

    def refresh(self):
        self._refresh_choices()
        self._render_shifts()
        self._render_assignments()
        self._render_leaves()
        self._render_holidays()
        self._render_people()
        if self.has_unsaved_changes():
            self.status_var.set('Kaydedilmemiş yerel taslak var — uygulamak için Kaydet düğmesine basın.')

    # ----- persistence ----------------------------------------------------
    def _save(self, section):
        if not self._guard():
            return False
        data = self._validate_or_error(copy.deepcopy(self._config), 'Puantaj ayarları')
        if data is None:
            return False
        if not messagebox.askyesno(
                'Yerel politikayı kaydet',
                f'{section} ayarları yerel olarak kaydedilecek.\n\n'
                'Raporlar yeni ayarlarla yeniden hesaplanır; '
                'eski ham kayıtlar değişmez. Onaylıyor musunuz?', parent=self):
            return False
        try:
            result = self.store.save_config(self.source, copy.deepcopy(data))
            if isinstance(result, dict) and result.get('ok') is False:
                raise RuntimeError(result.get('message') or 'Kaydetme reddedildi.')
        except Exception as exc:
            self.status_var.set('Kaydetme başarısız; önceki ayarlar korunuyor. ' + self._message(exc))
            messagebox.showerror('Kaydetme başarısız',
                                 'Yerel ayarlar kaydedilemedi; önceki ayarlar korunuyor.\n' +
                                 self._message(exc), parent=self)
            return False
        self._saved = copy.deepcopy(data)
        self._config = copy.deepcopy(data)
        self.status_var.set(f'{section} kaydedildi.')
        self.refresh()
        if self.on_change:
            self.on_change()
        return True

    def _guard(self):
        if self.enabled and self.source:
            return True
        messagebox.showwarning('Puantaj planlama',
                               'Kaynak seçilmedi veya yerel ayarlar yüklenemedi; değişiklik '
                               'yapılamaz.', parent=self)
        return False

    def _validate_or_error(self, candidate, title):
        try:
            return validate_config(candidate)
        except WorkforceError as exc:
            messagebox.showerror(title, str(exc), parent=self)
        except Exception as exc:
            messagebox.showerror(title, 'Ayarlar doğrulanamadı: ' + self._message(exc), parent=self)
        return None

    @staticmethod
    def _message(exc):
        return str(exc).strip() or exc.__class__.__name__

    # ----- build UI -------------------------------------------------------
    def _build(self):
        self.shift_name = tk.StringVar()
        self.shift_start = tk.StringVar(value='09:00')
        self.shift_end = tk.StringVar(value='18:00')
        self.shift_days = [tk.BooleanVar(value=index < 5) for index in range(7)]
        self.shift_break_minutes = tk.StringVar(value='0')
        self.shift_break_after_minutes = tk.StringVar(value='0')
        self.shift_late_tolerance = tk.StringVar(value='0')
        self.shift_early_tolerance = tk.StringVar(value='0')
        self.shift_overtime_after_minutes = tk.StringVar(value='0')
        self.assign_person = tk.StringVar(value='*')
        self.assign_shift = tk.StringVar()
        self.assign_from = tk.StringVar()
        self.assign_to = tk.StringVar()
        self.assign_department = tk.StringVar()
        self.leave_person = tk.StringVar()
        self.leave_start = tk.StringVar()
        self.leave_end = tk.StringVar()
        self.leave_kind = tk.StringVar(value=LEAVE_KINDS[0])
        self.holiday_date = tk.StringVar()
        self.holiday_name = tk.StringVar()
        self.meta_person = tk.StringVar()
        self.meta_department = tk.StringVar()
        self.meta_employee_no = tk.StringVar()

        header = ttk.Frame(self, padding=(10, 6, 10, 0))
        header.pack(fill='x')
        ttk.Label(header, text='Puantaj planlama (yalnız yerel)',
                  font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        self.source_var = tk.StringVar(value='Cihaz/arşiv: seçilmedi')
        ttk.Label(header, textvariable=self.source_var).pack(anchor='w')
        ttk.Label(header, text='Ekle/Güncelle taslağı değiştirir; Kaydet kalıcılaştırır. Cihaza yazılmaz.',
                  foreground='#304c64').pack(anchor='w')
        self.status_var = tk.StringVar()
        ttk.Label(header, textvariable=self.status_var, foreground='#304c64',
                  wraplength=960).pack(anchor='w', pady=(2, 6))
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self._build_shifts()
        self._build_assignments()
        self._build_leave()
        self._build_people()
        self._bind_form_variables()

    def _build_shifts(self):
        tab = self._scroll_tab('Vardiyalar')
        ttk.Label(tab, text='Vardiya ekle/güncelle. Bitiş başlangıçtan önceyse gece vardiyasıdır; '
                  'aynı saat yasaktır. 0 = mola/gecikme düşümü yok; fazla mesai eşiği 0 ise '
                  'vardiya planı net süresini izler. Ücret/hukuk hesabı yapılmaz.',
                  wraplength=940, foreground='#304c64').pack(anchor='w', pady=(0, 6))
        pane = ttk.Frame(tab)
        pane.pack(fill='both', expand=True)
        self.shift_list = self._tree(
            pane, ('name', 'start', 'end', 'days'), ('Vardiya', 'Başlangıç', 'Bitiş', 'Günler'),
            (150, 70, 70, 250), 7)
        self.shift_list.bind('<<TreeviewSelect>>', self._on_shift_select)
        form = ttk.Frame(pane, padding=(12, 0, 0, 0))
        form.pack(side='left', fill='y')
        self._field(form, 0, 'Ad', self._entry(form, 0, self.shift_name, 26))
        self._field(form, 1, 'Başlangıç SS:DD', self._entry(form, 1, self.shift_start, 10))
        self._field(form, 2, 'Bitiş SS:DD', self._entry(form, 2, self.shift_end, 10))
        ttk.Label(form, text='Günler (0=Pazartesi … 6=Pazar)').grid(
            row=3, column=0, columnspan=2, sticky='w', pady=(4, 0))
        days = ttk.Frame(form)
        days.grid(row=4, column=0, columnspan=2, sticky='w')
        for index, label in enumerate(('Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt', 'Paz')):
            ttk.Checkbutton(days, text=label, variable=self.shift_days[index]).pack(side='left')
        rows = (('Mola dk', self.shift_break_minutes),
                ('Mola düşme eşiği dk', self.shift_break_after_minutes),
                ('Geç tolerans dk', self.shift_late_tolerance),
                ('Erken tolerans dk', self.shift_early_tolerance),
                ('Fazla mesai eşiği dk', self.shift_overtime_after_minutes))
        for offset, (label, variable) in enumerate(rows):
            self._field(form, 5 + offset, label, self._entry(form, 5 + offset, variable, 10))
        buttons = ttk.Frame(form)
        buttons.grid(row=10, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Button(buttons, text='Ekle', command=self.add_shift).pack(side='left')
        ttk.Button(buttons, text='Güncelle', command=self.update_shift).pack(side='left', padx=4)
        ttk.Button(buttons, text='Sil', command=self.delete_shift).pack(side='left')
        ttk.Button(buttons, text='Kaydet', command=self.save_shifts).pack(side='left', padx=4)

    def _build_assignments(self):
        tab = self._scroll_tab('Atamalar')
        ttk.Label(tab, text='* tüm personel varsayılanıdır; kişisel atama * varsayılanını geçersiz '
                  'kılar. Tarihler dahildir; boş bitiş açık uçludur. Aynı kişi için çakışan '
                  'aralıklar reddedilir. Silme yalnız yerel plandan kaldırır.',
                  wraplength=940, foreground='#304c64').pack(anchor='w', pady=(0, 6))
        pane = ttk.Frame(tab)
        pane.pack(fill='both', expand=True)
        self.assignment_list = self._tree(
            pane, ('person', 'shift', 'from', 'to'), ('Personel', 'Vardiya', 'Başlangıç', 'Bitiş'),
            (180, 190, 95, 95), 7)
        form = ttk.Frame(pane, padding=(12, 0, 0, 0))
        form.pack(side='left', fill='y')
        self.assign_person_combo = self.assign_combo(form, 0, self.assign_person, ('*',), 26,
                                                     readonly=True)
        self._field(form, 0, 'Personel (* tümü)', self.assign_person_combo)
        self.assign_shift_combo = self.assign_combo(form, 1, self.assign_shift, (), 26,
                                                    readonly=True)
        self._field(form, 1, 'Vardiya', self.assign_shift_combo)
        self._field(form, 2, 'Başlangıç YYYY-AA-GG', self._entry(form, 2, self.assign_from, 16))
        self._field(form, 3, 'Bitiş (boş = açık)', self._entry(form, 3, self.assign_to, 16))
        self.assign_department_combo = self.assign_combo(form, 4, self.assign_department, (), 26, readonly=True)
        self._field(form, 4, 'Bölüm (toplu atama)', self.assign_department_combo)
        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Button(buttons, text='Ekle', command=self.add_assignment).pack(side='left')
        ttk.Button(buttons, text='Kaldır', command=self.remove_assignment).pack(side='left', padx=4)
        ttk.Button(buttons, text='Kaydet', command=self.save_assignments).pack(side='left')
        ttk.Button(form, text='Bölümdeki personele ekle…', command=self.add_department_assignment).grid(
            row=6, column=0, columnspan=2, sticky='w', pady=6)

    def _build_leave(self):
        tab = self._scroll_tab('İzin / Tatil')
        ttk.Label(tab, text='İzinler tam gündür; saat girilmez. Tatiller tarih + ad olarak yerel '
                  'plana eklenir. Otomatik izin hakkı/ücret hesabı yapılmaz; silme ham kayıtları '
                  'değiştirmez.', wraplength=940, foreground='#304c64').pack(anchor='w', pady=(0, 6))
        sub = ttk.Notebook(tab)
        sub.pack(fill='both', expand=True)
        leaves = ttk.Frame(sub, padding=6)
        sub.add(leaves, text='İzinler')
        pane = ttk.Frame(leaves)
        pane.pack(fill='both', expand=True)
        self.leave_list = self._tree(pane, ('person', 'start', 'end', 'kind'),
                                     ('Personel', 'Başlangıç', 'Bitiş', 'Tür'), (160, 90, 90, 130), 5)
        form = ttk.Frame(pane, padding=(12, 0, 0, 0))
        form.pack(side='left', fill='y')
        self.leave_person_combo = self.assign_combo(form, 0, self.leave_person, (), 22,
                                                    readonly=True)
        self._field(form, 0, 'Personel', self.leave_person_combo)
        self._field(form, 1, 'Başlangıç YYYY-AA-GG', self._entry(form, 1, self.leave_start, 16))
        self._field(form, 2, 'Bitiş YYYY-AA-GG', self._entry(form, 2, self.leave_end, 16))
        self.leave_kind_combo = self.assign_combo(form, 3, self.leave_kind, LEAVE_KINDS, 20,
                                                  readonly=True)
        self._field(form, 3, 'Tür', self.leave_kind_combo)
        buttons = ttk.Frame(form)
        buttons.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Button(buttons, text='Ekle', command=self.add_leave).pack(side='left')
        ttk.Button(buttons, text='Kaldır', command=self.remove_leave).pack(side='left', padx=4)
        ttk.Button(buttons, text='Kaydet', command=self.save_leaves).pack(side='left')

        holidays = ttk.Frame(sub, padding=6)
        sub.add(holidays, text='Tatiller')
        pane = ttk.Frame(holidays)
        pane.pack(fill='both', expand=True)
        self.holiday_list = self._tree(pane, ('date', 'name'), ('Tarih', 'Tatil'),
                                       (110, 300), 5)
        form = ttk.Frame(pane, padding=(12, 0, 0, 0))
        form.pack(side='left', fill='y')
        self._field(form, 0, 'Tarih YYYY-AA-GG', self._entry(form, 0, self.holiday_date, 16))
        self._field(form, 1, 'Ad', self._entry(form, 1, self.holiday_name, 24))
        buttons = ttk.Frame(form)
        buttons.grid(row=2, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Button(buttons, text='Ekle', command=self.add_holiday).pack(side='left')
        ttk.Button(buttons, text='Kaldır', command=self.remove_holiday).pack(side='left', padx=4)
        ttk.Button(buttons, text='Kaydet', command=self.save_holidays).pack(side='left')

    def _build_people(self):
        tab = self._scroll_tab('Personel Bilgisi')
        ttk.Label(tab, text='Yalnız yerel puantaj bilgisi: bölüm ve sicil. Cihaz personel '
                  'kaydına yazılmaz.', wraplength=940,
                  foreground='#304c64').pack(anchor='w', pady=(0, 6))
        pane = ttk.Frame(tab)
        pane.pack(fill='both', expand=True)
        self.people_list = self._tree(pane, ('uid', 'name', 'department', 'no'),
                                      ('Sabit UID', 'Personel', 'Bölüm', 'Sicil'),
                                      (100, 170, 160, 110), 7)
        self.people_list.configure(displaycolumns=('name', 'department', 'no'))
        form = ttk.Frame(pane, padding=(12, 0, 0, 0))
        form.pack(side='left', fill='y')
        self.meta_person_combo = self.assign_combo(form, 0, self.meta_person, (), 26,
                                                   readonly=True)
        self._field(form, 0, 'Personel', self.meta_person_combo)
        self._field(form, 1, 'Bölüm', self._entry(form, 1, self.meta_department, 26))
        self._field(form, 2, 'Sicil no', self._entry(form, 2, self.meta_employee_no, 20))
        buttons = ttk.Frame(form)
        buttons.grid(row=3, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Button(buttons, text='Kaydet', command=self.save_person_metadata).pack(side='left')

    def _bind_form_variables(self):
        self.meta_person.trace_add('write', lambda *_: self._load_person_meta())

    def _scroll_tab(self, title):
        """Keep every planning control accessible on 1366x768 Windows screens."""
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text=title)
        canvas = tk.Canvas(outer, highlightthickness=0, background='#eef2f5')
        vertical = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        horizontal = ttk.Scrollbar(outer, orient='horizontal', command=canvas.xview)
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        body = ttk.Frame(canvas, padding=8)
        window = canvas.create_window((0, 0), window=body, anchor='nw')
        def resize(_event=None):
            canvas.itemconfigure(window, width=max(canvas.winfo_width(), body.winfo_reqwidth()))
            canvas.configure(scrollregion=canvas.bbox('all'))
        body.bind('<Configure>', resize)
        canvas.bind('<Configure>', resize)
        return body

    @staticmethod
    def _entry(parent, row, variable, width):
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(row=row, column=1, sticky='ew', padx=4, pady=2)
        return entry

    @staticmethod
    def _field(parent, row, label, widget):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', pady=2)
        return widget

    @staticmethod
    def _combobox(parent, row, variable, values, width, readonly=False):
        combo = ttk.Combobox(parent, textvariable=variable, values=list(values), width=width,
                             state='readonly' if readonly else 'normal')
        combo.grid(row=row, column=1, sticky='ew', padx=4, pady=2)
        return combo

    assign_combo = _combobox

    def _tree(self, parent, columns, headings, widths, height):
        frame = ttk.Frame(parent)
        frame.pack(side='left', fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse',
                            height=height)
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor='w')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        return tree

    # ----- form/state helpers --------------------------------------------
    def _apply_enabled(self):
        self.source_var.set(f'Cihaz/arşiv: {self.source}' if self.source else 'Cihaz/arşiv: seçilmedi')
        for index in range(self.notebook.index('end')):
            self.notebook.tab(index, state='normal' if self.enabled else 'disabled')
        if self.enabled and not self.notebook.select():
            self.notebook.select(0)

    def _refresh_choices(self):
        selected_shift = self._shift_choices.get(self.assign_shift.get())
        names = {}
        for employee in (self.status.get('employees') or []):
            if not isinstance(employee, dict):
                continue
            try:
                value = canonical_uid(str(employee.get('uid', '')))
            except (DeviceError, ValueError, TypeError, AttributeError):
                continue
            names.setdefault(value, str(employee.get('name') or value))
        for person in self._config['people']:
            names.setdefault(person, person)
        self._person_names = names
        labels, label_uids = [], {}
        for uid_value, name in names.items():
            base = (name or uid_value).strip() or 'Personel'
            label, number = base, 2
            while label in label_uids and label_uids[label] != uid_value:
                label, number = f'{base} ({number})', number + 1
            labels.append(label)
            label_uids[label] = uid_value
        self._person_labels = label_uids
        departments = sorted({meta['department'] for meta in self._config['people'].values() if meta['department']})
        self.assign_department_combo.configure(values=departments)
        if self.assign_department.get() not in departments:
            self.assign_department.set(departments[0] if departments else '')
        people = labels
        self.assign_person_combo.configure(values=['*'] + people)
        self.leave_person_combo.configure(values=people)
        self.meta_person_combo.configure(values=people)
        def current_label(value):
            if value == '*':
                return '*'
            if value in label_uids:
                return value
            return next((label for label, uid_value in label_uids.items() if uid_value == value), None)
        for variable in (self.assign_person, self.leave_person, self.meta_person):
            value = variable.get().strip()
            mapped = current_label(value)
            if mapped is None:
                mapped = '*' if variable is self.assign_person else (people[0] if people else '')
            variable.set(mapped)
        labels, mapping = [], {}
        for shift in self._config['shifts']:
            base = f"{shift['name']} ({shift['start']}–{shift['end']})"
            label, number = base, 2
            while label in mapping:
                label, number = f'{base} #{number}', number + 1
            labels.append(label)
            mapping[label] = shift['id']
        self._shift_choices = mapping
        self.assign_shift_combo.configure(values=labels)
        if selected_shift in mapping.values():
            self.assign_shift.set(next(label for label, value in mapping.items() if value == selected_shift))
        elif self.assign_shift.get() not in labels:
            self.assign_shift.set(labels[0] if labels else '')

    def _render_shifts(self):
        rows = []
        for shift in self._config['shifts']:
            days = ','.join(('Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt', 'Paz')[day] for day in shift['weekdays'])
            rows.append((shift['id'], (shift['name'], shift['start'], shift['end'], days)))
        sync_rows(self.shift_list, rows)
        if self._editing_shift_id and not self.shift_list.exists(self._editing_shift_id):
            self._editing_shift_id = None

    def _render_assignments(self):
        self._sync_indexed(self.assignment_list, [(row_key(item), (
                self._person_label(item['uid']), self._shift_name(item['shift_id']),
                item['effective_from'], item['effective_to'] or '—')) for item in self._config['assignments']])

    def _render_leaves(self):
        self._sync_indexed(self.leave_list, [(row_key(item), (
                self._person_label(item['uid']), item['start'], item['end'], item['kind']))
                for item in self._config['leaves']])

    def _render_holidays(self):
        sync_rows(self.holiday_list, [(day, (day, label)) for day, label in self._config['holidays'].items()])

    def _render_people(self):
        sync_rows(self.people_list, [(person, (
                person, self._person_label(person), meta['department'], meta['employee_no']))
                for person, meta in self._config['people'].items()])

    @staticmethod
    def _sync_indexed(tree, rows):
        # Retain legacy numeric widget IDs, but bind them to policy identity, not position.
        previous = getattr(tree, '_policy_ids', {})
        next_id = getattr(tree, '_next_policy_id', 0)
        mapping, items = {}, []
        for identity, values in rows:
            key = previous.get(identity)
            if key is None:
                key, next_id = str(next_id), next_id + 1
            mapping[identity] = key
            items.append((key, values))
        tree._policy_ids, tree._next_policy_id = mapping, next_id
        sync_rows(tree, items)

    def _on_shift_select(self, _event=None):
        shift_id = self._selected_shift()
        if shift_id and shift_id != self._editing_shift_id:
            self._load_shift(shift_id)

    def _load_shift(self, shift_id):
        shift = next((item for item in self._config['shifts'] if item['id'] == shift_id), None)
        if not shift:
            return
        self._editing_shift_id = shift_id
        self.shift_name.set(shift['name'])
        self.shift_start.set(shift['start'])
        self.shift_end.set(shift['end'])
        for index, variable in enumerate(self.shift_days):
            variable.set(index in shift['weekdays'])
        for field, variable in zip(NUMERIC_FIELDS, (
                self.shift_break_minutes, self.shift_break_after_minutes,
                self.shift_late_tolerance, self.shift_early_tolerance,
                self.shift_overtime_after_minutes)):
            variable.set(str(shift[field]))

    def _load_person_meta(self):
        person = self._person_uid(self.meta_person.get())
        meta = self._config['people'].get(person, {})
        self.meta_department.set(meta.get('department', ''))
        self.meta_employee_no.set(meta.get('employee_no', ''))

    def _shift_payload(self, shift_id):
        days = [index for index, variable in enumerate(self.shift_days) if variable.get()]
        if not days:
            raise WorkforceError('En az bir vardiya günü seçin.')
        payload = {'id': shift_id, 'name': self.shift_name.get().strip(), 'start':
                   self.shift_start.get().strip(), 'end': self.shift_end.get().strip(),
                   'weekdays': days}
        labels = ('Mola', 'Moladan sonra', 'Geç tolerans', 'Erken tolerans',
                  'Fazla mesai eşiği')
        for field, label, variable in zip(NUMERIC_FIELDS, labels, (
                self.shift_break_minutes, self.shift_break_after_minutes,
                self.shift_late_tolerance, self.shift_early_tolerance,
                self.shift_overtime_after_minutes)):
            try:
                value = int(str(variable.get()).strip())
            except ValueError:
                raise WorkforceError(f'{label} tam sayı olmalı (0–1440).') from None
            if not 0 <= value <= 1440:
                raise WorkforceError(f'{label} 0–1440 arasında olmalı.')
            payload[field] = value
        return payload

    def _assignment_payload(self):
        person = self._person_uid(self.assign_person.get())
        if not person:
            raise WorkforceError('Personel seçin (tüm personel için *).')
        shift_id = self._shift_id_for_choice()
        if not shift_id:
            raise WorkforceError('Önce bir vardiya ekleyin ve seçin.')
        return {'uid': person, 'shift_id': shift_id,
                'effective_from': self.assign_from.get().strip(),
                'effective_to': self.assign_to.get().strip()}

    def _shift_id_for_choice(self):
        value = self.assign_shift.get().strip()
        if value in self._shift_choices:
            return self._shift_choices[value]
        if any(shift['id'] == value for shift in self._config['shifts']):
            return value
        return None

    def _selected_shift(self):
        selection = self.shift_list.selection()
        return selection[0] if selection else None

    @staticmethod
    def _selected_index(tree):
        selection = tree.selection()
        return tree.index(selection[0]) if selection else None

    def _person_uid(self, value):
        value = str(value or '').strip()
        if value == '*':
            return '*'
        if value in self._config.get('people', {}) or value in self._person_names:
            return value
        return self._person_labels.get(value, value)

    def _person_label(self, person):
        if person == '*':
            return 'Tüm personel (*)'
        return self._person_names.get(person, person)

    def _shift_name(self, shift_id):
        shift = next((item for item in self._config['shifts'] if item['id'] == shift_id), None)
        return f"{shift['name']} ({shift['start']}–{shift['end']})" if shift else shift_id
