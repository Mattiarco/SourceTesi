"""Backend Ollama (modelli locali, nessuna API key, solo standard library).

Scelte che contano per una tesi:

* **riproducibilità** — `seed` e `temperature` passano in `options`; modello,
  digest (da `/api/tags`), versione del server e opzioni effettive finiscono in
  :meth:`meta`, che il workflow copia in `report.json`;
* **streaming** — `/api/chat` con `stream=true`: il timeout di socket vale per
  il singolo chunk, quindi una generazione lunga non scade finché il modello
  produce token; un tetto separato limita la durata totale;
* **output strutturati** — `format` accetta uno JSON schema: il campionamento
  è vincolato alla grammatica, niente più JSON estratto dal testo;
* **finestra di contesto** — la lunghezza reale si legge da `/api/show`; un
  `num_ctx` maggiore viene ridotto (Ollama non lo rifiuta, ma oltre la
  lunghezza di addestramento la qualità crolla).
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, Message

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Rimuove i blocchi di ragionamento `<think>…</think>` (qwen3, deepseek-r1…).

    Un `<think>` mai chiuso (risposta troncata durante il ragionamento) lascia
    una risposta vuota: è corretto, il contenuto utile non è mai arrivato.
    """
    text = _THINK_RE.sub("", text)
    low = text.lower()
    if "<think>" in low:
        text = text[:low.index("<think>")]
    # alcuni template emettono solo il tag di chiusura
    if "</think>" in text.lower():
        text = text[text.lower().rindex("</think>") + len("</think>"):]
    return text.strip()


def normalize_model_name(name: str) -> str:
    """`qwen2.5-coder` e `qwen2.5-coder:latest` sono lo stesso modello."""
    return name if ":" in name else f"{name}:latest"


class OllamaProvider(LLMProvider):
    name = "ollama"

    #: cache per (host, modello): /api/show è costoso, e ogni agente ha il suo provider
    _info_cache: dict[tuple[str, str], dict] = {}

    def __init__(self, model: str = "qwen2.5-coder:14b",
                 host: str = "http://localhost:11434", num_ctx: int = 32768,
                 seed: int | None = None, keep_alive: str | int | None = "30m",
                 chunk_timeout: int = 600, **kw: Any) -> None:
        super().__init__(model, **kw)
        self.host = host.rstrip("/")
        if not self.host.startswith(("http://", "https://")):
            self.host = "http://" + self.host          # OLLAMA_HOST=127.0.0.1:11434
        self.num_ctx = num_ctx
        self.seed = seed
        self.keep_alive = keep_alive
        #: attesa massima fra due chunk consecutivi. Il primo chunk include il
        #: caricamento del modello e la valutazione del prompt: va largo.
        self.chunk_timeout = chunk_timeout
        self.requested_num_ctx = num_ctx

    # ------------------------------------------------------------------ http
    def _request(self, path: str, payload: dict | None = None,
                 timeout: float | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"{self.host}{path}", data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET")
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.chunk_timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:400]
            if e.code == 404 and "not found" in body.lower():
                raise LLMError(f"Ollama HTTP 404: modello '{self.model}' non installato "
                               f"(`ollama pull {self.model}`). {body}") from e
            raise LLMError(f"Ollama HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise LLMError(f"Ollama: timeout di connessione ({e.reason})") from e
            raise LLMError(
                f"Impossibile contattare Ollama su {self.host} ({e.reason}). "
                "Avvia il demone con `ollama serve` o imposta OLLAMA_HOST.") from e
        except (socket.timeout, TimeoutError) as e:
            raise LLMError(f"Ollama: timeout in attesa della risposta ({e})") from e

    def _get_json(self, path: str, payload: dict | None = None, timeout: float = 30) -> dict:
        with self._request(path, payload, timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ------------------------------------------------------------- metadati
    def model_info(self, refresh: bool = False) -> dict:
        """Digest, context length e versione del server (cache per processo).

        Non solleva mai: se Ollama non risponde i campi restano ``None``.
        """
        key = (self.host, normalize_model_name(self.model))
        if key in self._info_cache and not refresh:
            return self._info_cache[key]
        info: dict[str, Any] = {"digest": None, "context_length": None,
                                "server_version": None, "parameter_size": None,
                                "quantization": None, "family": None}
        try:
            info["server_version"] = self._get_json("/api/version").get("version")
        except Exception:
            pass
        try:
            for m in self._get_json("/api/tags").get("models", []):
                if normalize_model_name(m.get("name", "")) == key[1]:
                    info["digest"] = m.get("digest")
                    break
        except Exception:
            pass
        try:
            show = self._get_json("/api/show", {"model": self.model}, timeout=60)
            info["context_length"] = context_length_from_show(show)
            det = show.get("details") or {}
            info["parameter_size"] = det.get("parameter_size")
            info["quantization"] = det.get("quantization_level")
            info["family"] = det.get("family")
        except Exception:
            pass
        self._info_cache[key] = info
        return info

    def prepare(self) -> list[str]:
        """Legge i metadati e adatta `num_ctx`. Ritorna gli avvisi da mostrare."""
        notes = []
        ctx = self.model_info().get("context_length")
        if ctx and self.num_ctx > ctx:
            notes.append(f"num_ctx={self.num_ctx} supera la context length di "
                         f"'{self.model}' ({ctx}): ridotto a {ctx}.")
            self.num_ctx = ctx
        return notes

    def options(self, **overrides: Any) -> dict:
        opts = {
            "temperature": overrides.get("temperature", self.temperature),
            "num_ctx": self.num_ctx,
            "num_predict": overrides.get("max_tokens", self.max_tokens),
        }
        seed = overrides.get("seed", self.seed)
        if seed is not None:
            opts["seed"] = int(seed)
        return opts

    def meta(self) -> dict:
        info = self.model_info()
        return {
            "provider": self.name,
            "host": self.host,
            "model": self.model,
            "digest": info.get("digest"),
            "parameter_size": info.get("parameter_size"),
            "quantization": info.get("quantization"),
            "model_context_length": info.get("context_length"),
            "server_version": info.get("server_version"),
            "num_ctx_requested": self.requested_num_ctx,
            "keep_alive": self.keep_alive,
            "options": self.options(),
        }

    # ------------------------------------------------------------------ chat
    def _chat(self, system: str, messages: list[Message], **overrides: Any) -> LLMResponse:
        # Ollama tronca SILENZIOSAMENTE i prompt più lunghi di num_ctx, e lo fa
        # dall'inizio: sparirebbe proprio la specifica MXFP4 nel system prompt.
        approx = (len(system) + sum(len(m.content) for m in messages)) // 4
        budget = self.num_ctx - overrides.get("max_tokens", self.max_tokens)
        if approx > budget and self.on_notice:
            self.on_notice(
                f"prompt ~{approx} token contro una finestra utile di ~{budget} "
                f"(num_ctx={self.num_ctx}): Ollama troncherebbe la specifica MXFP4. "
                f"Rilancia con --num-ctx {2 ** (approx + 4096).bit_length()} "
                f"oppure --no-static-review.")

        payload: dict[str, Any] = {
            "model": overrides.get("model", self.model),
            "messages": [{"role": "system", "content": system}] + [m.as_dict() for m in messages],
            "stream": True,
            "options": self.options(**overrides),
        }
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        fmt = overrides.get("format")
        if fmt:
            payload["format"] = fmt

        final, parts = self._stream(payload)
        raw_text = "".join(parts)
        text = strip_think(raw_text)
        if not text:
            hint = (" (solo ragionamento <think>, nessuna risposta: alza --max-tokens)"
                    if "<think>" in raw_text.lower() else "")
            raise LLMError(f"Risposta vuota da Ollama{hint}: {str(final)[:300]}")
        stop = "max_tokens" if final.get("done_reason") == "length" else final.get("done_reason", "")
        return LLMResponse(
            text=text,
            model=payload["model"],
            provider=self.name,
            input_tokens=final.get("prompt_eval_count", 0) or 0,
            output_tokens=final.get("eval_count", 0) or 0,
            raw={k: v for k, v in final.items() if k != "message"},
            stop_reason=stop or "",
        )

    def _stream(self, payload: dict) -> tuple[dict, list[str]]:
        """Consuma lo stream NDJSON. Ritorna (chunk finale, frammenti di testo)."""
        t0 = time.time()
        parts: list[str] = []
        final: dict = {}
        try:
            with self._request("/api/chat", payload, self.chunk_timeout) as r:
                for line in r:
                    if not line.strip():
                        continue
                    chunk = json.loads(line.decode("utf-8"))
                    if chunk.get("error"):
                        raise LLMError(f"Ollama: {chunk['error']}")
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        parts.append(msg["content"])
                    if chunk.get("done"):
                        final = chunk
                        break
                    if self.timeout and time.time() - t0 > self.timeout:
                        raise LLMError(
                            f"Ollama: generazione oltre il tetto totale di {self.timeout}s "
                            f"({len(parts)} chunk ricevuti). Alza --timeout o riduci --max-tokens.")
        except LLMError:
            raise
        except (socket.timeout, TimeoutError) as e:
            raise LLMError(f"Ollama: timeout per chunk ({self.chunk_timeout}s senza token)") from e
        except (http.client.HTTPException, ConnectionError, OSError) as e:
            # Tipicamente il server è stato ucciso mentre caricava il modello:
            # pesi + cache KV non stanno in memoria.
            raise LLMError(
                f"Ollama ha chiuso la connessione senza rispondere ({type(e).__name__}).\n"
                f"  Causa quasi certa: memoria insufficiente per '{self.model}' con "
                f"num_ctx={self.num_ctx} (il processo viene terminato dall'OOM killer).\n"
                f"  → prova un modello più piccolo, oppure --num-ctx 8192\n"
                f"  → verifica con:  nvidia-smi   free -h   dmesg | tail -20 | grep -i oom"
            ) from e
        if not final:
            raise LLMError("Ollama: stream interrotto prima del chunk finale (done=true)")
        return final, parts

    # ------------------------------------------------------------ transienti
    def is_transient(self, err: Exception) -> bool:
        low = str(err).lower()
        return any(s in low for s in ("timed out", "timeout", "connection reset",
                                      "chiuso la connessione", "remotedisconnected",
                                      "stream interrotto",
                                      "http 500", "http 502", "http 503"))

    # ---------------------------------------------------------------- health
    def installed_models(self) -> list[str]:
        return [m["name"] for m in self._get_json("/api/tags", timeout=10).get("models", [])]

    def health_check(self) -> tuple[bool, str]:
        try:
            names = self.installed_models()
        except Exception as e:
            return False, f"Ollama non raggiungibile su {self.host}: {e}"
        want = normalize_model_name(self.model)
        if want not in {normalize_model_name(n) for n in names}:
            return False, (f"Modello '{self.model}' non installato. "
                           f"Esegui `ollama pull {self.model}`. "
                           f"Disponibili: {', '.join(names) or 'nessuno'}")
        return True, f"Ollama ok su {self.host} ({len(names)} modelli, uso '{self.model}')"


def context_length_from_show(show: dict) -> int | None:
    """Estrae la context length da una risposta di `/api/show`.

    La chiave dipende dall'architettura (`qwen2.context_length`,
    `llama.context_length`, …). Se il Modelfile fissa `num_ctx` nei parametri,
    quello NON è il limite del modello e viene ignorato.
    """
    for k, v in (show.get("model_info") or {}).items():
        if k.endswith(".context_length") and isinstance(v, int):
            return v
    return None
