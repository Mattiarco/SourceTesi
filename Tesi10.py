from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx
from pathlib import Path
from datetime import datetime


# =========================================================
# 1. IR BUILDER (RESEARCH-GRADE SSA + DFG)
# =========================================================

class IRBuilder(ast.NodeVisitor):

    def __init__(self):

        self.ir = []
        self.graph = nx.DiGraph()

        self.op_id = 0

        # SSA map
        self.var_map = {}

        # Inputs
        self.input_ports = {"a", "b", "c", "d"}

        # Const dedup
        self.const_cache = {}

        # Return tracking
        self.return_value = None

        # Loop tracking
        self.loop_stack = []

    # =====================================================
    # NEW OP
    # =====================================================

    def new_op(self):
        self.op_id += 1
        return f"op_{self.op_id}"

    # =====================================================
    # RESOLVE SSA VALUE
    # =====================================================

    def resolve(self, value):

        if isinstance(value, str):

            # Already SSA op
            if value.startswith("op_"):
                return value

            # Variable mapped to SSA
            if value in self.var_map:
                return self.var_map[value]

            # Input port
            if value in self.input_ports:
                return value

        return value

    # =====================================================
    # CONST DEDUP
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
    # GENERIC NODE INSERT
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

        # Dependency edges
        for inp in inputs:

            if isinstance(inp, str):

                self.graph.add_node(inp)
                self.graph.add_edge(inp, op)

        return op

    # =====================================================
    # EXTRACT
    # =====================================================

    def extract(self, node):

        if isinstance(node, ast.Name):
            return self.resolve(node.id)

        if isinstance(node, ast.Constant):
            return self.get_const(node.value)

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

        return ast.dump(node)

    # =====================================================
    # ASSIGN
    # =====================================================

    def visit_Assign(self, node):

        target = node.targets[0].id

        # -------------------------
        # Binary operation
        # -------------------------

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

            # Loop-carried dependency
            if self.loop_stack:

                previous = self.var_map.get(target)

                if previous:
                    self.graph.add_edge(previous, op_id)

            self.var_map[target] = op_id

        # -------------------------
        # Simple assign
        # -------------------------

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

        condition = ast.dump(node.test)

        branch_node = self.add_node(
            "branch",
            "IF",
            [condition],
            latency=1
        )

        # Save pre-branch SSA
        before_if = dict(self.var_map)

        # Visit body
        for stmt in node.body:
            self.visit(stmt)

        after_if = dict(self.var_map)

        # PHI generation
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

        # Loop descriptor
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

        # Create induction variable
        self.var_map[loop_var] = loop_var

        for stmt in node.body:
            self.visit(stmt)

        self.loop_stack.pop()

    # =====================================================
    # WHILE LOOP
    # =====================================================

    def visit_While(self, node):

        loop_node = self.add_node(
            "loop",
            "WHILE",
            [ast.dump(node.test)],
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

        name = node.func.id if isinstance(node.func, ast.Name) else "call"

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
        builder.return_value
    )


# =========================================================
# 3. CRITICAL PATH
# =========================================================

def compute_critical_path(graph):

    dist = {}

    try:
        nodes = list(nx.topological_sort(graph))
    except nx.NetworkXUnfeasible:

        # Cyclic graph (loop-carried deps)
        nodes = list(graph.nodes())

    for n in nodes:

        preds = list(graph.predecessors(n))

        lat = graph.nodes[n].get("latency", 0)

        if not preds:

            dist[n] = lat

        else:

            best = max(dist.get(p, 0) for p in preds)

            dist[n] = best + lat

    return dist


# =========================================================
# 4. ASAP SCHEDULING
# =========================================================

def schedule(graph):

    sch = {}

    try:
        nodes = list(nx.topological_sort(graph))
    except nx.NetworkXUnfeasible:
        nodes = list(graph.nodes())

    for n in nodes:

        preds = list(graph.predecessors(n))

        base = max(
            [sch.get(p, 0) for p in preds],
            default=0
        )

        lat = graph.nodes[n].get("latency", 1)

        sch[n] = base + lat

    return sch


# =========================================================
# 5. PIPELINE GROUPING
# =========================================================

def pipeline(schedule):

    p = {}

    for node, stage in schedule.items():

        p.setdefault(stage, []).append(node)

    return p


# =========================================================
# 6. PYMTL GENERATOR
# =========================================================

def generate_pymtl(ir, var_map, return_value):

    # =====================================================
    # SSA RESOLUTION
    # =====================================================

    def resolve(x):

        if isinstance(x, str):

            # SSA node
            if x.startswith("op_"):
                return f"s.{x}"

            # Variable mapping
            if x in var_map:

                mapped = var_map[x]

                if isinstance(mapped, str):

                    if mapped.startswith("op_"):
                        return f"s.{mapped}"

            # Inputs
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
    # FILE GENERATION
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

    # Logic
    for node in ir:

        if node["type"] != "binary_op":
            continue

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

    # Output
    if return_value:

        lines.append(
            f"\n            s.out @= {resolve(return_value)}"
        )

    return "\n".join(lines)


# =========================================================
# 7. REPORT
# =========================================================

def save_report(
    file,
    ir,
    cp,
    sch,
    pipe,
    pymtl,
    ai
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

        f.write("\n\nPYMTL\n")
        f.write(pymtl)

        f.write("\n\nAI ANALYSIS\n")
        f.write(str(ai))

    return name


# =========================================================
# 8. LLM CONFIG
# =========================================================

llm = "ollama/qwen2.5-coder"

ir_analyzer = Agent(
    role="Compiler IR Analyzer",
    goal=(
        "Analyze ONLY the provided IR, DFG, "
        "schedule and pipeline data"
    ),
    backstory=(
        "Advanced HLS compiler backend engine."
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

STRICT RULES:
- Use ONLY provided data
- No invented nodes
- No invented dependencies
- No assumptions
- No hallucinations

Provide:
1. Dependency analysis
2. Critical path analysis
3. Loop-carried dependency analysis
4. Pipeline quality
5. Potential HLS optimizations

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
# 9. MAIN
# =========================================================

print("\n=== ADVANCED AI-HLS COMPILER V4 ===\n")

path = Path(input("Python file: ").strip())

if not path.exists():
    raise FileNotFoundError("File non trovato")

code = path.read_text(encoding="utf-8")


# =========================================================
# 10. PIPELINE
# =========================================================

ir, dfg, var_map, return_value = build_ir_and_dfg(code)

cp = compute_critical_path(dfg)

sch = schedule(dfg)

pipe = pipeline(sch)

pymtl = generate_pymtl(
    ir,
    var_map,
    return_value
)


# =========================================================
# 11. AI ANALYSIS
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
        "critical_path": json.dumps(cp, indent=2)
    }
)


# =========================================================
# 12. OUTPUT
# =========================================================

report = save_report(
    path,
    ir,
    cp,
    sch,
    pipe,
    pymtl,
    ai
)

print("\nDONE")
print("REPORT:", report)