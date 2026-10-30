from datetime import date

import pytest

from eta_agent.models import ExtractedLine
from eta_agent.normalize import DateParseError, evidence_consistent, normalize_line, parse_date, parse_qty
from eta_agent.workcal import tw_2026

REF = date(2026, 10, 26)


@pytest.mark.parametrize("text,expected", [
    ("115/11/09", date(2026, 11, 9)),
    ("民國115年11月9日", date(2026, 11, 9)),
    ("2026-11-09", date(2026, 11, 9)),
    ("2026/11/9", date(2026, 11, 9)),
    ("11/9", date(2026, 11, 9)),
    ("11月9日", date(2026, 11, 9)),
    ("下週二", date(2026, 11, 3)),
    ("本週五", date(2026, 10, 30)),
    ("下下週一", date(2026, 11, 9)),
    ("明天", date(2026, 10, 27)),
    ("Nov 9", date(2026, 11, 9)),
    ("9th Nov", date(2026, 11, 9)),
    ("November 9th, 2026", date(2026, 11, 9)),
    ("next Tuesday", date(2026, 11, 3)),
    ("1/5", date(2027, 1, 5)),
])
def test_parse_date(text, expected):
    assert parse_date(text, REF) == expected


@pytest.mark.parametrize("text", ["11月底", "下旬", "盡快", "ASAP", "11/9 或 11/10", "early Nov", "約 11/9",
                                  "2/30", "", "TBD"])
def test_ambiguous_dates_rejected(text):
    with pytest.raises(DateParseError):
        parse_date(text, REF)


@pytest.mark.parametrize("text,expected", [("1K", 1000), ("1,000 pcs", 1000), ("1.5k", 1500),
                                           ("500 個", 500), ("2千", 2000), ("800", 800)])
def test_parse_qty(text, expected):
    assert parse_qty(text) == expected


def test_ship_date_plus_transit_days():
    x = ExtractedLine(date_text="115/11/09", date_kind="ship", evidence="x")
    n, a = normalize_line(0, x, REF, 3, 3)
    assert n.eta == date(2026, 11, 12) and not n.issues
    assert "ETD" in a[0]


def test_unknown_date_kind_blocks_auto():
    x = ExtractedLine(date_text="11/9", date_kind="unknown", evidence="x")
    n, _ = normalize_line(0, x, REF, 3, 3)
    assert n.eta is None and any("日期種類不明" in i for i in n.issues)


def test_missing_supplier_transit_uses_default_with_assumption():
    x = ExtractedLine(date_text="11/9", date_kind="ship", evidence="x")
    n, a = normalize_line(0, x, REF, None, 3)
    assert n.eta == date(2026, 11, 12) and "預設" in a[0]


def test_evidence_must_be_in_email():
    body = "PUR-ORD-2026-00002 FM-32F4 只能於 11/9 出貨"
    ok = ExtractedLine(po_number="PUR-ORD-2026-00002", date_text="11/9", date_kind="ship",
                       evidence="只能於 11/9 出貨")
    bad = ExtractedLine(po_number="PUR-ORD-2026-00002", date_text="11/7", date_kind="ship",
                        evidence="只能於 11/7 出貨")
    assert evidence_consistent(ok, body) == []
    assert evidence_consistent(bad, body)


def test_workcal_tw_2026():
    cal = tw_2026()
    assert not cal.is_workday(date(2026, 10, 9))
    assert cal.next_workday_on_or_after(date(2026, 10, 24)) == date(2026, 10, 27)
    assert cal.add_workdays(date(2026, 11, 12), 3) == date(2026, 11, 17)
    assert cal.workdays_between(date(2026, 11, 23), date(2026, 11, 26)) == 3
