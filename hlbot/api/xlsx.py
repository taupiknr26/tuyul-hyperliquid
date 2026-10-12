"""A small writer for Excel workbooks (.xlsx), using only the standard
library.

Enough for a tidy export: several sheets, a bold frozen header row with
column filters, column widths, and typed cells (text, numbers with a
number format, date-times, durations, percentages). Rows are written
straight into the compressed file one at a time, so memory use does not
grow with the number of rows.

Text is always written as text: a value that begins with ``=`` is not a
formula in the workbook, so nothing a signal channel posted can run as
one.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Optional
from xml.sax.saxutils import escape

# cell kinds -> (style index, number format). Index 0 is the default style, 1 the header.
_FORMATS = {
    "text": None,
    "int": "0",
    "number": "#,##0.00",
    "money": '#,##0.00;[Red]-#,##0.00',
    "price": "#,##0.00####",
    "size": "0.00######",
    "percent": "0.00%",
    "ratio": "0.00",
    "datetime": "yyyy-mm-dd hh:mm:ss",
    "duration": "[h]:mm:ss",
}
_STYLE = {"text": 0, **{kind: index for index, kind in enumerate((k for k, fmt in _FORMATS.items() if fmt), start=2)}}
_EPOCH = datetime(1899, 12, 30, tzinfo=UTC)
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _column(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def excel_datetime(value: datetime, tz: timezone = UTC) -> float:
    """A date-time as Excel's day number, shown in ``tz`` (a workbook has no time zones of its own)."""
    local = value.astimezone(tz)
    return (local.replace(tzinfo=UTC) - _EPOCH) / timedelta(days=1)


def _text(value: Any) -> str:
    return escape(_ILLEGAL.sub("", str(value)))


def _cell(ref: str, value: Any, kind: str) -> str:
    if value is None or value == "":
        return ""
    if kind == "text" or isinstance(value, str):
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_text(value)}</t></is></c>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="inlineStr"><is><t>{"yes" if value else "no"}</t></is></c>'
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return ""
    shown = str(int(number)) if kind == "int" and number == int(number) else repr(number)  # a count reads back as a whole number
    return f'<c r="{ref}" s="{_STYLE[kind]}"><v>{shown}</v></c>'


class Sheet:
    def __init__(self, name: str, columns: Sequence[tuple[str, str, float]], rows: Iterable[list[Any]], *, table: bool = True):
        """``columns``: (header, cell kind, width in characters). ``table``: freeze the header and add column filters."""
        self.name, self.columns, self.rows, self.table = name[:31], columns, rows, table


def _write_sheet(out: Any, sheet: Sheet) -> None:
    width = len(sheet.columns)
    out.write(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
    if sheet.table:
        out.write(b'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
    out.write(b"<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, (_h, _k, w) in enumerate(sheet.columns)).encode() + b"</cols>")
    out.write(b"<sheetData>")
    out.write(('<row r="1">' + "".join(f'<c r="{_column(i)}1" s="1" t="inlineStr"><is><t xml:space="preserve">{_text(h)}</t></is></c>'
                                       for i, (h, _k, _w) in enumerate(sheet.columns)) + "</row>").encode())
    count = 1
    for row in sheet.rows:
        count += 1
        cells = "".join(
            _cell(f"{_column(i)}{count}", *(value if isinstance(value, tuple) else (value, sheet.columns[i][1])))  # (value, kind) overrides the column's kind
            for i, value in enumerate(row[:width])
        )
        out.write(f'<row r="{count}">{cells}</row>'.encode())
    out.write(b"</sheetData>")
    if sheet.table:
        out.write(f'<autoFilter ref="A1:{_column(width - 1)}{count}"/>'.encode())
    out.write(b"</worksheet>")


def workbook(sheets: list[Sheet], *, title: str = "", created: Optional[datetime] = None) -> bytes:
    """The finished .xlsx file as bytes."""
    created = created or datetime.now(UTC)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(1, len(sheets) + 1)) + "</Types>"))
        zf.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>'))
        zf.writestr("docProps/core.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{_text(title)}</dc:title><dc:creator>hlbot</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}</dcterms:created></cp:coreProperties>'))
        names = "".join(f'<sheet name="{_text(s.name)}" sheetId="{i}" r:id="rId{i}"/>' for i, s in enumerate(sheets, 1))
        zf.writestr("xl/workbook.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets>{names}</sheets></workbook>'))
        zf.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                      for i in range(1, len(sheets) + 1))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'))
        custom = [(164 + i, fmt) for i, fmt in enumerate(f for f in _FORMATS.values() if f)]
        zf.writestr("xl/styles.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<numFmts count="{len(custom)}">' + "".join(f'<numFmt numFmtId="{i}" formatCode="{escape(fmt, {chr(34): "&quot;"})}"/>' for i, fmt in custom) + "</numFmts>"
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFE7EEDA"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{2 + len(custom)}"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
            + "".join(f'<xf numFmtId="{i}" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' for i, _fmt in custom)
            + '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'))
        for index, sheet in enumerate(sheets, 1):
            with zf.open(f"xl/worksheets/sheet{index}.xml", "w") as out:
                _write_sheet(out, sheet)
    return buffer.getvalue()
