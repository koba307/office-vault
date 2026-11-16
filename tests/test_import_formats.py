"""Тесты определения форматов и парсеров импорта (Phase 08)."""

import io
import json
import struct
import zipfile

import pytest

from app.services.import_export.formats import (
    FORMAT_CSV,
    FORMAT_JSON,
    FORMAT_JSONL,
    FORMAT_TSV,
    FORMAT_TXT,
    FORMAT_XLS,
    FORMAT_XLSX,
    FORMAT_XML,
    FormatParseError,
    ParsedTable,
    UnsupportedFormatError,
    _parse_biff_workbook,
    decode_bytes,
    detect_encoding,
    detect_format,
    parse_csv,
    parse_file,
    parse_json,
    parse_jsonl,
    parse_tsv,
    parse_txt,
    parse_xls,
    parse_xlsx,
    parse_xml,
)

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# ---------------------------------------------------------------------------
# Генераторы тестовых файлов
# ---------------------------------------------------------------------------


def xlsx_bytes(matrix, *, shared: bool = False) -> bytes:
    """Собирает минимальный XLSX: inlineStr или sharedStrings."""
    strings: list[str] = []
    rows_xml: list[str] = []
    for row_index, row in enumerate(matrix, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row):
            reference = f"{chr(65 + column_index)}{row_index}"
            if shared:
                strings.append(str(value))
                cells.append(f'<c r="{reference}" t="s"><v>{len(strings) - 1}</v></c>')
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>')
        rows_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows_xml)}</sheetData></worksheet>"
    )
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        if shared:
            items = "".join(f"<si><t>{value}</t></si>" for value in strings)
            archive.writestr(
                "xl/sharedStrings.xml",
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"{items}</sst>",
            )
    return buffer.getvalue()


def record(record_id: int, data: bytes = b"") -> bytes:
    return struct.pack("<HH", record_id, len(data)) + data


def biff_stream(rows: list[list[str]]) -> bytes:
    """BIFF8-поток с одним листом: SST + LABELSST + MULRK."""
    bof = record(0x0809, struct.pack("<HH", 0x0600, 0x0005))
    eof = record(0x000A)
    strings = [value for row in rows for value in row]
    body = struct.pack("<II", len(strings), len(strings))
    for value in strings:
        encoded = value.encode("utf-16-le")
        body += struct.pack("<HB", len(value), 1) + encoded
    sst = record(0x00FC, body)

    def boundsheet(offset: int) -> bytes:
        name = "Sheet1"
        return record(
            0x0085,
            struct.pack("<I", offset)
            + struct.pack("<H", 0)
            + bytes([len(name), 0])
            + name.encode("cp1252"),
        )

    globals_prefix = bof + boundsheet(0) + sst + eof
    sheet_cells = b""
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            index = strings.index(value)
            sheet_cells += record(0x00FD, struct.pack("<HHHI", row_index, column_index, 0, index))
    sheet = bof + sheet_cells + record(0x0208, b"\x00" * 12) + eof
    return bof + boundsheet(len(globals_prefix)) + sst + eof + sheet


# ---------------------------------------------------------------------------
# Определение формата и кодировки
# ---------------------------------------------------------------------------


def test_detect_format_by_content_and_extension():
    assert detect_format(b"a,b\n1,2\n", "data.csv") == FORMAT_CSV
    assert detect_format(b"a\tb\n1\t2\n", "data.tsv") == FORMAT_TSV
    assert detect_format(json.dumps([{"a": 1}]).encode(), "data.json") == FORMAT_JSON
    assert detect_format(b'{"a": 1}\n{"a": 2}\n', "data.jsonl") == FORMAT_JSONL
    assert detect_format(b"<root><item>x</item></root>", "data.xml") == FORMAT_XML
    assert detect_format(b"hello world", "data.txt") == FORMAT_TXT
    assert detect_format(xlsx_bytes([["a"]]), "data.xlsx") == FORMAT_XLSX
    assert detect_format(OLE_MAGIC + b"\x00" * 600, "data.xls") == FORMAT_XLS


def test_detect_format_prefers_magic_bytes_over_extension():
    content = xlsx_bytes([["a"]])
    assert detect_format(content, "renamed.csv") == FORMAT_XLSX


def test_detect_format_single_column_text_falls_back_to_extension():
    assert detect_format(b"just text", "notes.csv") == FORMAT_CSV
    assert detect_format(b"just text", "no_extension") == FORMAT_TXT


def test_detect_format_rejects_empty_file():
    with pytest.raises(FormatParseError):
        detect_format(b"", "empty.csv")


def test_detect_encoding_handles_bom_utf8_utf16_and_cyrillic():
    assert detect_encoding("Привет".encode()) == "utf-8"
    assert detect_encoding("Привет".encode("utf-8-sig")) == "utf-8-sig"
    assert detect_encoding("Привет".encode("utf-16")) == "utf-16"
    assert detect_encoding("Привет мир".encode("cp1251")) == "cp1251"


def test_decode_bytes_survives_broken_encoding():
    raw = "Привет".encode("cp1251")
    assert decode_bytes(raw, "utf-8") == raw.decode("utf-8", errors="replace")
    assert decode_bytes("Привет".encode(), "unknown-charset") == "Привет"


def test_parsed_table_row_count_and_columns():
    table = ParsedTable(headers=["a", "b"], rows=[{"a": "1", "b": "2"}])
    assert table.row_count == 1
    assert table.column_values("a") == ["1"]


# ---------------------------------------------------------------------------
# CSV / TSV / TXT
# ---------------------------------------------------------------------------


def test_parse_csv_detects_semicolon_and_quotes():
    data = 'Название;Описание\nАльфа;"строка, с запятой"\n'
    table = parse_csv(data.encode("cp1251"))
    assert table.headers == ["Название", "Описание"]
    assert table.rows == [{"Название": "Альфа", "Описание": "строка, с запятой"}]
    assert table.encoding == "cp1251"


def test_parse_csv_deduplicates_headers_and_skips_empty_rows():
    data = b"title,title,\na,1,x\n\nb,2,y\n"
    table = parse_csv(data)
    assert table.headers == ["title", "title_2", "column_3"]
    assert len(table.rows) == 2


def test_parse_tsv_uses_tab_delimiter():
    table = parse_tsv(b"title\tlogin\nAlpha\tuser1\n")
    assert table.format == FORMAT_TSV
    assert table.rows == [{"title": "Alpha", "login": "user1"}]


def test_parse_txt_detects_delimiter_and_plain_lines():
    delimited = parse_txt(b"title;login\nAlpha;user1\n")
    assert delimited.headers == ["title", "login"]
    assert delimited.rows[0]["login"] == "user1"

    plain = parse_txt(b"first\nsecond\n")
    assert plain.headers == ["text"]
    assert plain.rows == [{"text": "first"}, {"text": "second"}]


# ---------------------------------------------------------------------------
# JSON / JSONL
# ---------------------------------------------------------------------------


def test_parse_json_accepts_list_and_wrapper():
    table = parse_json(b'[{"title": "A", "n": 1}, {"title": "B", "extra": true}]')
    assert table.headers == ["title", "n", "extra"]
    assert table.rows[1] == {"title": "B", "n": "", "extra": "true"}

    wrapped = parse_json(b'{"records": [{"title": "A"}], "total": 1}')
    assert wrapped.rows == [{"title": "A"}]


def test_parse_json_flattens_nested_objects():
    table = parse_json(b'[{"title": "A", "meta": {"n": 1}}]')
    assert json.loads(table.rows[0]["meta"]) == {"n": 1}


def test_parse_json_rejects_invalid_payload():
    with pytest.raises(FormatParseError):
        parse_json(b"{not json}")
    with pytest.raises(FormatParseError):
        parse_json(b'"just a string"')


def test_parse_jsonl_reads_each_line_and_reports_bad_line():
    table = parse_jsonl(b'{"title": "A"}\n\n{"title": "B"}\n')
    assert [row["title"] for row in table.rows] == ["A", "B"]
    with pytest.raises(FormatParseError, match="строке 2"):
        parse_jsonl(b'{"title": "A"}\nnot-json\n')


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


def test_parse_xml_repeated_elements_and_attributes():
    content = (
        "<records>"
        '<record code="a"><title>A</title><meta><n>1</n></meta></record>'
        '<record code="b"><title>B</title><meta><n>2</n></meta></record>'
        "</records>"
    )
    table = parse_xml(content.encode())
    assert table.headers == ["code", "title", "meta"]
    assert table.rows[0] == {"code": "a", "title": "A", "meta": "1"}
    assert table.rows[1]["code"] == "b"


def test_parse_xml_single_object():
    table = parse_xml(b"<record><title>Only</title></record>")
    assert table.rows == [{"title": "Only"}]


def test_parse_xml_invalid_raises_format_error():
    with pytest.raises(FormatParseError):
        parse_xml(b"<records><record></records>")


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def test_parse_xlsx_inline_strings_and_numbers():
    table = parse_xlsx(xlsx_bytes([["Title", "Login"], ["Alpha", "user1"], ["Beta", 42]]))
    assert table.headers == ["Title", "Login"]
    assert table.rows == [
        {"Title": "Alpha", "Login": "user1"},
        {"Title": "Beta", "Login": "42"},
    ]
    assert table.format == FORMAT_XLSX


def test_parse_xlsx_shared_strings():
    table = parse_xlsx(xlsx_bytes([["Название", "Логин"], ["Альфа", "user"]], shared=True))
    assert table.rows == [{"Название": "Альфа", "Логин": "user"}]


def test_parse_xlsx_handles_column_gaps():
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1"><c r="A1" t="inlineStr"><is><t>title</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>login</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Alpha</t></is></c>'
        '<c r="C2" t="inlineStr"><is><t>user1</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    table = parse_xlsx(buffer.getvalue())
    assert table.headers == ["title", "column_2", "login"]
    assert table.rows[0]["login"] == "user1"


def test_parse_xlsx_rejects_broken_zip():
    with pytest.raises(FormatParseError):
        parse_xlsx(b"not a zip")


# ---------------------------------------------------------------------------
# XLS
# ---------------------------------------------------------------------------


def test_parse_xls_rejects_non_ole_file():
    with pytest.raises(FormatParseError):
        parse_xls(b"plain text")


def test_biff_workbook_reader_handles_sst_labelsst_and_mulrk():
    stream = biff_stream([["Title", "Login"], ["Альфа", "user1"]])
    matrix = _parse_biff_workbook(stream)
    assert matrix == [["Title", "Login"], ["Альфа", "user1"]]


def test_biff_workbook_reader_handles_mulrk():
    bof = record(0x0809, struct.pack("<HH", 0x0600, 0x0005))
    eof = record(0x000A)
    sst = record(
        0x00FC, struct.pack("<II", 1, 1) + struct.pack("<HB", 5, 1) + "Title".encode("utf-16-le")
    )
    labelsst = record(0x00FD, struct.pack("<HHHI", 0, 0, 0, 0))
    # row=1, first col=0, два RK: 10 и 20, last col=1
    mulrk_data = struct.pack("<HH", 1, 0)
    mulrk_data += struct.pack("<HI", 0, (10 << 2) | 0x02)
    mulrk_data += struct.pack("<HI", 0, (20 << 2) | 0x02)
    mulrk_data += struct.pack("<H", 1)
    mulrk = record(0x00BD, mulrk_data)
    sheet_prefix = bof + labelsst + mulrk + eof
    name = "Sheet1"
    boundsheet_data = struct.pack("<I", 0) + struct.pack("<H", 0) + bytes([len(name), 0])
    boundsheet_data += name.encode("cp1252")
    boundsheet = record(0x0085, boundsheet_data)
    offset = len(bof) + len(boundsheet) + len(sst) + len(eof)
    boundsheet = record(0x0085, struct.pack("<I", offset) + boundsheet_data[4:])
    stream = bof + boundsheet + sst + eof + sheet_prefix
    matrix = _parse_biff_workbook(stream)
    assert matrix == [["Title", ""], ["10", "20"]]


# ---------------------------------------------------------------------------
# Диспетчер
# ---------------------------------------------------------------------------


def test_parse_file_dispatches_by_format():
    csv_table = parse_file(b"title\nA\n", "x.csv")
    assert csv_table.format == FORMAT_CSV
    xml_table = parse_file(b"<r><i><title>A</title></i></r>", "x.xml")
    assert xml_table.format == FORMAT_XML


def test_parse_file_rejects_unknown_format():
    with pytest.raises(UnsupportedFormatError):
        parse_file(b"data", "x.bin", format_name="YAML")
