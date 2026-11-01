"""Local-only attendance exports: XLSX via stdlib OOXML, PDF via ReportLab.
Never contacts a device, never opens/prints/e-mails output and never emits raw settings.
"""
import io
import os
from datetime import date
from pathlib import Path
import tempfile
from xml.sax.saxutils import escape
import zipfile

from windows_app.workforce_model import DAILY_COLUMNS, SUMMARY_COLUMNS, WorkforceError

FONT_ID = 'UmtNotoSans'
FONT_PATH = Path(__file__).resolve().parent / 'assets' / 'NotoSans-Regular.ttf'
WEEKLY_COLUMNS = [('week_start', 'Hafta başlangıcı'), ('name', 'Personel'), ('uid', 'Sabit UID'),
                  ('net_seconds', 'Net süre'), ('overtime_seconds', 'Fazla mesai adayı')]
REPORT_SUMMARY_COLUMNS = SUMMARY_COLUMNS + [('days', 'Çalışılan gün')]


def _fail():
    return WorkforceError('Dışa aktarım raporu geçersiz veya eksik; hiçbir dosya yazılmadı.')


def _text(value, limit=300):
    if (not isinstance(value, str) or len(value) > limit or
            any(ord(char) < 32 or ord(char) == 127 for char in value)): raise _fail()
    return value


def _items(value, limit):
    if not isinstance(value, list) or len(value) > limit: raise _fail()
    return value


def _count(value):
    if type(value) is not int or not 0 <= value <= 10 ** 9: raise _fail()
    return value


def _row(row, columns):
    if not isinstance(row, dict) or any(key not in row for key, _ in columns): raise _fail()
    return {key: (_count(row[key]) if key == 'days' or key.endswith(('_seconds', '_minutes', '_count', '_days'))
                  else _text(row[key], 2000 if key == 'status' else 300)) for key, _ in columns}


def _day(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise _fail() from None
    if parsed.isoformat() != value: raise _fail()
    return value


def _daily(row):
    item = _row(row, DAILY_COLUMNS)
    item['flags'] = [_text(flag, 120) for flag in _items(row.get('flags', []), 24)]
    return item


def _weekly(row):
    item = _row(row, WEEKLY_COLUMNS)
    _day(item['week_start'])
    return item


def _summary(row, daily):
    item = _row(row, SUMMARY_COLUMNS)
    item['days'] = _count(row.get('days', len({r['date'] for r in daily
                                            if r['uid'] == item['uid'] and r['gross_seconds'] > 0})))
    return item


def _checked(report):
    if not isinstance(report, dict): raise _fail()
    keys = {'rows', 'summary', 'weekly', 'warnings', 'start', 'end', 'source', 'synced_at',
            'generated_at', 'config_hash', 'policy_notes'}
    if not keys <= set(report): raise _fail()
    start, end = _day(report['start']), _day(report['end'])
    if end < start: raise _fail()
    notes = report['policy_notes']
    if isinstance(notes, str): notes = [notes] if notes else []
    daily = [_daily(row) for row in _items(report['rows'], 20000)]
    return {'rows': daily,
            'summary': [_summary(row, daily) for row in _items(report['summary'], 5000)],
            'weekly': [_weekly(row) for row in _items(report['weekly'], 20000)],
            'warnings': [_text(item) for item in _items(report['warnings'], 200)],
            'policy_notes': [_text(item, 500) for item in _items(notes, 100)],
            'start': start, 'end': end, 'source': _text(report['source'], 200),
            'synced_at': _text(report['synced_at'], 40), 'generated_at': _text(report['generated_at'], 40),
            'config_hash': _text(report['config_hash'], 128)}


def _duration(seconds):
    hours, rest = divmod(seconds, 3600); minutes, seconds = divmod(rest, 60)
    return f'{hours} sa {minutes:02d} dk' + (f' {seconds:02d} sn' if seconds else '')


def _col(index):
    name = ''
    while index:
        index, offset = divmod(index - 1, 26)
        name = chr(65 + offset) + name
    return name


def _cell(ref, value, kind, style=0):
    if kind == 'int': return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'


def _width(key):
    return {'name': 26, 'department': 18, 'status': 20, 'flags': 24, 'shift': 16,
            'first_in': 22, 'last_out': 22, 'week_start': 14}.get(key, 13)


def _layout(columns, rows, flags=False):
    cols = [(label, _width(key), 'int' if key == 'days' or key.endswith(('_minutes', '_count', '_days')) else 'text')
            for key, label in columns]
    raw = [(f'Ham sn: {label}', 14, 'int') for key, label in columns if key.endswith('_seconds')]
    data = [[_duration(row[key]) if key.endswith('_seconds') else str(row[key]) for key, _ in columns] +
            (['; '.join(row['flags'])] if flags else []) +
            [row[key] for key, _ in columns if key.endswith('_seconds')] for row in rows]
    return cols + ([('Bayraklar', 24, 'text')] if flags else []) + raw, data


def _sheet_xml(cols, data):
    last = f'A1:{_col(len(cols))}{len(data) + 1}'
    head = ''.join(_cell(f'{_col(index)}1', label, 'text', 1) for index, (label, _, _) in enumerate(cols, 1))
    body = ''.join(f'<row r="{number}">' + ''.join(_cell(f'{_col(index)}{number}', value, cols[index - 1][2])
                   for index, value in enumerate(values, 1)) + '</row>' for number, values in enumerate(data, 2))
    widths = ''.join(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>' for index, (_, width, _) in enumerate(cols, 1))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<dimension ref="{last}"/><sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<sheetFormatPr defaultRowHeight="15"/><cols>{widths}</cols><sheetData><row r="1">{head}</row>{body}'
            f'</sheetData><autoFilter ref="{last}"/><pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" '
            'header="0.2" footer="0.2"/></worksheet>')


_CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{sheets}<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
_ROOT_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
_WORKSHEET = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet'
_STYLES = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="10"/><name val="Calibri"/><family val="2"/></font><font><b/><sz val="10"/><name val="Calibri"/><family val="2"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE4E4E4"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'


def _xlsx_bytes(clean):
    names = ['Günlük', 'Özet', 'Haftalık', 'Rapor Bilgisi']
    metadata = [[label, clean[key]] for key, label in (
        ('source', 'Kaynak'), ('start', 'Başlangıç'), ('end', 'Bitiş'),
        ('synced_at', 'Son veri okuma (UTC)'), ('generated_at', 'Oluşturma (UTC)'), ('config_hash', 'Politika SHA256'))]
    metadata += [['Not', 'Fazla mesai adaydır; ücret/bordro hakkı hesabı değildir.']]
    metadata += [['Kural', value] for value in clean['policy_notes']]
    metadata += [['Uyarı', value] for value in clean['warnings']]
    sheets = [_sheet_xml(*_layout(DAILY_COLUMNS, clean['rows'], flags=True)),
              _sheet_xml(*_layout(REPORT_SUMMARY_COLUMNS, clean['summary'])),
              _sheet_xml(*_layout(WEEKLY_COLUMNS, clean['weekly'])),
              _sheet_xml([('Alan', 28, 'text'), ('Değer', 110, 'text')], metadata)]
    overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for n in range(1, len(sheets) + 1))
    entries = ''.join(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>' for n, name in enumerate(names, 1))
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns='
                '"http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r='
                f'"http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{entries}</sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns='
            '"http://schemas.openxmlformats.org/package/2006/relationships">' +
            ''.join(f'<Relationship Id="rId{n}" Type="{_WORKSHEET}" Target="worksheets/sheet{n}.xml"/>' for n in range(1, len(sheets) + 1)) +
            f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    buffer = io.BytesIO()
    parts = [('[Content_Types].xml', _CONTENT_TYPES.format(sheets=overrides)), ('_rels/.rels', _ROOT_RELS),
             ('xl/workbook.xml', workbook), ('xl/_rels/workbook.xml.rels', rels), ('xl/styles.xml', _STYLES)]
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts + [(f'xl/worksheets/sheet{n}.xml', sheet) for n, sheet in enumerate(sheets, 1)]:
            archive.writestr(name, text)
    return buffer.getvalue()


def _discard(path):
    try: os.unlink(path)
    except OSError: pass


def _temp(path, suffix):
    target = Path(os.fspath(path))
    try:
        handle, temporary = tempfile.mkstemp(prefix='.umt-export-', suffix=suffix, dir=str(target.parent))
    except (OSError, TypeError):
        raise WorkforceError('Dışa aktarım hedef klasörüne yazılamadı; mevcut dosya korundu.') from None
    os.close(handle)
    return target, temporary


def _commit(target, temporary):
    try:
        os.replace(temporary, target)
    except OSError:
        _discard(temporary)
        raise WorkforceError('Dışa aktarım dosyası yazılamadı; mevcut dosya korundu.') from None


def _write_bytes(path, data, suffix):
    target, temporary = _temp(path, suffix)
    try:
        with open(temporary, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    except OSError:
        _discard(temporary)
        raise WorkforceError('Dışa aktarım dosyası yazılamadı; mevcut dosya korundu.') from None
    _commit(target, temporary)


def write_xlsx(path, report):
    """Write four OOXML sheets including policy metadata (all user text stays literal)."""
    clean = _checked(report)
    try:
        data = _xlsx_bytes(clean)
    except WorkforceError:
        raise
    except Exception:
        raise WorkforceError('Excel raporu oluşturulamadı; mevcut dosya korundu.') from None
    _write_bytes(path, data, '.xlsx.tmp')


def _pdf_table(available, colors, font, body, head_style, headers, rows, weights):
    from reportlab.platypus import Paragraph, Table, TableStyle
    widths = [available * weight / float(sum(weights)) for weight in weights]
    head = [Paragraph(escape(label), head_style) for label in headers]
    cells = [[Paragraph(escape(str(value)), body) for value in row] for row in rows]
    style = [('FONTNAME', (0, 0), (-1, -1), font), ('FONTSIZE', (0, 0), (-1, -1), 6.2),
             ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E4E4E4')),
             ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#8C8C8C')),
             ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 2),
             ('RIGHTPADDING', (0, 0), (-1, -1), 2), ('TOPPADDING', (0, 0), (-1, -1), 1.2),
             ('BOTTOMPADDING', (0, 0), (-1, -1), 1.2)]
    if rows: style.append(('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F5F5F5')]))
    return Table([head] + cells, colWidths=widths, repeatRows=1, style=TableStyle(style))


def _build_pdf(path, clean):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer
    except ImportError:
        raise WorkforceError('PDF için ReportLab kurulu değil; hiçbir dosya yazılmadı.') from None
    if not FONT_PATH.is_file(): raise WorkforceError('PDF yazı tipi bulunamadı; hiçbir dosya yazılmadı.')
    if FONT_ID not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_ID, str(FONT_PATH)))
    width, height = landscape(A4)
    margin = 8 * mm
    available = width - 2 * margin
    body = ParagraphStyle('umtBody', fontName=FONT_ID, fontSize=6.2, leading=7.4)
    small = ParagraphStyle('umtSmall', parent=body, fontSize=7.2, leading=9.2)
    heading = ParagraphStyle('umtHead', parent=small, fontSize=9.5, leading=12, spaceBefore=6, spaceAfter=2)
    title = ParagraphStyle('umtTitle', parent=heading, fontSize=13, leading=16, spaceAfter=3)
    table_head = ParagraphStyle('umtTableHead', parent=body)

    def values(row, columns):
        return [_duration(row[key]) if key.endswith('_seconds') else str(row[key]) for key, _ in columns]

    methods = [('Özet', REPORT_SUMMARY_COLUMNS, clean['summary'], None),
               ('Günlük ayrıntı (ham/manual durum)', DAILY_COLUMNS, clean['rows'], 'Bayraklar'),
               ('Haftalık toplamlar', WEEKLY_COLUMNS, clean['weekly'], None)]
    story = [Paragraph(escape('Yerel puantaj raporu'), title),
             Paragraph(escape(f'Kaynak: {clean["source"]}  ·  Dönem: {clean["start"]} – {clean["end"]}  ·  '
                              f'Oluşturma: {clean["generated_at"]}  ·  Cihaz eşitleme: {clean["synced_at"]}'), small),
             Paragraph(escape(f'Politika özeti (SHA-256): {clean["config_hash"] or "yok"}'), small)]
    story.extend(Paragraph(escape('- ' + note), small) for note in clean['policy_notes'])
    if clean['warnings']:
        story.append(Paragraph(escape('Uyarılar'), heading))
        story.extend(Paragraph(escape('- ' + warning), small) for warning in clean['warnings'])
    story.append(Paragraph(escape('Fazla mesai yalnızca adaydır (ücret hakkı doğurmaz); durum alanı ham/manual '
                                  'kayıt ayrımını gösterir. Saniye değerleri Excel ham sütunlarında aynen korunur.'), small))
    for label, columns, rows, extra in methods:
        headers = [item[1] for item in columns] + ([extra] if extra else [])
        data = [values(row, columns) + (['; '.join(row['flags'])] if extra else []) for row in rows]
        weights = [_width(key) for key, _ in columns] + ([24] if extra else [])
        story.append(Paragraph(escape(label), heading))
        if not data: story.append(Paragraph(escape('Bu bölümde kayıt yok.'), small))
        story.append(_pdf_table(available, colors, FONT_ID, body, table_head, headers, data, weights))
    story.append(Spacer(1, 14))
    story.append(Paragraph(escape('Hazırlayan: ______________________    Kontrol eden: ______________________    '
                                  'Tarih: ____________'), small))
    story.append(Paragraph(escape('Yerel dosya; otomatik yazdırma, e-posta veya cihaz yazımı yapılmaz.'), small))

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setFont(FONT_ID, 8)
        canvas.drawString(margin, height - 16, 'Puantaj raporu (yerel)')
        canvas.drawRightString(width - margin, height - 16, f'Dönem: {clean["start"]} – {clean["end"]}')
        canvas.setFont(FONT_ID, 6.5)
        canvas.drawString(margin, height - 26, f'Kaynak: {clean["source"]}')
        canvas.drawRightString(width - margin, height - 26,
                               f'Oluşturma: {clean["generated_at"]} · Eşitleme: {clean["synced_at"]}')
        canvas.drawString(margin, 14, f'Politika özeti: {clean["config_hash"] or "yok"}')
        canvas.drawRightString(width - margin, 14, f'Sayfa {document.page}')
        canvas.setStrokeColor(colors.HexColor('#777777'))
        canvas.setLineWidth(0.4)
        canvas.line(margin, 20, width - margin, 20)
        canvas.restoreState()

    document = BaseDocTemplate(str(path), pagesize=landscape(A4), leftMargin=margin, rightMargin=margin,
                               topMargin=38, bottomMargin=26, title='Yerel puantaj raporu',
                               author='UMT yerel puantaj')
    frame = Frame(margin, 26, available, height - 64, id='main',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    document.addPageTemplates([PageTemplate(id='rapor', frames=[frame], onPage=decorate)])
    document.build(story)


def write_pdf(path, report):
    """Write the filtered report as a landscape, multi-page, font-embedded PDF."""
    clean = _checked(report)
    target, temporary = _temp(path, '.pdf.tmp')
    try:
        _build_pdf(temporary, clean)
    except WorkforceError:
        _discard(temporary)
        raise
    except Exception:
        _discard(temporary)
        raise WorkforceError('PDF raporu oluşturulamadı; mevcut dosya korundu.') from None
    _commit(target, temporary)
