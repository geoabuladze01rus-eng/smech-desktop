"""Minimal dependency-free OOXML XLSX reader/writer for basic tabular sheets.

Supports common XLSX cell types, shared strings, inline strings, numeric cells,
and cached formula results. Not a full spreadsheet engine. Never evaluates macros.
"""
import io
import re
import zipfile
from defusedxml import ElementTree as ET
from xml.sax.saxutils import escape

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
DOCREL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKGREL = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS = {'m': MAIN, 'r': DOCREL}


def col_idx(ref):
    match = re.match(r'([A-Za-z]+)', ref or '')
    if not match:
        return 0
    out = 0
    for c in match.group(1).upper():
        out = out * 26 + ord(c) - 64
    return out - 1


def column(n):
    value = ''
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        value = chr(65 + rem) + value
    return value


def parse_xlsx(data, max_rows=10000, max_cols=90):
    if not isinstance(data, bytes) or len(data) > 15_000_000:
        raise ValueError('Файл слишком большой или имеет неверный формат')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        if len(names) > 500 or sum(f.file_size for f in z.infolist()) > 50_000_000 or any(f.file_size > 30_000_000 or f.flag_bits & 1 for f in z.infolist()):
            raise ValueError('Книга слишком большая для безопасного импорта')
        if 'xl/workbook.xml' not in names or 'xl/_rels/workbook.xml.rels' not in names:
            raise ValueError('Повреждённая или неподдерживаемая XLSX-книга')
        shared = []
        if 'xl/sharedStrings.xml' in names:
            tree = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in tree.findall('m:si', NS)[:150000]:
                shared.append(''.join(t.text or '' for t in si.findall('.//m:t', NS)))
        relation_tree = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        relations = {r.attrib.get('Id'): r.attrib.get('Target', '') for r in relation_tree}
        book = ET.fromstring(z.read('xl/workbook.xml'))
        result = []
        for sheet in book.findall('m:sheets/m:sheet', NS):
            rid = sheet.attrib.get(f'{{{DOCREL}}}id')
            target = relations.get(rid, '')
            # Resolve the internal package path without allowing traversal or external targets.
            if not target or '://' in target or '\\' in target:
                continue
            from posixpath import normpath
            path = normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
            if path.startswith('../') or not path.startswith('xl/worksheets/'):
                continue
            if path not in names:
                continue
            root = ET.fromstring(z.read(path))
            rows = []
            warnings = []
            last_row = 0
            for row in root.findall('m:sheetData/m:row', NS):
                if len(rows) >= max_rows:
                    break
                row_num = int(row.attrib.get('r', str(last_row + 1)))
                # cap large sparse gaps
                while last_row < row_num - 1 and len(rows) < max_rows:
                    rows.append([])
                    last_row += 1
                vals = []
                for c in row.findall('m:c', NS):
                    idx = col_idx(c.attrib.get('r', 'A'))
                    if idx >= max_cols:
                        continue
                    while len(vals) <= idx:
                        vals.append('')
                    kind = c.attrib.get('t', '')
                    v = c.find('m:v', NS)
                    if c.find('m:f', NS) is not None:
                        if len(warnings) < 25:
                            warnings.append(f'{c.attrib.get("r", "?")}: формула Excel (использовано только сохранённое значение, оно может быть устаревшим)')
                    if kind == 'inlineStr':
                        cell = ''.join(t.text or '' for t in c.findall('.//m:is//m:t', NS))
                    elif kind == 's':
                        pos = int(v.text) if v is not None and v.text else -1
                        cell = shared[pos] if 0 <= pos < len(shared) else ''
                    elif kind == 'b':
                        cell = 'Да' if v is not None and v.text == '1' else 'Нет'
                    elif kind == 'e':
                        cell = ''
                    else:
                        cell = v.text if v is not None and v.text else ''
                    vals[idx] = cell
                rows.append(vals)
                last_row = row_num
            result.append({'name': sheet.attrib.get('name', 'Лист'), 'rows': rows, 'warnings': warnings})
        if not result:
            raise ValueError('Не найдено доступных листов XLSX')
        return result


def create_xlsx(sheets):
    if not isinstance(sheets, list) or not sheets or len(sheets) > 12:
        raise ValueError('Нет листов для выгрузки или их слишком много')
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1))
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+overrides+'</Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        names = []
        links = []
        for i, s in enumerate(sheets, 1):
            if not isinstance(s, dict) or not isinstance(s.get('rows', []), list) or len(s['rows']) > 15000:
                raise ValueError('Некорректные строки листа или слишком большой объём')
            sheetname = re.sub(r'[\\/*?:\[\]]',' ',str(s.get('name') or f'Лист{i}'))[:31]
            names.append(f'<sheet name="{escape(sheetname, {"\"":"&quot;"})}" sheetId="{i}" r:id="rId{i}"/>')
            links.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
            xmlrows = []
            for j, cells in enumerate(s.get('rows', [])[:15000], 1):
                cs = []
                if not isinstance(cells, (list, tuple)) or len(cells) > 80:
                    raise ValueError('Допустимо не более 80 столбцов')
                for k, raw in enumerate(cells[:80]):
                    if len(str(raw)) > 10000:
                        raise ValueError('Слишком длинное содержимое ячейки')
                    ref = column(k) + str(j)
                    if isinstance(raw, (float, int)) and not isinstance(raw, bool):
                        cs.append(f'<c r="{ref}"><v>{raw}</v></c>')
                    else:
                        val = escape(''.join(ch for ch in str(raw if raw is not None else '') if ch in '\t\n\r' or ord(ch) >= 32))
                        cs.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{val}</t></is></c>')
                xmlrows.append(f'<row r="{j}">{"".join(cs)}</row>')
            z.writestr(f'xl/worksheets/sheet{i}.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="'+MAIN+'"><sheetData>'+''.join(xmlrows)+'</sheetData></worksheet>')
        z.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="'+MAIN+'" xmlns:r="'+DOCREL+'"><sheets>'+''.join(names)+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="'+PKGREL+'">'+''.join(links)+'<Relationship Id="rId'+str(len(sheets)+1)+'" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="'+MAIN+'"><fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs></styleSheet>')
    return out.getvalue()
