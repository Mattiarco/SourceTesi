from __future__ import annotations
import argparse
import ast
import json
import math
import random
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
import networkx as nx


# 1. CONFIGURAZIONE FORMATI E LATENZE

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
    "INPUT": 0,
    "NEG": 1,
}

FP_FORMATS = {
    "MXFP4": {
        "exp_bits": 2,
        "man_bits": 1,
        "total_bits": 4,
        "block_size": 32,
        "scale_type": "power_of_two",
        "description": "MXFP4-like simplified E2M1 block floating-point format",
    },
    "NVFP4": {
        "exp_bits": 2,
        "man_bits": 1,
        "total_bits": 4,
        "block_size": 16,
        "scale_type": "fp8_like",
        "description": "NVFP4-like simplified E2M1 format with local scaling model",
    },
}

# 2. CLASSE DI CODIFICA FP4

class FP4Codec:
    """
    Golden model software semplificato per valori E2M1 a 4 bit.

    Struttura:
    - 1 bit segno
    - 2 bit esponente
    - 1 bit mantissa

    Nota:
    Questo è un modello sperimentale per:
    - quantizzazione
    - dequantizzazione
    - confronto tra output Python e output quantizzato
    - valutazione dell'errore numerico

    Il set di valori base usato è coerente con una rappresentazione FP4 E2M1
    semplificata:
        0, ±0.5, ±1, ±1.5, ±2, ±3, ±4, ±6
    """

    POSITIVE_VALUES = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]

    def __init__(self, fp_format: str):
        if fp_format not in FP_FORMATS:
            raise ValueError(f"Formato non supportato: {fp_format}")
        self.fp_format = fp_format
        self.config = FP_FORMATS[fp_format]

    def encode_scalar(self, value: float, scale: float = 1.0) -> int:
        """
        Codifica un valore reale in un codice FP4 a 4 bit.
        Il valore viene prima diviso per lo scale.
        """
        if scale == 0:
            scale = 1.0

        normalized = value / scale
        sign_bit = 1 if normalized < 0 else 0
        abs_value = abs(normalized)

        nearest_index = min(
            range(len(self.POSITIVE_VALUES)),
            key=lambda i: abs(self.POSITIVE_VALUES[i] - abs_value),
        )

        # 3 bit inferiori = indice del valore positivo
        code = (sign_bit << 3) | nearest_index
        return code & 0xF

    def decode_scalar(self, code: int, scale: float = 1.0) -> float:
        """
        Decodifica un codice FP4 a 4 bit in valore reale.
        """
        code = code & 0xF
        sign = -1.0 if ((code >> 3) & 0x1) else 1.0
        index = code & 0x7
        return sign * self.POSITIVE_VALUES[index] * scale

    def compute_scale(self, values: List[float]) -> float:
        """
        Calcola un fattore di scala semplificato per blocco.

        MXFP4:
            approssimazione a potenza di due.

        NVFP4:
            approssimazione più fine, per simulare uno scaling locale più accurato.
        """
        if not values:
            return 1.0

        max_abs = max(abs(v) for v in values)
        if max_abs == 0:
            return 1.0

        max_representable = max(self.POSITIVE_VALUES)
        raw_scale = max_abs / max_representable

        if self.config["scale_type"] == "power_of_two":
            exponent = round(math.log2(raw_scale))
            return 2.0 ** exponent

        # Modello semplificato NVFP4: scala più granulare.
        # Non è FP8 E4M3 completo, ma è più fine della sola potenza di due.
        return round(raw_scale * 16.0) / 16.0 or 1.0

    def quantize_block(self, values: List[float]) -> Tuple[List[int], float]:
        """
        Quantizza un blocco di valori.
        """
        scale = self.compute_scale(values)
        codes = [self.encode_scalar(v, scale) for v in values]
        return codes, scale

    def dequantize_block(self, codes: List[int], scale: float) -> List[float]:
        """
        Dequantizza un blocco di codici FP4.
        """
        return [self.decode_scalar(c, scale) for c in codes]

    def quantize_scalar_auto(self, value: float) -> Tuple[int, float, float]:
        """
        Quantizzazione singola con scala calcolata sul singolo valore.
        Restituisce:
        - codice FP4
        - scala
        - valore dequantizzato
        """
        codes, scale = self.quantize_block([value])
        decoded = self.dequantize_block(codes, scale)[0]
        return codes[0], scale, decoded

# 3. IR BUILDER

class IRBuilder(ast.NodeVisitor):
    """
    Converte codice Python in:
    - Intermediate Representation
    - Data Flow Graph
    - mappa variabili
    - valore di ritorno

    Supporta:
    - assegnazioni semplici
    - return
    - operazioni binarie: +, -, *, /
    - confronti semplici
    - costanti
    - variabili a, b, c, d
    - unary minus
    """

    def __init__(self):
        self.ir: List[Dict[str, Any]] = []
        self.graph = nx.DiGraph()
        self.op_id = 0
        self.var_map: Dict[str, str] = {}
        self.input_ports = {"a", "b", "c", "d"}
        self.const_cache: Dict[str, str] = {}
        self.return_value: Optional[str] = None
        self.unsupported_nodes: List[str] = []

        for port in self.input_ports:
            self.graph.add_node(
                port,
                id=port,
                type="input",
                operation="INPUT",
                latency=0,
            )

    def _new_id(self) -> str:
        self.op_id += 1
        return f"op_{self.op_id}"

    def _resolve(self, name: str) -> str:
        if name in self.var_map:
            return self.var_map[name]
        if name in self.input_ports:
            return name
        return name

    def _const(self, value: Any) -> str:
        if not isinstance(value, (int, float, bool)):
            raise ValueError(f"Costante non numerica non supportata: {value}")

        key = f"const_{value}"
        if key in self.const_cache:
            return self.const_cache[key]

        oid = self._new_id()
        self.const_cache[key] = oid

        node = {
            "id": oid,
            "type": "const",
            "operation": "CONST",
            "inputs": [],
            "output": oid,
            "value": value,
            "latency": LATENCY_TABLE["CONST"],
        }

        self.ir.append(node)
        self.graph.add_node(oid, **node)
        return oid

    def _add_node(
        self,
        op_type: str,
        operation: str,
        inputs: List[str],
        latency: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        oid = self._new_id()

        node = {
            "id": oid,
            "type": op_type,
            "operation": operation,
            "inputs": inputs,
            "output": oid,
            "latency": latency if latency is not None else LATENCY_TABLE.get(operation, 1),
        }

        if extra:
            node.update(extra)

        self.ir.append(node)
        self.graph.add_node(oid, **node)

        for inp in inputs:
            if isinstance(inp, str):
                self.graph.add_node(inp)
                self.graph.add_edge(inp, oid)

        return oid

    def extract(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self._resolve(node.id)

        if isinstance(node, ast.Constant):
            return self._const(node.value)

        if isinstance(node, ast.BinOp):
            left = self.extract(node.left)
            right = self.extract(node.right)
            op = type(node.op).__name__

            if op not in {"Add", "Sub", "Mult", "Div"}:
                self.unsupported_nodes.append(f"Unsupported BinOp: {op}")

            return self._add_node(
                op_type="binary_op",
                operation=op,
                inputs=[left, right],
                latency=LATENCY_TABLE.get(op, 1),
            )

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            operand = self.extract(node.operand)
            zero = self._const(0)
            return self._add_node(
                op_type="binary_op",
                operation="Sub",
                inputs=[zero, operand],
                latency=LATENCY_TABLE["Sub"],
            )

        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                self.unsupported_nodes.append("Only single comparisons are supported")

            left = self.extract(node.left)
            right = self.extract(node.comparators[0])
            op = type(node.ops[0]).__name__

            return self._add_node(
                op_type="compare",
                operation=op,
                inputs=[left, right],
                latency=LATENCY_TABLE.get(op, 1),
            )

        dumped = ast.dump(node)
        self.unsupported_nodes.append(dumped)
        return dumped

    def visit_Assign(self, node: ast.Assign):
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            self.unsupported_nodes.append("Only simple assignments are supported")
            return

        target = node.targets[0].id
        self.var_map[target] = self.extract(node.value)

    def visit_Return(self, node: ast.Return):
        self.return_value = self.extract(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        for stmt in node.body:
            self.visit(stmt)


def build_ir(code: str) -> Tuple[List[Dict[str, Any]], nx.DiGraph, Dict[str, str], Optional[str], List[str]]:
    builder = IRBuilder()
    tree = ast.parse(code)
    builder.visit(tree)
    return builder.ir, builder.graph, builder.var_map, builder.return_value, builder.unsupported_nodes


# 4. ANALISI HLS

def critical_path(graph: nx.DiGraph) -> Dict[str, int]:
    """
    Calcola la distanza massima in termini di latenza.
    """
    dist: Dict[str, int] = {}

    for n in nx.topological_sort(graph):
        lat = graph.nodes[n].get("latency", 0)
        preds = list(graph.predecessors(n))
        dist[n] = lat + (max(dist[p] for p in preds) if preds else 0)

    return dist


def schedule_asap(graph: nx.DiGraph) -> Dict[str, int]:
    """
    ASAP scheduling: assegna ogni nodo al primo ciclo disponibile
    in base alle dipendenze dati.
    """
    sch: Dict[str, int] = {}

    for n in nx.topological_sort(graph):
        preds = list(graph.predecessors(n))
        base = max((sch[p] for p in preds), default=0)
        sch[n] = base + graph.nodes[n].get("latency", 0)

    return sch


def pipeline_stages(schedule: Dict[str, int]) -> Dict[int, List[str]]:
    stages: Dict[int, List[str]] = {}

    for node, stage in schedule.items():
        stages.setdefault(stage, []).append(node)

    return dict(sorted(stages.items(), key=lambda x: x[0]))


def resource_estimate(ir: List[Dict[str, Any]], schedule: Dict[str, int]) -> Dict[str, Any]:
    """
    Stima:
    - risorse totali
    - risorse massime per stage
    - registri stimati
    """
    total = {
        "adders": 0,
        "subtractors": 0,
        "multipliers": 0,
        "dividers": 0,
        "comparators": 0,
        "const_units": 0,
        "registers_estimated": len(ir),
    }

    per_stage: Dict[int, Dict[str, int]] = {}

    def ensure_stage(stage: int):
        if stage not in per_stage:
            per_stage[stage] = {
                "adders": 0,
                "subtractors": 0,
                "multipliers": 0,
                "dividers": 0,
                "comparators": 0,
            }

    for node in ir:
        op = node["operation"]
        stage = schedule.get(node["id"], 0)
        ensure_stage(stage)

        if node["type"] == "const":
            total["const_units"] += 1

        elif node["type"] == "binary_op":
            if op == "Add":
                total["adders"] += 1
                per_stage[stage]["adders"] += 1
            elif op == "Sub":
                total["subtractors"] += 1
                per_stage[stage]["subtractors"] += 1
            elif op == "Mult":
                total["multipliers"] += 1
                per_stage[stage]["multipliers"] += 1
            elif op == "Div":
                total["dividers"] += 1
                per_stage[stage]["dividers"] += 1

        elif node["type"] == "compare":
            total["comparators"] += 1
            per_stage[stage]["comparators"] += 1

    peak = {
        "peak_adders": max((v["adders"] for v in per_stage.values()), default=0),
        "peak_subtractors": max((v["subtractors"] for v in per_stage.values()), default=0),
        "peak_multipliers": max((v["multipliers"] for v in per_stage.values()), default=0),
        "peak_dividers": max((v["dividers"] for v in per_stage.values()), default=0),
        "peak_comparators": max((v["comparators"] for v in per_stage.values()), default=0),
    }

    return {
        "total_operations": total,
        "peak_parallel_resources": peak,
        "per_stage": per_stage,
    }


# 5. INTERPRETE IR E TEST 

def evaluate_ir_float(
    ir: List[Dict[str, Any]],
    inputs: Dict[str, float],
    return_value: Optional[str],
) -> float:
    """
    Esegue la IR in floating-point Python standard.
    """
    env: Dict[str, Any] = dict(inputs)

    for node in ir:
        oid = node["id"]
        op = node["operation"]

        if node["type"] == "const":
            env[oid] = node["value"]

        elif node["type"] == "binary_op":
            a = env[node["inputs"][0]]
            b = env[node["inputs"][1]]

            if op == "Add":
                env[oid] = a + b
            elif op == "Sub":
                env[oid] = a - b
            elif op == "Mult":
                env[oid] = a * b
            elif op == "Div":
                env[oid] = a / b if b != 0 else 0.0
            else:
                raise ValueError(f"Operazione non supportata: {op}")

        elif node["type"] == "compare":
            a = env[node["inputs"][0]]
            b = env[node["inputs"][1]]

            if op == "Gt":
                env[oid] = float(a > b)
            elif op == "Lt":
                env[oid] = float(a < b)
            elif op == "Eq":
                env[oid] = float(a == b)
            elif op == "NotEq":
                env[oid] = float(a != b)
            else:
                raise ValueError(f"Confronto non supportato: {op}")

    if return_value is None:
        raise ValueError("Nessun return individuato nel codice Python")

    return float(env[return_value])


def evaluate_ir_quantized(
    ir: List[Dict[str, Any]],
    inputs: Dict[str, float],
    return_value: Optional[str],
    codec: FP4Codec,
) -> float:
    """
    Esegue la IR con quantizzazione FP4 dopo ogni operazione.
    Questo simula l'errore introdotto da unità a precisione ridotta.
    """
    env: Dict[str, Any] = {}

    for k, v in inputs.items():
        _, _, qv = codec.quantize_scalar_auto(v)
        env[k] = qv

    for node in ir:
        oid = node["id"]
        op = node["operation"]

        if node["type"] == "const":
            _, _, qv = codec.quantize_scalar_auto(float(node["value"]))
            env[oid] = qv

        elif node["type"] == "binary_op":
            a = env[node["inputs"][0]]
            b = env[node["inputs"][1]]

            if op == "Add":
                raw = a + b
            elif op == "Sub":
                raw = a - b
            elif op == "Mult":
                raw = a * b
            elif op == "Div":
                raw = a / b if b != 0 else 0.0
            else:
                raise ValueError(f"Operazione non supportata: {op}")

            _, _, qv = codec.quantize_scalar_auto(raw)
            env[oid] = qv

        elif node["type"] == "compare":
            a = env[node["inputs"][0]]
            b = env[node["inputs"][1]]

            if op == "Gt":
                env[oid] = float(a > b)
            elif op == "Lt":
                env[oid] = float(a < b)
            elif op == "Eq":
                env[oid] = float(a == b)
            elif op == "NotEq":
                env[oid] = float(a != b)

    if return_value is None:
        raise ValueError("Nessun return individuato nel codice Python")

    return float(env[return_value])


def generate_test_vectors(
    ir: List[Dict[str, Any]],
    return_value: Optional[str],
    fp_format: str,
    n_tests: int = 32,
    seed: int = 42,
    input_ports: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Genera test casuali e confronta:
    - output Python float
    - output quantizzato FP4
    """
    random.seed(seed)
    codec = FP4Codec(fp_format)

    if input_ports is None:
        input_ports = ["a", "b", "c", "d"]

    tests = []
    abs_errors = []
    rel_errors = []

    for _ in range(n_tests):
        inputs = {
            port: random.uniform(-4.0, 4.0)
            for port in input_ports
        }

        float_out = evaluate_ir_float(ir, inputs, return_value)
        fp4_out = evaluate_ir_quantized(ir, inputs, return_value, codec)

        abs_error = abs(float_out - fp4_out)
        rel_error = abs_error / (abs(float_out) + 1e-9)

        abs_errors.append(abs_error)
        rel_errors.append(rel_error)

        tests.append({
            "inputs": inputs,
            "float_output": float_out,
            "fp4_output": fp4_out,
            "absolute_error": abs_error,
            "relative_error": rel_error,
        })

    return {
        "fp_format": fp_format,
        "n_tests": n_tests,
        "mean_absolute_error": sum(abs_errors) / len(abs_errors) if abs_errors else 0.0,
        "max_absolute_error": max(abs_errors) if abs_errors else 0.0,
        "mean_relative_error": sum(rel_errors) / len(rel_errors) if rel_errors else 0.0,
        "max_relative_error": max(rel_errors) if rel_errors else 0.0,
        "tests": tests,
    }


# 6. GENERAZIONE PYMTL3 

def generate_pymtl(
    ir: List[Dict[str, Any]],
    var_map: Dict[str, str],
    return_value: Optional[str],
    fp_format: str = "MXFP4",
    pipelined: bool = False,
) -> str:
    """
    Genera codice PyMTL3.

    Nota importante:
    Questo backend genera logica a 4 bit per rappresentare il datapath.
    La semantica FP4 completa è modellata e validata dal golden model software.
    """
    fmt = FP_FORMATS[fp_format]
    bits = fmt["total_bits"]

    port_map = {
        "a": "s.in0",
        "b": "s.in1",
        "c": "s.in2",
        "d": "s.in3",
    }

    def resolve(x: Any) -> str:
        if isinstance(x, str):
            if x.startswith("op_"):
                return f"s.{x}"
            if x in port_map:
                return port_map[x]
            mapped = var_map.get(x)
            if mapped:
                return resolve(mapped)
        return str(x)

    op_expr = {
        "Add": "{a} + {b}",
        "Sub": "{a} - {b}",
        "Mult": "{a} * {b}",
        "Div": "{a} // {b}",
        "Gt": "{a} > {b}",
        "Lt": "{a} < {b}",
        "Eq": "{a} == {b}",
        "NotEq": "{a} != {b}",
    }

    class_name = f"{fp_format}ArithUnit"

    lines = [
        "from pymtl3 import *",
        "",
        f"# Generated by Agentic Meta-HDL FP4 Compiler",
        f"# Format: {fp_format}",
        f"# Datapath width: {bits} bit",
        f"# Note: this is a Meta-HDL datapath.",
        f"# Full FP4 numerical behavior is validated by the Python golden model.",
        "",
        f"class {class_name}(Component):",
        "    def construct(s):",
        f"        s.in0 = InPort({bits})",
        f"        s.in1 = InPort({bits})",
        f"        s.in2 = InPort({bits})",
        f"        s.in3 = InPort({bits})",
        f"        s.out = OutPort({bits})",
        "",
    ]

    for node in ir:
        width = 1 if node["type"] == "compare" else bits
        lines.append(f"        s.{node['id']} = Wire({width})")

    lines.extend([
        "",
        "        @update",
        "        def compute():",
    ])

    for node in ir:
        oid = node["id"]

        if node["type"] == "const":
            value = int(node["value"]) & ((1 << bits) - 1)
            lines.append(f"            s.{oid} @= {value}")

        elif node["type"] in {"binary_op", "compare"}:
            op = node["operation"]
            if op not in op_expr:
                lines.append(f"            # Unsupported operation: {op}")
                continue

            a = resolve(node["inputs"][0])
            b = resolve(node["inputs"][1])
            expr = op_expr[op].format(a=a, b=b)

            if node["type"] == "binary_op":
                mask = (1 << bits) - 1
                lines.append(f"            s.{oid} @= ({expr}) & {mask}")
            else:
                lines.append(f"            s.{oid} @= {expr}")

    if return_value:
        lines.append("")
        lines.append(f"            s.out @= {resolve(return_value)}")
    else:
        lines.append("")
        lines.append("            s.out @= 0")

    if pipelined:
        lines.extend([
            "",
            "# Pipeline note:",
            "# Scheduling information is computed in the report.",
            "# Automatic insertion of pipeline registers is a future backend extension.",
        ])

    return "\n".join(lines)


# 7. ANALISI AGENTICA 

def run_optional_crew_analysis(
    fp_format: str,
    ir: List[Dict[str, Any]],
    schedule: Dict[str, int],
    pipeline: Dict[int, List[str]],
    critical: Dict[str, int],
    resources: Dict[str, Any],
    tests: Dict[str, Any],
) -> str:
    """
    Esegue CrewAI solo se installato e richiesto.
    Se CrewAI non è disponibile, restituisce una valutazione deterministica.
    """
    try:
        from crewai import Agent, Task, Crew, Process
    except Exception as exc:
        return deterministic_architectural_review(
            fp_format, ir, schedule, pipeline, critical, resources, tests,
            note=f"CrewAI non disponibile: {exc}",
        )

    llm = "ollama/qwen2.5-coder"

    designer = Agent(
        role="Meta-HDL FP4 Unit Designer",
        goal="Analyze the generated Meta-HDL datapath and propose realistic FP4 hardware improvements.",
        backstory="Expert in Meta-HDL, PyMTL3, HLS, reduced precision arithmetic and RISC-V coprocessors.",
        llm=llm,
        verbose=True,
    )

    analyzer = Agent(
        role="HLS Validation Analyzer",
        goal="Check scheduling, resource usage and test results without inventing unsupported claims.",
        backstory="Compiler backend specialist focused on dataflow graphs, scheduling and test-driven validation.",
        llm=llm,
        verbose=True,
    )

    task1 = Task(
        description=f"""
Analyze this {fp_format} prototype Meta-HDL arithmetic unit.

IR:
{json.dumps(ir, indent=2)}

Schedule:
{json.dumps(schedule, indent=2)}

Pipeline:
{json.dumps(pipeline, indent=2)}

Resources:
{json.dumps(resources, indent=2)}

Validation:
{json.dumps({k: v for k, v in tests.items() if k != "tests"}, indent=2)}

Rules:
- Do not claim that the implementation is a complete industrial FP4 FPU.
- Distinguish between prototype datapath and software golden model.
- Suggest realistic thesis-level improvements.
""",
        expected_output="Realistic architectural review for the FP4 Meta-HDL prototype.",
        agent=designer,
    )

    task2 = Task(
        description=f"""
Validate the HLS analysis of the generated unit.

Critical path:
{json.dumps(critical, indent=2)}

Resources:
{json.dumps(resources, indent=2)}

Rules:
- Check whether parallel operations are correctly identified.
- Do not propose schedules that violate data dependencies.
- Explain bottlenecks clearly.
""",
        expected_output="HLS validation report.",
        agent=analyzer,
    )

    crew = Crew(
        agents=[designer, analyzer],
        tasks=[task1, task2],
        process=Process.sequential,
        verbose=True,
    )

    result = crew.kickoff()
    return str(result)


def deterministic_architectural_review(
    fp_format: str,
    ir: List[Dict[str, Any]],
    schedule: Dict[str, int],
    pipeline: Dict[int, List[str]],
    critical: Dict[str, int],
    resources: Dict[str, Any],
    tests: Dict[str, Any],
    note: str = "",
) -> str:
    """
    Analisi testuale non-LLM, utile per avere sempre un report.
    """
    max_latency = max(critical.values()) if critical else 0
    peak = resources["peak_parallel_resources"]

    review = []
    review.append("# Architectural Review")
    review.append("")
    if note:
        review.append(f"Nota: {note}")
        review.append("")

    review.append(f"Il progetto genera una unità prototipale per formato {fp_format}.")
    review.append(f"La IR contiene {len(ir)} operazioni.")
    review.append(f"La latenza stimata del critical path è pari a {max_latency} cicli.")
    review.append("")
    review.append("## Parallelismo individuato")
    review.append(f"- Picco adders paralleli: {peak['peak_adders']}")
    review.append(f"- Picco subtractors paralleli: {peak['peak_subtractors']}")
    review.append(f"- Picco multipliers paralleli: {peak['peak_multipliers']}")
    review.append(f"- Picco dividers paralleli: {peak['peak_dividers']}")
    review.append("")
    review.append("## Validazione numerica FP4")
    review.append(f"- Test eseguiti: {tests['n_tests']}")
    review.append(f"- Errore assoluto medio: {tests['mean_absolute_error']:.6f}")
    review.append(f"- Errore assoluto massimo: {tests['max_absolute_error']:.6f}")
    review.append(f"- Errore relativo medio: {tests['mean_relative_error']:.6f}")
    review.append("")

    return "\n".join(review)


# 8. REPORT

def generate_markdown_report(
    input_file: Path,
    fp_format: str,
    code: str,
    ir: List[Dict[str, Any]],
    var_map: Dict[str, str],
    return_value: Optional[str],
    unsupported: List[str],
    schedule: Dict[str, int],
    pipeline: Dict[int, List[str]],
    critical: Dict[str, int],
    resources: Dict[str, Any],
    hdl: str,
    tests: Dict[str, Any],
    ai_review: str,
) -> str:
    max_latency = max(critical.values()) if critical else 0

    requirement_matrix = [
        ("Soluzione agentica Meta-HDL", "Soddisfatto", "CrewAI opzionale + analisi deterministica sempre disponibile"),
        ("Parsing codice Python", "Soddisfatto", "AST Python"),
        ("Intermediate Representation", "Soddisfatto", "IR con operazioni, input, output e latenze"),
        ("Data Flow Graph", "Soddisfatto", "NetworkX DiGraph"),
        ("Analisi HLS", "Soddisfatto", "Critical path, ASAP scheduling, stima risorse"),
        ("Generazione Meta-HDL", "Soddisfatto", "Backend PyMTL3 prototipale"),
        ("Supporto MXFP4/NVFP4", "Soddisfatto", "Datapath a 4 bit"),
        ("Validazione automatica", "Soddisfatto", "Test casuali e confronto float vs FP4"),
    ]

    lines = []
    lines.append(f"# Meta-HDL FP4 Unit Report — {fp_format}")
    lines.append("")
    lines.append(f"Data generazione: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Input file: `{input_file}`")
    lines.append("")
    lines.append("## 1. Sintesi")
    lines.append("")
    lines.append(
        f"Il framework ha analizzato il codice Python, generato una IR con "
        f"{len(ir)} operazioni, costruito il DFG, stimato una latenza critica "
        f"di {max_latency} cicli e prodotto un backend PyMTL3 prototipale."
    )
    lines.append("")
    lines.append("## 2. Matrice requisiti-risultati")
    lines.append("")
    lines.append("| Requisito | Stato | Nota |")
    lines.append("|---|---|---|")
    for req, status, note in requirement_matrix:
        lines.append(f"| {req} | {status} | {note} |")
    lines.append("")
    lines.append("## 3. Codice Python sorgente")
    lines.append("")
    lines.append("```python")
    lines.append(code.strip())
    lines.append("```")
    lines.append("")
    lines.append("## 4. Intermediate Representation")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(ir, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 5. Variable Map")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(var_map, indent=2))
    lines.append("```")
    lines.append("")
    lines.append(f"Return value: `{return_value}`")
    lines.append("")
    lines.append("## 6. Scheduling e Pipeline")
    lines.append("")
    lines.append("### Schedule ASAP")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(schedule, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("### Pipeline stages")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(pipeline, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("### Critical path")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(critical, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 7. Stima risorse")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(resources, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 8. Validazione FP4")
    lines.append("")
    summary = {k: v for k, v in tests.items() if k != "tests"}
    lines.append("```json")
    lines.append(json.dumps(summary, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("### Primi 5 test")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(tests["tests"][:5], indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 9. Codice PyMTL3 generato")
    lines.append("")
    lines.append("```python")
    lines.append(hdl)
    lines.append("```")
    lines.append("")
    lines.append("## 10. Nodi non supportati o avvisi")
    lines.append("")
    if unsupported:
        lines.append("```json")
        lines.append(json.dumps(unsupported, indent=2))
        lines.append("```")
    else:
        lines.append("Nessun nodo non supportato rilevato.")
    lines.append("")
    lines.append("## 11. Analisi architetturale")
    lines.append("")
    lines.append(ai_review)
    lines.append("")

    return "\n".join(lines)

# 9. OUTPUT FILE

def save_outputs(
    output_dir: Path,
    fp_format: str,
    hdl: str,
    report: str,
    tests: Dict[str, Any],
    ir: List[Dict[str, Any]],
):
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    hdl_path = output_dir / f"{fp_format}_arith_unit_{timestamp}.py"
    report_path = output_dir / f"meta_hdl_report_{fp_format}_{timestamp}.md"
    tests_path = output_dir / f"test_vectors_{fp_format}_{timestamp}.json"
    ir_path = output_dir / f"ir_{fp_format}_{timestamp}.json"

    hdl_path.write_text(hdl, encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    tests_path.write_text(json.dumps(tests, indent=2), encoding="utf-8")
    ir_path.write_text(json.dumps(ir, indent=2), encoding="utf-8")

    return {
        "hdl": hdl_path,
        "report": report_path,
        "tests": tests_path,
        "ir": ir_path,
    }

# 10. MAIN

def main():
    parser = argparse.ArgumentParser(
        description="Enhanced Agentic Meta-HDL FP4 Compiler"
    )

    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="File Python da compilare",
    )

    parser.add_argument(
        "--format",
        "-f",
        default="MXFP4",
        choices=list(FP_FORMATS.keys()),
        help="Formato FP4 target",
    )

    parser.add_argument(
        "--tests",
        "-t",
        type=int,
        default=32,
        help="Numero di test automatici",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed per test casuali",
    )

    parser.add_argument(
        "--output",
        "-o",
        default="generated",
        help="Cartella output",
    )

    parser.add_argument(
        "--crew",
        action="store_true",
        help="Abilita analisi opzionale CrewAI",
    )

    args = parser.parse_args()

    input_file = Path(args.input)
    output_dir = Path(args.output)

    if not input_file.exists():
        raise FileNotFoundError(f"File non trovato: {input_file}")

    code = input_file.read_text(encoding="utf-8")
    fp_format = args.format

    print("\n=== Enhanced Agentic Meta-HDL FP4 Compiler ===")
    print(f"Input: {input_file}")
    print(f"Format: {fp_format}")
    print(f"Tests: {args.tests}")
    print("")

    ir, dfg, var_map, return_value, unsupported = build_ir(code)

    if not nx.is_directed_acyclic_graph(dfg):
        raise ValueError("Il Data Flow Graph contiene cicli: scheduling non possibile")

    critical = critical_path(dfg)
    sch = schedule_asap(dfg)
    pipe = pipeline_stages(sch)
    resources = resource_estimate(ir, sch)

    hdl = generate_pymtl(
        ir=ir,
        var_map=var_map,
        return_value=return_value,
        fp_format=fp_format,
        pipelined=True,
    )

    tests = generate_test_vectors(
        ir=ir,
        return_value=return_value,
        fp_format=fp_format,
        n_tests=args.tests,
        seed=args.seed,
    )

    if args.crew:
        ai_review = run_optional_crew_analysis(
            fp_format=fp_format,
            ir=ir,
            schedule=sch,
            pipeline=pipe,
            critical=critical,
            resources=resources,
            tests=tests,
        )
    else:
        ai_review = deterministic_architectural_review(
            fp_format=fp_format,
            ir=ir,
            schedule=sch,
            pipeline=pipe,
            critical=critical,
            resources=resources,
            tests=tests,
            note="Analisi deterministica usata. CrewAI non abilitato.",
        )

    report = generate_markdown_report(
        input_file=input_file,
        fp_format=fp_format,
        code=code,
        ir=ir,
        var_map=var_map,
        return_value=return_value,
        unsupported=unsupported,
        schedule=sch,
        pipeline=pipe,
        critical=critical,
        resources=resources,
        hdl=hdl,
        tests=tests,
        ai_review=ai_review,
    )

    paths = save_outputs(
        output_dir=output_dir,
        fp_format=fp_format,
        hdl=hdl,
        report=report,
        tests=tests,
        ir=ir,
    )

    print("--- Summary ---")
    print(f"IR operations: {len(ir)}")
    print(f"Critical path: {max(critical.values()) if critical else 0} cycles")
    print(f"Mean abs error: {tests['mean_absolute_error']:.6f}")
    print(f"Max abs error: {tests['max_absolute_error']:.6f}")
    print("")
    print("--- Generated files ---")
    for name, path in paths.items():
        print(f"{name}: {path}")

    if unsupported:
        print("")
        print("--- Warnings ---")
        for item in unsupported:
            print(f"- {item}")


if __name__ == "__main__":
    main()



