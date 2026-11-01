"""Exporter tests: stdlib ZIP/XML checks plus ReportLab PDF checks; no device or network."""
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.workforce_export import FONT_PATH, write_pdf, write_xlsx
from windows_app.workforce_model import WorkforceError

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
TEMP_ROOT = Path(os.environ.get('UMT_TEST_TMP', r'C:\Users\PC\AppData\Local\Temp\opencode'))
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
PDF_READY = importlib.util.find_spec('reportlab') is not None and FONT_PATH.is_file()
FORMULA_NAME = '=HYPERLINK("http://example.invalid","tıkla")&SUM(A1)'
LONG_NAME = 'Ayşe Gül ' + ('Çok Uzun Soyadı ' * 7) + 'Şahin-Çelik'
SHEET_PARTS = ['xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml', 'xl/worksheets/sheet3.xml']


def daily_row(**overrides):
    row = {'date': '2026-10-26', 'name': 'Ayşe Gül Şahin-Çelik', 'uid': '0000000A', 'employee_no': 'S-001',
           'department': 'Üretim', 'shift': 'Gündüz', 'first_in': '08:02', 'last_out': '17:40',
           'card_seconds': 34100, 'manual_seconds': 0, 'gross_seconds': 34100, 'break_seconds': 3600,
           'net_seconds': 30500, 'overtime_seconds': 500, 'late_minutes': 2, 'early_minutes': 0,
           'status': 'ham', 'flags': ['mola eksik']}
    row.update(overrides)
    return row


def summary_row(**overrides):
    row = {'name': 'Ayşe Gül Şahin-Çelik', 'uid': '0000000A', 'department': 'Üretim',
           'net_seconds': 30500, 'card_seconds': 34100, 'manual_seconds': 0, 'overtime_seconds': 500,
           'late_count': 1, 'early_count': 0, 'absent_days': 0, 'exception_days': 1}
    row.update(overrides)
    return row


def weekly_row(**overrides):
    row = {'uid': '0000000A', 'name': 'Ayşe Gül Şahin-Çelik', 'week_start': '2026-10-26',
           'net_seconds': 30500, 'overtime_seconds': 500}
    row.update(overrides)
    return row


def make_report(rows=None, summary=None, weekly=None, **extra):
    report = {
        'rows': [daily_row(), daily_row(date='2026-10-27', name=FORMULA_NAME, uid='0000000B',
                                         employee_no='S-002', department='Bakım & Onarım <Saha>',
                                         shift="Gece 'A'", first_in='19:00', last_out='23:30',
                                         card_seconds=100000, gross_seconds=100000, net_seconds=100000,
                                         overtime_seconds=8000, late_minutes=0, early_minutes=15,
                                         status='manuel', flags=[])] if rows is None else rows,
        'summary': [summary_row(), summary_row(name='Mehmet Öztürk', uid='0000000B', department='Bakım',
                                               net_seconds=100000, card_seconds=100000,
                                               overtime_seconds=8000, manual_seconds=100000,
                                               late_count=0, early_count=1, exception_days=2)] if summary is None else summary,
        'weekly': [weekly_row(), weekly_row(uid='0000000B', name='Mehmet Öztürk',
                                            net_seconds=100000, overtime_seconds=8000)] if weekly is None else weekly,
        'warnings': ['Açık oturum: 1 kayıt manuel inceleme gerektirir.'],
        'start': '2026-10-26', 'end': '2026-11-01', 'source': 'Yerel cihaz arşivi',
        'synced_at': '2026-11-01T08:00:00', 'generated_at': '2026-11-01T08:05:00',
        'config_hash': 'a' * 64, 'policy_notes': ['Vardiya toleransı 10 dk.', 'Fazla mesai adaydır.'],
    }
    report.update(extra)
    return report


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='umt-export-', dir=TEMP_ROOT))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read_sheet(self, archive, part):
        root = ET.fromstring(archive.read(part))
        rows = []
        for row in root.findall('m:sheetData/m:row', NS):
            values = []
            for cell in row.findall('m:c', NS):
                if cell.get('t') == 'inlineStr':
                    node = cell.find('m:is/m:t', NS)
                    values.append(node.text if node is not None and node.text is not None else '')
                else:
                    node = cell.find('m:v', NS)
                    values.append(int(node.text) if node is not None else None)
            rows.append(values)
        return root, rows

    def test_xlsx_package_parts_crc_and_xml(self):
        path = self.dir / 'rapor.xlsx'
        write_xlsx(path, make_report())
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            for part in (['[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml',
                          'xl/_rels/workbook.xml.rels', 'xl/styles.xml'] + SHEET_PARTS):
                self.assertIn(part, archive.namelist())
            for info in archive.infolist():
                self.assertEqual(info.CRC, zlib.crc32(archive.read(info.filename)) & 0xffffffff)
            for name in archive.namelist():
                if name.endswith(('.xml', '.rels')):
                    ET.fromstring(archive.read(name))
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            self.assertEqual([sheet.get('name') for sheet in workbook.findall('m:sheets/m:sheet', NS)],
                             ['Günlük', 'Özet', 'Haftalık', 'Rapor Bilgisi'])
            self.assertEqual(len(ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))), 5)

    def test_xlsx_policy_metadata_is_included(self):
        path = self.dir / 'bilgi.xlsx'
        write_xlsx(path, make_report())
        with zipfile.ZipFile(path) as archive:
            rows = self.read_sheet(archive, 'xl/worksheets/sheet4.xml')[1]
            flat = str(rows)
            self.assertIn('a' * 64, flat)
            self.assertIn('2026-10-26', flat)
            self.assertIn('Açık oturum', flat)
            summary = self.read_sheet(archive, SHEET_PARTS[1])[1]
            self.assertIn('Çalışılan gün', summary[0])

    def test_xlsx_headers_freeze_filter_and_turkish_durations(self):
        path = self.dir / 'rapor.xlsx'
        write_xlsx(path, make_report())
        with zipfile.ZipFile(path) as archive:
            root, rows = self.read_sheet(archive, SHEET_PARTS[0])
            pane = root.find('m:sheetViews/m:sheetView/m:pane', NS)
            self.assertEqual(pane.get('state'), 'frozen')
            self.assertEqual(root.find('m:autoFilter', NS).get('ref'), 'A1:X3')
            header, first, second = rows[0], rows[1], rows[2]
            index = {label: position for position, label in enumerate(header)}
            for label in ('Tarih', 'Personel', 'Sabit UID', 'Geç dakika', 'Durum', 'Bayraklar',
                          'Net süre', 'Ham sn: Net süre', 'Ham sn: Kart süresi'):
                self.assertIn(label, index)
            self.assertEqual(first[index['Personel']], 'Ayşe Gül Şahin-Çelik')
            self.assertEqual(first[index['Net süre']], '8 sa 28 dk 20 sn')
            self.assertEqual(first[index['Ham sn: Net süre']], 30500)
            self.assertEqual(first[index['Bayraklar']], 'mola eksik')
            self.assertEqual(second[index['Net süre']], '27 sa 46 dk 40 sn')
            self.assertEqual(second[index['Ham sn: Net süre']], 100000)
            self.assertEqual(second[index['Geç dakika']], 0)
            summary = self.read_sheet(archive, SHEET_PARTS[1])[1]
            self.assertEqual(summary[0][0], 'Personel')
            self.assertIn('Ham sn: Fazla mesai adayı', summary[0])
            weekly = self.read_sheet(archive, SHEET_PARTS[2])[1]
            self.assertEqual(weekly[0][0], 'Hafta başlangıcı')
            self.assertEqual(weekly[1][0], '2026-10-26')
            self.assertEqual(weekly[1][2], '0000000A')

    def test_xlsx_user_text_is_literal_not_formula_or_script(self):
        path = self.dir / 'rapor.xlsx'
        write_xlsx(path, make_report(rows=[daily_row(name=FORMULA_NAME, department='<script>alert(1)</script>',
                                                      status='=cmd|calc')]))
        with zipfile.ZipFile(path) as archive:
            raw = archive.read(SHEET_PARTS[0])
            self.assertNotIn(b'<f>', raw)
            self.assertNotIn(b'<f ', raw)
            root, rows = self.read_sheet(archive, SHEET_PARTS[0])
            self.assertEqual(root.findall('.//m:f', NS), [])
            self.assertEqual(rows[1][rows[0].index('Personel')], FORMULA_NAME)
            self.assertEqual(rows[1][rows[0].index('Bölüm')], '<script>alert(1)</script>')
            cells = root.findall('.//m:c', NS)
            self.assertTrue(any(cell.get('t') == 'inlineStr' for cell in cells))
            self.assertFalse(any(cell.get('t') in ('str', 'b', 'e') for cell in cells))
            self.assertNotIn(b'javascript', raw.lower())

    def test_xlsx_empty_report_is_valid(self):
        path = self.dir / 'bos.xlsx'
        write_xlsx(path, make_report(rows=[], summary=[], weekly=[], warnings=[], policy_notes=[]))
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            for part in SHEET_PARTS:
                self.assertIn(part, archive.namelist())
            root, rows = self.read_sheet(archive, SHEET_PARTS[0])
            self.assertEqual(len(rows), 1)
            self.assertEqual(root.find('m:autoFilter', NS).get('ref'), 'A1:X1')

    def test_raw_config_and_unrelated_keys_are_not_exported(self):
        report = make_report(raw_config={'password': 'gizli-parola'})
        report['rows'] = [daily_row(secret_token='TOKEN-123', extra_staff=['gizli-personel'])]
        path = self.dir / 'rapor.xlsx'
        write_xlsx(path, report)
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                raw = archive.read(name)
                self.assertNotIn(b'gizli-parola', raw)
                self.assertNotIn(b'TOKEN-123', raw)
                self.assertNotIn(b'gizli-personel', raw)

    def test_invalid_report_raises_and_preserves_existing_files(self):
        xlsx, pdf = self.dir / 'rapor.xlsx', self.dir / 'rapor.pdf'
        write_xlsx(xlsx, make_report())
        if PDF_READY:
            write_pdf(pdf, make_report())
        before_x, before_p = xlsx.read_bytes(), pdf.read_bytes() if pdf.exists() else None
        broken = [
            make_report(rows=[daily_row(net_seconds=-1)]),
            make_report(rows=[daily_row(net_seconds=1.5)]),
            make_report(rows=[daily_row(name='kötü\x01ad')]),
            make_report(rows=[daily_row(flags='mola')]),
            make_report(rows='satır'),
            make_report(summary=[{'name': 'eksik'}]),
            make_report(weekly=[weekly_row(week_start='2026-13-01')]),
            make_report(start='2026-11-02', end='2026-11-01'),
            make_report(warnings=[7]),
            make_report(policy_notes=5),
            {key: value for key, value in make_report().items() if key != 'source'},
            'rapor',
        ]
        for report in broken:
            with self.assertRaises(WorkforceError):
                write_xlsx(xlsx, report)
            if PDF_READY:
                with self.assertRaises(WorkforceError):
                    write_pdf(pdf, report)
        self.assertEqual(xlsx.read_bytes(), before_x)
        if PDF_READY:
            self.assertEqual(pdf.read_bytes(), before_p)
        expected = ['rapor.pdf', 'rapor.xlsx'] if PDF_READY else ['rapor.xlsx']
        self.assertEqual(sorted(item.name for item in self.dir.iterdir()), expected)

    def test_output_path_errors_leave_no_files(self):
        for writer, suffix in ((write_xlsx, '.xlsx'), (write_pdf, '.pdf')):
            if writer is write_pdf and not PDF_READY:
                continue
            with self.assertRaises(WorkforceError):
                writer(self.dir / 'yok' / ('rapor' + suffix), make_report())
            with self.assertRaises(WorkforceError):
                writer(self.dir, make_report())
        self.assertEqual(list(self.dir.iterdir()), [])

    @unittest.skipUnless(PDF_READY, 'ReportLab veya gömülü Noto Sans yazı tipi yok')
    def test_pdf_header_font_embedding_and_no_javascript(self):
        path = self.dir / 'rapor.pdf'
        write_pdf(path, make_report())
        data = path.read_bytes()
        self.assertTrue(data.startswith(b'%PDF-'))
        self.assertTrue(data.rstrip().endswith(b'%%EOF'))
        self.assertIn(b'FontFile2', data)
        self.assertIn(b'NotoSans', data)
        self.assertIn(b'/ToUnicode', data)
        self.assertNotIn(b'/JavaScript', data)
        self.assertNotIn(b'/OpenAction', data)
        self.assertGreaterEqual(len(re.findall(rb'/Type\s*/Page\b(?!s)', data)), 1)

    @unittest.skipUnless(PDF_READY, 'ReportLab veya gömülü Noto Sans yazı tipi yok')
    def test_pdf_multipage_with_long_turkish_names_and_limits(self):
        rows = [daily_row(date='2026-10-%02d' % (index % 28 + 1), uid='%08X' % index,
                          name=LONG_NAME + ' #%d' % index) for index in range(1, 121)]
        rows[0] = daily_row(name=LONG_NAME, department='Bakım & Onarım <Saha> İşçişçi')
        path = self.dir / 'uzun.pdf'
        write_pdf(path, make_report(rows=rows))
        data = path.read_bytes()
        self.assertTrue(data.startswith(b'%PDF-'))
        self.assertGreaterEqual(len(re.findall(rb'/Type\s*/Page\b(?!s)', data)), 2)
        self.assertIn(b'FontFile2', data)
        self.assertNotIn(b'/JavaScript', data)

    @unittest.skipUnless(PDF_READY, 'ReportLab veya gömülü Noto Sans yazı tipi yok')
    def test_pdf_empty_report_is_valid(self):
        path = self.dir / 'bos.pdf'
        write_pdf(path, make_report(rows=[], summary=[], weekly=[], warnings=[], policy_notes=[]))
        data = path.read_bytes()
        self.assertTrue(data.startswith(b'%PDF-'))
        self.assertTrue(data.rstrip().endswith(b'%%EOF'))
        self.assertGreaterEqual(len(re.findall(rb'/Type\s*/Page\b(?!s)', data)), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
