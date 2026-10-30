from __future__ import annotations

import re
from datetime import date, timedelta

from .models import ExtractedLine, NormalizedLine

WEEKDAYS_ZH = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
WEEKDAYS_EN = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS_EN = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

AMBIGUOUS = re.compile(
    r"(月底|月初|上旬|中旬|下旬|左右|前後|大約|約|預計.*或|盡快|儘快|待定|TBD|ASAP|as soon as|around|approx|"
    r"early|mid|late|end of|or so|~|～|或)", re.I)


class DateParseError(ValueError):
    pass


def _year_for(month: int, day: int, ref: date) -> date:
    d = date(ref.year, month, day)
    if (ref - d).days > 60:
        d = date(ref.year + 1, month, day)
    return d


def parse_date(text: str, ref: date) -> date:
    if not text or not text.strip():
        raise DateParseError("日期為空")
    t = text.strip()
    if AMBIGUOUS.search(t):
        raise DateParseError(f"日期不明確：{t}")
    tl = t.lower()

    if tl in ("今天", "today"):
        return ref
    if tl in ("明天", "tomorrow"):
        return ref + timedelta(days=1)
    if tl in ("後天",):
        return ref + timedelta(days=2)
    m = re.fullmatch(r"(本|這|下|下下)(?:週|周|星期|禮拜)([一二三四五六日天])", t)
    if m:
        offset = {"本": 0, "這": 0, "下": 1, "下下": 2}[m.group(1)]
        monday = ref - timedelta(days=ref.weekday())
        return monday + timedelta(weeks=offset, days=WEEKDAYS_ZH[m.group(2)])
    m = re.fullmatch(r"(this|next)\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*", tl)
    if m:
        monday = ref - timedelta(days=ref.weekday())
        offset = 0 if m.group(1) == "this" else 1
        return monday + timedelta(weeks=offset, days=WEEKDAYS_EN[m.group(2)])

    m = re.fullmatch(r"(?:民國)?\s*(\d{2,4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})\s*日?", t)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        if y < 1000:
            y += 1911
        return _safe(y, mo, d, t)
    m = re.fullmatch(r"(\d{1,2})\s*[/月]\s*(\d{1,2})\s*[日號]?", t)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        _safe(ref.year, mo, d, t)
        return _year_for(mo, d, ref)
    m = re.fullmatch(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", tl)
    if m and m.group(1) in MONTHS_EN:
        mo, d = MONTHS_EN[m.group(1)], int(m.group(2))
        return _safe(int(m.group(3)), mo, d, t) if m.group(3) else _year_for(mo, d, ref)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\.?(?:,?\s*(\d{4}))?", tl)
    if m and m.group(2) in MONTHS_EN:
        mo, d = MONTHS_EN[m.group(2)], int(m.group(1))
        return _safe(int(m.group(3)), mo, d, t) if m.group(3) else _year_for(mo, d, ref)
    raise DateParseError(f"無法辨識的日期：{t}")


def _safe(y: int, mo: int, d: int, raw: str) -> date:
    try:
        return date(y, mo, d)
    except ValueError as e:
        raise DateParseError(f"日期不存在：{raw}") from e


_QTY = re.compile(r"^\s*([\d,]*\.?\d+)\s*([kK千萬]?)\s*(?:pcs?|ea|個|顆|片|只|件|units?)?\s*$", re.I)


def parse_qty(text: str) -> float:
    m = _QTY.match(text or "")
    if not m:
        raise ValueError(f"無法辨識的數量：{text}")
    n = float(m.group(1).replace(",", ""))
    mult = {"k": 1000, "K": 1000, "千": 1000, "萬": 10000}.get(m.group(2), 1)
    return n * mult


_PRICE = re.compile(r"(?:NT\$|NTD|TWD|US\$|USD|\$)?\s*([\d,]*\.?\d+)")


def parse_price(text: str) -> float:
    m = _PRICE.search(text or "")
    if not m:
        raise ValueError(f"無法辨識的單價：{text}")
    return float(m.group(1).replace(",", ""))


def normalize_line(idx: int, x: ExtractedLine, email_date: date, transit_days: int | None,
                   default_transit: int) -> tuple[NormalizedLine, list[str]]:
    issues: list[str] = []
    assumptions: list[str] = []
    qty = price = None
    if x.qty_text:
        try:
            qty = parse_qty(x.qty_text)
        except ValueError as e:
            issues.append(str(e))
    if x.unit_price_text:
        try:
            price = parse_price(x.unit_price_text)
        except ValueError as e:
            issues.append(str(e))

    stated = eta = None
    if x.date_text:
        try:
            stated = parse_date(x.date_text, email_date)
        except DateParseError as e:
            issues.append(str(e))
        if stated:
            if x.date_kind == "arrival":
                eta = stated
            elif x.date_kind == "ship":
                if transit_days is None:
                    assumptions.append(f"供應商未設定運輸天數，採預設 {default_transit} 天")
                    eta = stated + timedelta(days=default_transit)
                else:
                    eta = stated + timedelta(days=transit_days)
                    assumptions.append(f"ETA = ETD {stated} + 運輸 {transit_days} 日曆天")
            else:
                issues.append(f"日期種類不明（無法確定 {x.date_text} 是出貨日或到貨日）")
    return NormalizedLine(
        index=idx, po_number=(x.po_number or "").strip() or None,
        item_code=(x.item_code or "").strip() or None, qty=qty, unit_price=price,
        date_text=x.date_text, date_kind=x.date_kind, stated_date=stated, eta=eta,
        evidence=x.evidence, issues=issues,
    ), assumptions


def evidence_consistent(x: ExtractedLine, body: str) -> list[str]:
    problems: list[str] = []
    flat = re.sub(r"\s+", "", body)
    ev = re.sub(r"\s+", "", x.evidence or "")
    if not ev or ev not in flat:
        problems.append("引用的原文片段不在信件中")
        return problems
    for label, val in (("日期", x.date_text), ("數量", x.qty_text), ("採購單號", x.po_number),
                       ("料號", x.item_code), ("單價", x.unit_price_text)):
        if val and re.sub(r"\s+", "", val) not in flat:
            problems.append(f"{label}「{val}」不在信件原文中")
    return problems


NULL_STRINGS = {"", "null", "none", "n/a", "na", "nil", "無", "未提供", "待定"}


def clean_nulls(x: ExtractedLine) -> ExtractedLine:
    upd = {}
    for f in ("po_number", "item_code", "qty_text", "unit_price_text", "date_text"):
        v = getattr(x, f)
        if isinstance(v, str) and v.strip().lower() in NULL_STRINGS:
            upd[f] = None
    if upd.get("date_text", x.date_text) is None and x.date_kind != "unknown":
        upd["date_kind"] = "unknown"
    return x.model_copy(update=upd) if upd else x


def align_item_alias(x: ExtractedLine, body: str, aliases: list[tuple[str, str | None]]) -> tuple[ExtractedLine, str | None]:
    code = (x.item_code or "").strip()
    if not code:
        return x, None
    flat = re.sub(r"\s+", "", body).upper()
    if code.upper() in flat:
        return x, None
    present = set()
    for item_code, part_no in aliases:
        names = [n for n in (item_code, part_no) if n]
        if code.upper() in (n.upper() for n in names):
            present |= {n for n in names if n.upper() in flat}
    if len(present) == 1:
        alias = present.pop()
        return x.model_copy(update={"item_code": alias}), f"料號 {code} 改回原文寫法 {alias}"
    return x, None


def align_date_text(x: ExtractedLine, body: str, ref: date) -> tuple[ExtractedLine, str | None]:
    from .llm.extractor import DATE_RE
    dt = (x.date_text or "").strip()
    if not dt or re.sub(r"\s+", "", dt) in re.sub(r"\s+", "", body):
        return x, None
    try:
        target = parse_date(dt, ref)
    except DateParseError:
        return x, None
    for scope in (x.evidence or "", body):
        hits = set()
        for tok in DATE_RE.findall(scope):
            try:
                if parse_date(tok, ref) == target:
                    hits.add(tok)
            except DateParseError:
                continue
        if len(hits) == 1:
            tok = hits.pop()
            return x.model_copy(update={"date_text": tok}), f"日期 {dt} 改回原文寫法 {tok}"
        if hits:
            return x, None
    return x, None
