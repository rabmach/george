#!/usr/bin/env bash
# healthcheck.sh — weekly boot/disk/firmware/security summary
set -uo pipefail

echo "=== [1/5] boot errors (journalctl -p err -b) ==="
journalctl -p err -b | tail -30

echo
echo "=== [2/5] disk free (/ and /home) ==="
df -h / /home

echo
echo "=== [3/5] NVMe SMART ==="
sudo smartctl -a /dev/nvme0n1 2>/dev/null \
  | grep -E "Model Number|Firmware|Percentage Used|Data Units (Written|Read)|Critical Warning|Temperature" \
  || echo "smartctl not available"

echo
echo "=== [4/5] pending firmware updates ==="
if command -v fwupdmgr >/dev/null; then
  fwupdmgr get-updates 2>/dev/null | tail -10
else
  echo "fwupdmgr not installed"
fi

echo
echo "=== [5/5] unfixed CVEs (debsecan) ==="
debsecan --suite trixie --format report 2>/dev/null | tail -20
