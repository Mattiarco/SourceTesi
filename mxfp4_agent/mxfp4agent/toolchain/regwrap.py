"""Wrapper a registri per misurare una Fmax reale con nextpnr.

Problema: un design combinatorio piazzato da solo non ha cammini fra registri,
quindi nextpnr non può dare una Fmax: dà solo il ritardo pin→pin, che dipende
da dove il placer mette i pin non vincolati (fino all'80% del ritardo è
routing verso i pad). Inoltre le unità element-wise hanno ~1300 bit di I/O e
non entrano nei 365 pin dell'LFE5U-85F CABGA381.

Soluzione (uguale per ogni design, quindi confrontabile):

    sin ─► [catena a scorrimento di IN_W flip-flop] ─► DUT ─► [OUT_W flip-flop] ─► XOR ─► sout

* gli ingressi del DUT sono le uscite di flip-flop, caricati in serie da un
  solo pin: ogni bit di ingresso è pilotato da un registro;
* le uscite del DUT sono registrate, poi ridotte con uno XOR su un solo pin
  (così nessuna uscita è "inutile" e la sintesi non può eliminare logica);
* il wrapper usa 4 pin (clk, rst, sin, sout) qualunque sia la larghezza.

La Fmax del clock `clk` è quindi registro→DUT→registro: il ritardo intrinseco
del datapath. I cammini sin→catena e registri→XOR→sout sono verso i pad e non
entrano nella Fmax. Per i design sequenziali il DUT riceve lo stesso clock e
`rst` dal pin, e la Fmax include i loro cammini interni.

Le porte del DUT si leggono dalla netlist JSON di Yosys: vale per qualunque
modulo, non solo per quelli con contratto.
"""
from __future__ import annotations

WRAPPER_NAME = "mxfp4_regwrap"
CLOCK_NAMES = ("clock", "clk")
RESET_NAMES = ("reset", "rst")


def dut_ports(netlist: dict, top: str) -> list[tuple[str, str, int]]:
    """Porte del modulo `top` come (nome, 'input'|'output', larghezza), in ordine."""
    mods = netlist.get("modules", {})
    mod = mods.get(top) or mods.get(f"\\{top}")
    if mod is None:
        raise KeyError(f"modulo {top!r} assente nella netlist ({', '.join(mods)})")
    return [(name, p["direction"], len(p["bits"])) for name, p in mod.get("ports", {}).items()]


def io_pins(ports: list[tuple[str, str, int]]) -> int:
    """Pin necessari per piazzare il DUT da solo."""
    return sum(w for _, _, w in ports)


def render_wrapper(top: str, ports: list[tuple[str, str, int]]) -> tuple[str, dict]:
    """Verilog del wrapper e informazioni (bit di ingresso/uscita registrati)."""
    clock = next((n for n, d, w in ports if d == "input" and n in CLOCK_NAMES and w == 1), None)
    reset = next((n for n, d, w in ports if d == "input" and n in RESET_NAMES and w == 1), None)
    ins = [(n, w) for n, d, w in ports if d == "input" and n not in (clock, reset)]
    outs = [(n, w) for n, d, w in ports if d == "output"]
    if not outs:
        raise ValueError(f"{top}: nessuna uscita, niente da misurare")
    in_w = sum(w for _, w in ins)
    out_w = sum(w for _, w in outs)

    conns, lo = [], 0
    if clock:
        conns.append(f"    .{clock}(clk)")
    if reset:
        conns.append(f"    .{reset}(rst)")
    for n, w in ins:
        conns.append(f"    .{n}(in_q[{lo + w - 1}:{lo}])")
        lo += w
    lo = 0
    for n, w in outs:
        conns.append(f"    .{n}(out_d[{lo + w - 1}:{lo}])")
        lo += w

    if in_w == 0:
        in_decl, shift = "", ""
    elif in_w == 1:
        in_decl = "  reg  in_q_r;\n  wire [0:0] in_q = in_q_r;\n"
        shift = "  always @(posedge clk) in_q_r <= sin;\n"
    else:
        in_decl = f"  reg  [{in_w - 1}:0] in_q;\n"
        shift = f"  always @(posedge clk) in_q <= {{in_q[{in_w - 2}:0], sin}};\n"

    verilog = f"""// GENERATO AUTOMATICAMENTE da mxfp4agent/toolchain/regwrap.py
// Wrapper di misura: ingressi da catena a scorrimento ({in_w} FF), uscite
// registrate ({out_w} FF) e ridotte a XOR. La Fmax di `clk` e' registro->{top}->registro.
module {WRAPPER_NAME} (
  input  wire clk,
  input  wire rst,
  input  wire sin,
  output wire sout
);
{in_decl}  reg  [{out_w - 1}:0] out_q;
  wire [{out_w - 1}:0] out_d;
{shift}
  {top} dut (
{(","+chr(10)).join(conns)}
  );

  always @(posedge clk) out_q <= out_d;
  assign sout = ^out_q;
endmodule
"""
    info = {"kind": "serial-in/xor-out", "in_bits": in_w, "out_bits": out_w,
            "registers_added": in_w + out_w, "pins": 4,
            "dut_clock": clock, "dut_reset": reset}
    return verilog, info
