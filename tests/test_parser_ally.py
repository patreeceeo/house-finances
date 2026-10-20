import datetime as dt
import hashlib
import os
from pathlib import Path

import pytest

from housefin.parsers import (
    AllyParser,
    ParseFormatError,
    detect_parser,
    fingerprint,
    normalize_description,
    parse_file,
)
from housefin.parsers.ally import parse_amount_cents

FIXTURES = Path(__file__).parent / "fixtures"
HEADER = "Date, Time, Amount, Type, Description\n"


def parse(body: str, header: str = HEADER):
    return AllyParser().parse_text(header + body)


def test_fixture_file_parses_all_rows():
    result = parse_file(FIXTURES / "ally_sample.csv")
    assert result.errors == []
    assert len(result.rows) == 4
    first = result.rows[0]
    assert first.date == dt.date(2026, 3, 5)
    assert first.time == dt.time(9, 15, 0)
    assert first.amount_cents == 25000
    assert first.type == "Deposit"
    assert first.line_no == 2
    assert [r.line_no for r in result.rows] == [2, 3, 4, 5]


def test_header_trimming_and_case():
    r = parse("2026-01-01,10:00:00,1.00,Deposit,X\n", header="  DATE ,Time,  amount, Type ,Description \n")
    assert r.errors == [] and len(r.rows) == 1


def test_bad_header_raises():
    with pytest.raises(ParseFormatError):
        parse("2026-01-01,10:00:00,1.00,Deposit,X\n", header="Date,Amount,Description\n")
    with pytest.raises(ParseFormatError):
        AllyParser().parse_text("")


def test_quoted_description_with_commas():
    r = parse('2026-01-02,11:00:00,-5.00,Withdrawal,"SHOP 1 Main St, Apt 3, TOWN, ZZ"\n')
    assert r.errors == []
    assert r.rows[0].description == "SHOP 1 Main St, Apt 3, TOWN, ZZ"


def test_cell_whitespace_trimmed():
    r = parse("  2026-01-02 , 11:00:00 , -5.00 , Withdrawal ,  SHOP  NAME  \n")
    assert r.errors == []
    row = r.rows[0]
    assert row.amount_cents == -500
    assert row.description == "SHOP  NAME"  # inner whitespace kept in raw text


@pytest.mark.parametrize(
    "text,cents",
    [("-600.00", -60000), ("1330.00", 133000), ("0.03", 3), ("-120.5", -12050),
     ("7", 700), ("+2.10", 210), ("1,234.56", 123456), ("-0.07", -7)],
)
def test_amount_cents(text, cents):
    assert parse_amount_cents(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "1.234", "--1.00", "1.2.3", "$"])
def test_amount_unparseable(text):
    with pytest.raises(ValueError):
        parse_amount_cents(text)


def test_sign_type_mismatch_reported_and_skipped():
    r = parse(
        "2026-01-03,12:00:00,10.00,Withdrawal,A\n"
        "2026-01-03,12:00:01,-10.00,Deposit,B\n"
        "2026-01-03,12:00:02,-10.00,Withdrawal,C\n"
    )
    assert [row.description for row in r.rows] == ["C"]
    assert [e.line_no for e in r.errors] == [2, 3]
    assert all("disagrees" in e.message for e in r.errors)


def test_zero_amount_accepted_with_either_type():
    r = parse("2026-01-03,12:00:00,0.00,Withdrawal,A\n2026-01-03,12:00:00,0.00,Deposit,B\n")
    assert r.errors == [] and len(r.rows) == 2


def test_unparseable_rows_reported_with_line_numbers():
    r = parse(
        "2026-13-01,10:00:00,1.00,Deposit,bad date\n"
        "2026-01-01,25:00:00,1.00,Deposit,bad time\n"
        "2026-01-01,10:00:00,lots,Deposit,bad amount\n"
        "2026-01-01,10:00:00,1.00,Refund,bad type\n"
        "2026-01-01,10:00:00,1.00,Deposit\n"
        "2026-01-01,10:00:00,1.00,Deposit,\n"
        "\n"
        "2026-01-01,10:00:00,1.00,Deposit,good\n"
    )
    assert [row.description for row in r.rows] == ["good"]
    assert r.rows[0].line_no == 9
    msgs = {e.line_no: e.message for e in r.errors}
    assert list(msgs) == [2, 3, 4, 5, 6, 7]
    assert "date" in msgs[2]
    assert "time" in msgs[3]
    assert "amount" in msgs[4]
    assert "type" in msgs[5]
    assert "columns" in msgs[6]
    assert "description" in msgs[7]


def test_line_numbers_account_for_multiline_quoted_field():
    r = parse('2026-01-01,10:00:00,1.00,Deposit,"two\nlines"\n2026-01-01,10:00:00,2.00,Deposit,next\n')
    assert [row.line_no for row in r.rows] == [2, 4]


def test_bom_handled(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_bytes(("﻿" + HEADER + "2026-01-01,10:00:00,1.00,Deposit,X\n").encode("utf-8"))
    assert isinstance(detect_parser(p), AllyParser)
    assert len(parse_file(p).rows) == 1


def test_detect_parser_rejects_unknown(tmp_path):
    p = tmp_path / "other.csv"
    p.write_text("Posted,Payee,Debit,Credit\n")
    with pytest.raises(ParseFormatError):
        detect_parser(p)


def test_normalize_description():
    assert normalize_description("  Zelle   payment\tfrom  Bob \n") == "Zelle payment from Bob"
    assert normalize_description("MiXeD Case") == "MiXeD Case"


def test_fingerprint():
    d, t = dt.date(2026, 1, 2), dt.time(3, 4, 5)
    expected = hashlib.sha256(b"2026-01-02|03:04:05|-1234|SHOP NAME").hexdigest()
    assert fingerprint(d, t, -1234, "  SHOP   NAME ") == expected
    assert fingerprint(d, t, -1234, "shop name") != expected  # case kept
    r = parse("2026-01-02,03:04:05,-12.34,Withdrawal,SHOP   NAME\n")
    assert r.rows[0].fingerprint == expected


SAMPLE = os.environ.get("HOUSEFIN_SAMPLE_CSV")


@pytest.mark.skipif(not SAMPLE, reason="HOUSEFIN_SAMPLE_CSV not set")
def test_real_sample_file():
    result = parse_file(SAMPLE)
    assert result.errors == []
    assert len(result.rows) == 124
    dates = [r.date for r in result.rows]
    assert min(dates) == dt.date(2026, 6, 25)
    assert max(dates) == dt.date(2026, 10, 20)
