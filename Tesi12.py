from crewai import Agent, Task, Crew, Process
import ast
import json
import networkx as nx
from pathlib import Path
from datetime import datetime

# Tabella temporale Hardware. 
LATENCY_TABLE = {
    "Add": 1,
    "Sub": 1,
    "Mult": 2,
    "Div": 4,
    "Gt": 1,
    "Lt": 1,
    "Eq": 1,
    "NotEq": 1,
    "CONST": 0,
    "PHI": 0
}


# Trasformo il code in un IR e costruisco il DFG e CFG.
class IRBuilder(ast.NodeVisitor):

    def __init__(self):

        self.ir = []

        self.graph = nx.DiGraph()

        self.cfg = nx.DiGraph()

        self.op_id = 0

        self.var_map = {}

        self.input_ports = {"a", "b", "c", "d"}

        self.const_cache = {}

        self.return_value = None

        self.loop_stack = []

        self.loop_var_updates = {}

        self.last_cfg_node = None

    # Stacco un nuovo ID per ogni operazione.
    def new_op(self):

        self.op_id += 1

        return f"op_{self.op_id}"

    # Collego i nodi nel CFG in ordine di visita.
    def connect_cfg(self, node_id):

        self.cfg.add_node(node_id)

        if self.last_cfg_node:
            self.cfg.add_edge(self.last_cfg_node, node_id)

        self.last_cfg_node = node_id


    # Risolvo variabili, costanti e input ports.
    def resolve(self, value):

        if isinstance(value, str):

            if value.startswith("op_"):
                return value

            if value in self.var_map:
                return self.var_map[value]

            if value in self.input_ports:
                return value

        return value

    # Creo nodi costanti con caching per evitare duplicati.
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

        self.connect_cfg(op)

        return op

    # Aggiungo un nodo generico all'IR e al DFG.
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

        self.connect_cfg(op)

        for inp in inputs:

            if isinstance(inp, str):

                self.graph.add_node(inp)

                if inp != op:
                    self.graph.add_edge(inp, op)

        return op

    # Estraggo nodi di confronto.
    def extract_compare(self, node):

        left = self.extract(node.left)

        right = self.extract(node.comparators[0])

        op_name = type(node.ops[0]).__name__

        return self.add_node(
            "compare",
            op_name,
            [left, right],
            latency=LATENCY_TABLE.get(op_name, 1)
        )

    # Estraggo variabili, costanti, operazioni binarie, confronti e chiamate di funzione.
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
                latency=LATENCY_TABLE.get(op_name, 1)
            )

        if isinstance(node, ast.Compare):

            return self.extract_compare(node)

        if isinstance(node, ast.Call):

            return self.visit_Call(node)

        return ast.dump(node)

    # Assegno variabili e rilevo ricorrenze nei loop.
    def visit_Assign(self, node):

        target = node.targets[0].id

        value = self.extract(node.value)

        if self.loop_stack:

            previous = self.loop_var_updates.get(target)

            if previous and previous != value:

                self.graph.add_edge(previous, value)

                self.graph[previous][value]["recurrence"] = True

            self.loop_var_updates[target] = value

        self.var_map[target] = value

    # Aggiorno variabili e rilevo ricorrenze nei loop.
    def visit_AugAssign(self, node):

        target = node.target.id

        left = self.resolve(target)

        right = self.extract(node.value)

        op_name = type(node.op).__name__

        op_id = self.add_node(
            "binary_op",
            op_name,
            [left, right],
            latency=LATENCY_TABLE.get(op_name, 1)
        )

        if self.loop_stack:

            self.graph.add_edge(left, op_id)

            self.graph[left][op_id]["recurrence"] = True

        self.var_map[target] = op_id

    # Ritorno valori.
    def visit_Return(self, node):

        self.return_value = self.extract(node.value)

    # Gestisco le if.
    def visit_If(self, node):

        condition = self.extract(node.test)

        branch_node = self.add_node(
            "branch",
            "IF",
            [condition],
            latency=1
        )

        before_if = dict(self.var_map)


        for stmt in node.body:
            self.visit(stmt)

        true_state = dict(self.var_map)

        self.var_map = dict(before_if)

        for stmt in node.orelse:
            self.visit(stmt)

        false_state = dict(self.var_map)

        merged = {}

        all_vars = (
            set(before_if.keys())
            | set(true_state.keys())
            | set(false_state.keys())
        )

        for var in all_vars:

            t = true_state.get(var, before_if.get(var))
            f = false_state.get(var, before_if.get(var))

            if t != f:

                phi = self.add_node(
                    "phi",
                    "PHI",
                    [t, f],
                    latency=0
                )

                merged[var] = phi

            else:
                merged[var] = t

        self.var_map = merged

    # Gestisco i loop for.
    def visit_For(self, node):

        loop_var = node.target.id

        trip_count = self.extract_trip_count(node.iter)

        loop_node = self.add_node(
            "loop",
            "FOR",
            [ast.dump(node.iter)],
            latency=1,
            extra={
                "trip_count": trip_count
            }
        )

        self.loop_stack.append(loop_node)

        self.var_map[loop_var] = loop_var

        for stmt in node.body:
            self.visit(stmt)

        self.loop_stack.pop()

    # Gestisco i loop while.
    def visit_While(self, node):

        condition = self.extract(node.test)

        loop_node = self.add_node(
            "loop",
            "WHILE",
            [condition],
            latency=1
        )

        self.loop_stack.append(loop_node)

        for stmt in node.body:
            self.visit(stmt)

        self.loop_stack.pop()

    # Gestisco le chiamate alle funzioni.
    def visit_Call(self, node):

        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else "call"
        )

        args = [self.extract(a) for a in node.args]

        return self.add_node(
            "call",
            name,
            args,
            latency=3
        )

    # Estraggo il trip count se è un range costante.
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


# Costruisco IR, DFG, CFG e mappa delle variabili.
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


# Analizzo le ricorrenze nei loop per identificare le dipendenze.
def compute_recurrence_ii(graph):

    recurrence_edges = []

    for u, v, data in graph.edges(data=True):

        if data.get("recurrence", False):

            lat_u = graph.nodes[u].get("latency", 1)

            lat_v = graph.nodes[v].get("latency", 1)

            rec_mii = max(lat_u, lat_v)

            recurrence_edges.append({
                "from": u,
                "to": v,
                "latency_u": lat_u,
                "latency_v": lat_v,
                "distance": 1,
                "recMII": rec_mii
            })

    return recurrence_edges


# Calcolo il percorso critico del DFG.
def compute_critical_path(graph):

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


# Programmo i nodi in base alle dipendenze e latenze.
def schedule(graph):

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

        lat = acyclic.nodes[n].get("latency", 0)

        sch[n] = base + lat

    return sch


# Organizzo i nodi in stage di pipeline in base alla schedule.
def pipeline(schedule):

    p = {}

    for node, stage in schedule.items():

        p.setdefault(stage, []).append(node)

    return p


# Stimo le risorse necessarie in base ai nodi presenti nell'IR.
def estimate_resources(ir):

    resources = {
        "adders": 0,
        "multipliers": 0,
        "dividers": 0,
        "comparators": 0,
        "registers": 0
    }

    for node in ir:

        if node["type"] == "binary_op":

            if node["operation"] in ["Add", "Sub"]:
                resources["adders"] += 1

            elif node["operation"] == "Mult":
                resources["multipliers"] += 1

            elif node["operation"] == "Div":
                resources["dividers"] += 1

        elif node["type"] == "compare":
            resources["comparators"] += 1

        resources["registers"] += 1

    return resources


# Genero codice PyMTL.
def generate_pymtl(ir, var_map, return_value):

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

    for node in ir:

        width = 1 if node["type"] == "compare" else 32

        lines.append(
            f"        s.{node['id']} = Wire({width})"
        )

    lines.append("\n        @update")
    lines.append("        def compute():\n")

    for node in ir:

        if node["type"] == "const":

            lines.append(
                f"            s.{node['id']} @= {node['value']}"
            )

        elif node["type"] == "binary_op":

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

        elif node["type"] == "phi":

            a = resolve(node["inputs"][0])

            b = resolve(node["inputs"][1])

            # placeholder mux
            lines.append(
                f"            s.{node['id']} @= {b}"
            )

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


# Salvo un report completo con tutte le analisi e il codice generato.

def save_report(
    file,
    ir,
    cp,
    sch,
    pipe,
    pymtl,
    ai,
    recurrence,
    resources
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

        f.write("\n\nRESOURCE ESTIMATION\n")
        f.write(json.dumps(resources, indent=2))

        f.write("\n\nPYMTL\n")
        f.write(pymtl)

        f.write("\n\nAI ANALYSIS\n")
        f.write(str(ai))

    return name


# Configuro l'agente AI.
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

RESOURCES:
{resources}

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
5. Resource estimation analysis
6. HLS optimization opportunities

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


# Main input7output flow.
print("\n=== ADVANCED AI-HLS COMPILER V6 ===\n")

path = Path(input("Python file: ").strip())

if not path.exists():
    raise FileNotFoundError("File non trovato")

code = path.read_text(encoding="utf-8")


# Pipeline completo di analisi e generazione codice.
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

resources = estimate_resources(ir)

pymtl = generate_pymtl(
    ir,
    var_map,
    return_value
)


# Analisi AI.
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
        ),
        "resources": json.dumps(
            resources,
            indent=2
        )
    }
)


# Output del report completo.
report = save_report(
    path,
    ir,
    cp,
    sch,
    pipe,
    pymtl,
    ai,
    recurrence,
    resources
)

print("\nDONE")
print("REPORT:", report)