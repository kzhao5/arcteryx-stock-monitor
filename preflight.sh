#!/usr/bin/env bash
# Can this machine run the stock monitor? Answers the three questions that
# actually decide it: outbound network, a working chromium, and a way to
# keep something scheduled.
#
#   bash preflight.sh
#
# Read-only. Installs nothing, changes nothing.

echo "=== host ==="
hostname
uname -sr
echo

echo "=== shared cluster? ==="
# Long-running personal processes on an HPC login node are usually against
# policy. If these resolve, check your site's acceptable-use rules first.
for c in sinfo squeue qstat bsub; do
  command -v "$c" >/dev/null 2>&1 && echo "found $c -> this looks like a shared HPC cluster"
done
[ -n "$SLURM_JOB_ID" ] && echo "inside SLURM job $SLURM_JOB_ID"
grep -qiE 'login|head' <<<"$(hostname)" && echo "hostname suggests a LOGIN node -- do not run long jobs here"
echo

echo "=== outbound https ==="
# Compute nodes often have no direct egress even when login nodes do.
for h in https://arcteryx.com https://pypi.org; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$h" 2>/dev/null)
  if [ "$code" = "000" ]; then
    echo "  $h  UNREACHABLE (blocked, or needs a proxy)"
  else
    echo "  $h  HTTP $code"
  fi
done
[ -n "$HTTPS_PROXY$https_proxy" ] && echo "  proxy env is set: ${HTTPS_PROXY:-$https_proxy}"
echo

echo "=== python ==="
for p in python3.12 python3.11 python3; do
  command -v "$p" >/dev/null 2>&1 && echo "  $p -> $($p -V 2>&1)"
done
command -v module >/dev/null 2>&1 && echo "  'module' available -- you may need: module load python"
echo

echo "=== chromium ==="
# playwright installs browsers into ~/.cache, no root needed. The usual
# failure is a missing system library, which DOES need root to fix.
if python3 -c "import playwright" 2>/dev/null; then
  BIN=$(python3 - <<'PY' 2>/dev/null
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(p.chromium.executable_path)
PY
)
  if [ -n "$BIN" ] && [ -x "$BIN" ]; then
    echo "  binary: $BIN"
    missing=$(ldd "$BIN" 2>/dev/null | grep 'not found' | awk '{print $1}' | sort -u)
    if [ -n "$missing" ]; then
      echo "  MISSING SYSTEM LIBS (needs root to install):"
      echo "$missing" | sed 's/^/    /'
    else
      echo "  all shared libraries resolve"
      python3 - <<'PY'
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True); b.close()
    print("  launch test: OK")
except Exception as e:
    print(f"  launch test: FAILED -- {e.__class__.__name__}: {str(e)[:200]}")
PY
    fi
  else
    echo "  playwright installed but no browser yet. Run: playwright install chromium"
  fi
else
  echo "  playwright not installed yet (pip install -r requirements.txt)"
fi
echo

echo "=== scheduling ==="
if crontab -l >/dev/null 2>&1; then
  echo "  crontab: usable"
else
  echo "  crontab: not usable here (shared clusters often disable it)"
fi
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  echo "  systemd --user: usable (survives logout only if lingering is enabled)"
else
  echo "  systemd --user: not usable"
fi
command -v tmux >/dev/null 2>&1 && echo "  tmux: available"
command -v screen >/dev/null 2>&1 && echo "  screen: available"
echo

echo "=== home quota ==="
df -h "$HOME" 2>/dev/null | tail -1
echo "  (chromium needs roughly 400MB under ~/.cache/ms-playwright)"
echo
echo "Done. See README.md -> 'Running it on a remote server' for how to read this."
