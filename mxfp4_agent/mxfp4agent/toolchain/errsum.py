"""Riassunto compatto degli errori di toolchain per il Fixer.

Un log di sbt è lungo centinaia di righe, quasi tutte rumore (risoluzione
delle dipendenze, stack trace interni di Chisel, ripetizioni). Un modello da
14B con finestra limitata si perde: corregge il sintomo sbagliato o tronca il
prompt. Qui estraiamo solo:

    <strumento> file:riga:col  messaggio
       riga-1 | …
    >  riga   | …            ← dal sorgente reale su disco
       riga+1 | …

Formati riconosciuti (verificati su log reali di sbt 1.10 / Chisel 6.6 /
firtool 1.62 / Verilator 5 / g++):

* scalac       ``[error] /…/Foo.scala:10:22: type mismatch;`` + righe found/required
* firtool      ``[error] …/Foo.scala:11:15: error: sink "w" not fully initialized``
* Chisel       ``chisel3.package$ChiselException: msg`` + ``at mxfp4.Foo.<init>(Foo.scala:11)``
* Verilator    ``%Error: /…/Foo.sv:12:3: msg`` / ``%Warning-WIDTH: …``
* g++          ``/…/tb_Foo.cpp:23:10: error: msg``
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SRC_EXT = r"(?:scala|sv|v|cpp|cc|h|hpp)"
# prefisso [error]/[warn] di sbt, poi path (anche Windows, anche relativo "\\src\\…")
_LOC = re.compile(
    rf"^(?:\[(?P<lvl>error|warn)\]\s*)?(?P<pfx>%(?:Error|Warning)[-\w]*:\s*)?"
    rf"(?P<file>(?:[A-Za-z]:)?[^\s:]*?[\w-]+\.{_SRC_EXT}):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
    rf"(?P<msg>.*)$")
_STACK = re.compile(r"^\s*(?:\[error\]\s*)?at\s+mxfp4\.[\w$.<>]+\((?P<file>[\w-]+\.scala):(?P<line>\d+)\)")
_EXC = re.compile(r"(?:^|\]\s)(?P<exc>[\w$.]+(?:Exception|Error)):\s*(?P<msg>.+)$")
_NOISE = re.compile(r"^\s*(\^|one error found|\d+ errors? found|Compilation failed|"
                    r"Total time|stack trace is suppressed|\(Compile /)")


@dataclass
class ErrorItem:
    tool: str
    file: str
    line: int
    col: int | None
    message: str
    severity: str = "error"

    @property
    def key(self) -> tuple:
        return (Path(self.file.replace("\\", "/")).name, self.line, self.message)


#: suggerimenti deterministici per gli errori più frequenti dei modelli piccoli
HINTS: list[tuple[str, str]] = [
    (r"not found: (value|type|object) (Cat|Fill|MuxLookup|MuxCase|Log2|log2Ceil|log2Up|"
     r"PopCount|Reverse|ShiftRegister|Valid|Decoupled|PriorityEncoder|OHToUInt|UIntToOH)\b",
     "manca `import chisel3.util._`"),
    (r"not fully initialized",
     "il segnale non è assegnato in tutti i rami: dai un default prima del `when` "
     "(`w := 0.U`) oppure usa `WireDefault(0.U(n.W))`"),
    (r"found\s*:\s*chisel3\.UInt\s+required\s*:\s*chisel3\.SInt",
     "converti esplicitamente: `.asSInt` reinterpreta i bit, `.zext` estende con uno zero "
     "(valore positivo garantito)"),
    (r"found\s*:\s*chisel3\.SInt\s+required\s*:\s*chisel3\.UInt",
     "converti esplicitamente con `.asUInt` (attenzione: un negativo diventa un numero grande)"),
    (r"different types",
     "sorgente e destinazione di `:=` hanno tipi diversi (UInt/SInt/Bool): converti con "
     "`.asSInt`/`.asUInt`/`.asBool`"),
    (r"cannot be applied to \(chisel3\.SInt\)",
     "l'ammontare di uno shift deve essere UInt o Int: vedi regola 6-bis"),
    (r"value (\w+) is not a member of chisel3\.(UInt|SInt|Bool)",
     "metodo inesistente su questo tipo hardware: controlla l'API Chisel 6"),
    (r"has no member named '(?!io_)(\w+)'",
     "nel testbench le porte di un modulo Chisel si chiamano `io_<nome>`"),
]


def hint_for(message: str) -> str:
    for pat, hint in HINTS:
        if re.search(pat, message):
            return hint
    return ""


def _strip_sbt(line: str) -> str:
    return re.sub(r"^\[(?:error|warn|info)\]\s?", "", line)


def _tool_for(file: str, msg: str, pfx: str | None) -> str:
    if pfx:
        return "verilator"
    if file.endswith(".scala"):
        return "firtool" if msg.startswith("error:") else "scalac"
    if file.endswith((".cpp", ".cc", ".h", ".hpp")):
        return "g++"
    return "verilator"


def parse_errors(log: str) -> list[ErrorItem]:
    """Errori (e warning con posizione) nell'ordine in cui compaiono, senza duplicati."""
    lines = log.splitlines()
    items: list[ErrorItem] = []
    seen: set[tuple] = set()
    pending_exc: str | None = None

    for i, raw in enumerate(lines):
        m = _LOC.match(raw.strip())
        if m:
            msg = m.group("msg").strip()
            sev = "warning" if (m.group("lvl") == "warn" or "warning" in msg[:12].lower()
                                or (m.group("pfx") or "").startswith("%Warning")) else "error"
            # scalac: le righe successive (found/required) completano il messaggio,
            # fino alla riga di codice citata e al caret.
            extra = []
            for nxt in lines[i + 1:i + 4]:
                body = _strip_sbt(nxt)
                if _LOC.match(nxt.strip()) or not body.strip() or _NOISE.match(body):
                    break
                if re.match(r"^\s*(found|required|\w+ is not a member|possible cause)", body):
                    extra.append(" ".join(body.split()))
                else:
                    break
            if extra:
                msg = f"{msg} {' '.join(extra)}"
            msg = re.sub(r"^(error|warning):\s*", "", msg, flags=re.I)
            fname = re.sub(r"/+", "/", m.group("file").replace("\\", "/"))
            it = ErrorItem(_tool_for(m.group("file"), m.group("msg"), m.group("pfx")),
                           fname, int(m.group("line")),
                           int(m.group("col")) if m.group("col") else None, msg, sev)
            if it.key not in seen:
                seen.add(it.key)
                items.append(it)
            continue
        e = _EXC.search(raw)
        if e and "FirtoolNonZeroExitCode" not in e.group("exc"):
            exc = re.split(r"[.$]", e.group("exc"))[-1]
            pending_exc = f"{exc}: {e.group('msg').strip()}"
            continue
        s = _STACK.match(raw)
        if s and pending_exc:
            it = ErrorItem("chisel", s.group("file"), int(s.group("line")), None, pending_exc)
            if it.key not in seen:
                seen.add(it.key)
                items.append(it)
            pending_exc = None

    if pending_exc and not items:   # eccezione senza riga utente nello stack
        items.append(ErrorItem("chisel", "?", 0, None, pending_exc))
    return items


def _resolve(file: str, root: Path | None) -> Path | None:
    if root is None or file == "?":
        return None
    p = Path(file.replace("\\", "/").lstrip("/"))
    direct = Path(file)
    if direct.is_absolute() and direct.exists():
        return direct
    if (root / p).exists():
        return root / p
    # i path dei log possono essere relativi a un'altra cwd o assoluti di un'altra
    # macchina: si cerca per nome nelle cartelle del progetto
    for sub in ("src/main/scala", "sim", "rtl"):
        hits = list((root / sub).rglob(p.name)) if (root / sub).is_dir() else []
        if hits:
            return hits[0]
    return None


def _context(path: Path | None, line: int, radius: int) -> list[str]:
    if path is None or line <= 0:
        return []
    try:
        src = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    lo, hi = max(1, line - radius), min(len(src), line + radius)
    return [f"   {'>' if n == line else ' '} {n:4d} | {src[n - 1]}" for n in range(lo, hi + 1)]


def _short(file: str, root: Path | None, resolved: Path | None = None) -> str:
    if resolved is not None and root is not None:
        try:
            return resolved.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    f = file.replace("\\", "/")
    for marker in ("src/main/scala/", "sim/", "rtl/"):
        if marker in f:
            return marker + f.split(marker, 1)[1]
    return f.lstrip("/")


def summarize_errors(log: str, root: Path | str | None = None, max_items: int = 8,
                     context: int = 1, include_warnings: bool = False) -> str:
    """Riassunto per il Fixer. Stringa vuota se non c'è niente di riconoscibile.

    ``context`` = righe prima e dopo quella dell'errore (1 → 3 righe totali).
    """
    root = Path(root) if root else None
    items = [i for i in parse_errors(log) if include_warnings or i.severity == "error"]
    if not items:
        return ""
    out = [f"{len(items)} errore/i rilevato/i (mostrati i primi {min(len(items), max_items)}):"]
    for it in items[:max_items]:
        path = _resolve(it.file, root)
        loc = f"{_short(it.file, root, path)}:{it.line}" + (f":{it.col}" if it.col else "")
        out.append(f"\n[{it.tool}] {loc}  {it.message}")
        out.extend(_context(path, it.line, context))
        hint = hint_for(it.message)
        if hint:
            out.append(f"   suggerimento: {hint}")
    return "\n".join(out)
