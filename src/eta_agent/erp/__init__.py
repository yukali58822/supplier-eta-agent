from .base import ERPClient, ERPError, line_fingerprint


def make_erp(backend: str | None = None) -> "ERPClient":
    from ..config import settings
    s = settings()
    if (backend or s.erp_backend) == "erpnext":
        from .erpnext import ERPNextClient
        return ERPNextClient(s.erpnext_url, s.erpnext_api_key, s.erpnext_api_secret,
                             warehouses=[w.strip() for w in s.erpnext_warehouses.split(",") if w.strip()])
    from .fake import FakeERP
    return FakeERP()


__all__ = ["ERPClient", "ERPError", "line_fingerprint", "make_erp"]
