"""Provider Ollama contro un server finto (http.server, nessuna rete esterna)."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.agents import PlannerAgent, ReviewerAgent  # noqa: E402
from mxfp4agent.agents.schemas import PLAN_SCHEMA, REVIEW_SCHEMA  # noqa: E402
from mxfp4agent.config import Config, load_profiles  # noqa: E402
from mxfp4agent.llm import build_provider  # noqa: E402
from mxfp4agent.llm.base import LLMError  # noqa: E402
from mxfp4agent.llm.mock_provider import MockProvider  # noqa: E402
from mxfp4agent.llm.ollama_provider import (OllamaProvider,  # noqa: E402
                                            context_length_from_show, strip_think)
from mxfp4agent.utils import Log, parse_json_response  # noqa: E402


# --------------------------------------------------------------- server finto
class FakeOllama:
    """Server Ollama minimale. `script` decide la risposta di /api/chat."""

    def __init__(self, chunks=None, context_length=32768, models=("qwen2.5-coder:14b",),
                 delay=0.0, fail_first=0, fail_code=503):
        self.chunks = chunks if chunks is not None else ["ciao ", "mondo"]
        self.context_length = context_length
        self.models = list(models)
        self.delay = delay
        self.fail_first = fail_first
        self.fail_code = fail_code
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silenzio
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append((self.path, {}))
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.9.9-test"})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [
                        {"name": m, "digest": f"sha-{m}"} for m in outer.models]})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append((self.path, body))
                if self.path == "/api/show":
                    return self._json(200, {
                        "details": {"parameter_size": "14.8B", "quantization_level": "Q4_K_M",
                                    "family": "qwen2"},
                        "model_info": {"general.architecture": "qwen2",
                                       "qwen2.context_length": outer.context_length}})
                if self.path != "/api/chat":
                    return self._json(404, {"error": "not found"})
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    return self._json(outer.fail_code, {"error": "server busy"})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for c in outer.chunks:
                    if outer.delay:
                        time.sleep(outer.delay)
                    line = {"message": {"role": "assistant", "content": c}, "done": False}
                    self.wfile.write((json.dumps(line) + "\n").encode())
                    self.wfile.flush()
                final = {"message": {"role": "assistant", "content": ""}, "done": True,
                         "done_reason": getattr(outer, "done_reason", "stop"),
                         "prompt_eval_count": 111, "eval_count": 22}
                self.wfile.write((json.dumps(final) + "\n").encode())

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # il client che chiude per timeout è un caso testato, non un errore

        self.server = Quiet(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def chat_payloads(self):
        return [b for p, b in self.requests if p == "/api/chat"]


def _prov(srv, **kw):
    OllamaProvider._info_cache.clear()
    kw.setdefault("max_retries", 0)
    return OllamaProvider(model=kw.pop("model", "qwen2.5-coder:14b"), host=srv.url, **kw)


# ------------------------------------------------------------------ streaming
def test_stream_assembles_text_and_passes_reproducibility_options():
    srv = FakeOllama(chunks=["ciao ", "mon", "do"])
    try:
        p = _prov(srv, seed=7, keep_alive="10m", temperature=0.1, num_ctx=8192,
                  max_tokens=512)
        r = p.chat("sys", [{"role": "user", "content": "u"}], format={"type": "object"})
        assert r.text == "ciao mondo"
        assert (r.input_tokens, r.output_tokens) == (111, 22)
        body = srv.chat_payloads()[-1]
        assert body["stream"] is True
        assert body["keep_alive"] == "10m"
        assert body["format"] == {"type": "object"}
        assert body["options"] == {"temperature": 0.1, "num_ctx": 8192,
                                   "num_predict": 512, "seed": 7}
        assert body["messages"][0] == {"role": "system", "content": "sys"}
    finally:
        srv.close()


def test_think_blocks_are_removed_from_the_stream():
    srv = FakeOllama(chunks=["<think>ragiono", " a lungo</think>\n", "RISPOSTA"])
    try:
        assert _prov(srv).complete("s", "u") == "RISPOSTA"
    finally:
        srv.close()


def test_strip_think_variants():
    assert strip_think("<think>x</think>ok") == "ok"
    assert strip_think("<THINK>a\nb</THINK>\n\nok") == "ok"
    assert strip_think("prima <think>mai chiuso") == "prima"
    assert strip_think("solo chiusura</think> ok") == "ok"
    assert strip_think("niente tag") == "niente tag"


def test_only_thinking_is_an_actionable_error():
    srv = FakeOllama(chunks=["<think>pensa pensa"])
    try:
        _prov(srv).complete("s", "u")
    except LLMError as e:
        assert "--max-tokens" in str(e)
    else:
        raise AssertionError("risposta vuota non segnalata")
    finally:
        srv.close()


def test_length_stop_is_reported_as_truncation():
    srv = FakeOllama(chunks=["mezzo file"])
    srv.done_reason = "length"
    try:
        r = _prov(srv).chat("s", [{"role": "user", "content": "u"}])
        assert r.stop_reason == "max_tokens"
    finally:
        srv.close()


def test_chunk_timeout_is_transient_and_retried():
    srv = FakeOllama(chunks=["a", "b"], delay=1.5)
    try:
        p = _prov(srv, chunk_timeout=0.5)
        try:
            p.complete("s", "u")
        except LLMError as e:
            assert p.is_transient(e), str(e)
        else:
            raise AssertionError("timeout per chunk non rilevato")
    finally:
        srv.close()


def test_slow_but_steady_generation_does_not_time_out():
    # 4 chunk da 0.3 s = 1.2 s totali > chunk_timeout, ma nessuna pausa lo supera
    srv = FakeOllama(chunks=["a", "b", "c", "d"], delay=0.3)
    try:
        assert _prov(srv, chunk_timeout=1).complete("s", "u") == "abcd"
    finally:
        srv.close()


def test_transient_http_errors_are_retried_and_fatal_ones_are_not():
    srv = FakeOllama(fail_first=2, fail_code=503)
    try:
        p = _prov(srv, max_retries=3, retry_backoff=0.0)
        notes = []
        p.on_retry = notes.append
        assert p.complete("s", "u") == "ciao mondo"
        assert len(notes) == 2 and p.stats["retries"] == 2
    finally:
        srv.close()
    srv = FakeOllama(fail_first=5, fail_code=400)
    try:
        p = _prov(srv, max_retries=3, retry_backoff=0.0)
        try:
            p.complete("s", "u")
        except LLMError as e:
            assert "400" in str(e)
        else:
            raise AssertionError("un 400 doveva propagarsi")
        assert len(srv.chat_payloads()) == 1, "un 400 non va ritentato"
    finally:
        srv.close()


# ------------------------------------------------------------------ metadati
def test_context_length_is_read_and_num_ctx_clamped():
    srv = FakeOllama(context_length=4096)
    try:
        p = _prov(srv, num_ctx=32768)
        notes = p.prepare()
        assert p.num_ctx == 4096 and notes and "4096" in notes[0]
        m = p.meta()
        assert m["digest"] == "sha-qwen2.5-coder:14b"
        assert m["server_version"] == "0.9.9-test"
        assert m["model_context_length"] == 4096
        assert m["num_ctx_requested"] == 32768
        assert m["options"]["num_ctx"] == 4096
    finally:
        srv.close()


def test_context_length_from_show_ignores_unrelated_keys():
    assert context_length_from_show({"model_info": {"llama.context_length": 131072,
                                                    "llama.embedding_length": 4096}}) == 131072
    assert context_length_from_show({}) is None


def test_health_check_requires_the_exact_tag():
    srv = FakeOllama(models=["qwen2.5-coder:14b", "llama3:latest"])
    try:
        assert _prov(srv, model="qwen2.5-coder:14b").health_check()[0]
        assert _prov(srv, model="llama3").health_check()[0]          # :latest implicito
        ok, detail = _prov(srv, model="qwen2.5-coder:32b").health_check()
        assert not ok and "ollama pull qwen2.5-coder:32b" in detail
    finally:
        srv.close()


def test_unreachable_server_is_reported():
    p = OllamaProvider(model="x", host="http://127.0.0.1:9", max_retries=0)
    ok, detail = p.health_check()
    assert not ok and "non raggiungibile" in detail


# ------------------------------------------------------- output strutturati
class _Capture(MockProvider):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.overrides = []

    def _chat(self, system, messages, **ov):
        self.overrides.append(ov)
        return super()._chat(system, messages, **ov)


def test_planner_and_reviewer_request_a_json_schema():
    prov = _Capture()
    log = Log(verbose=False, color=False)
    plan = PlannerAgent(prov, log).run("dot product").payload
    assert prov.overrides[-1]["format"] is PLAN_SCHEMA
    ReviewerAgent(prov, log).review(plan, [])
    assert prov.overrides[-1]["format"] is REVIEW_SCHEMA


def test_parse_json_response_is_tolerant():
    assert parse_json_response('{"a": 1}') == {"a": 1}
    assert parse_json_response('  \n{"a": 1}\n') == {"a": 1}
    assert parse_json_response('ecco:\n```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json_response('prosa {"a": 3} altra prosa') == {"a": 3}
    try:
        parse_json_response("niente json qui")
    except ValueError:
        pass
    else:
        raise AssertionError


def test_plan_schema_requires_the_contract_fields():
    for k in ("module_name", "ports", "test_plan", "coder_prompt"):
        assert k in PLAN_SCHEMA["required"]
    assert REVIEW_SCHEMA["properties"]["verdict"]["enum"] == ["approved", "changes_required"]


# ------------------------------------------------------------------ profili
def test_agent_profiles_precedence(tmp_path):
    f = tmp_path / "profiles.json"
    f.write_text(json.dumps({"planner": {"model": "qwen2.5-coder:32b", "num_ctx": 16384},
                             "coder": {"temperature": 0.05}}), encoding="utf-8")
    cfg = Config(model="qwen2.5-coder:14b", profiles=load_profiles(f),
                 coder_model="override:7b", seed=42)
    p = cfg.agent_settings("planner")
    assert (p["model"], p["num_ctx"], p["seed"]) == ("qwen2.5-coder:32b", 16384, 42)
    c = cfg.agent_settings("coder")
    assert c["model"] == "override:7b" and c["temperature"] == 0.05   # CLI > profilo
    assert cfg.agent_settings("tester")["model"] == "qwen2.5-coder:14b"


def test_bad_profiles_are_rejected(tmp_path):
    f = tmp_path / "p.json"
    for bad in ({"plannr": {"model": "x"}}, {"planner": {"modle": "x"}}):
        f.write_text(json.dumps(bad), encoding="utf-8")
        try:
            load_profiles(f)
        except ValueError:
            continue
        raise AssertionError(f"profilo non valido accettato: {bad}")


def test_build_provider_forwards_ollama_options():
    p = build_provider("ollama", seed=3, keep_alive=-1, chunk_timeout=42)
    assert (p.seed, p.keep_alive, p.chunk_timeout) == (3, -1, 42)
