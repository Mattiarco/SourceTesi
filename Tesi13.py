"""
Agentic AI solution for Meta-HDL floating-point arithmetic units.
Target: MXFP4 / NVFP4 arithmetic units for coprocessor integration.
Ref: "An agentic-driven approach for Meta-HDL"
"""

from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx

# ── Latenze hardware per operazioni FP ridotte (MXFP4/NVFP4) ──────────────────
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

# Formati floating-point supportati e loro configurazione bit
FP_FORMATS = {
    "MXFP4": {"exp_bits": 2, "man_bits": 1, "total_bits": 4},
    "NVFP4": {"exp_bits": 2, "man_bits": 1, "total_bits": 4},
}


# ── IR Builder: converte Python in IR + DFG ───────────────────────────────────
class IRBuilder(ast.NodeVisitor):
    """Trasforma codice Python in Intermediate Representation e Data Flow Graph."""

    def __init__(self):
        self.ir        = []
        self.graph     = nx.DiGraph()
        self.op_id     = 0
        self.var_map   = {}
        self.input_ports   = {"a", "b", "c", "d"}
        self.const_cache   = {}
        self.return_value  = None

    def _new_id(self):
        self.op_id += 1
        return f"op_{self.op_id}"

    def _resolve(self, name):
        if isinstance(name, str):
            if name.startswith("op_") or name in self.input_ports:
                return name
            if name in self.var_map:
                return self.var_map[name]
        return name

    # ── Nodo costante (con cache) ──────────────────────────────────────────────
    def _const(self, value):
        key = f"const_{value}"
        if key in self.const_cache:
            return self.const_cache[key]
        oid = self._new_id()
        self.const_cache[key] = oid
        node = {"id": oid, "type": "const", "operation": "CONST",
                "inputs": [], "output": oid, "value": value, "latency": 0}
        self.ir.append(node)
        self.graph.add_node(oid, **node)
        return oid

    # ── Nodo generico ─────────────────────────────────────────────────────────
    def _add_node(self, op_type, operation, inputs, latency=1, extra=None):
        oid = self._new_id()
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

    # ── Estrazione espressioni ─────────────────────────────────────────────────
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
    """Ritorna (ir, graph, var_map, return_value) dal codice Python."""
    builder = IRBuilder()
    builder.visit(ast.parse(code))
    return builder.ir, builder.graph, builder.var_map, builder.return_value


# ── Analisi del grafo ──────────────────────────────────────────────────────────
def critical_path(graph: nx.DiGraph) -> dict:
    """Calcola il percorso critico (latenza massima) nel DFG."""
    dist = {}
    for n in nx.topological_sort(graph):
        lat   = graph.nodes[n].get("latency", 0)
        preds = list(graph.predecessors(n))
        dist[n] = lat + (max(dist[p] for p in preds) if preds else 0)
    return dist

def schedule(graph: nx.DiGraph) -> dict:
    """Assegna ogni nodo al suo ciclo di clock minimo."""
    sch = {}
    for n in nx.topological_sort(graph):
        preds  = list(graph.predecessors(n))
        base   = max((sch[p] for p in preds), default=0)
        sch[n] = base + graph.nodes[n].get("latency", 0)
    return sch

def pipeline_stages(sch: dict) -> dict:
    """Raggruppa i nodi per stage di pipeline."""
    stages = {}
    for node, stage in sch.items():
        stages.setdefault(stage, []).append(node)
    return stages

def resource_estimate(ir: list) -> dict:
    """Stima le risorse hardware necessarie."""
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


# ── Generazione Meta-HDL (PyMTL3) ────────────────────────────────────────────
def generate_pymtl(ir: list, var_map: dict,
                   return_value, fp_format: str = "MXFP4") -> str:
    """Genera un componente PyMTL3 per l'unità aritmetica FP specificata."""
    fmt  = FP_FORMATS.get(fp_format, FP_FORMATS["MXFP4"])
    bits = fmt["total_bits"]

    PORT_MAP = {"a": "s.in0", "b": "s.in1", "c": "s.in2",
                "d": "s.in3"}

    def resolve(x):
        if isinstance(x, str):
            if x.startswith("op_"):  return f"s.{x}"
            if x in PORT_MAP:        return PORT_MAP[x]
            mapped = var_map.get(x)
            if mapped and isinstance(mapped, str) and mapped.startswith("op_"):
                return f"s.{mapped}"
        return str(x)

    OP_EXPR = {
        "Add": "{a} + {b}", "Sub": "{a} - {b}",
        "Mult": "{a} * {b}", "Div": "{a} // {b}",
        "Gt": "{a} > {b}", "Lt": "{a} < {b}",
        "Eq": "{a} == {b}", "NotEq": "{a} != {b}",
    }

    lines = [
        "from pymtl3 import *\n",
        f"# Arithmetic unit for {fp_format} "
        f"(exp={fmt['exp_bits']}b, man={fmt['man_bits']}b)\n",
        f"class {fp_format}ArithUnit(Component):",
        "    def construct(s):",
        f"        # Input ports ({bits}-bit {fp_format})",
        *[f"        s.in{i} = InPort({bits})" for i in range(4)],
        f"        s.out  = OutPort({bits})\n",
        *[f"        s.{n['id']} = Wire({'1' if n['type']=='compare' else bits})"
          for n in ir],
        "\n        @update",
        "        def compute():",
    ]

    for node in ir:
        oid = node["id"]
        if node["type"] == "const":
            lines.append(f"            s.{oid} @= {node['value']}")
        elif node["type"] in ("binary_op", "compare") and node["operation"] in OP_EXPR:
            a = resolve(node["inputs"][0])
            b = resolve(node["inputs"][1])
            expr = OP_EXPR[node["operation"]].format(a=a, b=b)
            lines.append(f"            s.{oid} @= {expr}")

    if return_value:
        lines.append(f"\n            s.out @= {resolve(return_value)}")

    return "\n".join(lines)


# ── Agenti CrewAI ─────────────────────────────────────────────────────────────
LLM = "ollama/qwen2.5-coder"

designer = Agent(
    role="Meta-HDL FP Unit Designer",
    goal="Select the appropriate Meta-HDL language and implement a custom FP arithmetic unit for MXFP4/NVFP4.",
    backstory="Expert in floating-point hardware design and Meta-HDL frameworks (PyMTL3, Chisel, SpinalHDL).",
    llm=LLM,
    verbose=True,
)

analyzer = Agent(
    role="HLS Compiler Analyzer",
    goal="Analyze the IR and DFG to identify optimization opportunities for the FP unit.",
    backstory="Compiler backend specialist focused on high-level synthesis for arithmetic coprocessors.",
    llm=LLM,
    verbose=True,
)

design_task = Task(
    description="""
Design a {fp_format} arithmetic unit using Meta-HDL.

IR:
{ir}

SCHEDULE:
{schedule}

PIPELINE:
{pipeline}

RESOURCES:
{resources}

Rules:
- Use ONLY the provided IR data.
- Target {fp_format} format: {fp_config}.
- Select the best Meta-HDL language for this unit.
- Describe the RTL architecture and any pipelining strategy.
""",
    expected_output="RTL architecture description for the FP arithmetic unit.",
    agent=designer,
)

analysis_task = Task(
    description="""
Analyze the following compiler structures for HLS optimizations.

IR:
{ir}

CRITICAL PATH:
{critical_path}

RESOURCES:
{resources}

Rules:
- Use ONLY provided data.
- Identify bottlenecks and suggest optimizations for {fp_format} silicon area and latency.
""",
    expected_output="HLS optimization report for the FP arithmetic unit.",
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

    from pathlib import Path
    path = Path(input("File Python da compilare: ").strip())
    if not path.exists():
        raise FileNotFoundError(f"File non trovato: {path}")

    code = path.read_text(encoding="utf-8")

    # Analisi
    ir, dfg, var_map, ret = build_ir(code)
    cp   = critical_path(dfg)
    sch  = schedule(dfg)
    pipe = pipeline_stages(sch)
    res  = resource_estimate(ir)
    hdl  = generate_pymtl(ir, var_map, ret, fp_format)

    # Output locale
    print("\n--- PyMTL3 Generated ---")
    print(hdl)
    print("\n--- Resources ---")
    print(json.dumps(res, indent=2))
    print("\n--- Critical Path ---")
    print(json.dumps(cp, indent=2))

    # Crew AI
    ai_result = crew.kickoff(inputs={
        "fp_format":    fp_format,
        "fp_config":    json.dumps(FP_FORMATS[fp_format]),
        "ir":           json.dumps(ir, indent=2),
        "schedule":     json.dumps(sch, indent=2),
        "pipeline":     json.dumps(pipe, indent=2),
        "critical_path": json.dumps(cp, indent=2),
        "resources":    json.dumps(res, indent=2),
    })

    # Salva report
    from datetime import datetime
    report_name = f"meta_hdl_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(report_name, "w", encoding="utf-8") as f:
        f.write(f"Meta-HDL FP Unit Report — {fp_format}\n\n")
        f.write("=== PyMTL3 ===\n"); f.write(hdl)
        f.write("\n\n=== IR ===\n"); f.write(json.dumps(ir, indent=2))
        f.write("\n\n=== Schedule ===\n"); f.write(json.dumps(sch, indent=2))
        f.write("\n\n=== Pipeline ===\n"); f.write(json.dumps(pipe, indent=2))
        f.write("\n\n=== Critical Path ===\n"); f.write(json.dumps(cp, indent=2))
        f.write("\n\n=== Resources ===\n"); f.write(json.dumps(res, indent=2))
        f.write("\n\n=== AI Analysis ===\n"); f.write(str(ai_result))

    print(f"\nDONE — Report: {report_name}")


if __name__ == "__main__":
    main()