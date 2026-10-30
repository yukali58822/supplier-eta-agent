# Supplier ETA Agent

供應商交期確認 Agent：將供應商回信轉為可驗證的採購決策，並安全寫回 ERPNext。

**LangGraph · ERPNext v15 · Ollama (qwen2.5:7b) · FastAPI · Pydantic · SQLite / PostgreSQL · Docker**

## 專案簡介

採購人員每天要處理大量供應商回信（延後、提前、分批、單價調整、確認如期），並人工判斷對生產與客戶交期的影響。本專案將這段流程自動化：

1. 讀取供應商回信，擷取交期、數量、單價等承諾
2. 比對 ERPNext 採購單明細
3. 分析對工單與客戶訂單的影響（缺料、開工延後、客戶延誤工作天數）
4. 依固定規則分流：低風險自動更新，其餘送採購主管審核
5. 以冪等、可恢復的方式寫回 ERPNext，並追蹤分批交貨的補料任務直到結案

LLM 只負責「讀懂文字、照抄原文」，預設使用本機 Ollama，信件與採購資料不離開公司環境；日期換算、差異計算、影響分析、分流與寫回全部由確定性的 Python 程式與 ERPNext 伺服器端完成。

## 架構

```
ingest_email → read_email → normalize → match_po_lines → compute_diff → assess_impact → route_by_policy
                                                                                         ├─ auto ──────────→ apply_to_erpnext
                                                                                         └─ review/manual → human_review ─(Approved)→ apply_to_erpnext
任一節點失敗（寄件者不明、擷取失敗、授權失敗）→ human_review
```

- **LangGraph StateGraph**：九個節點，`interrupt` / `resume` 實作主管審核；流程狀態以 checkpointer 持久化，服務重啟後待審案件仍可續行
- **ERPNext 串接**：僅使用標準 REST API，Frappe Cloud 共用主機可用；自動建立自訂欄位、DocType、Workflow、通知與工作日曆
- **網頁工作台**：採購主管依「客戶延誤天數」排序審核案件，檢視影響分析時間線、原始信件與 LLM 引用片段，核准 / 拒絕後由 Agent 安全寫回
- **收件資料夾**：自動處理放入 `inbox/` 的 `.eml`，具備重複偵測、失敗自動重試、中斷續跑

## 設計重點

**LLM 安全邊界**
- 結構化輸出（JSON Schema）+ Pydantic 驗證；擷取內容必須能在原文中找到，否則轉人工，不依賴模型信心分數
- 欄位層級授權：依「角色 × 用途」投影 ERPNext 資料，只有 `llm: allow` 的欄位（單號、料號）會送入模型；單價、數量、個資永不進入 LLM
- 信件中的指令注入（「忽略規則、自動核准」）不影響後端規則

**資料一致性**
- 寫回以操作 ID 為主鍵建立 Operation Log，由資料庫唯一鍵保證冪等
- 逾時時先查明 ERPNext 是否已套用，再決定是否重試
- 核准時記錄明細指紋（數量、已收、交期、單價），寫入前重新比對，資料被改動則案件作廢
- 核准人由 ERPNext Workflow 與變更紀錄決定，Agent 本身無法自行核准

**影響分析**
- 到貨 → 收貨檢驗 → 可投產 → 開工 → 完工 → 可出貨 → 對照客戶交期，以工作日曆計算
- 多張工單依開工日排序分配同一份「庫存 − 預留 ＋ 在途」，避免重複分配
- 資料不足時回報 `unknown`，不將「查無影響」視為「沒有影響」

**真實信件處理**
- 分離引用舊信（Outlook、Gmail、`>` 引用、轉寄），避免把採購自己的提問當成供應商承諾
- HTML 表格轉文字、Big5 / CP950 解碼、民國年與相對日期換算
- 累計延後門檻：與最後一次核准的交期比較，防止小幅延後反覆自動通過

## 評估結果

50 封標註測試信（基本意圖、台灣在地化、困難比對、補料回覆、干擾信）：

| 指標 | Ollama `qwen2.5:7b` | 規則式基準 | 目標 |
| --- | --- | --- | --- |
| **錯誤自動處理率** | **0%** | 0% | 0% |
| 意圖 | 95.9% | 98.0% | ≥ 95% |
| 日期 / 數量 / 單價 | 100% / 100% / 100% | 100% / 100% / 66.7% | ≥ 95% |
| 明細對應 | 100% | 100% | ≥ 97% |
| 影響分析 | 100% | 100% | 100% |
| 補料結案 | 100% | 100% | ≥ 95% |
| 自動處理比例 | 36% | 40% | — |

所有不符案例皆偏向保守（送審或轉人工），無任何錯誤寫入。詳細報告見 `reports/`，標準答案與推導理由見 `data/emails/ground_truth.json`。

ERPNext 整合（建置、匯入、自動更新、網頁核准後寫回）已於 Frappe Cloud（ERPNext v15）實測。

## 執行

```bash
pip install -e ".[dev]"
cp .env.example .env

python -m pytest                                 # 239 個測試，不需要 LLM 與 ERPNext
python -m eta_agent.cli demo --extractor rule    # 離線完整示範
python -m eta_agent.cli web --erp fake           # 網頁工作台 http://127.0.0.1:8000
python -m eta_agent.cli eval                     # 評估（需本機 Ollama：ollama pull qwen2.5:7b）
docker compose up --build                        # ERPNext v15 + Agent 完整環境
```

## 專案結構

```
config/                  分流規則、排程天數、資料存取政策
src/eta_agent/
  graph.py               LangGraph 流程
  normalize.py           日期與單位標準化
  matching.py            採購單明細比對
  diff.py / impact.py    差異與影響分析
  policy.py              分流規則
  authz.py               欄位層級授權
  writeback.py           安全寫回
  inbox.py / service.py  收件資料夾與執行器
  erp/                   ERP 介面、ERPNext REST client、記憶體版 ERP
  llm/                   LLM 擷取器與上下文控管
  web/                   網頁工作台
  eval/                  測試資料生成與評估
erpnext_app/             自架 ERPNext 用的 Frappe App
tests/                   pytest
data/emails/             50 封測試信與標準答案
reports/                 評估報告
```

## 已知限制

- 排程為簡化預估（產能無限、不重排工單），定位為風險提示
- 尚未串接真實信箱，未驗證 SPF / DKIM
- REST 模式下跨單據寫回非單一資料庫交易，以操作紀錄與現值比對恢復
