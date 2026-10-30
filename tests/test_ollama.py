import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("langchain_ollama")

from eta_agent.llm.extractor import LLMExtractor  # noqa: E402

ANSWER = {"intent": "date_change", "confidence": 0.9, "notes": None, "lines": [
    {"po_number": "PUR-ORD-2026-00002", "item_code": "FM-32F4", "qty_text": "800 pcs", "unit_price_text": None,
     "date_text": "115/11/09", "date_kind": "ship", "evidence": "只能於 115/11/09 出貨"}]}


def _server(reply_content):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            msg = {"model": body["model"], "created_at": "2026-10-26T00:00:00Z",
                   "message": {"role": "assistant", "content": reply_content}, "done": True,
                   "done_reason": "stop", "total_duration": 1, "eval_count": 1, "prompt_eval_count": 1}
            data = (json.dumps(msg) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_ollama_structured_extraction():
    srv, seen = _server(json.dumps(ANSWER, ensure_ascii=False))
    try:
        ex = LLMExtractor("ollama:qwen2.5:7b", base_url=f"http://127.0.0.1:{srv.server_port}")
        out = ex.extract("RE", "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨", {})
    finally:
        srv.shutdown()
    assert out is not None and out.intent == "date_change" and out.lines[0].date_kind == "ship"
    path, req = seen[0]
    assert path == "/api/chat" and req["model"] == "qwen2.5:7b"
    fmt = req["format"]
    assert isinstance(fmt, dict) and "lines" in fmt["required"]
    line = fmt["$defs"]["ExtractedLine"]
    assert set(line["required"]) == set(line["properties"]) and next(iter(line["properties"])) == "evidence"
    assert req["options"]["temperature"] == 0


def test_ollama_bad_output_returns_none_after_retry():
    srv, seen = _server("我覺得這封信是在說延期")
    try:
        ex = LLMExtractor("ollama:qwen2.5:7b", base_url=f"http://127.0.0.1:{srv.server_port}")
        assert ex.extract("RE", "x", {}) is None
    finally:
        srv.shutdown()
    assert len(seen) == 2
    assert ex.last_error


def test_ollama_unreachable_returns_none():
    ex = LLMExtractor("ollama:qwen2.5:7b", base_url="http://127.0.0.1:9")
    assert ex.extract("RE", "x", {}) is None and ex.last_error


def test_empty_lines_triggers_one_retry():
    empty = dict(ANSWER, lines=[])
    srv, seen = _server(json.dumps(empty))
    try:
        ex = LLMExtractor("ollama:qwen2.5:7b", base_url=f"http://127.0.0.1:{srv.server_port}")
        out = ex.extract("RE", "x", {})
    finally:
        srv.shutdown()
    assert len(seen) == 2 and out is not None and out.lines == []


def test_numeric_price_from_model_is_accepted_as_text():
    bad = dict(ANSWER, intent="price_change", lines=[dict(ANSWER["lines"][0], unit_price_text=0.06,
                                                          date_text=None, date_kind="unknown")])
    srv, seen = _server(json.dumps(bad))
    try:
        ex = LLMExtractor("ollama:qwen2.5:7b", base_url=f"http://127.0.0.1:{srv.server_port}")
        out = ex.extract("RE", "x", {})
    finally:
        srv.shutdown()
    assert out is not None and out.lines[0].unit_price_text == "0.06"
    assert "格式修正" in (out.notes or "") and len(seen) == 1


def test_out_of_range_confidence_becomes_zero_so_it_goes_to_review():
    srv, seen = _server(json.dumps(dict(ANSWER, confidence=95)))
    try:
        out = LLMExtractor("ollama:qwen2.5:7b", base_url=f"http://127.0.0.1:{srv.server_port}").extract("RE", "x", {})
    finally:
        srv.shutdown()
    assert out.confidence == 0.0


def test_failure_reason_is_specific():
    srv, seen = _server(json.dumps(dict(ANSWER, lines=[dict(ANSWER["lines"][0], date_kind="tomorrow")])))
    try:
        ex = LLMExtractor("ollama:qwen2.5:7b", base_url=f"http://127.0.0.1:{srv.server_port}")
        assert ex.extract("RE", "x", {}) is None
    finally:
        srv.shutdown()
    assert "lines.0" in ex.last_error and "date_kind" in ex.last_error
    ex2 = LLMExtractor("ollama:qwen2.5:7b", base_url="http://127.0.0.1:9")
    ex2.extract("RE", "x", {})
    assert "連不上 Ollama" in ex2.last_error
