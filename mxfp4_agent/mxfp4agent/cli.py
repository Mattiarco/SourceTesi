"""Interfaccia a riga di comando."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import Config
from .llm import DEFAULT_MODELS, build_provider
from .toolchain import ToolchainRunner, format_tool_report, tool_report
from .workflow import Workflow

BANNER = r"""
  __  ____  __ _____ ____  _  _
 |  \/  \ \/ /|  ___|  _ \| || |    Meta-HDL agentic generator
 | |\/| |\  / | |_  | |_) | || |_   Planner → Coder → Reviewer → Tester
 |_|  |_|/_/  |_|   |  __/|__   _|  Chisel/SystemVerilog + Verilator
                     |_|      |_|
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mxfp4-agent",
        description="Genera unità aritmetiche MXFP4 in Chisel/SystemVerilog con un "
                    "workflow multi-agente e le verifica con Verilator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""esempi:
  mxfp4-agent "unità dot-product MXFP4 a 32 elementi, combinatoria"
  mxfp4-agent "MAC MXFP4 pipelined a 2 stadi con accumulo FP32" --provider claude
  mxfp4-agent "moltiplicatore element-wise MXFP4" --target systemverilog
  mxfp4-agent --doctor                 # verifica ambiente
  mxfp4-agent --selftest               # smoke test offline (provider mock)
""")
    p.add_argument("request", nargs="?", help="richiesta in linguaggio naturale")
    p.add_argument("-f", "--request-file", type=Path, help="leggi la richiesta da file")

    g = p.add_argument_group("LLM")
    g.add_argument("--provider", choices=["ollama", "claude", "mock"], default="ollama")
    g.add_argument("--model", help=f"default: {DEFAULT_MODELS}")
    g.add_argument("--planner-model")
    g.add_argument("--coder-model")
    g.add_argument("--reviewer-model")
    g.add_argument("--tester-model")
    g.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    g.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    g.add_argument("--temperature", type=float, default=0.2)
    g.add_argument("--max-tokens", type=int, default=None,
                   help="budget di output; default per provider (claude: 32000, "
                        "perché l'extended thinking consuma max_tokens)")
    g.add_argument("--timeout", type=int, default=900)

    d = p.add_argument_group("design")
    d.add_argument("--target", choices=["chisel", "systemverilog"],
                   help="forza il Meta-HDL (default: lo sceglie il Planner)")
    d.add_argument("-k", "--block-size", type=int, default=32)

    w = p.add_argument_group("workflow")
    w.add_argument("-o", "--outdir", type=Path, default=Path("out"))
    w.add_argument("--max-fix-rounds", type=int, default=4)
    w.add_argument("--no-static-review", action="store_true")
    w.add_argument("--few-shot", action="store_true",
                   help="includi un design di riferimento nel prompt (utile con modelli piccoli)")
    w.add_argument("--vectors", type=int, default=64, help="vettori casuali di test")
    w.add_argument("--seed", type=int, default=1234)
    w.add_argument("--keep-going", action="store_true")
    w.add_argument("-q", "--quiet", action="store_true")
    w.add_argument("-c", "--config", type=Path, help="file JSON di configurazione")

    m = p.add_argument_group("modalità speciali")
    m.add_argument("--doctor", action="store_true", help="controlla LLM e toolchain")
    m.add_argument("--selftest", action="store_true", help="pipeline completa con provider mock")
    m.add_argument("--resume", type=Path, metavar="DIR",
                   help="riesegui solo la toolchain su un progetto già generato")
    return p


def cmd_doctor(args) -> int:
    print(BANNER)
    rep = tool_report()
    print("Toolchain esterna:")
    print(format_tool_report(rep))
    print("\nProvider LLM:")
    for kind in ("ollama", "claude", "mock"):
        try:
            prov = build_provider(kind, model=args.model, host=args.host, api_key=args.api_key)
            ok, detail = prov.health_check()
        except Exception as e:  # pragma: no cover
            ok, detail = False, str(e)
        print(f"  {'✔' if ok else '✘'} {kind:<8} {detail}")
    missing = [k for k, v in rep.items() if not v and k in ("verilator", "sbt")]
    if missing:
        print("\nPer installare i tool mancanti:")
        if "verilator" in missing:
            print("  verilator : sudo apt install verilator   |  brew install verilator")
        if "sbt" in missing:
            print("  sbt+JDK17 : https://www.scala-sbt.org/download  (serve solo per Chisel)")
    return 0 if not missing else 1


def cmd_resume(args) -> int:
    root = Path(args.resume)
    import json

    plan_file = root / "plan.json"
    if not plan_file.exists():
        print(f"plan.json non trovato in {root}", file=sys.stderr)
        return 2
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    runner = ToolchainRunner(root, plan["module_name"], plan["meta_hdl"])
    rep = runner.run_all()
    for s in rep.stages:
        state = "SKIP" if s.skipped else ("OK" if s.ok else "FAIL")
        print(f"[{state:<4}] {s.stage}")
        if not s.ok and not s.skipped:
            print(s.log[-4000:])
    return 0 if rep.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.doctor:
        return cmd_doctor(args)
    if args.resume:
        return cmd_resume(args)

    if args.config:
        cfg = Config.from_file(args.config)
    else:
        request = args.request or (args.request_file.read_text(encoding="utf-8")
                                   if args.request_file else None)
        if args.selftest and not request:
            request = ("Unità di dot-product MXFP4 combinatoria su blocchi da 32 elementi, "
                       "con accumulo intero esatto e uscita dell'esponente condiviso.")
        if not request:
            build_parser().print_help()
            return 2
        cfg = Config(
            request=request.strip(),
            target=args.target,
            block_size=args.block_size,
            provider="mock" if args.selftest else args.provider,
            model=args.model,
            planner_model=args.planner_model,
            coder_model=args.coder_model,
            reviewer_model=args.reviewer_model,
            tester_model=args.tester_model,
            host=args.host,
            api_key=args.api_key,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            outdir=args.outdir,
            max_fix_rounds=0 if args.selftest else args.max_fix_rounds,
            static_review=not args.no_static_review and not args.selftest,
            few_shot=args.few_shot,
            num_random_vectors=args.vectors,
            seed=args.seed,
            keep_going=args.keep_going or args.selftest,
            verbose=not args.quiet,
        )

    if not args.quiet:
        print(BANNER)
        print(f"Richiesta : {cfg.request}")
        print(f"Provider  : {cfg.provider} ({cfg.model or DEFAULT_MODELS.get(cfg.provider)})")
        print(f"Output    : {cfg.outdir.resolve()}\n")

    result = Workflow(cfg).run()

    print()
    if result.ok:
        print(f"✅ {result.message}")
    else:
        print(f"⚠️  {result.message}")
    if result.workdir:
        print(f"📁 {result.workdir.resolve()}")
        for f in result.files:
            print(f"   • {f.path}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
