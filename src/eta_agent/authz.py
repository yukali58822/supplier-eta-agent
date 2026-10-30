from __future__ import annotations

import re
from typing import Any, Iterable

from .config import DataPolicy, data_policy


class AccessDenied(PermissionError):
    pass


def _allowed(rule_read: Iterable[str], role: str, purpose: str) -> bool:
    for r in rule_read:
        rr, _, pp = r.partition(":")
        if rr == role and (pp in ("*", purpose)):
            return True
    return False


def authorize_and_project(doctype: str, record: dict[str, Any], *, role: str, purpose: str,
                          fields: Iterable[str] | None = None, policy: DataPolicy | None = None,
                          strict: bool = True) -> dict[str, Any]:
    pol = policy or data_policy()
    role_def = pol.roles.get(role)
    if role_def is None:
        raise AccessDenied(f"未定義的角色：{role}")
    if purpose not in role_def.get("purposes", []):
        raise AccessDenied(f"角色 {role} 無用途 {purpose} 的權限")
    rules = pol.doctypes.get(doctype)
    if rules is None:
        raise AccessDenied(f"未定義的 DocType：{doctype}")
    wanted = list(fields) if fields is not None else list(record.keys())
    out: dict[str, Any] = {}
    for f in wanted:
        rule = rules.get(f)
        if rule is None:
            if strict and fields is not None:
                raise AccessDenied(f"欄位未列入白名單：{doctype}.{f}")
            continue
        if not _allowed(rule.get("read", []), role, purpose):
            if fields is not None:
                raise AccessDenied(f"{role}:{purpose} 無權讀取 {doctype}.{f}")
            continue
        if f in record:
            out[f] = record[f]
    return out


def writable_fields(doctype: str, policy: DataPolicy | None = None) -> set[str]:
    pol = policy or data_policy()
    return {f for f, r in pol.doctypes.get(doctype, {}).items() if r.get("write")}


def assert_writable(doctype: str, fields: Iterable[str], policy: DataPolicy | None = None) -> None:
    allowed = writable_fields(doctype, policy)
    bad = [f for f in fields if f not in allowed]
    if bad:
        raise AccessDenied(f"寫回欄位超出允許清單：{doctype}.{bad}")


def redact_text(text: str, policy: DataPolicy | None = None) -> str:
    pol = policy or data_policy()
    for name, pat in pol.redact_patterns.items():
        text = re.sub(pat, f"[已遮蔽:{name}]", text)
    return text


def llm_context_guard(email_text: str, context: dict[str, list[dict[str, Any]]],
                      policy: DataPolicy | None = None) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    pol = policy or data_policy()
    safe: dict[str, list[dict[str, Any]]] = {}
    for doctype, rows in context.items():
        rules = pol.doctypes.get(doctype)
        if rules is None:
            raise AccessDenied(f"未定義的 DocType 不得進入 LLM：{doctype}")
        safe[doctype] = [{k: v for k, v in row.items() if rules.get(k, {}).get("llm") == "allow"}
                         for row in rows]
    return redact_text(email_text, pol), safe


SECRET_KEYS = re.compile(r"(rate|amount|price|discount|token|secret|password|api_key|email|mobile|phone)", re.I)


def redact_for_log(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEYS.search(str(k)) else redact_for_log(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_for_log(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
