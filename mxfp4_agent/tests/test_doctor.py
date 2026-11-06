"""--doctor: controlli dell'ambiente e istruzioni senza sudo."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mxfp4agent import doctor  # noqa: E402
from mxfp4agent.toolchain.shell import CmdResult  # noqa: E402


def _fake_tools(outputs: dict):
    """Sostituisce which/run del modulo doctor con risposte preconfezionate."""
    orig = doctor.which, doctor.run
    doctor.which = lambda name: f"/opt/{name}" if name in outputs else None
    doctor.run = lambda cmd, **kw: CmdResult(cmd, 0, *outputs[cmd[0]])
    return orig


def _restore(orig):
    doctor.which, doctor.run = orig


def test_java_versions():
    for text, ok, major in (('openjdk version "17.0.21" 2026-10-20', True, "17"),
                            ('java version "21.0.1" 2023-10-17 LTS', True, "21"),
                            ('java version "1.8.0_392"', False, "8")):
        orig = _fake_tools({"java": ("", text)})
        try:
            c = doctor.check_java()
        finally:
            _restore(orig)
        assert c.ok is ok and f"Java {major}" in c.detail, (text, c)
    orig = _fake_tools({})
    try:
        c = doctor.check_java()
    finally:
        _restore(orig)
    assert not c.ok and "cs java-home --jvm temurin:17" in c.hint


def test_missing_tools_give_no_sudo_hints():
    orig = _fake_tools({})
    try:
        v = doctor.check_tool("verilator", ["verilator", "--version"], doctor.OSS,
                              pattern=r"Verilator (\d+\.\d+)")
        s = doctor.check_sbt()
        g = doctor.check_gpu()
    finally:
        _restore(orig)
    assert not v.ok and "oss-cad-suite" in v.hint and "sudo apt" not in v.hint
    assert not s.ok and "cs install" in s.hint
    assert not g.ok and not g.required


def test_tool_version_parsing():
    orig = _fake_tools({"verilator": ("Verilator 5.055 devel rev v5.054", ""),
                        "yosys": ("Yosys 0.70+344 (git sha1 2f2307ad6)", ""),
                        "nvidia-smi": ("NVIDIA A100, 81920 MiB, 1200 MiB, 550.54\n", "")})
    try:
        v = doctor.check_tool("verilator", ["verilator", "--version"], "", pattern=r"Verilator (\d+\.\d+)")
        y = doctor.check_tool("yosys", ["yosys", "-V"], "", pattern=r"Yosys ([\d.+]+)")
        g = doctor.check_gpu()
    finally:
        _restore(orig)
    assert v.ok and v.detail.startswith("5.055")
    assert y.ok and y.detail.startswith("0.70+344")
    assert g.ok and "A100" in g.detail


def test_ollama_checks_against_fake_server():
    from test_ollama import FakeOllama

    srv = FakeOllama(models=["qwen2.5-coder:14b"])
    try:
        checks = doctor.check_ollama(srv.url, ["qwen2.5-coder:14b", "qwen2.5-coder:32b"])
    finally:
        srv.close()
    by = {c.name: c for c in checks}
    assert by["ollama"].ok and "0.9.9-test" in by["ollama"].detail
    assert by["modello qwen2.5-coder:14b"].ok and "context 32768" in by["modello qwen2.5-coder:14b"].detail
    assert not by["modello qwen2.5-coder:32b"].ok
    assert by["modello qwen2.5-coder:32b"].hint == "ollama pull qwen2.5-coder:32b"


def test_unreachable_ollama():
    (c,) = doctor.check_ollama("http://127.0.0.1:9", ["x"])
    assert not c.ok and "ollama-linux-amd64.tgz" in c.hint


def test_format_marks_required_and_optional():
    out = doctor.format_checks([doctor.Check("a", True, "ok"),
                                doctor.Check("b", False, "manca", "installa b"),
                                doctor.Check("c", False, "manca", "opzionale", required=False)])
    assert "✔ a" in out and "✘ b" in out and "! c" in out and "→ installa b" in out


def test_arch_aware_install_hints():
    assert "oss-cad-suite-linux-arm64-" in doctor.oss_hint("aarch64")
    assert "oss-cad-suite-linux-x64-" in doctor.oss_hint("x86_64")
    assert "ollama-linux-arm64.tgz" in doctor.ollama_hint("aarch64")
    assert "ollama-linux-amd64.tgz" in doctor.ollama_hint("x86_64")


def test_firtool_check(tmp_path):
    # x86_64 senza CHISEL_FIRTOOL_PATH: Chisel lo scarica da sé, niente da segnalare
    assert doctor.check_firtool("x86_64", {}) is None
    # aarch64 senza CHISEL_FIRTOOL_PATH: errore con l'indicazione dello script
    c = doctor.check_firtool("aarch64", {})
    assert not c.ok and c.required and "setup_server.sh" in c.hint
    # cartella inesistente (o variabile puntata al file invece che alla cartella)
    c = doctor.check_firtool("aarch64", {"CHISEL_FIRTOOL_PATH": str(tmp_path / "manca")})
    assert not c.ok and "CARTELLA" in c.hint
    d = tmp_path / "bin"
    d.mkdir()
    (d / "firtool").write_text("finto", encoding="utf-8")
    env = {"CHISEL_FIRTOOL_PATH": str(d)}
    orig = doctor.run
    try:
        doctor.run = lambda cmd, **kw: CmdResult(
            cmd, 0, "LLVM (http://llvm.org/):\n  LLVM version 18.0.0git\nCIRCT firtool-1.62.1\n", "")
        ok = doctor.check_firtool("aarch64", env)
        doctor.run = lambda cmd, **kw: CmdResult(cmd, 0, "CIRCT unknown git version\n", "")
        unknown = doctor.check_firtool("aarch64", env)
        doctor.run = lambda cmd, **kw: CmdResult(cmd, 0, "CIRCT firtool-1.70.0\n", "")
        other = doctor.check_firtool("x86_64", env)
    finally:
        doctor.run = orig
    assert ok.ok and "1.62.1" in ok.detail
    assert not unknown.ok and "unknown git version" in unknown.detail
    assert not other.ok and "1.70.0" in other.detail and "serve CIRCT firtool-1.62.1" in other.detail
