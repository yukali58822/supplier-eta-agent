from __future__ import annotations

from collections import defaultdict

from .models import LineMatch, NormalizedLine, POLine, ShortageTask


def _item_hit(line: POLine, code: str | None) -> bool:
    if not code:
        return False
    c = code.upper()
    return c == line.item_code.upper() or (line.supplier_part_no or "").upper() == c


def match_lines(lines: list[NormalizedLine], po_lines: list[POLine],
                tasks: list[ShortageTask]) -> list[LineMatch]:
    results: list[LineMatch] = []
    for n in lines:
        m = LineMatch(index=n.index)
        cands = po_lines
        if n.po_number:
            cands = [l for l in po_lines if l.po == n.po_number]
            if not cands:
                m.issues.append(f"採購單 {n.po_number} 不存在、已結案或不屬於此供應商")
                results.append(m)
                continue
        else:
            m.inferred_po = True
        if n.item_code:
            cands = [l for l in cands if _item_hit(l, n.item_code)]
        elif not n.po_number or len(cands) != 1:
            cands = []
            m.issues.append("信中沒有料號，無法確定明細")
        if len(cands) == 1:
            m.po_line = cands[0]
            if not n.item_code:
                m.issues.append(f"信中沒有料號，推定為這張單唯一未結的明細 {cands[0].item_code}，須人工確認")
        elif not m.issues:
            m.issues.append(f"找不到唯一明細（符合 {len(cands)} 筆）：{n.po_number or '未提供單號'} / {n.item_code}")

        if m.po_line:
            open_tasks = [t for t in tasks if t.po_item == m.po_line.line]
            hit = [t for t in open_tasks if n.qty is not None and abs(t.qty - n.qty) < 1e-9]
            if len(hit) == 1:
                m.shortage_task = hit[0]
            elif len(hit) > 1:
                m.issues.append("同一明細有多筆相同數量的未結補料任務")
        results.append(m)

    by_line: dict[str, list[tuple[NormalizedLine, LineMatch]]] = defaultdict(list)
    for n, m in zip(lines, results):
        if m.po_line and not m.shortage_task:
            by_line[m.po_line.line].append((n, m))
    for pairs in by_line.values():
        total = sum(n.qty or 0 for n, _ in pairs)
        remaining = pairs[0][1].po_line.remaining
        if total > remaining + 1e-9:
            for _, m in pairs:
                m.issues.append(f"承諾數量合計 {total:g} 超過剩餘未交量 {remaining:g}")
    return results
