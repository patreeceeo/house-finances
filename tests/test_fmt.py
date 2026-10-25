from decimal import Decimal

import pytest

from housefin import fmt


@pytest.mark.parametrize(
    "cents, text",
    [
        (0, "$0.00"),
        (5, "$0.05"),
        (-5, "-$0.05"),
        (123456, "$1,234.56"),
        (-123456789, "-$1,234,567.89"),
        (100, "$1.00"),
        (None, ""),
    ],
)
def test_format_cents(cents, text):
    assert fmt.cents(cents) == text


def test_cents_to_dollars():
    assert fmt.dollars(-1250) == -12.5
    assert fmt.dollars(None) is None


def test_format_month():
    assert fmt.month("2026-10") == "Oct 2026"
    assert fmt.month("weird") == "weird"
    assert fmt.month("2026-07", "de-DE") == "Juli 2026"


def test_format_day_span():
    assert fmt.day_span("2026-07-11", "2026-07-31") == "11–31 Jul"
    assert fmt.day_span("2026-07-11", "2026-07-31", "en-US") == "Jul 11\u2009–\u200931"  # thin spaces, per CLDR
    assert fmt.day_span("2026-07-11", "2026-07-11", "en-GB") == "11 Jul"


def test_md_cents_escapes_dollar_for_markdown():
    # Two unescaped '$' in one markdown string render as a math formula.
    assert fmt.md_cents(-1628633) == r"-\$16,286.33"


def test_signed_and_csv_dollars():
    assert [fmt.signed_cents(c) for c in (100, -100, 0)] == ["+$1.00", "-$1.00", "$0.00"]
    assert [fmt.csv_dollars(c) for c in (-123456, 5, 0)] == ["-1234.56", "0.05", "0.00"]


def test_percent_and_weight():
    assert fmt.percent(12.125) == "12.125%" and fmt.percent(50.0) == "50%"
    assert fmt.weight(Decimal(3100)) == "3,100"
    assert fmt.weight(Decimal("1034.625")) == "1,034.625"
