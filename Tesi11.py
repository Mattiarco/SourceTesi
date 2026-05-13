from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx
from pathlib import Path
from datetime import datetime


# =========================================================
# 1. IR BUILDER (ADVANCED SSA + CFG + DFG)
# =========================================================

class IRBuilder(ast.NodeVisitor):

    def __init__(self):

        self.ir = []

        # Main DFG
        self.graph = nx.DiGraph()

        # CFG
        self.cfg = nx.DiGraph()

        self.op_id = 0

        # SSA variable mapping
        self.var_map = {}

        # Input ports
        self.input_ports = {"a", "b", "c", "d"}

        # Const deduplication
        self.const_cache = {}

        # Return tracking
        self.return_value = None

        # Loop tracking
        self.loop_stack = []

        # Loop-carried dependencies
        self.loop_var_updates = {}

    # =====================================================
    # NEW OP ID
    # =====================================================

    def new_op(self):

        self.op_id += 1

        return f"op_{self.op_id}"

    # =====================================================
    # RESOLVE SSA
    # =====================================================

    def resolve(self, value):

        if isinstance(value, str):

            if value.startswith("op_"):
                return value

            if value in self.var_map:
                return self.var_map[value]

            if value in self.input_ports:
                return value

        return value

    # =====================================================
    # CONST HANDLING
    # =====================================================

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

    # =====================================================
    # ADD GENERIC NODE
    # =====================================================

    def add_node(
        self,
        op_type,
        operation,
        inputs,
        latency=1,
        extra=None
    ):

        op = self.new_op()

        node = {
            "id": op,
            "type": op_type,
            "operation": operation,
            "inputs": inputs,
            "output": op,
            "latency": latency
        }

        if extra:
            node.update(extra)

        self.ir.append(node)

        self.graph.add_node(op, **node)

        # DFG edges
        for inp in inputs:

            if isinstance(inp, str):

                self.graph.add_node(inp)

                self.graph.add_edge(inp, op)

        return op

    # =====================================================
    # COMPARE EXTRACTION
    # =====================================================

    def extract_compare(self, node):

        left = self.extract(node.left)

        right = self.extract(node.comparators[0])

        op_name = type(node.ops[0]).__name__

        return self.add_node(
            "compare",
            op_name,
            [left, right],
            latency=1
        )

    # =====================================================
    # EXTRACT
    # =====================================================

    def extract(self, node):

        # -------------------------
        # Variable
        # -------------------------

        if isinstance(node, ast.Name):

            return self.resolve(node.id)

        # -------------------------
        # Constant
        # -------------------------

        if isinstance(node, ast.Constant):

            return self.get_const(node.value)

        # -------------------------
        # Binary operation
        # -------------------------

        if isinstance(node, ast.BinOp):

            left = self.extract(node.left)

            right = self.extract(node.right)

            op_name = type(node.op).__name__

            return self.add_node(
                "binary_op",
                op_name,
                [left, right],
                latency=1
            )

        # -------------------------
        # Compare
        # -------------------------

        if isinstance(node, ast.Compare):

            return self.extract_compare(node)

        return ast.dump(node)

    # =====================================================
    # ASSIGN
    # =====================================================

    def visit_Assign(self, node):

        target = node.targets[0].id

        # ---------------------------------------------
        # Binary operation assignment
        # ---------------------------------------------

        if isinstance(node.value, ast.BinOp):

            left = self.extract(node.value.left)

            right = self.extract(node.value.right)

            op_name = type(node.value.op).__name__

            op_id = self.add_node(
                "binary_op",
                op_name,
                [left, right],
                latency=1
            )

            # -----------------------------------------
            # Loop-carried recurrence
            # -----------------------------------------

            if self.loop_stack:

                if target in self.loop_var_updates:

                    prev = self.loop_var_updates[target]

                    # REAL recurrence edge
                    self.graph.add_edge(prev, op_id)

                    # recurrence metadata
                    self.graph[prev][op_id]["recurrence"] = True

                self.loop_var_updates[target] = op_id

            self.var_map[target] = op_id

        # ---------------------------------------------
        # Simple assignment
        # ---------------------------------------------

        elif isinstance(node.value, ast.Name):

            self.var_map[target] = self.resolve(node.value.id)

        self.generic_visit(node)

    # =====================================================
    # RETURN
    # =====================================================

    def visit_Return(self, node):

        self.return_value = self.extract(node.value)

    # =====================================================
    # IF
    # =====================================================

    def visit_If(self, node):

        condition = self.extract_compare(node.test)

        branch_node = self.add_node(
            "branch",
            "IF",
            [condition],
            latency=1
        )

        # CFG edge
        self.cfg.add_node(branch_node)

        before_if = dict(self.var_map)

        # -----------------------------------------
        # Visit IF body
        # -----------------------------------------

        for stmt in node.body:
            self.visit(stmt)

        after_if = dict(self.var_map)

        # -----------------------------------------
        # PHI insertion
        # -----------------------------------------

        for var in before_if:

            old_val = before_if[var]

            new_val = after_if.get(var, old_val)

            if old_val != new_val:

                phi = self.add_node(
                    "phi",
                    "PHI",
                    [old_val, new_val],
                    latency=0
                )

                self.var_map[var] = phi

    # =====================================================
    # FOR LOOP
    # =====================================================

    def visit_For(self, node):

        loop_var = node.target.id

        loop_node = self.add_node(
            "loop",
            "FOR",
            [ast.dump(node.iter)],
            latency=2,
            extra={
                "trip_count": self.extract_trip_count(node.iter)
            }
        )

        self.loop_stack.append(loop_node)

        # Induction variable
        self.var_map[loop_var] = loop_var

        for stmt in node.body:
            self.visit(stmt)

        self.loop_stack.pop()

    # =====================================================
    # WHILE LOOP
    # =====================================================

    def visit_While(self, node):

        condition = self.extract_compare(node.test)

        loop_node = self.add_node(
            "loop",
            "WHILE",
            [condition],
            latency=2
        )

        self.loop_stack.append(loop_node)

        for stmt in node.body:
            self.visit(stmt)

        self.loop_stack.pop()

    # =====================================================
    # CALL
    # =====================================================

    def visit_Call(self, node):

        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else "call"
        )

        args = [self.extract(a) for a in node.args]

        self.add_node(
            "call",
            name,
            args,
            latency=3
        )

    # =====================================================
    # LOOP ANALYSIS
    # =====================================================

    def extract_trip_count(self, node):

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "range"
        ):

            if len(node.args) == 1:

                if isinstance(node.args[0], ast.Constant):

                    return node.args[0].value

        return None


# =========================================================
# 2. BUILD IR + DFG
# =========================================================

def build_ir_and_dfg(code):

    tree = ast.parse(code)

    builder = IRBuilder()

    builder.visit(tree)

    return (
        builder.ir,
        builder.graph,
        builder.var_map,
        builder.return_value,
        builder.cfg
    )


# =========================================================
# 3. RECURRENCE ANALYSIS
# =========================================================

def compute_recurrence_ii(graph):

    recurrence_edges = []

    for u, v, data in graph.edges(data=True):

        if data.get("recurrence", False):

            lat_u = graph.nodes[u].get("latency", 1)

            recurrence_edges.append({
                "from": u,
                "to": v,
                "latency": lat_u,
                "distance": 1,
                "recMII": lat_u
            })

    return recurrence_edges


# =========================================================
# 4. CRITICAL PATH (ACYCLIC ONLY)
# =========================================================

def compute_critical_path(graph):

    # Remove recurrence edges temporarily
    acyclic = nx.DiGraph()

    for u, v, data in graph.edges(data=True):

        if not data.get("recurrence", False):

            acyclic.add_edge(u, v)

    for n, attrs in graph.nodes(data=True):

        acyclic.add_node(n, **attrs)

    dist = {}

    nodes = list(nx.topological_sort(acyclic))

    for n in nodes:

        preds = list(acyclic.predecessors(n))

        lat = acyclic.nodes[n].get("latency", 0)

        if not preds:

            dist[n] = lat

        else:

            best = max(dist[p] for p in preds)

            dist[n] = best + lat

    return dist


# =========================================================
# 5. ASAP SCHEDULING
# =========================================================

def schedule(graph):

    # Remove recurrence edges
    acyclic = nx.DiGraph()

    for u, v, data in graph.edges(data=True):

        if not data.get("recurrence", False):

            acyclic.add_edge(u, v)

    for n, attrs in graph.nodes(data=True):

        acyclic.add_node(n, **attrs)

    sch = {}

    nodes = list(nx.topological_sort(acyclic))

    for n in nodes:

        preds = list(acyclic.predecessors(n))

        base = max(
            [sch.get(p, 0) for p in preds],
            default=0
        )

        lat = acyclic.nodes[n].get("latency", 1)

        sch[n] = base + lat

    return sch


# =========================================================
# 6. PIPELINE GROUPING
# =========================================================

def pipeline(schedule):

    p = {}

    for node, stage in schedule.items():

        p.setdefault(stage, []).append(node)

    return p


# =========================================================
# 7. PYMTL GENERATOR
# =========================================================

def generate_pymtl(ir, var_map, return_value):

    # =====================================================
    # RESOLVE
    # =====================================================

    def resolve(x):

        if isinstance(x, str):

            if x.startswith("op_"):
                return f"s.{x}"

            if x in var_map:

                mapped = var_map[x]

                if (
                    isinstance(mapped, str)
                    and mapped.startswith("op_")
                ):
                    return f"s.{mapped}"

            ports = {
                "a": "s.in0",
                "b": "s.in1",
                "c": "s.in2",
                "d": "s.in3",
                "i": "s.in4"
            }

            if x in ports:
                return ports[x]

        return str(x)

    # =====================================================
    # FILE
    # =====================================================

    lines = []

    lines.append("from pymtl3 import *\n")

    lines.append("class GeneratedAccelerator(Component):\n")

    lines.append("    def construct(s):\n")

    # Inputs
    lines.append("        s.in0 = InPort(32)")
    lines.append("        s.in1 = InPort(32)")
    lines.append("        s.in2 = InPort(32)")
    lines.append("        s.in3 = InPort(32)")
    lines.append("        s.in4 = InPort(32)")
    lines.append("        s.out = OutPort(32)\n")

    # Wires
    for node in ir:

        lines.append(
            f"        s.{node['id']} = Wire(32)"
        )

    lines.append("\n        @update")
    lines.append("        def compute():\n")

    # =====================================================
    # GENERATE LOGIC
    # =====================================================

    for node in ir:

        # -----------------------------------------
        # Binary ops
        # -----------------------------------------

        if node["type"] == "binary_op":

            a = resolve(node["inputs"][0])

            b = resolve(node["inputs"][1])

            expr = {
                "Add": f"{a} + {b}",
                "Sub": f"{a} - {b}",
                "Mult": f"{a} * {b}",
                "Div": f"{a} // {b}",
            }.get(node["operation"], "0")

            lines.append(
                f"            s.{node['id']} @= {expr}"
            )

        # -----------------------------------------
        # PHI node
        # -----------------------------------------

        elif node["type"] == "phi":

            a = resolve(node["inputs"][0])

            b = resolve(node["inputs"][1])

            # Placeholder mux
            lines.append(
                f"            s.{node['id']} @= {b}"
            )

        # -----------------------------------------
        # Compare
        # -----------------------------------------

        elif node["type"] == "compare":

            a = resolve(node["inputs"][0])

            b = resolve(node["inputs"][1])

            expr = {
                "Gt": f"{a} > {b}",
                "Lt": f"{a} < {b}",
                "Eq": f"{a} == {b}",
                "NotEq": f"{a} != {b}"
            }.get(node["operation"], "0")

            lines.append(
                f"            s.{node['id']} @= {expr}"
            )

    # Output
    if return_value:

        lines.append(
            f"\n            s.out @= {resolve(return_value)}"
        )

    return "\n".join(lines)


# =========================================================
# 8. REPORT
# =========================================================

def save_report(
    file,
    ir,
    cp,
    sch,
    pipe,
    pymtl,
    ai,
    recurrence
):

    name = (
        f"HLS_REPORT_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )

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

        f.write("\n\nRECURRENCE ANALYSIS\n")
        f.write(json.dumps(recurrence, indent=2))

        f.write("\n\nPYMTL\n")
        f.write(pymtl)

        f.write("\n\nAI ANALYSIS\n")
        f.write(str(ai))

    return name


# =========================================================
# 9. LLM CONFIG
# =========================================================

llm = "ollama/qwen2.5-coder"

ir_analyzer = Agent(
    role="Compiler IR Analyzer",
    goal=(
        "Analyze ONLY provided compiler structures"
    ),
    backstory=(
        "Advanced AI-HLS backend engine."
    ),
    llm=llm,
    verbose=True
)

task = Task(
    description="""

Analyze the following compiler structures.

IR:
{ir}

GRAPH:
{graph}

SCHEDULE:
{schedule}

PIPELINE:
{pipeline}

CRITICAL_PATH:
{critical_path}

RECURRENCE:
{recurrence}

STRICT RULES:
- Use ONLY provided data
- No invented nodes
- No invented dependencies
- No assumptions
- No hallucinations

Provide:
1. Dependency analysis
2. Critical path analysis
3. Recurrence analysis
4. Pipeline quality
5. HLS optimization opportunities

""",
    expected_output="Ground-truth compiler analysis",
    agent=ir_analyzer
)

crew = Crew(
    agents=[ir_analyzer],
    tasks=[task],
    process=Process.sequential,
    verbose=True
)


# =========================================================
# 10. MAIN
# =========================================================

print("\n=== ADVANCED AI-HLS COMPILER V5 ===\n")

path = Path(input("Python file: ").strip())

if not path.exists():
    raise FileNotFoundError("File non trovato")

code = path.read_text(encoding="utf-8")


# =========================================================
# 11. PIPELINE
# =========================================================

(
    ir,
    dfg,
    var_map,
    return_value,
    cfg
) = build_ir_and_dfg(code)

cp = compute_critical_path(dfg)

sch = schedule(dfg)

pipe = pipeline(sch)

recurrence = compute_recurrence_ii(dfg)

pymtl = generate_pymtl(
    ir,
    var_map,
    return_value
)


# =========================================================
# 12. AI ANALYSIS
# =========================================================

ai = crew.kickoff(
    inputs={
        "ir": json.dumps(ir, indent=2),
        "graph": json.dumps(
            nx.node_link_data(dfg),
            indent=2
        ),
        "schedule": json.dumps(sch, indent=2),
        "pipeline": json.dumps(pipe, indent=2),
        "critical_path": json.dumps(cp, indent=2),
        "recurrence": json.dumps(
            recurrence,
            indent=2
        )
    }
)


# =========================================================
# 13. OUTPUT
# =========================================================

report = save_report(
    path,
    ir,
    cp,
    sch,
    pipe,
    pymtl,
    ai,
    recurrence
)

print("\nDONE")
print("REPORT:", report)