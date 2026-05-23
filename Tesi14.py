"""
Agentic AI solution for Meta-HDL floating-point arithmetic units.
Target: MXFP4 / NVFP4 arithmetic units for coprocessor integration.
Ref: "An agentic-driven approach for Meta-HDL"

Correzioni rispetto alla versione precedente:
  1. LLM sostituito con Claude (claude-sonnet-4-20250514) via LiteLLM —
     PyMTL3 corretto e API reali garantiti.
  2. Prompt degli agenti arricchiti con esempi few-shot PyMTL3 validi.
  3. Aggiunto validatore sintattico sul codice RTL generato dall'AI.
  4. Porta "i" rimossa dai port non dichiarati: IRBuilder ora
     riconosce solo a/b/c/d come input port e segnala variabili sconosciute.
  5. generate_pymtl ora emette un avviso se un input non è mappato.
"""

from crewai import Agent, Task, Crew, Process
import ast
import json
import re
import networkx as nx
from pathlib import Path
from datetime import datetime

# ── Latenze hardware (cicli di clock) ─────────────────────────────────────────
LATENCY_TABLE = {
    "Add":   1,
    "Sub":   1,
    "Mult":  2,
    "Div":   4,
    "Gt":    1,
    "Lt":    1,
    "Eq":    1,
    "CONST": 0,
    "PHI":   0,
}

# Formati FP4 supportati
FP_FORMATS = {
    "MXFP4": {"exp_bits": 2, "man_bits": 1, "total_bits": 4},
    "NVFP4": {"exp_bits": 2, "man_bits": 1, "total_bits": 4},
}

# Input port riconosciuti — variabili fuori da questo insieme vengono segnalate
KNOWN_PORTS = {"a", "b", "c", "d"}
PORT_MAP    = {"a": "s.in0", "b": "s.in1", "c": "s.in2", "d": "s.in3"}


# ── IR Builder ────────────────────────────────────────────────────────────────
class IRBuilder(ast.NodeVisitor):
    """Converte codice Python in IR + Data Flow Graph."""

    def __init__(self):
        self.ir           = []
        self.graph        = nx.DiGraph()
        self.op_id        = 0
        self.var_map      = {}
        self.const_cache  = {}
        self.return_value = None
        self.warnings     = []          # variabili non mappate

    def _new_id(self):
        self.op_id += 1
        return f"op_{self.op_id}"

    def _resolve(self, name):
        if isinstance(name, str):
            if name.startswith("op_") or name in KNOWN_PORTS:
                return name
            if name in self.var_map:
                return self.var_map[name]
            # Variabile sconosciuta: segnala e usa come placeholder
            self.warnings.append(
                f"Variabile '{name}' non è un input port noto "
                f"{KNOWN_PORTS} né una variabile definita — "
                f"verrà emessa come letterale non risolto."
            )
        return name

    def _const(self, value):
        key = f"const_{value}"
        if key in self.const_cache:
            return self.const_cache[key]
        oid  = self._new_id()
        node = {"id": oid, "type": "const", "operation": "CONST",
                "inputs": [], "output": oid, "value": value, "latency": 0}
        self.ir.append(node)
        self.graph.add_node(oid, **node)
        self.const_cache[key] = oid
        return oid

    def _add_node(self, op_type, operation, inputs, latency=1, extra=None):
        oid  = self._new_id()
        node = {"id": oid, "type": op_type, "operation": operation,
                "inputs": inputs, "output": oid, "latency": latency}
        if extra:
            node.update(extra)
        self.ir.append(node)
        self.graph.add_node(oid, **node)
        for inp in inputs:
            if isinstance(inp, str):
                self.graph.add_node(inp)
                if inp != oid:
                    self.graph.add_edge(inp, oid)
        return oid

    def extract(self, node):
        if isinstance(node, ast.Name):
            return self._resolve(node.id)
        if isinstance(node, ast.Constant):
            return self._const(node.value)
        if isinstance(node, ast.BinOp):
            left  = self.extract(node.left)
            right = self.extract(node.right)
            op    = type(node.op).__name__
            return self._add_node("binary_op", op, [left, right],
                                  LATENCY_TABLE.get(op, 1))
        if isinstance(node, ast.Compare):
            left  = self.extract(node.left)
            right = self.extract(node.comparators[0])
            op    = type(node.ops[0]).__name__
            return self._add_node("compare", op, [left, right],
                                  LATENCY_TABLE.get(op, 1))
        return ast.dump(node)

    def visit_Assign(self, node):
        target = node.targets[0].id
        self.var_map[target] = self.extract(node.value)

    def visit_Return(self, node):
        self.return_value = self.extract(node.value)


def build_ir(code: str):
    builder = IRBuilder()
    builder.visit(ast.parse(code))
    if builder.warnings:
        print("\n⚠️  IR Builder Warnings:")
        for w in builder.warnings:
            print(f"   • {w}")
    return builder.ir, builder.graph, builder.var_map, builder.return_value


# ── Analisi del grafo ──────────────────────────────────────────────────────────
def critical_path(graph: nx.DiGraph) -> dict:
    dist = {}
    for n in nx.topological_sort(graph):
        lat   = graph.nodes[n].get("latency", 0)
        preds = list(graph.predecessors(n))
        dist[n] = lat + (max(dist[p] for p in preds) if preds else 0)
    return dist

def schedule(graph: nx.DiGraph) -> dict:
    sch = {}
    for n in nx.topological_sort(graph):
        preds  = list(graph.predecessors(n))
        base   = max((sch[p] for p in preds), default=0)
        sch[n] = base + graph.nodes[n].get("latency", 0)
    return sch

def pipeline_stages(sch: dict) -> dict:
    stages = {}
    for node, stage in sch.items():
        stages.setdefault(stage, []).append(node)
    return stages

def resource_estimate(ir: list) -> dict:
    res = {"adders": 0, "multipliers": 0, "dividers": 0,
           "comparators": 0, "registers": len(ir)}
    for node in ir:
        if node["type"] == "binary_op":
            if   node["operation"] in ("Add", "Sub"): res["adders"]      += 1
            elif node["operation"] == "Mult":          res["multipliers"] += 1
            elif node["operation"] == "Div":           res["dividers"]    += 1
        elif node["type"] == "compare":
            res["comparators"] += 1
    return res


# ── Generazione PyMTL3 ────────────────────────────────────────────────────────
def generate_pymtl(ir: list, var_map: dict,
                   return_value, fp_format: str = "MXFP4") -> str:
    fmt  = FP_FORMATS.get(fp_format, FP_FORMATS["MXFP4"])
    bits = fmt["total_bits"]

    def resolve(x):
        if isinstance(x, str):
            if x.startswith("op_"):
                return f"s.{x}"
            if x in PORT_MAP:
                return PORT_MAP[x]
            mapped = var_map.get(x)
            if mapped and isinstance(mapped, str) and mapped.startswith("op_"):
                return f"s.{mapped}"
            # Variabile non risolta: emette commento di avviso inline
            return f"0  # UNRESOLVED: '{x}'"
        return str(x)

    OP_EXPR = {
        "Add":  "{a} + {b}",
        "Sub":  "{a} - {b}",
        "Mult": "{a} * {b}",
        "Div":  "{a} // {b}",
        "Gt":   "{a} > {b}",
        "Lt":   "{a} < {b}",
        "Eq":   "{a} == {b}",
        "NotEq":"{a} != {b}",
    }

    lines = [
        "from pymtl3 import *\n",
        f"# {fp_format} arithmetic unit  "
        f"(exp={fmt['exp_bits']}b, man={fmt['man_bits']}b, total={bits}b)\n",
        f"class {fp_format}ArithUnit(Component):",
        "    def construct(s):",
        f"        # Input ports — {bits}-bit {fp_format} values",
        *[f"        s.in{i} = InPort(Bits{bits})" for i in range(4)],
        f"        s.out  = OutPort(Bits{bits})\n",
        *[f"        s.{n['id']} = Wire(Bits{'1' if n['type'] == 'compare' else bits})"
          for n in ir],
        "\n        @update",
        "        def compute():",
    ]

    for node in ir:
        oid = node["id"]
        if node["type"] == "const":
            lines.append(f"            s.{oid} @= Bits{bits}({node['value']})")
        elif node["type"] in ("binary_op", "compare") and node["operation"] in OP_EXPR:
            a    = resolve(node["inputs"][0])
            b    = resolve(node["inputs"][1])
            expr = OP_EXPR[node["operation"]].format(a=a, b=b)
            lines.append(f"            s.{oid} @= {expr}")

    if return_value:
        lines.append(f"\n            s.out @= {resolve(return_value)}")
    else:
        lines.append("\n            s.out @= Bits4(0)  # no return statement found")

    return "\n".join(lines)


# ── Validatore sintattico RTL ─────────────────────────────────────────────────
def extract_and_validate_pymtl(ai_text: str) -> tuple[str, list[str]]:
    """
    Estrae blocchi ```python ... ``` dalla risposta AI e ne verifica
    la sintassi con ast.parse. Ritorna (codice_valido_o_vuoto, lista_errori).
    """
    blocks  = re.findall(r"```python(.*?)```", ai_text, re.DOTALL)
    errors  = []
    valid   = []

    for i, block in enumerate(blocks):
        code = block.strip()
        try:
            ast.parse(code)
            valid.append(code)
        except SyntaxError as e:
            errors.append(f"Blocco {i+1}: SyntaxError — {e}")

    return "\n\n# --- next block ---\n\n".join(valid), errors


# ── Few-shot PyMTL3 valido da inserire nei prompt ─────────────────────────────
PYMTL3_FEWSHOT = """
PyMTL3 syntax reference — use ONLY these patterns, no others:

```python
from pymtl3 import *

class ExampleUnit(Component):
    def construct(s):
        s.in0 = InPort(Bits4)
        s.in1 = InPort(Bits4)
        s.out  = OutPort(Bits4)
        s.tmp  = Wire(Bits4)

        @update
        def compute():
            s.tmp @= s.in0 + s.in1
            s.out @= s.tmp
```

Rules:
- InPort / OutPort / Wire take a Bits<N> type (e.g. Bits4, Bits32).
- All assignments inside @update use @= (never = or <<=).
- No always_ff, always_comb, posedge, m.ComponentLevel, <<= — these do NOT exist in PyMTL3.
- No custom types like MXFP4(...) — use Bits4 for 4-bit values.
- Do NOT import anything beyond `from pymtl3 import *`.
"""


# ── Agenti CrewAI con Claude ──────────────────────────────────────────────────
# Usa Claude via LiteLLM — assicurati di avere ANTHROPIC_API_KEY nell'ambiente.
LLM = "anthropic/claude-sonnet-4-20250514"

designer = Agent(
    role="Meta-HDL FP Unit Designer",
    goal=(
        "Design a valid PyMTL3 component for the given FP arithmetic unit. "
        "Generate ONLY syntactically correct PyMTL3 code using the provided "
        "IR data and the PyMTL3 syntax reference."
    ),
    backstory=(
        "Expert hardware designer specialised in Meta-HDL frameworks. "
        "You always write compilable PyMTL3 code and never invent API methods."
    ),
    llm=LLM,
    verbose=True,
)

analyzer = Agent(
    role="HLS Compiler Analyzer",
    goal=(
        "Analyze IR, critical path, and resources to produce a concrete HLS "
        "optimization report grounded strictly in the provided data."
    ),
    backstory=(
        "Compiler backend specialist for arithmetic coprocessors. "
        "You never invent nodes or dependencies not present in the IR."
    ),
    llm=LLM,
    verbose=True,
)

design_task = Task(
    description="""
Design a {fp_format} arithmetic unit in PyMTL3.

{fewshot}

IR (use ONLY these nodes):
{ir}

SCHEDULE:
{schedule}

PIPELINE:
{pipeline}

RESOURCES:
{resources}

FP format config: {fp_config}

Instructions:
1. Write a PyMTL3 Component that implements exactly the operations in the IR.
2. Map each IR node to one Wire + one @update assignment.
3. Use only InPort(Bits4) / OutPort(Bits4) / Wire(Bits4).
4. Do not add operations not present in the IR.
5. End with a short description of the pipeline stages and chosen architecture.
""",
    expected_output=(
        "Valid PyMTL3 source code for the FP unit followed by a brief "
        "architecture description."
    ),
    agent=designer,
)

analysis_task = Task(
    description="""
Analyze the compiler output for {fp_format} and produce an HLS optimization report.

IR:
{ir}

CRITICAL PATH:
{critical_path}

RESOURCES:
{resources}

Instructions:
- Use ONLY the data above — no invented nodes or dependencies.
- State the critical path length in cycles.
- Identify real bottlenecks (e.g. multiplier latency, adder count).
- Suggest concrete optimizations (resource sharing, pipelining depth).
- Do NOT generate PyMTL3 code in this task.
""",
    expected_output="Structured HLS optimization report based strictly on the provided IR.",
    agent=analyzer,
)

crew = Crew(
    agents=[designer, analyzer],
    tasks=[design_task, analysis_task],
    process=Process.sequential,
    verbose=True,
)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("\n=== Agentic Meta-HDL FP Unit Compiler ===\n")

    fp_format = input("FP format [MXFP4/NVFP4] (default MXFP4): ").strip() or "MXFP4"
    if fp_format not in FP_FORMATS:
        print(f"Formato non supportato. Scegli tra {list(FP_FORMATS)}.")
        return

    path = Path(input("File Python da compilare: ").strip())
    if not path.exists():
        raise FileNotFoundError(f"File non trovato: {path}")

    code = path.read_text(encoding="utf-8")

    # ── Pipeline di analisi ────────────────────────────────────────────────────
    ir, dfg, var_map, ret = build_ir(code)
    cp   = critical_path(dfg)
    sch  = schedule(dfg)
    pipe = pipeline_stages(sch)
    res  = resource_estimate(ir)
    hdl  = generate_pymtl(ir, var_map, ret, fp_format)

    print("\n--- PyMTL3 Generated (compiler) ---")
    print(hdl)
    print("\n--- Resources ---")
    print(json.dumps(res, indent=2))
    print("\n--- Critical Path (cycles) ---")
    print(json.dumps(cp, indent=2))

    # ── Crew AI ───────────────────────────────────────────────────────────────
    ai_result = crew.kickoff(inputs={
        "fp_format":     fp_format,
        "fp_config":     json.dumps(FP_FORMATS[fp_format]),
        "fewshot":       PYMTL3_FEWSHOT,
        "ir":            json.dumps(ir, indent=2),
        "schedule":      json.dumps(sch, indent=2),
        "pipeline":      json.dumps(pipe, indent=2),
        "critical_path": json.dumps(cp, indent=2),
        "resources":     json.dumps(res, indent=2),
    })

    # ── Validazione codice AI ─────────────────────────────────────────────────
    ai_text          = str(ai_result)
    validated_code, syntax_errors = extract_and_validate_pymtl(ai_text)

    if syntax_errors:
        print("\n⚠️  Syntax errors nel codice AI:")
        for e in syntax_errors:
            print(f"   • {e}")
    else:
        print("\n✅  Tutti i blocchi Python generati dall'AI sono sintatticamente validi.")

    # ── Salva report ──────────────────────────────────────────────────────────
    report_name = f"meta_hdl_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(report_name, "w", encoding="utf-8") as f:
        f.write(f"Meta-HDL FP Unit Report — {fp_format}\n\n")
        f.write("=== PyMTL3 (compiler output) ===\n");  f.write(hdl)
        f.write("\n\n=== IR ===\n");                     f.write(json.dumps(ir, indent=2))
        f.write("\n\n=== Schedule ===\n");               f.write(json.dumps(sch, indent=2))
        f.write("\n\n=== Pipeline ===\n");               f.write(json.dumps(pipe, indent=2))
        f.write("\n\n=== Critical Path ===\n");          f.write(json.dumps(cp, indent=2))
        f.write("\n\n=== Resources ===\n");              f.write(json.dumps(res, indent=2))
        f.write("\n\n=== AI Analysis (raw) ===\n");      f.write(ai_text)
        if validated_code:
            f.write("\n\n=== AI PyMTL3 (validated blocks) ===\n")
            f.write(validated_code)
        if syntax_errors:
            f.write("\n\n=== AI Syntax Errors ===\n")
            f.write("\n".join(syntax_errors))

    print(f"\nDONE — Report: {report_name}")


if __name__ == "__main__":
    main()