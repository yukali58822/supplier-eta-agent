from __future__ import annotations

from typing import Any

from ..authz import llm_context_guard
from ..models import POLine, ShortageTask


def build_llm_context(erp: Any, supplier: str, body: str) -> tuple[list[POLine], list[ShortageTask], str,
                                                                    dict[str, list[dict[str, Any]]], list[str]]:
    dropped: list[str] = []
    lines = []
    for l in erp.open_po_lines(supplier):
        if l.supplier == supplier:
            lines.append(l)
        else:
            dropped.append(f"採購明細 {l.po}/{l.line} 屬於 {l.supplier}，不是 {supplier}")
    tasks = []
    for t in erp.open_shortage_tasks(supplier):
        if t.supplier == supplier:
            tasks.append(t)
        else:
            dropped.append(f"補料任務 {t.name} 屬於 {t.supplier}，不是 {supplier}")
    safe_body, ctx = llm_context_guard(body, {
        "Purchase Order Item": [{"parent": l.po, "item_code": l.item_code, "supplier_part_no": l.supplier_part_no,
                                 "qty": l.qty, "rate": l.rate} for l in lines],
        "Shortage Task": [{"name": t.name, "item_code": t.item_code, "purchase_order": t.purchase_order}
                          for t in tasks]})
    return lines, tasks, safe_body, ctx, dropped
