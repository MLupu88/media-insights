from datetime import date

import pytest
from openpyxl import Workbook

from app.models.article import ImportStatus
from app.services.excel_parser import ParserError, normalize_header, parse_date, parse_numeric, parse_workbook


def test_standard_workbook_detects_header_and_all_data_rows(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")

    assert result.workbook_sheet == "Monitorizare"
    assert len(result.rows) == 5


def test_title_hyperlink_becomes_mediatrust_url(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")
    first = result.rows[0]

    assert first.title == "Auchan lanseaza promotie de Paste"
    assert first.mediatrust_url == "https://mediatrust.example.com/a1"


def test_source_hyperlink_becomes_article_url(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")
    first = result.rows[0]

    assert first.source == "Ziarul Financiar"
    assert first.article_url == "https://zf.ro/articol-1"


def test_missing_hyperlinks_become_null(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")
    second = result.rows[1]

    assert second.title == "Auchan deschide un nou magazin"
    assert second.mediatrust_url is None
    assert second.source == "Adevarul"


def test_date_parsing_handles_datetime_and_text_formats(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")

    assert result.rows[0].publication_date == date(2026, 4, 3)
    assert result.rows[1].publication_date == date(2026, 4, 12)


def test_unparseable_date_becomes_null_without_failing_row(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")
    row = result.rows[4]

    assert row.publication_date is None
    assert row.import_status == ImportStatus.VALID


def test_numeric_parsing_handles_romanian_decimal_comma(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")

    assert result.rows[0].ave == pytest.approx(1234.56)
    assert result.rows[1].ave == pytest.approx(2500.0)


def test_unparseable_numeric_becomes_null(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")

    assert result.rows[4].ave is None


def test_missing_audience_stays_null_not_zero(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")

    assert result.rows[1].audience is None


def test_invalid_row_without_title_or_source_is_preserved_and_marked(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")
    invalid_rows = [r for r in result.rows if r.import_status == ImportStatus.INVALID]

    assert len(invalid_rows) == 1
    assert invalid_rows[0].title is None
    assert invalid_rows[0].source is None
    assert invalid_rows[0].import_error


def test_duplicate_rows_share_the_same_fingerprint(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")

    assert result.rows[0].fingerprint == result.rows[2].fingerprint
    assert result.rows[0].fingerprint != result.rows[1].fingerprint


def test_retailer_inferred_from_filename(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")

    assert all(row.retailer == "Auchan" for row in result.rows)


def test_explicit_retailer_hint_overrides_filename(standard_workbook_path):
    result = parse_workbook(
        str(standard_workbook_path), "Auchan Q2 2026.xlsx", retailer_hint="Carrefour"
    )

    assert all(row.retailer == "Carrefour" for row in result.rows)


def test_raw_json_preserves_original_values(standard_workbook_path):
    result = parse_workbook(str(standard_workbook_path), "Auchan Q2 2026.xlsx")
    raw = result.rows[0].raw_json

    assert raw["Titlu"] == "Auchan lanseaza promotie de Paste"
    assert raw["AVE"] == "1.234,56"


def test_penny_workbook_missing_subfolder_2_does_not_fail_import(penny_workbook_path):
    result = parse_workbook(str(penny_workbook_path), "Penny - Rewe Q2 2026.xlsx")

    assert len(result.rows) == 2
    assert all(row.subfolder_2 is None for row in result.rows)
    assert result.rows[0].subfolder_1 == "Retail"


def test_penny_retailer_inferred_from_filename(penny_workbook_path):
    result = parse_workbook(str(penny_workbook_path), "Penny - Rewe Q2 2026.xlsx")

    assert all(row.retailer == "Penny / Rewe" for row in result.rows)


def test_penny_header_at_first_row_is_detected(penny_workbook_path):
    result = parse_workbook(str(penny_workbook_path), "Penny - Rewe Q2 2026.xlsx")

    assert result.rows[0].title == "Penny Rewe extinde reteaua de magazine"
    assert result.rows[0].mediatrust_url == "https://mediatrust.example.com/p1"


def test_parser_raises_when_no_recognizable_headers(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["foo", "bar", "baz"])
    ws.append([1, 2, 3])
    path = tmp_path / "unrecognized.xlsx"
    wb.save(path)

    with pytest.raises(ParserError):
        parse_workbook(str(path), "unrecognized.xlsx")


def test_normalize_header_strips_diacritics_and_case():
    assert normalize_header("Ședință") == "sedinta"
    assert normalize_header("  Județ  ") == "judet"


def test_parse_date_variants():
    assert parse_date(None) is None
    assert parse_date("") is None
    assert parse_date("03.04.2026") == date(2026, 4, 3)
    assert parse_date("2026-04-03") == date(2026, 4, 3)
    assert parse_date("not a date") is None


def test_parse_numeric_variants():
    assert parse_numeric(None) is None
    assert parse_numeric("") is None
    assert parse_numeric(True) is None
    assert parse_numeric(1234) == 1234.0
    assert parse_numeric("1.234,56") == pytest.approx(1234.56)
    assert parse_numeric("1,234.56") == pytest.approx(1234.56)
    assert parse_numeric("garbage") is None
