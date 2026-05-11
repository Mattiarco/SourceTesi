from crewai import Agent, Task, Crew, Process
import ast
import json


# =========================================================
# 1. STATIC ANALYSIS LAYER (NO LLM)
# =========================================================

def python_to_ir(code: str):
    tree = ast.parse(code)

    ir = {
        "functions": [],
        "variables": set(),
        "operations": []
    }

    for node in ast.walk(tree):

        # variabili
        if isinstance(node, ast.Name):
            ir["variables"].add(node.id)

        # assegnazioni
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    ir["operations"].append({
                        "type": "assign",
                        "dest": target.id
                    })

        # operazioni binarie
        if isinstance(node, ast.BinOp):
            ir["operations"].append({
                "type": "binary_op",
                "op": type(node.op).__name__
            })

        # funzioni
        if isinstance(node, ast.FunctionDef):
            ir["functions"].append(node.name)

    ir["variables"] = list(ir["variables"])

    return ir


# =========================================================
# 2. LLM MODEL
# =========================================================

llm = "ollama/qwen2.5-coder"


# =========================================================
# 3. AGENTS (ORA LAVORANO SU IR, NON PYTHON)
# =========================================================

python_analyzer = Agent(
    role="IR Analyzer",
    goal="Analyze Intermediate Representation and extract hardware structures",
    backstory="Expert in hardware compilation and IR-based synthesis",
    llm=llm,
    verbose=True
)

hardware_architect = Agent(
    role="Hardware Architect",
    goal="Transform IR into hardware architecture (datapath + control)",
    backstory="FPGA and ASIC architect expert",
    llm=llm,
    verbose=True
)

hdl_generator = Agent(
    role="HDL Generator",
    goal="Generate synthesizable Verilog from hardware architecture",
    backstory="Expert in RTL design",
    llm=llm,
    verbose=True
)

verification_agent = Agent(
    role="Verification Engineer",
    goal="Generate testbench and validate HDL correctness",
    backstory="Expert in digital verification",
    llm=llm,
    verbose=True
)


# =========================================================
# 4. TASKS (INPUT = IR, NON PYTHON CODE)
# =========================================================

analyze_task = Task(
    description="""
    Analyze the following Intermediate Representation (IR):

    {ir}

    Extract:
    - datapath elements
    - operations
    - variables usage
    - function structure
    """,
    expected_output="Structured hardware-oriented IR analysis",
    agent=python_analyzer
)

architecture_task = Task(
    description="""
    Convert IR into hardware architecture.

    Define:
    - datapath
    - control logic
    - registers
    - FSM
    - dependencies
    """,
    expected_output="Hardware architecture specification",
    agent=hardware_architect
)

hdl_task = Task(
    description="""
    Generate synthesizable Verilog HDL code.

    Include:
    - module declaration
    - synchronous logic
    - combinational logic
    - reset handling
    """,
    expected_output="Synthesizable Verilog code",
    agent=hdl_generator
)

verification_task = Task(
    description="""
    Generate Verilog testbench.

    Validate correctness of the HDL implementation.
    Compare expected behavior from IR.
    """,
    expected_output="Testbench and verification report",
    agent=verification_agent
)


# =========================================================
# 5. CREW
# =========================================================

crew = Crew(
    agents=[
        python_analyzer,
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
# 6. INPUT PYTHON CODE
# =========================================================

python_code = """
def adder(a, b):
    return a + b
"""


# =========================================================
# 7. PIPELINE
# =========================================================

ir = python_to_ir(python_code)

print("\n=== GENERATED IR ===")
print(json.dumps(ir, indent=2))

result = crew.kickoff(inputs={
    "ir": json.dumps(ir)
})

print("\n=== FINAL RESULT ===")
print(result)