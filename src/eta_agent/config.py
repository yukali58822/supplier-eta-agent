from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("ETA_CONFIG_DIR", ROOT / "config"))


class ConfigError(RuntimeError):
    pass


def _load(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise ConfigError(f"缺少設定檔 {path}，系統拒絕處理")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"設定檔格式錯誤：{path}")
    return data


@dataclass(frozen=True)
class Policy:
    auto_intents: tuple[str, ...]
    max_delay_days: int
    max_advance_days: int
    min_llm_confidence: float
    required_impact_confidence: str
    allow_inferred_po: bool
    shortage_auto_close: bool

    KNOWN_KEYS = frozenset({"auto_intents", "max_delay_days", "max_advance_days", "min_llm_confidence",
                            "required_impact_confidence", "allow_inferred_po", "shortage"})
    KNOWN_SHORTAGE_KEYS = frozenset({"auto_close_when_on_time"})

    @classmethod
    def load(cls) -> "Policy":
        d = _load("policy.yaml")
        unknown = set(d) - cls.KNOWN_KEYS
        unknown |= {f"shortage.{k}" for k in (d.get("shortage") or {})} - {
            f"shortage.{k}" for k in cls.KNOWN_SHORTAGE_KEYS}
        if unknown:
            raise ConfigError(f"policy.yaml 有程式不認得的設定：{sorted(unknown)}（拼錯或未實作），拒絕啟動")
        return cls(
            auto_intents=tuple(d["auto_intents"]),
            max_delay_days=int(d["max_delay_days"]),
            max_advance_days=int(d["max_advance_days"]),
            min_llm_confidence=float(d["min_llm_confidence"]),
            required_impact_confidence=str(d["required_impact_confidence"]),
            allow_inferred_po=bool(d.get("allow_inferred_po", False)),
            shortage_auto_close=bool(d.get("shortage", {}).get("auto_close_when_on_time", True)),
        )


PRODUCT_DAY_KEYS = ("production_days", "test_pack_ship_days")


@dataclass(frozen=True)
class Scheduling:
    holiday_list: str
    inspection_days: dict[str, int]
    prep_days: int
    default_product: dict[str, int]
    default_transit_days: int

    KNOWN_KEYS = frozenset({"holiday_list", "inspection_days", "prep_days", "default_product",
                            "default_transit_days"})

    @classmethod
    def load(cls) -> "Scheduling":
        d = _load("scheduling.yaml")
        if "products" in d:
            raise ConfigError("scheduling.yaml 的 products 已停用：各成品的生產天數、測試包裝出貨天數改在 "
                              "ERPNext 料件主檔設定（Item 的「生產天數」「測試包裝出貨天數」欄位）。"
                              "請刪除 products 段落，並執行 python -m eta_agent.cli erpnext setup / seed")
        unknown = set(d) - cls.KNOWN_KEYS
        if unknown:
            raise ConfigError(f"scheduling.yaml 有程式不認得的設定：{sorted(unknown)}（拼錯或未實作），拒絕啟動")
        dp = d["default_product"] or {}
        bad = set(dp) ^ set(PRODUCT_DAY_KEYS)
        if bad:
            raise ConfigError(f"scheduling.yaml 的 default_product 必須剛好有 {list(PRODUCT_DAY_KEYS)}，"
                              f"目前多出或缺少：{sorted(bad)}")
        return cls(
            holiday_list=d["holiday_list"],
            inspection_days={k: int(v) for k, v in d["inspection_days"].items()},
            prep_days=int(d["prep_days"]),
            default_product={k: int(v) for k, v in dp.items()},
            default_transit_days=int(d["default_transit_days"]),
        )

    def inspection_for(self, category: str | None) -> int:
        if category and category in self.inspection_days:
            return self.inspection_days[category]
        return self.inspection_days["default"]

    def product(self, item: Any) -> tuple[dict[str, int], list[str]]:
        days, missing = {}, []
        for k in PRODUCT_DAY_KEYS:
            v = getattr(item, k, None) if item is not None else None
            if v:
                days[k] = int(v)
            else:
                days[k] = self.default_product[k]
                missing.append(k)
        return days, missing


@dataclass(frozen=True)
class DataPolicy:
    roles: dict[str, Any]
    doctypes: dict[str, dict[str, dict[str, Any]]]
    redact_patterns: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "DataPolicy":
        d = _load("data_policy.yaml")
        if not d.get("doctypes"):
            raise ConfigError("data_policy.yaml 未定義任何欄位，拒絕處理")
        return cls(roles=d.get("roles", {}), doctypes=d["doctypes"],
                   redact_patterns=d.get("redact_patterns", {}))


@lru_cache
def policy() -> Policy:
    return Policy.load()


@lru_cache
def scheduling() -> Scheduling:
    return Scheduling.load()


@lru_cache
def data_policy() -> DataPolicy:
    return DataPolicy.load()


@dataclass(frozen=True)
class Settings:
    erp_backend: str = field(default_factory=lambda: os.environ.get("ERP_BACKEND", "fake"))
    erpnext_url: str = field(default_factory=lambda: os.environ.get("ERPNEXT_URL", "http://frontend:8080"))
    erpnext_api_key: str = field(default_factory=lambda: os.environ.get("ERPNEXT_API_KEY", ""))
    erpnext_api_secret: str = field(default_factory=lambda: os.environ.get("ERPNEXT_API_SECRET", ""))
    erpnext_company: str = field(default_factory=lambda: os.environ.get("ERPNEXT_COMPANY", "").strip())
    erpnext_warehouses: str = field(default_factory=lambda: os.environ.get("ERPNEXT_WAREHOUSES", "").strip())
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./data/agent.db"))
    extractor: str = field(default_factory=lambda: os.environ.get("EXTRACTOR", "llm"))
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", "ollama:qwen2.5:7b"))
    ollama_base_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    company_timezone: str = field(default_factory=lambda: os.environ.get("COMPANY_TZ", "Asia/Taipei"))


def load_dotenv(path: Path | None = None) -> None:
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.split(" #")[0].strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def settings() -> Settings:
    load_dotenv()
    return Settings()
