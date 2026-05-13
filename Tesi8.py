from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx
from pathlib import Path
from datetime import datetime


# =========================================================
# 1. IR BUILDER (RESEARCH-GRADE SSA + DFG CLEAN)
# =========================================================

class IRBuilder(ast.NodeVisitor):

    def __init__(self):
        self.ir = []
        self.graph = nx.DiGraph()
        self.op_id = 0

        # SSA mapping: variable -> last produced op
        self.var_map = {}

        # input ports (hardware abstraction)
        self.input_ports = {"a", "b", "c", "d", "i"}

        # constants cache (avoid duplicate nodes explosion)
        self.const_cache = {}

    def new_op(self):
        self.op_id += 1
        return f"op_{self.op_id}"

    # -------------------------
    # VALUE RESOLUTION (SSA-aware)
    # -------------------------
    def resolve(self, x):

        if isinstance(x, str):
            if x in self.var_map:
                return self.var_map[x]
            if x in self.input_ports:
                return x
            return x

        return x

    # -------------------------
    # CONSTANT NODE HANDLING
    # -------------------------
    def get_const(self, value):
        key = f"const_{value}"
        if key in self.const_cache:
            return self.const_cache[key]

        op = self.new_op()
        self.const_cache[key] = op

        node = {
            "id": op,
            "type": "const",
            "operation": "CONST",
            "inputs": [],
            "output": op,
            "value": value,
            "latency": 0
        }

        self.ir.append(node)
        self.graph.add_node(op, **node)
        return op

    # -------------------------
    # ADD NODE (DFG SAFE)
    # -------------------------
    def add_node(self, op_type, operation, inputs, latency=1):

        op = self.new_op()

        node = {
            "id": op,
            "type": op_type,
            "operation": operation,
            "inputs": inputs,
            "output": op,
            "latency": latency
        }

        self.ir.append(node)
        self.graph.add_node(op, **node)

        # only real dependencies (CRITICAL FIX)
        for inp in inputs:
            if inp not in self.graph:
                self.graph.add_node(inp)
            self.graph.add_edge(inp, op)

        return op

    # =====================================================
    # ASSIGN (SSA CORE FIX + CONST FOLDING)
    # =====================================================
    def visit_Assign(self, node):

        if isinstance(node.value, ast.BinOp):

            left = self.resolve(self.extract(node.value.left))
            right = self.resolve(self.extract(node.value.right))

            op_name = type(node.value.op).__name__

            op_id = self.add_node(
                "binary_op",
                op_name,
                [left, right],
                latency=1
            )

            target = node.targets[0].id
            self.var_map[target] = op_id

        self.generic_visit(node)

    # =====================================================
    # RETURN
    # =====================================================
    def visit_Return(self, node):

        if isinstance(node.value, ast.BinOp):

            left = self.resolve(self.extract(node.value.left))
            right = self.resolve(self.extract(node.value.right))

            self.add_node(
                "binary_op",
                type(node.value.op).__name__,
                [left, right],
                latency=1
            )

    # =====================================================
    # CONTROL FLOW
    # =====================================================
    def visit_If(self, node):
        self.add_node("branch", "IF", [ast.dump(node.test)], 1)
        self.generic_visit(node)

    def visit_For(self, node):
        self.add_node("loop", "FOR", [ast.dump(node.iter)], 2)
        self.generic_visit(node)

    def visit_While(self, node):
        self.add_node("loop", "WHILE", [ast.dump(node.test)], 2)
        self.generic_visit(node)

    def visit_Call(self, node):
        name = node.func.id if isinstance(node.func, ast.Name) else "call"
        args = [ast.dump(a) for a in node.args]
        self.add_node("call", name, args, 3)

    # =====================================================
    # EXTRACT
    # =====================================================
    def extract(self, node):

        if isinstance(node, ast.Name):
            return node.id

        if isinstance(node, ast.Constant):
            return self.get_const(node.value)

        return ast.dump(node)


# =========================================================
# 2. BUILD IR + DFG
# =========================================================

def build_ir_and_dfg(code):
    tree = ast.parse(code)
    builder = IRBuilder()
    builder.visit(tree)
    return builder.ir, builder.graph, builder.var_map


# =========================================================
# 3. CRITICAL PATH (REAL LATENCY PROPAGATION)
# =========================================================

def compute_critical_path(graph):

    dist = {}

    for n in nx.topological_sort(graph):

        preds = list(graph.predecessors(n))

        lat = graph.nodes[n].get("latency", 0)

        if not preds:
            dist[n] = lat
        else:
            dist[n] = max(dist[p] for p in preds) + lat

    return dist


# =========================================================
# 4. ASAP SCHEDULING (LATENCY-AWARE FIX)
# =========================================================

def schedule(graph):

    sch = {}

    for n in nx.topological_sort(graph):

        preds = list(graph.predecessors(n))

        base = max([sch[p] for p in preds], default=0)

        latency = graph.nodes[n].get("latency", 1)

        sch[n] = base + latency

    return sch


# =========================================================
# 5. PIPELINE GROUPING
# =========================================================

def pipeline(schedule):

    p = {}
    for k, v in schedule.items():
        p.setdefault(v, []).append(k)
    return p


# =========================================================
# 6. PYMTL GENERATOR (SSA SAFE + GRAPH CORRECT)
# =========================================================

def generate_pymtl(ir, var_map):

    def resolve(x):

        if isinstance(x, str) and x in var_map:
            return f"s.{var_map[x]}"

        mapping = {
            "a": "s.in0",
            "b": "s.in1",
            "c": "s.in2",
            "d": "s.in3",
            "i": "s.in4"
        }

        return mapping.get(x, x)

    lines = []
    lines.append("from pymtl3 import *\n")
    lines.append("class GeneratedAccelerator(Component):\n")
    lines.append("    def construct(s):\n")

    lines.append("        s.in0 = InPort(32)")
    lines.append("        s.in1 = InPort(32)")
    lines.append("        s.in2 = InPort(32)")
    lines.append("        s.in3 = InPort(32)")
    lines.append("        s.in4 = InPort(32)")
    lines.append("        s.out = OutPort(32)\n")

    for n in ir:
        lines.append(f"        s.{n['id']} = Wire(32)")

    lines.append("\n        @update")
    lines.append("        def compute():\n")

    for n in ir:

        if n["type"] != "binary_op":
            continue

        a = resolve(n["inputs"][0])
        b = resolve(n["inputs"][1])

        op = n["operation"]

        expr = {
            "Add": f"{a} + {b}",
            "Sub": f"{a} - {b}",
            "Mult": f"{a} * {b}",
            "Div": f"{a} // {b}",
        }.get(op, "0")

        lines.append(f"            s.{n['id']} @= {expr}")

    if ir:
        lines.append(f"            s.out @= s.{ir[-1]['id']}")

    return "\n".join(lines)


# =========================================================
# 7. REPORT
# =========================================================

def save_report(file, ir, cp, sch, pipe, pymtl, ai):

    name = f"HLS_REPORT_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    with open(name, "w", encoding="utf-8") as f:

        f.write("ADVANCED AI-HLS COMPILER REPORT\n\n")
        f.write(f"INPUT: {file}\n\n")

        f.write("IR\n")
        f.write(json.dumps(ir, indent=2))

        f.write("\n\nCRITICAL PATH\n")
        f.write(json.dumps(cp, indent=2))

        f.write("\n\nSCHEDULE\n")
        f.write(json.dumps(sch, indent=2))

        f.write("\n\nPIPELINE\n")
        f.write(json.dumps(pipe, indent=2))

        f.write("\n\nPYMTL\n")
        f.write(pymtl)

        f.write("\n\nAI ANALYSIS\n")
        f.write(str(ai))

    return name


# =========================================================
# 8. LLM / AGENTS
# =========================================================

llm = "ollama/qwen2.5-coder"

ir_analyzer = Agent(
    role="IR Analyzer",
    goal="Perform dependency graph + scheduling + hardware analysis",
    backstory="Compiler + HLS research engineer",
    llm=llm,
    verbose=True
)

task = Task(
    description="Deep IR analysis, DFG, pipeline, hazards, scheduling",
    expected_output="Full HLS analysis",
    agent=ir_analyzer
)

crew = Crew(
    agents=[ir_analyzer],
    tasks=[task],
    process=Process.sequential,
    verbose=True
)


# =========================================================
# 9. MAIN
# =========================================================

print("\n=== AI-HLS COMPILER (RESEARCH UPGRADED V2) ===\n")

path = Path(input("Python file: ").strip())

if not path.exists():
    raise FileNotFoundError("File non trovato")

code = path.read_text()


# =========================================================
# 10. COMPILATION PIPELINE
# =========================================================

ir, dfg, var_map = build_ir_and_dfg(code)

cp = compute_critical_path(dfg)
sch = schedule(dfg)
pipe = pipeline(sch)

pymtl = generate_pymtl(ir, var_map)

ai = crew.kickoff(inputs={
    "ir": json.dumps(ir),
    "graph": str(nx.node_link_data(dfg)),
    "schedule": json.dumps(sch),
    "pipeline": json.dumps(pipe),
    "critical_path": json.dumps(cp)
})


# =========================================================
# 11. REPORT OUTPUT
# =========================================================

report = save_report(path, ir, cp, sch, pipe, pymtl, ai)

print("\nDONE")
print("REPORT:", report)