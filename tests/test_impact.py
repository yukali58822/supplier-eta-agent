from datetime import date

import pytest

from eta_agent.config import scheduling
from eta_agent.erp.fake import FakeERP
from eta_agent.impact import assess_impact, gather_snapshot
from eta_agent.models import ImpactKind
from eta_agent.workcal import weekend_calendar

D = date.fromisoformat


def run(erp, overrides, items=None, assumptions=None, task_override=None):
    sched = scheduling()
    items = items or sorted({erp.po_lines[l]["item_code"] for l in overrides})
    snap = gather_snapshot(erp, items, sched)
    return assess_impact(snap, sched, overrides, task_override or {}, items, assumptions)


@pytest.fixture
def erp():
    return FakeERP()


def test_scenario_a_stock_sufficient_no_impact(erp):
    r = run(erp, {"POI-0001-1": [(2000, D("2026-11-07"))]})
    assert r.result == ImpactKind.NO_IMPACT and r.confidence == "high"


def test_scenario_b_mcu_delays_so_0042_by_3_workdays(erp):
    r = run(erp, {"POI-0002-1": [(800, D("2026-11-12"))]})
    assert r.result == ImpactKind.IMPACT_DETECTED
    wo = next(a for a in r.affected if a.work_order == "WO-0001")
    assert (wo.projected_start, wo.projected_finish, wo.projected_ship) == (D("2026-11-17"), D("2026-11-24"), D("2026-11-26"))
    assert wo.sales_order == "SO-0042" and wo.delay_workdays == 3


def test_same_two_day_delay_but_mcu_causes_shortage(erp):
    r = run(erp, {"POI-0002-1": [(800, D("2026-11-06"))]})
    assert r.result == ImpactKind.IMPACT_DETECTED
    wo = r.affected[0]
    assert wo.projected_start == D("2026-11-11") and wo.delay_workdays == 0


def test_pcb_two_day_delay_still_ready_on_planned_start(erp):
    r = run(erp, {"POI-0003-1": [(600, D("2026-11-06"))]})
    assert r.result == ImpactKind.NO_IMPACT


def test_pcb_five_day_delay_causes_shortage(erp):
    r = run(erp, {"POI-0003-1": [(600, D("2026-11-09"))]})
    assert r.result == ImpactKind.IMPACT_DETECTED
    assert r.affected[0].projected_start == D("2026-11-11")


def test_weekend_eta_rolls_to_monday(erp):
    r = run(erp, {"POI-0003-1": [(600, D("2026-11-07"))]})
    assert r.result == ImpactKind.IMPACT_DETECTED
    assert r.affected[0].projected_start == D("2026-11-11")


def test_long_holiday_pushes_ready_date(erp):
    extra = {D("2026-11-09"), D("2026-11-10"), D("2026-11-11")}
    cal = weekend_calendar("TW 2026", 2026, 2026, extra)
    erp.holiday_calendar = lambda name: cal
    r = run(erp, {"POI-0003-1": [(600, D("2026-11-06"))]})
    assert r.result == ImpactKind.IMPACT_DETECTED
    assert r.affected[0].projected_start == D("2026-11-13")


def test_calendar_out_of_range_is_unknown(erp):
    r = run(erp, {"POI-0002-1": [(800, D("2026-12-29"))]})
    assert r.result == ImpactKind.UNKNOWN


def test_multiple_items_delayed_at_once(erp):
    r = run(erp, {"POI-0002-1": [(800, D("2026-11-12"))], "POI-0003-1": [(600, D("2026-11-16"))]},
            items=["MCU-STM32", "PCB-GW200"])
    wo = next(a for a in r.affected if a.work_order == "WO-0001")
    assert set(wo.short_items) == {"MCU-STM32", "PCB-GW200"}
    assert wo.projected_start == D("2026-11-18")


def test_competing_work_orders_do_not_double_allocate(erp):
    r = run(erp, {"POI-0002-1": [(500, D("2026-11-04")), (300, None)]})
    assert r.result == ImpactKind.IMPACT_DETECTED
    wo2 = next(a for a in r.affected if a.work_order == "WO-0002")
    assert wo2.projected_start is None


def test_reserved_stock_is_excluded(erp):
    erp.reserved["CAP-100UF"] = 4000
    r = run(erp, {"POI-0001-1": [(2000, D("2026-11-20"))]})
    assert r.result == ImpactKind.IMPACT_DETECTED


def test_in_transit_from_other_po_counts_once(erp):
    r = run(erp, {"POI-0006-1": [(200, D("2026-11-10"))]})
    assert r.result == ImpactKind.NO_IMPACT


def test_cancel_balance_removes_supply(erp):
    r = run(erp, {"POI-0002-1": []})
    assert r.result == ImpactKind.IMPACT_DETECTED


def test_earlier_delivery_no_impact(erp):
    r = run(erp, {"POI-0003-1": [(600, D("2026-11-02"))]})
    assert r.result == ImpactKind.NO_IMPACT


def test_item_not_used_by_any_work_order(erp):
    r = run(erp, {"POI-0005-1": [(1000, D("2026-11-30"))]})
    assert r.result == ImpactKind.NO_IMPACT
    assert "未被任何未完工工單使用" in r.reasons[0]


def test_missing_planned_start_is_unknown(erp):
    erp.work_orders["WO-0001"]["planned_start_date"] = None
    assert run(erp, {"POI-0002-1": [(800, D("2026-11-04"))]}).result == ImpactKind.UNKNOWN


def test_missing_customer_date_is_unknown(erp):
    erp.sales_orders["SO-0042"]["delivery_date"] = None
    assert run(erp, {"POI-0002-1": [(800, D("2026-11-04"))]}).result == ImpactKind.UNKNOWN


def test_item_missing_from_master_is_unknown(erp):
    del erp.items["MCU-STM32"]
    assert run(erp, {"POI-0002-1": [(800, D("2026-11-04"))]}, items=["MCU-STM32"]).result == ImpactKind.UNKNOWN


def test_default_transit_assumption_lowers_confidence(erp):
    r = run(erp, {"POI-0004-1": [(500, D("2026-11-12"))]},
            assumptions=["供應商未設定運輸天數，採預設 3 天"])
    assert r.result == ImpactKind.NO_IMPACT and r.confidence == "low"


def test_result_records_basis_and_assumptions(erp):
    r = run(erp, {"POI-0002-1": [(800, D("2026-11-12"))]})
    assert any("資料快照時間" in b for b in r.basis)
    assert any("MCU-STM32: 庫存 50" in b for b in r.basis)
    assert any("不是 ERPNext 排程" in a for a in r.assumptions)


def test_product_days_come_from_item_master(erp):
    erp.items["GW-200"]["production_days"] = 3
    r = run(erp, {"POI-0002-1": [(800, D("2026-11-12"))]})
    wo = next(a for a in r.affected if a.work_order == "WO-0001")
    assert (wo.projected_finish, wo.projected_ship, wo.delay_workdays) == (D("2026-11-20"), D("2026-11-24"), 1)
    assert r.confidence == "high"
    assert any("生產 3 天" in b and "ERPNext 料件主檔" in b for b in r.basis)


def test_product_without_days_uses_default_and_low_confidence(erp):
    del erp.items["GW-200"]["production_days"]
    r = run(erp, {"POI-0002-1": [(800, D("2026-11-12"))]})
    assert r.result == ImpactKind.IMPACT_DETECTED and r.confidence == "low"
    assert any("未設定生產天數" in a and "預設值" in a for a in r.assumptions)
    assert r.affected[0].projected_ship == D("2026-11-26")


def test_product_with_one_day_field_missing_is_low_confidence(erp):
    erp.items["GW-200"]["test_pack_ship_days"] = 0
    r = run(erp, {"POI-0002-1": [(800, D("2026-11-12"))]})
    assert r.confidence == "low"
    assert any("未設定測試包裝出貨天數" in a for a in r.assumptions)
    assert not any("生產天數、" in a for a in r.assumptions)
