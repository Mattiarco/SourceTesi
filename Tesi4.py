from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx


# =========================================================
# 1. IR + DFG BUILDER (COMPILER CORE)
# =========================================================

def build_ir_and_dfg(code: str):
    tree = ast.parse(code)

    ir = []
    graph = nx.DiGraph()

    op_id = 0

    def new_op():
        nonlocal op_id
        op_id += 1
        return f"op_{op_id}"

    for node in ast.walk(tree):

        # FUNCTION (metadata only)
        if isinstance(node, ast.FunctionDef):
            continue

        # BINARY OPERATION
        if isinstance(node, ast.BinOp):

            op = new_op()

            left = getattr(node.left, "id", "input")
            right = getattr(node.right, "id", "input")

            ir_node = {
                "id": op,
                "type": "binary_op",
                "op": type(node.op).__name__,
                "inputs": [left, right],
                "output": op,
                "latency": 1
            }

            ir.append(ir_node)

            graph.add_node(op, **ir_node)

            # dependency edges
            graph.add_edge(left, op)
            graph.add_edge(right, op)

    return ir, graph


# =========================================================
# 2. CRITICAL PATH ANALYSIS
# =========================================================

def compute_critical_path(graph: nx.DiGraph):

    longest = {}

    for node in nx.topological_sort(graph):

        preds = list(graph.predecessors(node))

        if not preds:
            longest[node] = 1
        else:
            longest[node] = max(longest[p] for p in preds) + 1

    return longest


# =========================================================
# 3. ASAP SCHEDULING (VERY SIMPLIFIED)
# =========================================================

def asap_schedule(graph: nx.DiGraph, critical_path):

    schedule = {}

    for node in nx.topological_sort(graph):
        preds = list(graph.predecessors(node))

        if not preds:
            schedule[node] = 0
        else:
            schedule[node] = max(schedule[p] for p in preds) + 1

    return schedule


# =========================================================
# 4. LLM MODEL
# =========================================================

llm = "ollama/qwen2.5-coder"


# =========================================================
# 5. AGENTS (ORA SONO "COMPILER STAGES")
# =========================================================

ir_analyzer = Agent(
    role="IR Analyzer",
    goal="Analyze IR + scheduling + DFG to extract hardware structure",
    backstory="Compiler and HLS expert",
    llm=llm,
    verbose=True
)

hardware_architect = Agent(
    role="Hardware Architect",
    goal="Design datapath, FSM and pipeline from scheduled IR",
    backstory="FPGA architect expert",
    llm=llm,
    verbose=True
)

hdl_generator = Agent(
    role="RTL Generator",
    goal="Generate synthesizable Verilog from scheduled architecture",
    backstory="RTL design expert",
    llm=llm,
    verbose=True
)

verification_agent = Agent(
    role="Verification Engineer",
    goal="Validate RTL correctness vs IR execution model",
    backstory="Formal verification expert",
    llm=llm,
    verbose=True
)


# =========================================================
# 6. TASKS (NOW COMPILER-AWARE)
# =========================================================

analyze_task = Task(
    description="""
You are given:

IR:
{ir}

DFG:
{dfg}

CRITICAL PATH:
{critical_path}

ASAP SCHEDULE:
{schedule}

Analyze:
- parallelism opportunities
- pipeline stages
- bottlenecks
- hardware mapping
""",
    expected_output="Hardware-aware IR analysis",
    agent=ir_analyzer
)

architecture_task = Task(
    description="""
Design hardware architecture from scheduled IR.

Include:
- datapath
- FSM states
- pipeline stages
- resource sharing strategy
- cycle-level execution
""",
    expected_output="Hardware architecture spec",
    agent=hardware_architect
)

hdl_task = Task(
    description="""
Generate synthesizable Verilog RTL.

Must respect:
- schedule (cycle accuracy)
- dependencies
- synchronous logic
""",
    expected_output="Verilog RTL",
    agent=hdl_generator
)

verification_task = Task(
    description="""
Generate verification plan:

- compare IR execution vs RTL simulation
- define test vectors
- validate cycle accuracy
""",
    expected_output="Verification report",
    agent=verification_agent
)


# =========================================================
# 7. CREW
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
# 8. INPUT
# =========================================================

python_code = """
def adder(a, b):
    return a + b
"""


# =========================================================
# 9. PIPELINE EXECUTION
# =========================================================

ir, dfg = build_ir_and_dfg(python_code)

critical_path = compute_critical_path(dfg)
schedule = asap_schedule(dfg, critical_path)

print("\n=== IR ===")
print(json.dumps(ir, indent=2))

print("\n=== CRITICAL PATH ===")
print(json.dumps(critical_path, indent=2))

print("\n=== SCHEDULE ===")
print(json.dumps(schedule, indent=2))


result = crew.kickoff(inputs={
    "ir": json.dumps(ir),
    "dfg": str(nx.node_link_data(dfg)),
    "critical_path": json.dumps(critical_path),
    "schedule": json.dumps(schedule)
})

print("\n=== FINAL RESULT ===")
print(result)