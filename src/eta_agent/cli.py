from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .demo import run_demo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eta-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    dm = sub.add_parser("demo", help="以記憶體 ERP 跑完整展示（情境 A/B、核准寫回、補料閉環）")
    pr = sub.add_parser("process", help="處理 .eml 檔")
    pr.add_argument("files", nargs="+")
    ev = sub.add_parser("eval", help="執行 50 封測試信評估並產生報告")
    ev.add_argument("--data", default="data/emails")
    ev.add_argument("--out", default="reports")
    ev.add_argument("--limit", type=int, default=None, help="只跑前 N 封（本機模型較慢時先試跑）")
    ck = sub.add_parser("check-llm", help="用一封範例信測試 LLM / Ollama 連線與結構化輸出")
    ck.add_argument("--file", help="改用這封 .eml 測試，並顯示模型的原始輸出（不會處理信件、不會改任何資料）")
    ck.add_argument("--erp", choices=["fake", "erpnext"], help="--file 時，候選採購明細從哪裡讀；預設看 .env")
    for sp in (dm, pr, ev, ck):
        sp.add_argument("--extractor", choices=["llm", "rule"], help="覆蓋 .env 的 EXTRACTOR")
        sp.add_argument("--model", help="覆蓋 .env 的 LLM_MODEL，例如 ollama:qwen2.5:7b")
    li = sub.add_parser("llm-input", help="顯示某封信「實際會送給 LLM 的完整內容」（不呼叫 LLM）")
    li.add_argument("file")
    for sp in (dm, pr, li):
        sp.add_argument("--erp", choices=["fake", "erpnext"], help="資料來源：fake（記憶體）或 erpnext；預設看 .env 的 ERP_BACKEND")
    er = sub.add_parser("erpnext", help="ERPNext 串接：setup（建立結構）/ seed（匯入測試資料）/ check / reset")
    er.add_argument("action", choices=["setup", "seed", "check", "reset"])
    wb = sub.add_parser("web", help="啟動網頁介面（採購主管與採購人員使用）")
    wb.add_argument("--host", default="127.0.0.1", help="預設只開放本機；要給同網路的人用，改成 0.0.0.0")
    wb.add_argument("--port", type=int, default=8000)
    wb.add_argument("--erp", choices=["fake", "erpnext"], help="資料來源；預設看 .env 的 ERP_BACKEND")
    wb.add_argument("--no-inbox", action="store_true", help="不要在背景自動檢查收件資料夾")
    ib = sub.add_parser("inbox", help="處理收件資料夾（inbox\\）裡的 .eml")
    ib.add_argument("--watch", action="store_true", help="持續檢查，不結束（按 Ctrl+C 停止）")
    ib.add_argument("--interval", type=float, default=10.0, help="--watch 時每幾秒檢查一次")
    ib.add_argument("--erp", choices=["fake", "erpnext"], help="資料來源；預設看 .env 的 ERP_BACKEND")
    gen = sub.add_parser("generate", help="重新產生測試信與標準答案")
    gen.add_argument("--out", default="data/emails")
    a = p.parse_args(argv)
    if getattr(a, "extractor", None):
        os.environ["EXTRACTOR"] = a.extractor
    if getattr(a, "model", None):
        os.environ["LLM_MODEL"] = a.model
        if not getattr(a, "extractor", None):
            os.environ["EXTRACTOR"] = "llm"

    if getattr(a, "erp", None):
        os.environ["ERP_BACKEND"] = a.erp
    if a.cmd == "demo":
        return run_demo(backend=getattr(a, "erp", None))
    elif a.cmd == "erpnext":
        return erpnext_cmd(a.action)
    elif a.cmd == "inbox":
        return inbox_cmd(a.watch, a.interval)
    elif a.cmd == "web":
        if a.no_inbox:
            os.environ["INBOX_WATCH"] = "0"
        import uvicorn
        from .inbox import inbox_dir
        print(f"收件資料夾：{inbox_dir()}（{'不自動檢查' if a.no_inbox else '每 10 秒自動檢查'}）")
        print(f"網頁介面：http://{'127.0.0.1' if a.host in ('0.0.0.0', '127.0.0.1') else a.host}:{a.port}  （按 Ctrl+C 停止）")
        uvicorn.run("eta_agent.web.app:app", host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "process":
        from .service import make_agent
        ag = make_agent()
        for f in a.files:
            res = ag.process_eml(Path(f).read_bytes())
            res.pop("state", None)
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif a.cmd == "eval":
        from .eval.run_eval import run
        path = run(Path(a.data), Path(a.out), limit=a.limit)
        print(f"報告：{path}")
    elif a.cmd == "check-llm":
        return check_llm(Path(a.file) if a.file else None)
    elif a.cmd == "llm-input":
        return llm_input(Path(a.file))
    elif a.cmd == "generate":
        from .eval.generate import generate
        n = generate(Path(a.out))
        print(f"已產生 {n} 封測試信到 {a.out}")
    return 0


def inbox_cmd(watch: bool, interval: float) -> int:
    from .inbox import InboxScanner
    from .service import make_agent
    holder = {}

    def agent():
        if "a" not in holder:
            holder["a"] = make_agent()
        return holder["a"]
    scanner = InboxScanner(agent, min_age_seconds=0 if not watch else 2.0)
    scanner.ensure_folders()
    labels = {"done": "已自動處理", "waiting_review": "已送主管審核", "manual": "轉人工處理",
              "duplicate": "重複信件，略過", "failed": "處理失敗"}
    print(f"收件資料夾：{scanner.folder}")
    while True:
        for r in scanner.scan_once():
            print(f"  {r.file}：{labels.get(r.status, r.status)}" + (f"（{r.error}）" if r.error else ""))
        if scanner.last_error:
            print(f"  ❌ {scanner.last_error}")
        if not watch:
            left = len(scanner.waiting_files())
            print(f"完成。資料夾裡還有 {left} 封未處理。" if left else "完成。")
            return 0
        time.sleep(interval)


def erpnext_cmd(action: str) -> int:
    from .config import settings
    from .erp import setup_erpnext as se
    from .erp.frappe_rest import FrappeREST
    s = settings()
    rest = FrappeREST(s.erpnext_url, s.erpnext_api_key, s.erpnext_api_secret)
    if action == "setup":
        se.setup(rest)
    elif action == "seed":
        if not s.erpnext_company:
            print("請在 .env 設定 ERPNEXT_COMPANY（ERPNext 裡的公司名稱）")
            return 1
        se.seed(rest, s.erpnext_company)
    elif action == "reset":
        se.reset(rest)
    elif action == "check":
        from .erp import make_erp
        return 0 if se.check(make_erp("erpnext")) else 1
    return 0


def check_llm_file(ex, file: Path) -> int:
    from .email_ingest import parse_eml
    from .erp import make_erp
    from .llm.context import build_llm_context
    em = parse_eml(file.read_bytes())
    erp = make_erp()
    sup = erp.find_supplier_by_email(em.sender)
    if sup is None:
        print(f"寄件者 {em.sender} 不是已登記的供應商信箱：正式處理時不會送給 LLM")
        return 1
    _, _, body, ctx, _ = build_llm_context(erp, sup.name, em.body)
    t0 = time.time()
    out = ex.extract(em.subject, body, ctx)
    print(f"耗時 {time.time() - t0:.1f}s")
    print("模型原始輸出：")
    print(json.dumps(ex.last_raw, ensure_ascii=False, indent=2, default=str) if ex.last_raw is not None else "（沒有收到）")
    if out is None:
        print(f"❌ 擷取失敗：{ex.last_error}")
        return 1
    print("整理、驗證後的結果：")
    print(json.dumps(out.model_dump(), ensure_ascii=False, indent=2))
    print("✅ 格式正確（之後還要通過原文比對與分流規則）")
    return 0


def llm_input(path: Path) -> int:
    from .email_ingest import parse_eml
    from .erp import make_erp
    from .llm.context import build_llm_context
    from .llm.extractor import SYSTEM_PROMPT, build_user_prompt
    em = parse_eml(path.read_bytes())
    erp = make_erp()
    sup = erp.find_supplier_by_email(em.sender)
    print(f"寄件者：{em.sender}")
    if sup is None:
        print("→ 不是已登記的供應商信箱：停止處理，什麼都不會送給 LLM")
        return 0
    lines, tasks, body, ctx, dropped = build_llm_context(erp, sup.name, em.body)
    print(f"供應商：{sup.name}（由寄件信箱比對 ERPNext 決定）")
    print(f"候選資料：這家供應商的未結採購明細 {len(lines)} 筆、未結補料任務 {len(tasks)} 筆")
    for d in dropped:
        print(f"  ⚠ 已丟棄：{d}")
    print("\n" + "=" * 30 + " 系統提示（固定內容）" + "=" * 30)
    print(f"（{len(SYSTEM_PROMPT)} 字的規則與範例，不含任何 ERP 資料）")
    print("=" * 30 + " 實際送給 LLM 的使用者訊息 " + "=" * 30)
    print(build_user_prompt(em.subject, body, ctx))
    return 0


def check_llm(file: Path | None = None) -> int:
    from .config import settings
    from .llm.extractor import LLMExtractor
    s = settings()
    print(f"模型：{s.llm_model}" + (f"（Ollama：{s.ollama_base_url}）" if s.llm_model.startswith("ollama:") else ""))
    ex = LLMExtractor(s.llm_model, base_url=s.ollama_base_url)
    if file:
        return check_llm_file(ex, file)
    body = "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨，請見諒。"
    cands = {"Purchase Order Item": [{"parent": "PUR-ORD-2026-00002", "item_code": "MCU-STM32",
                                      "supplier_part_no": "FM-32F4"}]}
    t0 = time.time()
    out = ex.extract("RE: PUR-ORD-2026-00002 交期", body, cands)
    dt = time.time() - t0
    if out is None:
        print(f"❌ 擷取失敗（{dt:.1f}s）：{ex.last_error}")
        if s.llm_model.startswith("ollama:"):
            print("請確認：1) Ollama 已啟動  2) 已執行 ollama pull " + s.llm_model.split(":", 1)[1]
                  + f"  3) {s.ollama_base_url} 可連線")
        return 1
    print(json.dumps(out.model_dump(), ensure_ascii=False, indent=2))
    ok = out.intent == "date_change" and out.lines and out.lines[0].date_kind == "ship" \
        and (out.lines[0].date_text or "").replace(" ", "") == "115/11/09"
    print(f"{'✅' if ok else '⚠️'} 耗時 {dt:.1f}s；預期 intent=date_change、date_text=115/11/09、date_kind=ship")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
