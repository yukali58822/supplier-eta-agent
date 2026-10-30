from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .config import Scheduling
from .models import (AffectedOrder, ImpactKind, ImpactResult, Item, POLine, SalesOrder, ShortageTask,
                     WorkOrder)
from .workcal import CalendarCoverageError, WorkCalendar

Batches = list[tuple[float, date | None]]
NEVER = date.max


@dataclass
class PlanningSnapshot:
    taken_at: str
    items: dict[str, Item | None]
    stock: dict[str, tuple[float, float]]
    po_lines: list[POLine]
    shortage_tasks: list[ShortageTask]
    work_orders: list[WorkOrder]
    sales_orders: dict[str, SalesOrder | None]
    calendar: WorkCalendar | None
    calendar_error: str | None = None
    notes: list[str] = field(default_factory=list)


def gather_snapshot(erp: Any, changed_items: list[str], sched: Scheduling) -> PlanningSnapshot:
    wos = erp.open_work_orders()
    relevant_items = set(changed_items)
    products = set()
    for w in wos:
        if set(w.required) & set(changed_items):
            relevant_items |= set(w.required)
            products.add(w.production_item)
    items = sorted(relevant_items)
    sos = {w.sales_order: erp.get_sales_order(w.sales_order) for w in wos if w.sales_order}
    cal, cal_err = None, None
    try:
        cal = erp.holiday_calendar(sched.holiday_list)
    except Exception as e:  # noqa: BLE001
        cal_err = f"無法讀取工作日曆 {sched.holiday_list}：{type(e).__name__}"
    return PlanningSnapshot(
        taken_at=datetime.now().isoformat(timespec="seconds"),
        items={i: erp.get_item(i) for i in sorted(relevant_items | products)},
        stock=erp.stock(items),
        po_lines=erp.open_po_lines_for_items(items),
        shortage_tasks=[t for t in erp.open_shortage_tasks() if t.item_code in relevant_items],
        work_orders=wos, sales_orders=sos, calendar=cal, calendar_error=cal_err)


def _ready(cal: WorkCalendar, sched: Scheduling, item: Item, eta: date) -> date:
    receipt = cal.next_workday_on_or_after(eta)
    return cal.add_workdays(receipt, sched.inspection_for(item.inspection_category) + sched.prep_days)


def _allocate(item: Item, snap: PlanningSnapshot, sched: Scheduling, cal: WorkCalendar,
              line_override: dict[str, Batches], task_override: dict[str, tuple[float, date | None]],
              basis: list[str], label: str) -> dict[str, date | None]:
    code = item.item_code
    actual, reserved = snap.stock.get(code, (0.0, 0.0))
    on_hand = max(actual - reserved, 0.0)
    receipts: list[tuple[date, float]] = []
    open_tasks = {t.po_item: t for t in snap.shortage_tasks if t.item_code == code}
    for pl in snap.po_lines:
        if pl.item_code != code:
            continue
        if pl.line in line_override:
            batches = line_override[pl.line]
        else:
            rem = pl.remaining
            batches = []
            t = open_tasks.get(pl.line)
            if t:
                rem -= t.qty
                q, d = task_override.get(t.name, (t.qty, None))
                batches.append((q, d))
            batches.append((rem, pl.schedule_date))
        for q, eta in batches:
            if q <= 0:
                continue
            receipts.append((_ready(cal, sched, item, eta) if eta else NEVER, q))
    receipts.sort()
    basis.append(f"[{label}] {code}: 庫存 {actual:g} − 預留 {reserved:g} = {on_hand:g}；在途 " +
                 (", ".join(f"{q:g}@{'無承諾' if r == NEVER else r}" for r, q in receipts) or "無"))

    out: dict[str, date | None] = {}
    wos = sorted((w for w in snap.work_orders if code in w.required),
                 key=lambda w: (w.planned_start_date or date.max, w.name))
    for w in wos:
        need = w.required[code]
        latest = date.min
        take = min(on_hand, need)
        on_hand -= take
        need -= take
        while need > 1e-9 and receipts:
            r, q = receipts[0]
            use = min(q, need)
            need -= use
            latest = max(latest, r)
            if q - use > 1e-9:
                receipts[0] = (r, q - use)
            else:
                receipts.pop(0)
        out[w.name] = None if need > 1e-9 or latest == NEVER else latest
    return out


def assess_impact(snap: PlanningSnapshot, sched: Scheduling, line_override: dict[str, Batches],
                  task_override: dict[str, tuple[float, date | None]], changed_items: list[str],
                  assumptions_in: list[str] | None = None) -> ImpactResult:
    basis: list[str] = [f"資料快照時間 {snap.taken_at}"]
    assumptions = list(assumptions_in or [])
    assumptions += [
        "預估值，不是 ERPNext 排程；產能視為無限、不計替代料與拆分工單",
        f"檢驗 {sched.inspection_days}、備料 {sched.prep_days} 天（scheduling.yaml）；"
        "生產與測試包裝出貨天數取自 ERPNext 料件主檔（均為工作天）",
        f"工作日曆：{sched.holiday_list}",
    ]
    reasons: list[str] = []
    low_conf = any("預設" in a for a in assumptions)

    def unknown(why: str) -> ImpactResult:
        return ImpactResult(result=ImpactKind.UNKNOWN, confidence="low", basis=basis,
                            assumptions=assumptions, reasons=reasons + [why])

    if snap.calendar is None:
        return unknown(snap.calendar_error or "缺少工作日曆")
    cal = snap.calendar
    for code in changed_items:
        if snap.items.get(code) is None:
            return unknown(f"料號 {code} 不在料件主檔，無法判斷 BOM 用量")

    try:
        relevant = [w for w in snap.work_orders if set(w.required) & set(changed_items)]
        if not relevant:
            basis.append(f"{', '.join(changed_items)} 沒有被任何未完工工單使用")
            return ImpactResult(result=ImpactKind.NO_IMPACT, confidence="low" if low_conf else "high",
                                basis=basis, assumptions=assumptions,
                                reasons=["料件未被任何未完工工單使用"])
        for w in relevant:
            if w.planned_start_date is None:
                return unknown(f"工單 {w.name} 缺少計畫開工日")
            if w.sales_order and (snap.sales_orders.get(w.sales_order) is None or
                                  snap.sales_orders[w.sales_order].delivery_date is None):
                return unknown(f"客戶訂單 {w.sales_order} 缺少交期")
            for code in w.required:
                if snap.items.get(code) is None:
                    return unknown(f"工單 {w.name} 用料 {code} 不在料件主檔")

        all_items = sorted({c for w in relevant for c in w.required})
        ready: dict[str, dict[str, date | None]] = {}
        for code in all_items:
            ready[code] = _allocate(snap.items[code], snap, sched, cal, line_override, task_override, basis,
                                    "變更後")

        affected: list[AffectedOrder] = []
        detected = False
        for w in relevant:
            planned = w.planned_start_date
            short_all = [c for c in w.required if ready[c].get(w.name) is None or ready[c][w.name] > planned]
            short_changed = [c for c in short_all if c in changed_items]
            preexisting = [c for c in short_all if c not in changed_items]
            if preexisting:
                basis.append(f"{w.name} 另有既有缺料（非本信造成）：{', '.join(preexisting)}")
            if not short_changed:
                continue
            detected = True
            prod, missing = sched.product(snap.items.get(w.production_item))
            if missing:
                low_conf = True
                names = {"production_days": "生產天數", "test_pack_ship_days": "測試包裝出貨天數"}
                assumptions.append(f"成品 {w.production_item} 在 ERPNext 料件主檔未設定"
                                   f"{'、'.join(names[m] for m in missing)}，採 scheduling.yaml 的預設值")
            else:
                basis.append(f"成品 {w.production_item}：生產 {prod['production_days']} 天、"
                             f"測試包裝出貨 {prod['test_pack_ship_days']} 天（ERPNext 料件主檔）")
            so = snap.sales_orders.get(w.sales_order) if w.sales_order else None
            if any(ready[c].get(w.name) is None for c in short_all):
                no_date = [c for c in short_all if ready[c].get(w.name) is None]
                reasons.append(f"工單 {w.name} 缺料 {', '.join(no_date)}，且無承諾到貨日，無法估算開工日")
                affected.append(AffectedOrder(
                    work_order=w.name, sales_order=w.sales_order, customer_date=so.delivery_date if so else None,
                    planned_start=planned, projected_start=None, projected_finish=None,
                    projected_ship=None, delay_workdays=None, short_items=short_all))
                continue
            start = max([planned] + [ready[c][w.name] for c in short_all])
            finish = cal.add_workdays(start, prod["production_days"])
            ship = cal.add_workdays(finish, prod["test_pack_ship_days"])
            delay = cal.workdays_between(so.delivery_date, ship) if so and so.delivery_date else 0
            affected.append(AffectedOrder(
                work_order=w.name, sales_order=w.sales_order, customer_date=so.delivery_date if so else None,
                planned_start=planned, projected_start=start, projected_finish=finish, projected_ship=ship,
                delay_workdays=delay, short_items=short_all))
            msg = (f"工單 {w.name} 缺料 {', '.join(short_changed)}：開工 {planned} → {start}，"
                   f"完工 {finish}，可出貨 {ship}")
            if so:
                msg += (f"；客戶訂單 {so.name} 交期 {so.delivery_date}，預估延誤 {delay} 個工作天"
                        if delay > 0 else f"；客戶訂單 {so.name} 交期 {so.delivery_date}，尚不致延誤")
            reasons.append(msg)
    except CalendarCoverageError as e:
        return unknown(str(e))
    except Exception as e:  # noqa: BLE001
        return unknown(f"影響分析計算失敗：{type(e).__name__}")

    if detected:
        return ImpactResult(result=ImpactKind.IMPACT_DETECTED, confidence="low" if low_conf else "high",
                            affected=affected, basis=basis, assumptions=assumptions, reasons=reasons)
    return ImpactResult(result=ImpactKind.NO_IMPACT, confidence="low" if low_conf else "high",
                        basis=basis, assumptions=assumptions,
                        reasons=["變更後各工單用料於計畫開工日前皆可投產"])
