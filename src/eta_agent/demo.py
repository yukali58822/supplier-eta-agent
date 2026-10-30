from __future__ import annotations

from email.message import EmailMessage

from langgraph.checkpoint.memory import InMemorySaver

import json
import time

from .erp.fake import FakeERP
from .service import make_agent
from .store import Store


def make_eml(sender: str, subject: str, body: str, message_id: str,
             date: str = "Mon, 26 Oct 2026 10:00:00 +0800") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = sender, "purchasing@example.com", subject, date
    m["Message-ID"] = f"<{message_id}>"
    m.set_content(body)
    return bytes(m)


def _show(title: str, res: dict) -> None:
    st = res.get("state", {})
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    print(f"狀態：{res['status']}　分流：{res.get('route')}　案件：{res.get('case_id')}")
    imp = st.get("impact")
    if imp:
        print(f"影響分析：{imp['result']}（信心 {imp['confidence']}）")
        for r in imp["reasons"]:
            print(f"  • {r}")
    print("判斷理由：")
    for r in (st.get("decision") or {}).get("reasons", []):
        print(f"  • {r}")
    if res.get("result", {}).get("changes"):
        print("寫回異動：")
        for c in res["result"]["changes"]:
            print(f"  • {c}")


def _approve_and_resume(erp, agent, res: dict, title: str) -> None:
    if res["status"] != "waiting_review":
        print(f"\n（{title}：上一步沒有進入待審核，狀態為 {res['status']}，略過核准）")
        return
    erp.workflow_action(res["case_id"], "Approve", "manager@example.com")
    approver = erp.get_case(res["case_id"]).approver
    print(f"   ERPNext 記錄的核准人：{approver}")
    try:
        _show(title, agent.resume(res["thread_id"], approver))
    except Exception as e:  # noqa: BLE001
        print(f"   ❌ 無法恢復流程：{e}")


def _localizer(backend: str):
    if backend != "erpnext":
        return lambda t: t
    from .erp.setup_erpnext import MAP_PATH
    if not MAP_PATH.exists():
        raise SystemExit("找不到 data/erpnext_map.json，請先執行：python -m eta_agent.cli erpnext seed")
    pos = json.loads(MAP_PATH.read_text(encoding="utf-8"))["purchase_orders"]

    def f(text: str) -> str:
        for seed_name, real in sorted(pos.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(seed_name, real)
        return text
    return f


def run_demo(extractor=None, backend: str | None = None) -> int:
    from .config import settings
    from .llm.extractor import make_extractor
    s = settings()
    if extractor is None:
        extractor = make_extractor(s.extractor, s.llm_model, base_url=s.ollama_base_url)
    print(f"擷取器：{extractor.name}" + ("（規則式離線基準；要用 Ollama 請在 .env 設 EXTRACTOR=llm 或加 --model）"
                                       if extractor.name.startswith("rule") else ""))
    if s.extractor == "llm":
        probe = extractor.extract("ping", "PUR-ORD-2026-00001 AC-C100 確認如期到貨。", {})
        if probe is None:
            print(f"❌ 無法使用 LLM：{getattr(extractor, 'last_error', '')}")
            print("請確認 Ollama 已啟動且已執行 ollama pull " + s.llm_model.split(":", 1)[-1]
                  + "；或改用離線基準：python -m eta_agent.cli demo --extractor rule")
            return 1
    backend = backend or s.erp_backend
    if backend == "erpnext":
        from .erp import make_erp
        erp = make_erp("erpnext")
        print(f"ERP：ERPNext {s.erpnext_url}（所有資料即時從 ERPNext 讀取、寫回 ERPNext）")
    else:
        erp = FakeERP()
        print("ERP：記憶體版（測試資料）；要連 ERPNext 請加 --erp erpnext")
    loc = _localizer(backend)
    run = time.strftime("%Y%m%d%H%M%S")

    def mk(sender, subject, body, mid, **kw):
        return make_eml(sender, loc(subject), loc(body), f"{mid}-{run}", **kw)
    agent = make_agent(erp, Store("sqlite://"), extractor, InMemorySaver())

    a = mk("sales@acme-passive.com.tw", "RE: PUR-ORD-2026-00001 交期",
           "您好，PUR-ORD-2026-00001 料號 AC-C100 2,000 pcs 因產線調整，到貨日延後至 11/7。", "demo-a")
    _show("情境 A：電容延後 2 天，庫存足夠 → 自動更新", agent.process_eml(a))

    b = mk("delivery@formosa-mcu.com.tw", "RE: PUR-ORD-2026-00002",
           "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨，請見諒。", "demo-b")
    res = agent.process_eml(b)
    _show("情境 B：MCU 只承諾 11/9 出貨 → 影響客戶訂單 → 送審", res)

    print("\n→ 採購主管在 ERPNext 核准，然後呼叫 /resume")
    _approve_and_resume(erp, agent, res, "情境 B：核准後安全寫回")

    c = mk("sales@acme-passive.com.tw", "RE: PUR-ORD-2026-00005 外殼",
           "PUR-ORD-2026-00005 AC-BOX1 共 1,000 pcs，11/16 先交 600 pcs 到貨，剩餘 400 pcs 待定。", "demo-c1")
    res = agent.process_eml(c)
    _show("補料 1/2：供應商無法一次交齊 → 送審", res)
    _approve_and_resume(erp, agent, res, "補料 1/2：核准 → 建立補料任務並指派採購")
    print(f"未結補料任務：{[t.name + ' qty=' + format(t.qty, 'g') + ' 需求日=' + str(t.required_date) for t in erp.open_shortage_tasks()]}")

    d = mk("sales@acme-passive.com.tw", "RE: PUR-ORD-2026-00005 剩餘數量",
           "PUR-ORD-2026-00005 AC-BOX1 剩餘 400 pcs 確認將於 11/13 到貨。", "demo-c2",
           date="Thu, 29 Oct 2026 09:00:00 +0800")
    _show("補料 2/2：第二封信自動對應補料任務並結案", agent.process_eml(d))
    print(f"未結補料任務：{[t.name for t in erp.open_shortage_tasks()]}")

    print("\n重複匯入情境 A：", agent.process_eml(a)["status"])
    if backend == "erpnext":
        print("\n到 ERPNext 查看：採購單交期已更新、Supplier ETA Case（審批案件）、Shortage Task（補料任務）、"
              "ETA Operation Log（寫回紀錄）。重跑前可執行 python -m eta_agent.cli erpnext reset")
    return 0


if __name__ == "__main__":
    run_demo()
