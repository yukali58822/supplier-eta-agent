from __future__ import annotations

from typing import Any

from .config import Policy
from .models import (Extraction, ImpactKind, ImpactResult, LineDiff, LineMatch, NormalizedLine,
                     RouteDecision)

import os

DEFAULT_ASSIGNEE = os.environ.get("ETA_BUYER", "buyer@example.com")


def build_actions(intent: str, diffs: list[LineDiff], matches: list[LineMatch],
                  normalized: list[NormalizedLine], supplier: str,
                  versions: dict[str, str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    by_idx = {m.index: m for m in matches}
    if any(d.shortage_task for d in diffs):
        for d in diffs:
            if d.shortage_task:
                actions.append({"type": "close_shortage_task", "task": d.shortage_task,
                                "committed_date": str(d.new_eta) if d.new_eta else None})
        return actions
    seen: set[str] = set()
    for d in diffs:
        pl = by_idx[d.index].po_line
        base = {"po": d.po, "line": d.line, "expected_version": versions[d.line]}
        if intent == "confirm":
            actions.append({"type": "record_confirmation", "po": d.po, "line": d.line})
        elif intent == "date_change" and d.new_eta and d.date_diff_days:
            actions.append({**base, "type": "update_po_line", "set": {"schedule_date": str(d.new_eta)}})
        elif intent == "price_change":
            n = next(x for x in normalized if x.index == d.index)
            if n.unit_price is not None:
                actions.append({**base, "type": "update_po_line", "set": {"rate": n.unit_price}})
        elif intent == "cancel_balance" and d.line not in seen:
            actions.append({**base, "type": "update_po_line", "set": {"qty": pl.received_qty}})
        elif intent == "split_delivery" and d.line not in seen:
            line_diffs = [x for x in diffs if x.line == d.line]
            dated = sorted((x for x in line_diffs if x.new_eta and x.stated_qty), key=lambda x: x.new_eta)
            if dated and dated[0].new_eta != d.original_date:
                actions.append({**base, "type": "update_po_line", "set": {"schedule_date": str(dated[0].new_eta)}})
            first_qty = dated[0].stated_qty if dated else 0
            shortage = d.remaining_qty - first_qty
            if shortage > 0:
                actions.append({"type": "create_shortage_task", "po": d.po, "line": d.line, "supplier": supplier,
                                "item_code": d.item_code, "qty": shortage, "required_date": str(d.original_date),
                                "assigned_to": DEFAULT_ASSIGNEE})
        seen.add(d.line)
    return actions


def route(extraction: Extraction, normalized: list[NormalizedLine], evidence_problems: list[str],
          matches: list[LineMatch], diffs: list[LineDiff], impact: ImpactResult | None,
          pol: Policy) -> RouteDecision:
    reasons: list[str] = []
    intent = extraction.intent

    if intent == "other":
        return RouteDecision(route="manual", reasons=["意圖為 other：不更新採購單，交由人工處理"]
                             + ([f"擷取備註：{extraction.notes}"] if extraction.notes else []))
    if not normalized:
        return RouteDecision(route="review", reasons=["未擷取到任何明細"])

    if extraction.notes and "注入" in extraction.notes:
        reasons.append("信件含疑似要求忽略規則的文字（不影響授權與寫回規則，但須人工確認）")
    for p in evidence_problems:
        reasons.append(f"擷取與原文不一致：{p}")
    for n in normalized:
        for i in n.issues:
            reasons.append(f"第 {n.index + 1} 行：{i}")
    for m in matches:
        for i in m.issues:
            reasons.append(f"第 {m.index + 1} 行：{i}")
        if m.inferred_po and m.po_line and not pol.allow_inferred_po and not m.shortage_task:
            reasons.append(f"第 {m.index + 1} 行：信中未寫採購單號（推定為 {m.po_line.po}），須人工確認")
    if extraction.confidence < pol.min_llm_confidence:
        reasons.append(f"擷取信心 {extraction.confidence:.2f} 低於門檻 {pol.min_llm_confidence}")
    if len(diffs) != len(normalized):
        reasons.append("有明細無法對應採購單")

    shortage_lines = [d for d in diffs if d.shortage_task]
    if shortage_lines and len(shortage_lines) != len(diffs):
        reasons.append("同一封信混合補料回覆與一般交期，整封送審")

    if shortage_lines and len(shortage_lines) == len(diffs):
        for d in shortage_lines:
            if d.new_eta is None:
                reasons.append(f"補料任務 {d.shortage_task}：日期不明確，不自動結案")
            elif d.new_eta > d.original_date:
                reasons.append(f"補料任務 {d.shortage_task}：承諾 {d.new_eta} 晚於需求日 {d.original_date}，任務維持未結")
        if intent not in pol.auto_intents:
            reasons.append(f"意圖 {intent} 不屬於可自動處理的意圖")
        _impact_reasons(impact, pol, reasons)
        if not pol.shortage_auto_close:
            reasons.append("設定檔停用補料自動結案")
        if reasons:
            return RouteDecision(route="review", reasons=reasons)
        return RouteDecision(route="auto", reasons=[
            f"補料任務 {d.shortage_task}：承諾 {d.new_eta} 不晚於需求日 {d.original_date}，自動結案"
            for d in shortage_lines])

    if intent not in pol.auto_intents:
        reasons.append({"price_change": "單價變更一律人工審核",
                        "cancel_balance": "取消剩餘未交數量一律人工審核",
                        "split_delivery": "分批交貨一律人工審核"}.get(intent, f"意圖 {intent} 須人工審核"))
    for d in diffs:
        tag = f"{d.po} {d.item_code}"
        if d.new_eta is None:
            if intent in ("confirm", "date_change"):
                reasons.append(f"{tag}：沒有明確的到貨日")
        elif d.date_diff_days is not None:
            if intent == "confirm" and d.date_diff_days != 0:
                reasons.append(f"{tag}：確認信的日期 {d.new_eta} 與原交期 {d.original_date} 不同")
            else:
                total = d.cumulative_diff_days if d.cumulative_diff_days is not None else d.date_diff_days
                this = f"這次{'延後' if d.date_diff_days >= 0 else '提前'} {abs(d.date_diff_days)} 天，" \
                    if d.cumulative_diff_days is not None else ""
                since = f"（基準交期 {d.baseline_date}）" if d.cumulative_diff_days is not None else ""
                word = "累計" if d.cumulative_diff_days is not None else ""
                if total > pol.max_delay_days:
                    reasons.append(f"{tag}：{this}{word}延後 {total} 天{since}，超過 {pol.max_delay_days} 天門檻")
                elif -total > pol.max_advance_days:
                    reasons.append(f"{tag}：{this}{word}提前 {-total} 天{since}，超過 {pol.max_advance_days} 天門檻")
        if d.qty_diff not in (None, 0) and intent in ("confirm", "date_change"):
            reasons.append(f"{tag}：數量 {d.stated_qty:g} 與剩餘未交量 {d.remaining_qty:g} 不同")
        if d.price_changed and intent != "price_change":
            reasons.append(f"{tag}：信中單價與採購單不同")
    if intent in pol.auto_intents:
        _impact_reasons(impact, pol, reasons)
    if intent == "date_change" and diffs and all(d.date_diff_days == 0 for d in diffs) and not reasons:
        pass

    if reasons:
        return RouteDecision(route="review", reasons=list(dict.fromkeys(reasons)))
    ok = [f"{d.po} {d.item_code}：" + ("確認原交期" if not d.date_diff_days else
          f"{'延後' if d.date_diff_days > 0 else '提前'} {abs(d.date_diff_days)} 天"
          + (f"（累計 {d.cumulative_diff_days:+d} 天，門檻內）" if d.cumulative_diff_days is not None else "（門檻內）"))
          for d in diffs]
    return RouteDecision(route="auto", reasons=ok + ["影響分析：no_impact（信心 high）",
                                                     f"擷取信心 {extraction.confidence:.2f}"])


def _impact_reasons(impact: ImpactResult | None, pol: Policy, reasons: list[str]) -> None:
    if impact is None:
        reasons.append("未完成影響分析")
        return
    if impact.result == ImpactKind.IMPACT_DETECTED:
        reasons.extend(f"影響分析：{r}" for r in impact.reasons)
        if not impact.reasons:
            reasons.append("影響分析：impact_detected")
    elif impact.result == ImpactKind.UNKNOWN:
        reasons.append("影響分析結果 unknown：" + "；".join(impact.reasons))
    if impact.result == ImpactKind.NO_IMPACT and impact.confidence != pol.required_impact_confidence:
        reasons.append(f"影響分析信心為 {impact.confidence}（" + "；".join(
            a for a in impact.assumptions if "預設" in a) + "）")
