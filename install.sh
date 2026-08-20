#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
MAN_DIR="${HOME}/.local/share/man/man8"

echo "=== Installing Btrfs Recovery & Merging Toolset ==="

mkdir -p "${BIN_DIR}"
mkdir -p "${MAN_DIR}"

install -Dm755 "${SCRIPT_DIR}/scan_subvols.py" "${BIN_DIR}/btrfs-scan-subvols"
install -Dm755 "${SCRIPT_DIR}/declutter_home.py" "${BIN_DIR}/btrfs-declutter-home"
install -Dm755 "${SCRIPT_DIR}/clean_recovery.py" "${BIN_DIR}/btrfs-clean-recovery"

if [ -f "${SCRIPT_DIR}/btrfs-recovery-tool.8" ]; then
    install -Dm644 "${SCRIPT_DIR}/btrfs-recovery-tool.8" "${MAN_DIR}/btrfs-recovery-tool.8"
    echo "Installed man page to ${MAN_DIR}/btrfs-recovery-tool.8"
fi

echo "Installed btrfs recovery utilities to ${BIN_DIR}!"
echo "Commands available: btrfs-scan-subvols, btrfs-declutter-home, btrfs-clean-recovery"
