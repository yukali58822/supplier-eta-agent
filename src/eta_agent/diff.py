from __future__ import annotations

from collections import defaultdict
from datetime import date

from .models import LineDiff, LineMatch, NormalizedLine

Batches = list[tuple[float, date | None]]


def compute_diffs(lines: list[NormalizedLine], matches: list[LineMatch], intent: str) -> list[LineDiff]:
    out = []
    for n, m in zip(lines, matches):
        if not m.po_line:
            continue
        pl = m.po_line
        if m.shortage_task:
            base_date = m.shortage_task.required_date
        else:
            base_date = pl.schedule_date
        eta = n.eta
        if eta is None and intent == "confirm" and not n.date_text:
            eta = pl.schedule_date
        price_changed = None
        if n.unit_price is not None and pl.rate is not None:
            price_changed = abs(n.unit_price - pl.rate) > 1e-6
        qty_ref = m.shortage_task.qty if m.shortage_task else pl.remaining
        out.append(LineDiff(
            index=n.index, po=pl.po, line=pl.line, item_code=pl.item_code,
            original_date=base_date, new_eta=eta,
            date_diff_days=(eta - base_date).days if eta else None,
            original_qty=pl.qty, remaining_qty=pl.remaining, stated_qty=n.qty,
            qty_diff=(n.qty - qty_ref) if n.qty is not None else None,
            price_changed=price_changed,
            shortage_task=m.shortage_task.name if m.shortage_task else None))
    return out


def supply_scenario(diffs: list[LineDiff], intent: str) -> dict[str, Batches]:
    by_line: dict[str, list[LineDiff]] = defaultdict(list)
    for d in diffs:
        by_line[d.line].append(d)
    scen: dict[str, Batches] = {}
    for line, ds in by_line.items():
        if any(d.shortage_task for d in ds):
            continue
        remaining = ds[0].remaining_qty
        if intent == "cancel_balance":
            scen[line] = []
        elif intent == "price_change":
            continue
        elif intent == "split_delivery":
            batches: Batches = [(d.stated_qty or 0, d.new_eta) for d in ds if d.stated_qty]
            committed = sum(q for q, _ in batches)
            if remaining - committed > 1e-9:
                batches.append((remaining - committed, None))
            scen[line] = batches
        else:
            d = ds[0]
            scen[line] = [(remaining, d.new_eta)]
    return scen


def shortage_scenario(diffs: list[LineDiff]) -> dict[str, tuple[float, date | None]]:
    return {d.shortage_task: (d.stated_qty or 0, d.new_eta) for d in diffs if d.shortage_task}
