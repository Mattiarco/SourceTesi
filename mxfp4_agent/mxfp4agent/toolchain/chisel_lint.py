"""Lint statico e deterministico (senza LLM) per gli errori Chisel ricorrenti.

Gira prima di sbt: un round di elaborazione costa da 20 s a qualche minuto,
questo controllo millisecondi. Tre esiti per regola:

* **correzione automatica** — solo trasformazioni sicure e locali (import
  mancanti, `package mxfp4`, letterali negativi `-3.S(5.W)`);
* **errore** — il design è certamente sbagliato o incompatibile con il
  contratto (porte diverse dal piano, `Wire` mai assegnato, entry-point di
  elaborazione duplicato): sbt non viene lanciato, si passa al Fixer;
* **avviso** — sospetto ma non certo (`+` invece di `+&` nelle riduzioni,
  `Reg` senza reset su segnali di controllo, `Cat` su `SInt`, shift con
  ammontare `SInt`): l'avviso accompagna il log se una fase successiva fallisce.

Le regole lavorano su una copia del sorgente con commenti e stringhe
sbiancati (stesse righe e colonne), così un `Cat` in un commento non conta.
È un'analisi lessicale, non un parser Scala: preferisce tacere che dare falsi
positivi, perché un falso errore costerebbe un round al Fixer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

UTIL_SYMBOLS = ("Cat", "Fill", "MuxLookup", "MuxCase", "Log2", "log2Ceil", "log2Up",
                "log2Floor", "PopCount", "Reverse", "ShiftRegister", "PriorityEncoder",
                "PriorityMux", "OHToUInt", "UIntToOH", "RegEnable", "Valid", "Decoupled",
                "Counter", "Mux1H", "isPow2")
CORE_SYMBOLS = ("Module", "IO", "Bundle", "UInt", "SInt", "Bool", "Wire", "Reg", "RegInit",
                "RegNext", "Input", "Output", "Mux", "VecInit", "Vec", "when")
#: nomi di registri che in un pipeline portano controllo: senza reset il primo
#: ciclo dopo il reset produce un `valid` casuale
CONTROL_REG = re.compile(r"(valid|vld|busy|state|count|cnt|ready|start|done|first|last)",
                         re.IGNORECASE)


@dataclass
class LintIssue:
    rule: str
    severity: str          # "error" | "warning" | "fixed"
    line: int
    message: str
    hint: str = ""

    def format(self, path: str, src_lines: list[str]) -> str:
        loc = f"{path}:{self.line}" if self.line else path
        out = [f"[chisel-lint:{self.rule}] {self.severity.upper()} {loc}  {self.message}"]
        if 0 < self.line <= len(src_lines):
            out.append(f"   > {self.line:4d} | {src_lines[self.line - 1]}")
        if self.hint:
            out.append(f"   suggerimento: {self.hint}")
        return "\n".join(out)


@dataclass
class LintResult:
    source: str                   # sorgente dopo le correzioni automatiche
    issues: list[LintIssue]

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def fixed(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "fixed"]

    def report(self, path: str, only: tuple[str, ...] = ("error", "warning", "fixed")) -> str:
        lines = self.source.splitlines()
        return "\n".join(i.format(path, lines) for i in self.issues if i.severity in only)


# ------------------------------------------------------------------ lessico
def blank_comments_and_strings(src: str) -> str:
    """Sostituisce commenti e letterali stringa con spazi, preservando righe e colonne."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j == -1 else j
            out[i:j] = " " * (j - i)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in src[i:j]]
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j == -1 else j + 3
            out[i:j] = [ch if ch == "\n" else " " for ch in src[i:j]]
            i = j
        elif c == '"':
            j = i + 1
            while j < n and src[j] != '"' and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            out[i:j] = " " * (j - i)
            i = j
        else:
            i += 1
    return "".join(out)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _uses(code: str, names) -> list[str]:
    """Simboli usati come chiamata/costruttore (`Cat(`, `Vec[`), non come parte di altri nomi."""
    return [n for n in names if re.search(rf"(?<![\w.]){re.escape(n)}\s*[(\[]", code)]


# ------------------------------------------------------------------- regole
def _fix_package_and_imports(src: str, code: str, issues: list[LintIssue]) -> str:
    lines = src.splitlines()
    has_pkg = re.search(r"^\s*package\s+mxfp4\b", code, re.M)
    other_pkg = re.search(r"^\s*package\s+([\w.]+)", code, re.M)
    if other_pkg and not has_pkg:
        idx = _line_of(code, other_pkg.start()) - 1
        lines[idx] = re.sub(r"package\s+[\w.]+", "package mxfp4", lines[idx], count=1)
        issues.append(LintIssue("package", "fixed", idx + 1,
                                f"package '{other_pkg.group(1)}' sostituito con 'mxfp4' "
                                "(Elaborate.scala istanzia il modulo dal package mxfp4)"))
    elif not has_pkg:
        lines.insert(0, "package mxfp4")
        lines.insert(1, "")
        issues.append(LintIssue("package", "fixed", 1, "aggiunto 'package mxfp4'"))

    text = "\n".join(lines)
    code2 = blank_comments_and_strings(text)
    imports = re.findall(r"^\s*import\s+([^\n]+)", code2, re.M)
    has_core = any(re.match(r"chisel3\._\b|chisel3\.\{", imp.strip()) or
                   imp.strip().startswith("chisel3._") for imp in imports)
    has_util = any(re.match(r"chisel3\.util\.(_|\{)", imp.strip()) for imp in imports)
    util_named = {s for imp in imports
                  for s in re.findall(r"\w+", imp.split("chisel3.util.", 1)[-1])
                  if imp.strip().startswith("chisel3.util.")}
    need_core = not has_core and _uses(code2, CORE_SYMBOLS)
    need_util = [s for s in _uses(code2, UTIL_SYMBOLS) if not has_util and s not in util_named]

    to_add = []
    if need_core:
        to_add.append("import chisel3._")
    if need_util:
        to_add.append("import chisel3.util._")
    if to_add:
        tl = text.splitlines()
        pkg_idx = next((k for k, ln in enumerate(tl) if re.match(r"\s*package\s+mxfp4", ln)), -1)
        last_imp = max((k for k, ln in enumerate(tl) if re.match(r"\s*import\s", ln)), default=pkg_idx)
        at = last_imp + 1
        tl[at:at] = to_add
        text = "\n".join(tl)
        for imp in to_add:
            why = (f"usato {', '.join(need_util[:4])}" if "util" in imp else "usati tipi Chisel")
            issues.append(LintIssue("imports", "fixed", at + 1, f"aggiunto '{imp}' ({why})"))
    if src.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text


def _fix_negative_literals(src: str, code: str, issues: list[LintIssue]) -> str:
    """`-3.S(5.W)` è `-(3.S(5.W))`: l'operatore hardware allarga a 6 bit."""
    pat = re.compile(r"-(\d+)\.S\(")

    def unary(pos: int) -> bool:
        # il meno è unario solo dopo un operatore, una parentesi aperta, una
        # virgola, `=` o a inizio riga: `w -3.S(4.W)` è una sottrazione
        before = code[:pos].rstrip(" \t")
        return not before or before[-1] in "=(,[{:+-*/%&|^<>!\n"

    hits = [m for m in pat.finditer(code) if unary(m.start())]
    if not hits:
        return src
    out, last = [], 0
    for m in hits:
        out.append(src[last:m.start()])
        out.append(f"(-{m.group(1)}).S(")
        last = m.end()
        issues.append(LintIssue("neg-literal", "fixed", _line_of(code, m.start()),
                                f"'-{m.group(1)}.S(' riscritto come '(-{m.group(1)}).S(' "
                                "(il meno fuori dalle parentesi è un'operazione hardware "
                                "che allarga di un bit)"))
    out.append(src[last:])
    return "".join(out)


def _check_entrypoint(code: str, issues: list[LintIssue]) -> None:
    for m in re.finditer(r"\bobject\s+\w+\s+extends\s+App\b|\bChiselStage\b|\bemitVerilog\b|"
                         r"\bemitSystemVerilog\w*\b", code):
        issues.append(LintIssue("entrypoint", "error", _line_of(code, m.start()),
                                f"entry-point di elaborazione '{m.group(0)}' nel file del modulo",
                                "rimuovilo: Elaborate.scala esiste già e lo duplica"))
        return


def _check_deprecated(code: str, issues: list[LintIssue]) -> None:
    m = re.search(r"^\s*import\s+Chisel\._", code, re.M)
    if m:
        issues.append(LintIssue("deprecated-api", "error", _line_of(code, m.start()),
                                "import Chisel._ (API Chisel 2, rimossa in Chisel 6)",
                                "usa import chisel3._ e import chisel3.util._"))


def _declared(code: str) -> dict[str, str]:
    """Nome → tipo hardware dichiarato ('SInt' | 'UInt' | 'Bool') per val/Wire/Reg/IO."""
    types: dict[str, str] = {}
    for m in re.finditer(r"\bval\s+(\w+)\s*(?::\s*(SInt|UInt|Bool)\b)?\s*=\s*([^\n]+)", code):
        name, ann, rhs = m.group(1), m.group(2), m.group(3)
        t = ann
        if not t:
            if re.search(r"\b(Input|Output|Wire|WireDefault|WireInit|Reg|RegInit)\s*\(\s*SInt\b"
                         r"|\.S\s*\(|\.asSInt\b|\bS\(\d", rhs) or re.match(r"\s*\(?-?\d+\)?\.S\b", rhs):
                t = "SInt"
            elif re.search(r"\b(Input|Output|Wire|Reg|RegInit)\s*\(\s*UInt\b|\.asUInt\b", rhs):
                t = "UInt"
        if t:
            types[name] = t
    return types


def _check_wires(code: str, issues: list[LintIssue]) -> None:
    for m in re.finditer(r"\bval\s+(\w+)\s*=\s*Wire\s*\(", code):
        name = m.group(1)
        assigned = re.search(rf"(?<![\w.]){name}(\s*\([^)]*\))*(\.\w+)*\s*:=", code) or \
            re.search(rf"(?<![\w.]){name}\s*<>", code) or \
            re.search(rf"(?<![\w.]){name}(\.\w+)*\s*:=\s*DontCare", code)
        if not assigned:
            issues.append(LintIssue("wire-uninit", "error", _line_of(code, m.start()),
                                    f"Wire '{name}' dichiarato ma mai assegnato con ':='",
                                    f"assegnalo, oppure usa WireDefault(<valore>) "
                                    f"(firtool fallirebbe con 'sink \"{name}\" not fully initialized')"))


def _check_regs(code: str, issues: list[LintIssue]) -> None:
    for m in re.finditer(r"\bval\s+(\w+)\s*=\s*(Reg\s*\(|RegNext\s*\(([^()]|\([^()]*\))*\))", code):
        name, expr = m.group(1), m.group(2)
        if expr.startswith("RegNext") and "," in expr:
            continue          # RegNext(x, init) ha il reset
        if CONTROL_REG.search(name):
            issues.append(LintIssue("reg-noreset", "warning", _line_of(code, m.start()),
                                    f"registro di controllo '{name}' senza valore di reset",
                                    f"usa RegInit(false.B) o RegNext(x, false.B): dopo il reset "
                                    f"'{name}' sarebbe indefinito"))


def _check_arith(code: str, types: dict[str, str], issues: list[LintIssue]) -> None:
    for m in re.finditer(r"\.(reduce|reduceLeft|fold\([^)]*\))\s*\(\s*_\s*\+\s*_\s*\)", code):
        issues.append(LintIssue("plus-truncates", "warning", _line_of(code, m.start()),
                                "riduzione con '+': in Chisel '+' tronca alla larghezza massima "
                                "degli operandi", "usa '+&' (o un albero di somma con +&)"))
    for m in re.finditer(r"\bCat\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", code):
        args = re.findall(r"(?<![\w.])([A-Za-z_]\w*)(?![\w(])", m.group(1))
        bad = [a for a in args if types.get(a) == "SInt"]
        if bad or ".asSInt" in m.group(1) or re.search(r"\.S\b|\.S\(", m.group(1)):
            what = ", ".join(bad) or "un valore SInt"
            issues.append(LintIssue("cat-sint", "warning", _line_of(code, m.start()),
                                    f"Cat con argomento SInt ({what})",
                                    "Cat lavora su bit: converti con .asUInt e, se serve un "
                                    "risultato con segno, applica .asSInt al risultato"))
    for m in re.finditer(r"(<<|>>)\s*\(?\s*([A-Za-z_]\w*)\b", code):
        if types.get(m.group(2)) == "SInt":
            issues.append(LintIssue("shift-sint", "warning", _line_of(code, m.start()),
                                    f"ammontare di shift '{m.group(2)}' di tipo SInt",
                                    "l'ammontare deve essere UInt o Int (regola 6-bis): "
                                    "scegli la direzione con Mux sul segno"))
    for m in re.finditer(r"((?:\w+\.)*([A-Za-z_]\w*))\s*\.asSInt\b", code):
        full, name = m.group(1), m.group(2)
        # .asSInt su un valore UInt "numerico" (non un campo di bit) perde il bit alto:
        # 0xFF diventa -1. Segnaliamo solo i casi certi: input dichiarati UInt.
        if types.get(name) == "UInt" and re.search(rf"\bval\s+{name}\s*=\s*Input", code):
            issues.append(LintIssue("assint-input", "warning", _line_of(code, m.start()),
                                    f"'{full}.asSInt' reinterpreta l'ingresso UInt: il bit alto "
                                    "diventa segno", "se il valore è una quantità non negativa usa "
                                    f"'{full}.zext' (estende con uno zero)"))


def _io_ports(code: str) -> dict[str, tuple[str, str]]:
    """Porte del Bundle `io`: nome → (dir 'in'|'out', tipo)."""
    m = re.search(r"\bval\s+io\s*=\s*IO\s*\(\s*new\s+Bundle\s*\{", code)
    if not m:
        return {}
    depth, i = 1, m.end()
    while i < len(code) and depth:
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        i += 1
    body = code[m.end():i - 1]
    ports = {}
    for p in re.finditer(r"\bval\s+(\w+)\s*=\s*(Input|Output)\s*\(\s*(\w+)", body):
        ports[p.group(1)] = ("in" if p.group(2) == "Input" else "out", p.group(3))
    return ports


def _check_ports(code: str, plan: dict, issues: list[LintIssue]) -> None:
    want = {p["name"]: p for p in plan.get("ports", []) if p.get("name")}
    if not want:
        return
    m = re.search(r"\bval\s+io\s*=\s*IO\s*\(", code)
    line = _line_of(code, m.start()) if m else 0
    got = _io_ports(code)
    if not got:
        issues.append(LintIssue("ports", "error", line,
                                "nessun 'val io = IO(new Bundle { ... })' riconoscibile",
                                "dichiara le porte del piano dentro un unico Bundle io"))
        return
    missing = [n for n in want if n not in got]
    extra = [n for n in got if n not in want]
    wrong_dir = [n for n in want if n in got and got[n][0] != want[n].get("dir")]
    wrong_type = [n for n in want if n in got and want[n].get("type") in ("UInt", "SInt", "Bool")
                  and got[n][1] in ("UInt", "SInt", "Bool") and got[n][1] != want[n]["type"]]
    if missing or extra or wrong_dir or wrong_type:
        parts = []
        if missing:
            parts.append("mancano " + ", ".join(missing))
        if extra:
            parts.append("in più " + ", ".join(extra))
        if wrong_dir:
            parts.append("direzione errata " + ", ".join(wrong_dir))
        if wrong_type:
            parts.append("tipo errato " + ", ".join(f"{n} ({got[n][1]}≠{want[n]['type']})"
                                                     for n in wrong_type))
        contract = ", ".join(f"{p.get('dir')} {n}: {p.get('type')}({p.get('width')})"
                             for n, p in want.items())
        issues.append(LintIssue("ports", "error", line,
                                "porte diverse dal contratto del piano: " + "; ".join(parts),
                                f"il testbench usa esattamente: {contract}"))


# -------------------------------------------------------------------- API
def lint_chisel(src: str, plan: dict | None = None) -> LintResult:
    issues: list[LintIssue] = []
    code = blank_comments_and_strings(src)
    src = _fix_package_and_imports(src, code, issues)
    code = blank_comments_and_strings(src)
    src = _fix_negative_literals(src, code, issues)
    code = blank_comments_and_strings(src)

    _check_deprecated(code, issues)
    _check_entrypoint(code, issues)
    _check_wires(code, issues)
    _check_regs(code, issues)
    _check_arith(code, _declared(code), issues)
    if plan:
        _check_ports(code, plan, issues)
    order = {"error": 0, "warning": 1, "fixed": 2}
    issues.sort(key=lambda i: (order[i.severity], i.line))
    return LintResult(src, issues)
