from crewai import Agent, Task, Crew, Process
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4.1")

python_analyzer = Agent(
    role="Python HDL Analyzer",
    goal="Analyze Python code and extract hardware logic",
    backstory="Expert in compiler design and hardware synthesis",
    llm=llm,
    verbose=True
)

hardware_architect = Agent(
    role="Hardware Architect",
    goal="Transform software logic into synthesizable hardware architecture",
    backstory="FPGA and ASIC architect expert",
    llm=llm,
    verbose=True
)

hdl_generator = Agent(
    role="HDL Generator",
    goal="Generate synthesizable Verilog/VHDL code",
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

analyze_task = Task(
    description="""
    Analyze the Python code and extract:
    - variables
    - loops
    - state machines
    - arithmetic operations
    - memory structures
    """,
    expected_output="Structured hardware-oriented IR",
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
    - pipeline stages
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
    Generate a complete Verilog testbench.
    Validate expected behavior.
    """,
    expected_output="Testbench and verification report",
    agent=verification_agent
)

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

python_code = """
def adder(a, b):
    return a + b
"""

result = crew.kickoff(inputs={
    "python_code": python_code
})

print(result)