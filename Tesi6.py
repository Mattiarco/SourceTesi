from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx
from typing import Dict, List
from pathlib import Path
from datetime import datetime


# =========================================================
# 1. IR BUILDER
# =========================================================

class IRBuilder(ast.NodeVisitor):

    def __init__(self):
        self.ir = []
        self.graph = nx.DiGraph()
        self.op_id = 0

    def new_op(self):
        self.op_id += 1
        return f"op_{self.op_id}"

    def add_ir_node(
        self,
        op_type,
        operation,
        inputs,
        latency=1,
        datatype="int32",
        pipelineable=True
    ):

        op = self.new_op()

        ir_node = {
            "id": op,
            "type": op_type,
            "operation": operation,
            "inputs": inputs,
            "output": op,
            "latency": latency,
            "datatype": datatype,
            "pipelineable": pipelineable
        }

        self.ir.append(ir_node)

        self.graph.add_node(op, **ir_node)

        for inp in inputs:
            self.graph.add_edge(inp, op)

        return op

    # -----------------------------------------------------
    # FUNCTION
    # -----------------------------------------------------

    def visit_FunctionDef(self, node):
        self.generic_visit(node)

    # -----------------------------------------------------
    # ASSIGN
    # -----------------------------------------------------

    def visit_Assign(self, node):

        if isinstance(node.value, ast.BinOp):

            left = self.extract_operand(node.value.left)
            right = self.extract_operand(node.value.right)

            op_name = type(node.value.op).__name__

            latency_table = {
                "Add": 1,
                "Sub": 1,
                "Mult": 2,
                "Div": 4,
                "Mod": 3
            }

            datatype = "int32"

            self.add_ir_node(
                op_type="binary_op",
                operation=op_name,
                inputs=[left, right],
                latency=latency_table.get(op_name, 1),
                datatype=datatype
            )

        self.generic_visit(node)

    # -----------------------------------------------------
    # RETURN
    # -----------------------------------------------------

    def visit_Return(self, node):

        if isinstance(node.value, ast.BinOp):

            left = self.extract_operand(node.value.left)
            right = self.extract_operand(node.value.right)

            op_name = type(node.value.op).__name__

            self.add_ir_node(
                op_type="binary_op",
                operation=op_name,
                inputs=[left, right],
                latency=1
            )

    # -----------------------------------------------------
    # IF SUPPORT
    # -----------------------------------------------------

    def visit_If(self, node):

        cond = ast.dump(node.test)

        self.add_ir_node(
            op_type="branch",
            operation="IF",
            inputs=[cond],
            latency=1
        )

        self.generic_visit(node)

    # -----------------------------------------------------
    # FOR SUPPORT
    # -----------------------------------------------------

    def visit_For(self, node):

        iterator = ast.dump(node.iter)

        self.add_ir_node(
            op_type="loop",
            operation="FOR",
            inputs=[iterator],
            latency=2
        )

        self.generic_visit(node)

    # -----------------------------------------------------
    # WHILE SUPPORT
    # -----------------------------------------------------

    def visit_While(self, node):

        cond = ast.dump(node.test)

        self.add_ir_node(
            op_type="loop",
            operation="WHILE",
            inputs=[cond],
            latency=2
        )

        self.generic_visit(node)

    # -----------------------------------------------------
    # FUNCTION CALL SUPPORT
    # -----------------------------------------------------

    def visit_Call(self, node):

        func_name = "unknown"

        if isinstance(node.func, ast.Name):
            func_name = node.func.id

        args = []

        for arg in node.args:
            args.append(ast.dump(arg))

        self.add_ir_node(
            op_type="call",
            operation=func_name,
            inputs=args,
            latency=3
        )

        self.generic_visit(node)

    # -----------------------------------------------------
    # OPERAND EXTRACTION
    # -----------------------------------------------------

    def extract_operand(self, node):

        if isinstance(node, ast.Name):
            return node.id

        elif isinstance(node, ast.Constant):
            return str(node.value)

        elif isinstance(node, ast.BinOp):
            return ast.dump(node)

        return "tmp"


# =========================================================
# 2. BUILD IR + DFG
# =========================================================


def build_ir_and_dfg(code: str):

    tree = ast.parse(code)

    builder = IRBuilder()
    builder.visit(tree)

    return builder.ir, builder.graph


# =========================================================
# 3. CRITICAL PATH
# =========================================================


def compute_critical_path(graph: nx.DiGraph):

    longest = {}

    for node in nx.topological_sort(graph):

        preds = list(graph.predecessors(node))

        if not preds:
            longest[node] = 0

        else:
            max_pred = max(longest[p] for p in preds)
            latency = graph.nodes[node].get("latency", 1)
            longest[node] = max_pred + latency

    return longest


# =========================================================
# 4. RESOURCE-CONSTRAINED SCHEDULING
# =========================================================


def resource_constrained_schedule(graph, resources):

    schedule = {}
    resource_usage = {}

    for node in nx.topological_sort(graph):

        preds = list(graph.predecessors(node))

        earliest = 0

        if preds:
            earliest = max(schedule[p] for p in preds) + 1

        op_type = graph.nodes[node].get("operation", "Add")

        cycle = earliest

        while True:

            if cycle not in resource_usage:
                resource_usage[cycle] = {}

            used = resource_usage[cycle].get(op_type, 0)
            limit = resources.get(op_type, 1)

            if used < limit:
                break

            cycle += 1

        schedule[node] = cycle

        resource_usage[cycle][op_type] = \
            resource_usage[cycle].get(op_type, 0) + 1

    return schedule


# =========================================================
# 5. PIPELINE EXTRACTION
# =========================================================


def extract_pipeline_stages(schedule):

    pipeline = {}

    for op, cycle in schedule.items():

        if cycle not in pipeline:
            pipeline[cycle] = []

        pipeline[cycle].append(op)

    return pipeline


# =========================================================
# 6. PYMTL GENERATOR
# =========================================================


def generate_pymtl(ir):

    lines = []

    lines.append("from pymtl3 import *")
    lines.append("")
    lines.append("class GeneratedAccelerator(Component):")
    lines.append("")
    lines.append("    def construct(s):")
    lines.append("")

    lines.append("        s.in0 = InPort(32)")
    lines.append("        s.in1 = InPort(32)")
    lines.append("        s.out = OutPort(32)")
    lines.append("")

    for node in ir:
        lines.append(f"        s.{node['id']} = Wire(32)")

    lines.append("")
    lines.append("        @update")
    lines.append("        def compute():")
    lines.append("")

    for node in ir:

        if node["type"] != "binary_op":
            continue

        op = node["operation"]

        lhs = node["inputs"][0]
        rhs = node["inputs"][1]

        if lhs == "a":
            lhs = "s.in0"

        if rhs == "b":
            rhs = "s.in1"

        target = f"s.{node['id']}"

        if op == "Add":
            expr = f"{lhs} + {rhs}"

        elif op == "Sub":
            expr = f"{lhs} - {rhs}"

        elif op == "Mult":
            expr = f"{lhs} * {rhs}"

        elif op == "Div":
            expr = f"{lhs} // {rhs}"

        else:
            expr = "0"

        lines.append(f"            {target} @= {expr}")

    if ir:
        lines.append(f"            s.out @= s.{ir[-1]['id']}")

    return "\n".join(lines)


# =========================================================
# 7. REPORT GENERATION
# =========================================================


def generate_txt_report(
    input_file,
    ir,
    critical_path,
    schedule,
    pipeline,
    pymtl_code,
    ai_result
):

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    report_name = f"HLS_Report_{timestamp}.txt"

    with open(report_name, "w", encoding="utf-8") as f:

        f.write("=" * 80 + "\n")
        f.write("ADVANCED AI-HLS COMPILER REPORT\n")
        f.write("=" * 80 + "\n\n")

        f.write(f"INPUT FILE: {input_file}\n\n")

        f.write("=" * 80 + "\n")
        f.write("IR\n")
        f.write("=" * 80 + "\n")
        f.write(json.dumps(ir, indent=2))
        f.write("\n\n")

        f.write("=" * 80 + "\n")
        f.write("CRITICAL PATH\n")
        f.write("=" * 80 + "\n")
        f.write(json.dumps(critical_path, indent=2))
        f.write("\n\n")

        f.write("=" * 80 + "\n")
        f.write("SCHEDULE\n")
        f.write("=" * 80 + "\n")
        f.write(json.dumps(schedule, indent=2))
        f.write("\n\n")

        f.write("=" * 80 + "\n")
        f.write("PIPELINE\n")
        f.write("=" * 80 + "\n")
        f.write(json.dumps(pipeline, indent=2))
        f.write("\n\n")

        f.write("=" * 80 + "\n")
        f.write("GENERATED PYMTL\n")
        f.write("=" * 80 + "\n")
        f.write(pymtl_code)
        f.write("\n\n")

        f.write("=" * 80 + "\n")
        f.write("AI ANALYSIS\n")
        f.write("=" * 80 + "\n")
        f.write(str(ai_result))
        f.write("\n\n")

    return report_name


# =========================================================
# 8. LLM
# =========================================================

llm = "ollama/qwen2.5-coder"


# =========================================================
# 9. AGENTS
# =========================================================

ir_analyzer = Agent(
    role="IR Analyzer",
    goal="Analyze IR and DFG",
    backstory="Compiler IR expert",
    llm=llm,
    verbose=True
)

hardware_architect = Agent(
    role="Hardware Architect",
    goal="Design hardware architecture",
    backstory="FPGA architect",
    llm=llm,
    verbose=True
)

meta_hdl_generator = Agent(
    role="Meta-HDL Generator",
    goal="Generate PyMTL hardware",
    backstory="PyMTL expert",
    llm=llm,
    verbose=True
)

verification_agent = Agent(
    role="Verification Engineer",
    goal="Validate hardware behavior",
    backstory="Hardware verification expert",
    llm=llm,
    verbose=True
)


# =========================================================
# 10. TASKS
# =========================================================

analyze_task = Task(
    description="""
Analyze:
- dependencies
- bottlenecks
- scheduling
- parallelism
""",
    expected_output="IR analysis",
    agent=ir_analyzer
)

architecture_task = Task(
    description="""
Generate architecture specification.
Include:
- datapath
- pipeline
- FSM
- resource allocation
""",
    expected_output="Architecture spec",
    agent=hardware_architect
)

meta_hdl_task = Task(
    description="""
Generate synthesizable PyMTL model.
""",
    expected_output="PyMTL model",
    agent=meta_hdl_generator
)

verification_task = Task(
    description="""
Generate verification strategy.
""",
    expected_output="Verification report",
    agent=verification_agent
)


# =========================================================
# 11. CREW
# =========================================================

crew = Crew(
    agents=[
        ir_analyzer,
        hardware_architect,
        meta_hdl_generator,
        verification_agent
    ],

    tasks=[
        analyze_task,
        architecture_task,
        meta_hdl_task,
        verification_task
    ],

    process=Process.sequential,
    verbose=True
)


# =========================================================
# 12. INPUT FILE DA TERMINALE
# =========================================================

print("\n=== ADVANCED AI-HLS COMPILER ===\n")

python_file = input("Inserisci il percorso del file Python: ").strip()

path = Path(python_file)

if not path.exists():
    print("\nERRORE: file non trovato.")
    exit()

if path.suffix != ".py":
    print("\nERRORE: il file deve essere .py")
    exit()


# =========================================================
# 13. LOAD PYTHON SOURCE
# =========================================================

with open(path, "r", encoding="utf-8") as f:
    python_code = f.read()

print("\n=== PYTHON SOURCE LOADED ===\n")
print(python_code)


# =========================================================
# 14. FRONTEND COMPILATION
# =========================================================

ir, dfg = build_ir_and_dfg(python_code)

critical_path = compute_critical_path(dfg)

resources = {
    "Add": 2,
    "Sub": 2,
    "Mult": 1,
    "Div": 1,
    "IF": 1,
    "FOR": 1,
    "WHILE": 1
}

schedule = resource_constrained_schedule(
    dfg,
    resources
)

pipeline = extract_pipeline_stages(schedule)


# =========================================================
# 15. META-HDL GENERATION
# =========================================================

pymtl_code = generate_pymtl(ir)


# =========================================================
# 16. DEBUG OUTPUT
# =========================================================

print("\n=== IR ===")
print(json.dumps(ir, indent=2))

print("\n=== CRITICAL PATH ===")
print(json.dumps(critical_path, indent=2))

print("\n=== SCHEDULE ===")
print(json.dumps(schedule, indent=2))

print("\n=== PIPELINE ===")
print(json.dumps(pipeline, indent=2))

print("\n=== GENERATED PYMTL ===")
print(pymtl_code)


# =========================================================
# 17. AI COMPILER PIPELINE
# =========================================================

result = crew.kickoff(inputs={

    "ir": json.dumps(ir),

    "dfg": str(nx.node_link_data(dfg)),

    "critical_path": json.dumps(critical_path),

    "schedule": json.dumps(schedule),

    "pipeline": json.dumps(pipeline),

    "pymtl_code": pymtl_code
})


# =========================================================
# 18. GENERATE TXT REPORT
# =========================================================

report_file = generate_txt_report(
    input_file=python_file,
    ir=ir,
    critical_path=critical_path,
    schedule=schedule,
    pipeline=pipeline,
    pymtl_code=pymtl_code,
    ai_result=result
)


# =========================================================
# 19. FINAL OUTPUT
# =========================================================

print("\n=== FINAL RESULT ===\n")
print(result)

print(f"\nTXT report salvato in: {report_file}")


