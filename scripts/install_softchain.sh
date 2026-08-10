#!/bin/bash
# =============================================================================
#  OpenWXSDR — Soft-decode chain installer
# =============================================================================
#  Builds and installs the binaries needed for the fsk_demod soft-bit decode
#  chain (auto_rx method) and a working dft_detect:
#
#    fsk_demod   — soft-symbol FSK demodulator (from radiosonde_auto_rx)
#    dft_detect  — correlation-based sonde type detector (IQ-capable build)
#    iq_dec      — inline IQ DC-removal front-end (enables decoders.iq_dc_block)
#
#  Sources are taken from the radiosonde_auto_rx repository because it pins
#  known-good versions of both tools (OpenWXSDR issue: unpinned rs1729/RS
#  clones produced per-install behavior differences, including completely
#  broken dft_detect binaries).
#
#  Usage:  ./scripts/install_softchain.sh [--with-decoders]
#    --with-decoders   Also install auto_rx's rs41mod/dfm09mod/m10mod/m20mod
#                      builds (recommended: they support --softin/--json/--ecc)
#
#  Run from the OpenWXSDR installation directory.
# =============================================================================
set -e

INSTALL_DIR="$(pwd)"
DECODER_DIR="${INSTALL_DIR}/decoders/rs1729"
BUILD_DIR="/tmp/openwxsdr_softchain_build"
AUTORX_REPO="https://github.com/projecthorus/radiosonde_auto_rx.git"
WITH_DECODERS=0
[ "$1" = "--with-decoders" ] && WITH_DECODERS=1

echo "============================================="
echo " OpenWXSDR soft-decode chain installer"
echo "============================================="

if [ ! -d "$DECODER_DIR" ]; then
    echo "ERROR: $DECODER_DIR not found — run this from the OpenWXSDR install directory."
    exit 1
fi

echo "[1/4] Installing build dependencies..."
sudo apt-get install -y build-essential cmake git >/dev/null

echo "[2/4] Cloning radiosonde_auto_rx (pinned, known-good tool versions)..."
rm -rf "$BUILD_DIR"
git clone --depth 1 "$AUTORX_REPO" "$BUILD_DIR"

echo "[3/4] Building demodulators/decoders (this takes a few minutes on a Pi)..."
cd "$BUILD_DIR/auto_rx"
./build.sh

# iq_dec (inline DC-removal) is defined in demod/mod's Makefile but auto_rx's
# top-level build.sh doesn't always emit it — build it explicitly so the install
# step can find it. Best-effort; install_bin warns if it's still absent.
if [ -d "$BUILD_DIR/demod/mod" ]; then
    make -C "$BUILD_DIR/demod/mod" iq_dec >/dev/null 2>&1 \
        && cp -f "$BUILD_DIR/demod/mod/iq_dec" "$BUILD_DIR/auto_rx/iq_dec" 2>/dev/null || true
fi

# build.sh places the built binaries in the auto_rx working directory
echo "[4/4] Installing binaries into $DECODER_DIR ..."
install_bin() {
    local name="$1"
    local src dst
    src="$(find "$BUILD_DIR" -name "$name" -type f -executable | head -1)"
    if [ -z "$src" ]; then
        echo "  WARNING: $name not found in build output — skipped"
        return 1
    fi
    dst="$DECODER_DIR/$name"
    # Copy to a temp name then atomically rename into place. A plain `cp` over a
    # binary the running service is currently executing fails with "Text file
    # busy" (ETXTBSY); `mv` (rename) replaces the name even while the old inode
    # stays live for the running process. The service keeps using the OLD binary
    # until it is restarted — expected.
    cp "$src" "$dst.new"
    chmod +x "$dst.new"
    if ! mv -f "$dst.new" "$dst" 2>/dev/null; then
        rm -f "$dst.new"
        echo "  WARNING: could not install $name (in use?) — stop the service and retry"
        return 1
    fi
    echo "  installed: $name  ($(du -h "$dst" | cut -f1))"
}

# || true: one binary being busy/missing must not abort the others (set -e).
install_bin fsk_demod || true
install_bin dft_detect || true
install_bin iq_dec || true   # inline DC-removal stage (decoders.iq_dc_block)

if [ "$WITH_DECODERS" = "1" ]; then
    for d in rs41mod dfm09mod m10mod m20mod rs92mod imet54mod lms6mod mp3h1mod; do
        install_bin "$d" || true
    done
fi

echo ""
echo "Verification:"
# fsk_demod prints usage on bad invocation — any output means it runs
if "$DECODER_DIR/fsk_demod" 2>&1 | head -2 | grep -qi "usage\|fsk"; then
    echo "  fsk_demod: OK (runs and prints usage)"
else
    echo "  fsk_demod: WARNING - unexpected output, test manually"
fi
# dft_detect must produce SOME output (the broken field binary printed nothing)
DFT_OUT="$("$DECODER_DIR/dft_detect" --help 2>&1 | head -3)"
if [ -n "$DFT_OUT" ]; then
    echo "  dft_detect: OK (produces output)"
else
    echo "  dft_detect: WARNING - no output at all, test manually"
fi
# iq_dec is optional (only needed when decoders.iq_dc_block: true)
if [ -x "$DECODER_DIR/iq_dec" ]; then
    echo "  iq_dec: OK (installed; enable with decoders.iq_dc_block: true)"
else
    echo "  iq_dec: not installed (optional — needed only for iq_dc_block)"
fi

rm -rf "$BUILD_DIR"
echo ""
echo "Done. Restart the service: sudo systemctl restart openwxsdr"
echo "Then check the log for: 'Using fsk_demod soft-bit decode chain'"
