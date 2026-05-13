from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx
from typing import Dict, List


# =========================================================
# 1. IR + DFG BUILDER
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

    def visit_FunctionDef(self, node):
        self.generic_visit(node)

    def visit_Assign(self, node):

        if isinstance(node.value, ast.BinOp):

            left = self.extract_operand(node.value.left)
            right = self.extract_operand(node.value.right)

            op_name = type(node.value.op).__name__

            latency_table = {
                "Add": 1,
                "Sub": 1,
                "Mult": 2,
                "Div": 4
            }

            datatype = "fp4" if "fp" in left or "fp" in right else "int32"

            self.add_ir_node(
                op_type="binary_op",
                operation=op_name,
                inputs=[left, right],
                latency=latency_table.get(op_name, 1),
                datatype=datatype
            )

        self.generic_visit(node)

    def visit_Return(self, node):

        if isinstance(node.value, ast.BinOp):

            left = self.extract_operand(node.value.left)
            right = self.extract_operand(node.value.right)

            op_name = type(node.value.op).__name__

            datatype = "fp4" if "fp" in left or "fp" in right else "int32"

            self.add_ir_node(
                op_type="binary_op",
                operation=op_name,
                inputs=[left, right],
                latency=1,
                datatype=datatype
            )

    def extract_operand(self, node):

        if isinstance(node, ast.Name):
            return node.id

        elif isinstance(node, ast.Constant):
            return str(node.value)

        return "tmp"


def build_ir_and_dfg(code: str):

    tree = ast.parse(code)

    builder = IRBuilder()
    builder.visit(tree)

    return builder.ir, builder.graph


# =========================================================
# 2. CRITICAL PATH ANALYSIS
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
# 3. RESOURCE-CONSTRAINED ASAP SCHEDULER
# =========================================================

def resource_constrained_schedule(
    graph,
    resources
):

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
# 4. PIPELINE STAGE EXTRACTION
# =========================================================

def extract_pipeline_stages(schedule):

    pipeline = {}

    for op, cycle in schedule.items():

        if cycle not in pipeline:
            pipeline[cycle] = []

        pipeline[cycle].append(op)

    return pipeline


# =========================================================
# 5. META-HDL GENERATION (PyMTL)
# =========================================================

def generate_pymtl(ir: List[Dict]):

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
# 6. FLOATING-POINT UNIT PLACEHOLDER
# =========================================================

def generate_fp4_adder():

    return """
module fp4_adder(
    input [3:0] a,
    input [3:0] b,
    output [3:0] out
);

assign out = a + b;

endmodule
"""


# =========================================================
# 7. RISC-V COPROCESSOR WRAPPER
# =========================================================

def generate_riscv_wrapper():

    return """
module riscv_accelerator_wrapper(

    input clk,
    input rst,

    input start,
    input [31:0] a,
    input [31:0] b,

    output done,
    output [31:0] result
);

wire [31:0] acc_out;

GeneratedAccelerator acc(
    .clk(clk),
    .rst(rst),
    .in0(a),
    .in1(b),
    .out(acc_out)
);

assign result = acc_out;
assign done = start;

endmodule
"""


# =========================================================
# 8. LLM MODEL
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

scheduler_agent = Agent(
    role="HLS Scheduler",
    goal="Optimize hardware scheduling",
    backstory="High-level synthesis expert",
    llm=llm,
    verbose=True
)

hardware_architect = Agent(
    role="Hardware Architect",
    goal="Design hardware datapath and pipeline",
    backstory="FPGA architect",
    llm=llm,
    verbose=True
)

floating_point_architect = Agent(
    role="Floating Point Architect",
    goal="Design FP4 arithmetic units",
    backstory="Low precision arithmetic expert",
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

hdl_generator = Agent(
    role="RTL Generator",
    goal="Generate synthesizable Verilog",
    backstory="RTL design expert",
    llm=llm,
    verbose=True
)

verification_agent = Agent(
    role="Verification Engineer",
    goal="Validate RTL and PyMTL behavior",
    backstory="Hardware verification expert",
    llm=llm,
    verbose=True
)

riscv_integrator = Agent(
    role="RISC-V Integrator",
    goal="Integrate accelerator into RISC-V system",
    backstory="SoC architect",
    llm=llm,
    verbose=True
)


# =========================================================
# 10. TASKS
# =========================================================

analyze_task = Task(
    description="""
Analyze the IR and DFG.

Focus on:
- dependencies
- bottlenecks
- instruction-level parallelism
- operation latencies
""",
    expected_output="IR analysis",
    agent=ir_analyzer
)

schedule_task = Task(
    description="""
Optimize hardware scheduling.

Include:
- ASAP scheduling
- resource-constrained scheduling
- latency balancing
- pipeline opportunities
""",
    expected_output="Optimized schedule",
    agent=scheduler_agent
)

architecture_task = Task(
    description="""
Design accelerator architecture.

Include:
- datapath
- FSM
- registers
- pipeline stages
- execution timing
""",
    expected_output="Hardware architecture",
    agent=hardware_architect
)

floating_point_task = Task(
    description="""
Design FP4 arithmetic units.

Include:
- mantissa
- exponent
- normalization
- rounding
""",
    expected_output="FP4 architecture",
    agent=floating_point_architect
)

meta_hdl_task = Task(
    description="""
Generate PyMTL hardware model.

Requirements:
- synthesizable
- modular
- cycle accurate
""",
    expected_output="PyMTL model",
    agent=meta_hdl_generator
)

hdl_task = Task(
    description="""
Generate Verilog RTL from PyMTL model.

Requirements:
- synthesizable
- preserve scheduling
- preserve pipeline stages
""",
    expected_output="Verilog RTL",
    agent=hdl_generator
)

verification_task = Task(
    description="""
Generate verification strategy.

Include:
- IR vs RTL equivalence
- test vectors
- timing validation
""",
    expected_output="Verification report",
    agent=verification_agent
)

riscv_task = Task(
    description="""
Integrate accelerator as RISC-V coprocessor.

Include:
- control interface
- register mapping
- accelerator invocation
""",
    expected_output="RISC-V integration spec",
    agent=riscv_integrator
)


# =========================================================
# 11. CREW
# =========================================================

crew = Crew(
    agents=[
        ir_analyzer,
        scheduler_agent,
        hardware_architect,
        floating_point_architect,
        meta_hdl_generator,
        hdl_generator,
        verification_agent,
        riscv_integrator
    ],

    tasks=[
        analyze_task,
        schedule_task,
        architecture_task,
        floating_point_task,
        meta_hdl_task,
        hdl_task,
        verification_task,
        riscv_task
    ],

    process=Process.sequential,
    verbose=True
)


# =========================================================
# 12. INPUT PYTHON CODE
# =========================================================

python_code = """
def accelerator(a, b):

    c = a + b
    d = c * b

    return d
"""


# =========================================================
# 13. COMPILER FRONTEND
# =========================================================

ir, dfg = build_ir_and_dfg(python_code)

critical_path = compute_critical_path(dfg)

resources = {
    "Add": 2,
    "Sub": 2,
    "Mult": 1,
    "Div": 1
}

schedule = resource_constrained_schedule(
    dfg,
    resources
)

pipeline = extract_pipeline_stages(schedule)


# =========================================================
# 14. META-HDL GENERATION
# =========================================================

pymtl_code = generate_pymtl(ir)

fp4_code = generate_fp4_adder()

riscv_wrapper = generate_riscv_wrapper()


# =========================================================
# 15. DEBUG OUTPUT
# =========================================================

print("\n=== IR ===")
print(json.dumps(ir, indent=2))

print("\n=== CRITICAL PATH ===")
print(json.dumps(critical_path, indent=2))

print("\n=== SCHEDULE ===")
print(json.dumps(schedule, indent=2))

print("\n=== PIPELINE ===")
print(json.dumps(pipeline, indent=2))

print("\n=== PYMTL GENERATED ===")
print(pymtl_code)

print("\n=== FP4 UNIT ===")
print(fp4_code)

print("\n=== RISC-V WRAPPER ===")
print(riscv_wrapper)


# =========================================================
# 16. AI COMPILER PIPELINE
# =========================================================

result = crew.kickoff(inputs={

    "ir": json.dumps(ir),

    "dfg": str(nx.node_link_data(dfg)),

    "critical_path": json.dumps(critical_path),

    "schedule": json.dumps(schedule),

    "pipeline": json.dumps(pipeline),

    "pymtl_code": pymtl_code,

    "fp4_unit": fp4_code,

    "riscv_wrapper": riscv_wrapper
})


# =========================================================
# 17. FINAL RESULT
# =========================================================

print("\n=== FINAL RESULT ===")
print(result)