"""JSON schema degli output strutturati (campo `format` di Ollama `/api/chat`).

Con uno schema Ollama vincola il campionamento alla grammatica: il modello non
può emettere testo fuori dal JSON né dimenticare un campo obbligatorio. Il
parsing tollerante (`utils.parse_json_response`) resta come rete di sicurezza
per i server Ollama vecchi e per il provider mock.

Gli schemi sono volutamente piatti e senza vincoli esotici (niente `oneOf`,
`pattern`, `$ref`): il convertitore schema→grammatica di llama.cpp li supporta
in modo parziale.
"""
from __future__ import annotations

_STR = {"type": "string"}
_INT = {"type": "integer"}

PORT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": _STR,
        "dir": {"type": "string", "enum": ["in", "out"]},
        "width": _INT,
        "type": {"type": "string", "enum": ["UInt", "SInt", "Bool"]},
        "description": _STR,
    },
    "required": ["name", "dir", "width", "type", "description"],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "module_name": _STR,
        "rationale": _STR,
        "clocking": {"type": "string", "enum": ["combinational", "sequential"]},
        "latency_cycles": _INT,
        "parameters": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": _STR, "value": _INT, "description": _STR},
            "required": ["name", "value"]}},
        "ports": {"type": "array", "items": PORT_SCHEMA},
        "algorithm": {"type": "array", "items": _STR},
        "numerics": {"type": "object", "properties": {
            "accumulator_width": _INT, "rounding": _STR, "saturation": _STR,
            "special_cases": {"type": "array", "items": _STR}},
            "required": ["accumulator_width", "rounding"]},
        "test_plan": {"type": "object", "properties": {
            "kernel": {"type": "string",
                       "enum": ["dot_product", "elementwise_mul", "elementwise_add", "to_bf16",
                                "custom"]},
            "num_random": _INT,
            "directed": {"type": "array", "items": _STR}},
            "required": ["kernel"]},
        "risks": {"type": "array", "items": _STR},
        "coder_prompt": _STR,
    },
    "required": ["module_name", "rationale", "clocking", "latency_cycles", "ports",
                 "algorithm", "numerics", "test_plan", "coder_prompt"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approved", "changes_required"]},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                "file": _STR, "where": _STR, "problem": _STR, "fix": _STR},
            "required": ["severity", "file", "problem", "fix"]}},
        "notes": _STR,
    },
    "required": ["verdict", "issues"],
}
