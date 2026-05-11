from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx
from collections import defaultdict


# =========================================================
# 1. AST → IR + DFG (DETERMINISTIC CORE)
# =========================================================

def build_ir_and_dfg(code: str):
    tree = ast.parse(code)

    ir = {
        "functions": [],
        "variables": set(),
        "operations": []
    }

    graph = nx.DiGraph()

    op_id = 0

    def new_op():
        nonlocal op_id
        op_id += 1
        return f"op_{op_id}"

    for node in ast.walk(tree):

        # FUNCTIONS
        if isinstance(node, ast.FunctionDef):
            ir["functions"].append(node.name)

        # VARIABLES
        if isinstance(node, ast.Name):
            ir["variables"].add(node.id)

        # ASSIGNMENTS
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    op = new_op()

                    ir["operations"].append({
                        "id": op,
                        "type": "assign",
                        "dest": target.id
                    })

                    graph.add_node(op, type="assign", dest=target.id)

        # BINARY OPS
        if isinstance(node, ast.BinOp):
            op = new_op()

            ir["operations"].append({
                "id": op,
                "type": "binary_op",
                "op": type(node.op).__name__
            })

            graph.add_node(op, type="binary_op", op=type(node.op).__name__)

    ir["variables"] = list(ir["variables"])

    return ir, graph


# =========================================================
# 2. LLM MODEL
# =========================================================

llm = "ollama/qwen2.5-coder"


# =========================================================
# 3. AGENTS (ORA WORKFLOW HARDWARE-ORIENTED)
# =========================================================

ir_analyzer = Agent(
    role="IR & DFG Analyzer",
    goal="Analyze IR and Data Flow Graph for hardware synthesis",
    backstory="Expert in compiler IR and hardware mapping",
    llm=llm,
    verbose=True
)

hardware_architect = Agent(
    role="Hardware Architect",
    goal="Design datapath, control logic, FSM from IR",
    backstory="FPGA architect expert",
    llm=llm,
    verbose=True
)

hdl_generator = Agent(
    role="RTL Generator",
    goal="Generate synthesizable Verilog from architecture",
    backstory="RTL design expert",
    llm=llm,
    verbose=True
)

verification_agent = Agent(
    role="Verification Engineer",
    goal="Validate RTL against IR behavior",
    backstory="Digital verification expert",
    llm=llm,
    verbose=True
)


# =========================================================
# 4. TASKS (IR + GRAPH INPUT)
# =========================================================

analyze_task = Task(
    description="""
You are given:

IR:
{ir}

DATA FLOW GRAPH:
{dfg}

Analyze:
- dependencies between operations
- parallelism opportunities
- hardware-relevant structure
- critical path
""",
    expected_output="Hardware-aware IR analysis",
    agent=ir_analyzer
)

architecture_task = Task(
    description="""
From IR + DFG, design hardware architecture:

Include:
- datapath
- control unit
- FSM states
- pipeline stages
- resource sharing
""",
    expected_output="Hardware architecture specification",
    agent=hardware_architect
)

hdl_task = Task(
    description="""
Generate synthesizable Verilog RTL:

Must include:
- module definition
- clocked logic
- combinational logic
- reset handling
- correct mapping from IR operations
""",
    expected_output="Synthesizable Verilog",
    agent=hdl_generator
)

verification_task = Task(
    description="""
Generate testbench and verification strategy.

Validate:
- correctness against IR
- edge cases
- random input testing
""",
    expected_output="Testbench + verification report",
    agent=verification_agent
)


# =========================================================
# 5. CREW
# =========================================================

crew = Crew(
    agents=[
        ir_analyzer,
        hardware_architect,
        hdl_generator,
        verification_agent
    ],
    tasks=[
        analyze_task,
        architecture_task,
        hdl_task,
        verification_task
    ],
    process=Process.sequential,
    verbose=True
)


# =========================================================
# 6. INPUT
# =========================================================

python_code = """
def adder(a, b):
    return a + b
"""


# =========================================================
# 7. PIPELINE EXECUTION
# =========================================================

ir, dfg = build_ir_and_dfg(python_code)

dfg_serialized = nx.node_link_data(dfg)

print("\n=== IR ===")
print(json.dumps(ir, indent=2))

print("\n=== DFG ===")
print(json.dumps(dfg_serialized, indent=2))


result = crew.kickoff(inputs={
    "ir": json.dumps(ir),
    "dfg": json.dumps(dfg_serialized)
})

print("\n=== FINAL RESULT ===")
print(result)