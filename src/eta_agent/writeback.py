from __future__ import annotations

import hashlib
import json
from typing import Any

from .erp.base import ERPError
from .models import CaseStatus


def operation_id(message_id: str, case_id: str | None, actions: list[dict[str, Any]]) -> str:
    raw = json.dumps([message_id, case_id, actions], sort_keys=True, default=str)
    return "op-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def apply_actions(erp: Any, *, message_id: str, mode: str, actions: list[dict[str, Any]],
                  case_id: str | None = None, max_attempts: int = 2) -> dict[str, Any]:
    if not actions:
        return {"status": "nothing_to_apply"}

    if mode == "approved":
        case = erp.get_case(case_id) if case_id else None
        if case is None:
            return {"status": "rejected", "reason": "找不到審批案件"}
        if case.status != CaseStatus.APPROVED:
            return {"status": "rejected", "reason": f"案件狀態為 {case.status.value}，不得修改正式資料"}
        if not case.approver or "Purchase Manager" not in erp.user_roles(case.approver):
            return {"status": "rejected", "reason": "核准人無採購主管權限"}

    op_id = operation_id(message_id, case_id, actions)
    payload = {"op_id": op_id, "message_id": message_id, "mode": mode, "case_id": case_id, "actions": actions}
    last_err = ""
    for attempt in range(max_attempts):
        try:
            res = erp.apply_change(payload)
            res.setdefault("op_id", op_id)
            res["attempts"] = attempt + 1
            return res
        except ERPError as e:
            last_err = str(e)
            try:
                st = erp.operation_status(op_id)
            except ERPError:
                st = None
                last_err += "；無法查詢操作狀態"
            if st and st.get("status") == "Done":
                return {"status": "applied", "op_id": op_id, "recovered": True, "attempts": attempt + 1,
                        "changes": st.get("changes", [])}
    return {"status": "failed", "op_id": op_id, "reason": f"寫回失敗，ERPNext 未套用：{last_err}",
            "retryable": True}
