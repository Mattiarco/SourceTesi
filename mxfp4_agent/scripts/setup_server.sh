#!/usr/bin/env bash
# Installa nella home, SENZA sudo, tutto ciò che serve alla pipeline mxfp4_agent:
#
#   JDK 17 + sbt         tramite Coursier (binario `cs` del repo, o scaricato)
#   OSS CAD Suite        yosys, nextpnr-ecp5, verilator (build statiche ufficiali)
#   cache sbt/Chisel     dipendenze Maven, plugin Chisel e firtool pre-scaricati,
#                        così le run successive funzionano anche offline
#
# È idempotente: rilanciarlo salta ciò che è già presente. Uso:
#
#   bash scripts/setup_server.sh                     # installa in ~/.local/mxfp4
#   bash scripts/setup_server.sh --prefix /scratch/me/mxfp4
#   bash scripts/setup_server.sh --models qwen2.5-coder:14b,qwen2.5-coder:32b
#   bash scripts/setup_server.sh --add-to-bashrc     # aggiunge `source env.sh` a ~/.bashrc
#   bash scripts/setup_server.sh --skip-warmup       # niente elaborazione di prova
#
# Alla fine:   source ~/.local/mxfp4/env.sh   &&   python3 run.py --doctor
set -euo pipefail

PREFIX="${HOME}/.local/mxfp4"
MODELS=""
ADD_BASHRC=0
WARMUP=1
OSS_URL="${OSS_CAD_SUITE_URL:-}"   # forzabile, es. per una release precisa
JDK="${MXFP4_JDK:-temurin:17}"
SBT_VERSION="1.10.1"               # deve coincidere con toolchain/scaffold.py

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --models) MODELS="$2"; shift 2 ;;
    --add-to-bashrc) ADD_BASHRC=1; shift ;;
    --skip-warmup) WARMUP=0; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "opzione sconosciuta: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${PREFIX}/bin"
mkdir -p "${BIN}"
log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

# Architettura: decide i binari scaricati (Coursier, OSS CAD Suite, Ollama).
#   CS_ARCH    nome nell'asset di Coursier     cs-<CS_ARCH>-pc-linux.gz
#   OSS_ARCH   nome nell'asset di OSS CAD Suite oss-cad-suite-linux-<OSS_ARCH>-AAAAMMGG.tgz
#   OLLAMA_ARCH nome nell'archivio di Ollama   ollama-linux-<OLLAMA_ARCH>.tgz
#   ELF_MACHINE campo e_machine dell'header ELF (per riconoscere il `cs` del repo)
OS="$(uname -s)"
ARCH="$(uname -m)"
case "${OS}-${ARCH}" in
  Linux-x86_64|Linux-amd64)
    CS_ARCH="x86_64"; OSS_ARCH="x64";   OLLAMA_ARCH="amd64"; ELF_MACHINE="3e00" ;;
  Linux-aarch64|Linux-arm64)
    CS_ARCH="aarch64"; OSS_ARCH="arm64"; OLLAMA_ARCH="arm64"; ELF_MACHINE="b700" ;;
  *)
    echo "piattaforma non supportata: ${OS}-${ARCH} (supportate: Linux x86_64, Linux aarch64)" >&2
    exit 1 ;;
esac
log "piattaforma ${OS}-${ARCH}"

# True se $1 è un eseguibile ELF a 64 bit per l'architettura di questa macchina.
# Legge l'header direttamente (`file` spesso non è installato sui server):
# byte 0-3 = \x7fELF, byte 4 = 02 (64 bit), byte 18-19 = e_machine little-endian.
elf_matches_host() {
  [[ -f "$1" ]] || return 1
  local magic cls mach
  magic="$(od -An -tx1 -N4 "$1" 2>/dev/null | tr -d ' \n')"
  cls="$(od -An -tx1 -j4 -N1 "$1" 2>/dev/null | tr -d ' \n')"
  mach="$(od -An -tx1 -j18 -N2 "$1" 2>/dev/null | tr -d ' \n')"
  [[ "${magic}" == "7f454c46" && "${cls}" == "02" && "${mach}" == "${ELF_MACHINE}" ]]
}

# ------------------------------------------------------------------ Coursier
CS="${BIN}/cs"
if [[ -x "${CS}" ]] && ! elf_matches_host "${CS}"; then
  warn "Coursier in ${CS} non è per ${ARCH}: lo riscarico"
  rm -f "${CS}"
fi
if [[ ! -x "${CS}" ]]; then
  if elf_matches_host "${REPO}/cs"; then
    log "Coursier: uso il binario del repo (${ARCH})"
    cp "${REPO}/cs" "${CS}"
  else
    [[ -f "${REPO}/cs" ]] && log "Coursier: il binario del repo non è per ${ARCH}, lo ignoro"
    log "Coursier: download cs-${CS_ARCH}-pc-linux.gz"
    curl -fL "https://github.com/coursier/coursier/releases/latest/download/cs-${CS_ARCH}-pc-linux.gz" \
      | gzip -d > "${CS}"
  fi
  chmod +x "${CS}"
fi
ok "coursier $("${CS}" version)"

# ------------------------------------------------------------- JDK 17 + sbt
log "JDK ${JDK} (Scala 2.13.14 / Chisel 6 sono testati su 17)"
JAVA_HOME_17="$("${CS}" java-home --jvm "${JDK}")"
ok "JAVA_HOME=${JAVA_HOME_17}"

# Launcher fissato alla stessa versione del progetto: con `cs install sbt` si
# otterrebbe l'ultimo (2.x), che funziona ma rende la toolchain non riproducibile.
if ! grep -aq "v${SBT_VERSION}" "${BIN}/sbt" 2>/dev/null; then
  log "sbt ${SBT_VERSION} (launcher + client sbtn)"
  "${CS}" install --install-dir "${BIN}" "sbt:${SBT_VERSION}" "sbtn:${SBT_VERSION}" >/dev/null
fi
ok "sbt ${SBT_VERSION} in ${BIN}"

# ------------------------------------------------------------ OSS CAD Suite
if [[ ! -x "${PREFIX}/oss-cad-suite/bin/yosys" ]]; then
  if [[ -z "${OSS_URL}" ]]; then
    OSS_URL="$(curl -fsSL https://api.github.com/repos/YosysHQ/oss-cad-suite-build/releases/latest \
      | grep -o "https://[^\"]*/oss-cad-suite-linux-${OSS_ARCH}-[0-9]\{8\}\.tgz" | head -1)"
  fi
  [[ -n "${OSS_URL}" ]] || {
    echo "URL di OSS CAD Suite per linux-${OSS_ARCH} non trovato nell'ultima release" \
         "(imposta OSS_CAD_SUITE_URL a mano)" >&2; exit 1; }
  log "OSS CAD Suite: ${OSS_URL}"
  tmp="$(mktemp -d)"
  curl -fL -o "${tmp}/oss.tgz" "${OSS_URL}"
  tar -xzf "${tmp}/oss.tgz" -C "${PREFIX}"
  rm -rf "${tmp}"
  echo "${OSS_URL}" > "${PREFIX}/oss-cad-suite/RELEASE_URL"
fi
ok "$("${PREFIX}/oss-cad-suite/bin/yosys" -V)"
ok "$("${PREFIX}/oss-cad-suite/bin/verilator" --version)"
ok "$("${PREFIX}/oss-cad-suite/bin/nextpnr-ecp5" --version 2>&1 | head -1)"

# ------------------------------------------------------------------- env.sh
# NB: si espone solo oss-cad-suite/bin, NON il suo `environment` completo, che
# sostituirebbe anche python3 e altri tool di sistema.
cat > "${PREFIX}/env.sh" <<EOF
# generato da scripts/setup_server.sh — sorgente con: source ${PREFIX}/env.sh
export MXFP4_PREFIX="${PREFIX}"
export JAVA_HOME="${JAVA_HOME_17}"
export PATH="${BIN}:\${JAVA_HOME}/bin:${PREFIX}/oss-cad-suite/bin:\${PATH}"
# Chisel/firtool e sbt scrivono qui le cache (riusate offline)
export COURSIER_CACHE="\${COURSIER_CACHE:-\${HOME}/.cache/coursier}"
EOF
ok "ambiente in ${PREFIX}/env.sh"
if [[ ${ADD_BASHRC} -eq 1 ]] && ! grep -qF "${PREFIX}/env.sh" "${HOME}/.bashrc" 2>/dev/null; then
  echo "source ${PREFIX}/env.sh" >> "${HOME}/.bashrc"
  ok "aggiunto a ~/.bashrc"
fi
# shellcheck disable=SC1091
source "${PREFIX}/env.sh"

# ------------------------------------------------------------------- Ollama
if command -v ollama >/dev/null 2>&1 || curl -fsS "${OLLAMA_HOST:-http://localhost:11434}/api/version" >/dev/null 2>&1; then
  ok "Ollama raggiungibile"
  if [[ -n "${MODELS}" ]]; then
    IFS=',' read -ra ms <<< "${MODELS}"
    for m in "${ms[@]}"; do
      log "ollama pull ${m}"
      ollama pull "${m}"
    done
  fi
else
  warn "Ollama non trovato. Senza sudo: scarica https://ollama.com/download/ollama-linux-${OLLAMA_ARCH}.tgz,"
  warn "estrailo in ${PREFIX}/ollama e avvia '${PREFIX}/ollama/bin/ollama serve' (es. in tmux)."
fi

# ------------------------------------------------- warm-up: cache sbt/Chisel
# Elabora una volta il design di riferimento con il provider mock: scarica
# Scala, Chisel, il plugin e il binario firtool nelle cache di Coursier/sbt,
# e collauda verilator + yosys + nextpnr. Dopo questo passo l'elaborazione
# funziona anche senza rete (sbt usa la cache locale).
if [[ ${WARMUP} -eq 1 ]]; then
  log "warm-up: selftest completo (sbt + verilator + yosys + nextpnr), può durare qualche minuto"
  WARM="${PREFIX}/warmup"
  rm -rf "${WARM}"
  if (cd "${REPO}" && python3 run.py --selftest --strict -o "${WARM}" -q); then
    ok "selftest superato: toolchain completa e cache popolate"
  else
    warn "selftest fallito: controlla l'output sopra e 'python3 run.py --doctor'"
    exit 1
  fi
fi

log "fatto. Ricorda:  source ${PREFIX}/env.sh"
