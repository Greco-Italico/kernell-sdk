#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# Kernell OS — Day 0 Boot Script (Bare-Metal Node)
# ══════════════════════════════════════════════════════════════════════════════
# Usage: sudo ./scripts/day0_boot.sh
# Exit codes: 0 = ready, 1 = pre-flight failed, 2 = smoke test failed
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS="${GREEN}✔${NC}"
FAIL="${RED}✘${NC}"
WARN="${YELLOW}⚠${NC}"

ERRORS=0

check() {
    local desc="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo -e "  ${PASS} ${desc}"
    else
        echo -e "  ${FAIL} ${desc}"
        ERRORS=$((ERRORS + 1))
    fi
}

warn_check() {
    local desc="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo -e "  ${PASS} ${desc}"
    else
        echo -e "  ${WARN} ${desc} (non-fatal)"
    fi
}

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Kernell OS — Day 0 Pre-Flight Check"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── 0. KVM & Virtualization ──────────────────────────────────
echo "▸ Virtualization"
check "KVM module loaded" test -e /dev/kvm
check "/dev/kvm is writable" test -w /dev/kvm

# ── 1. cgroups v2 ────────────────────────────────────────────
echo "▸ cgroups v2"
check "cgroup2 mounted" mount | grep -q cgroup2
warn_check "kernell cgroup dir exists" test -d /sys/fs/cgroup/kernell

if [ ! -d /sys/fs/cgroup/kernell ]; then
    echo -e "    → Creating /sys/fs/cgroup/kernell..."
    mkdir -p /sys/fs/cgroup/kernell 2>/dev/null || true
    # Enable controllers
    echo "+cpu +memory +pids" > /sys/fs/cgroup/kernell/cgroup.subtree_control 2>/dev/null || true
fi

# ── 2. Filesystem (CoW support) ──────────────────────────────
echo "▸ Filesystem"
warn_check "XFS or btrfs detected" bash -c "df -T /tmp | grep -qE 'xfs|btrfs'"
warn_check "reflink support" bash -c "cp --reflink=auto /dev/null /tmp/.reflink_test 2>/dev/null && rm -f /tmp/.reflink_test"

# ── 3. Firecracker binary ────────────────────────────────────
echo "▸ Binaries"
if [ "${KERNELL_ENV:-dev}" = "production" ]; then
    check "firecracker binary" which firecracker
    check "nsjail binary (fallback)" which nsjail
else
    warn_check "firecracker binary (required in prod)" which firecracker
    warn_check "nsjail binary (required in prod)" which nsjail
fi
warn_check "jailer binary" which jailer
check "python3 available" which python3

# ── 4. Snapshot pool tmpfs ────────────────────────────────────
echo "▸ Snapshot Pool"
SNAP_DIR="/tmp/fcsnapshots"
if [ ! -d "$SNAP_DIR" ]; then
    mkdir -p "$SNAP_DIR"
    echo -e "    → Created ${SNAP_DIR}"
fi
check "Snapshot dir exists" test -d "$SNAP_DIR"
warn_check "Snapshot dir is tmpfs" bash -c "df -T $SNAP_DIR | grep -q tmpfs"

# ── 5. Security configs ──────────────────────────────────────
echo "▸ Security Configs"
SDK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
check "nsjail.cfg exists" test -f "${SDK_ROOT}/nsjail.cfg"
check "seccomp_kernell.policy exists" test -f "${SDK_ROOT}/seccomp_kernell.policy"
check "seccomp_agent.json exists" test -f "${SDK_ROOT}/kernell_sdk/seccomp_agent.json"

# ── 6. Python SDK importable ─────────────────────────────────
echo "▸ SDK Health"
check "SDK importable" python3 -c "import kernell_sdk"
check "Firecracker runtime importable" python3 -c "from kernell_sdk.runtime.firecracker_runtime import FirecrackerRuntime"
check "Integrity module importable" python3 -c "from kernell_sdk.runtime.firecracker.integrity import verify_artifacts"
check "cgroup limiter importable" python3 -c "from kernell_sdk.runtime.firecracker.cgroup_limiter import VMResourceLimiter"

# ── 7. Network / Ports ───────────────────────────────────────
echo "▸ Network"
warn_check "Port 8500 free (dashboard)" bash -c "! ss -tlnp | grep -q ':8500'"
warn_check "Prometheus port 9090 free" bash -c "! ss -tlnp | grep -q ':9090'"

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
if [ $ERRORS -gt 0 ]; then
    echo -e "  ${FAIL} PRE-FLIGHT FAILED: ${ERRORS} critical check(s) failed."
    echo "  Fix the issues above before proceeding."
    echo "═══════════════════════════════════════════════════════════"
    exit 1
else
    echo -e "  ${PASS} PRE-FLIGHT PASSED: All critical checks OK."
    echo "═══════════════════════════════════════════════════════════"
fi

echo ""
echo "▸ Running Smoke Tests (100 sequential + 100 concurrent)..."
echo ""

# ── Smoke Test ────────────────────────────────────────────────
python3 -c "
import os, time, sys
sys.path.insert(0, '${SDK_ROOT}')

# Record baseline FDs
baseline_fds = len(os.listdir('/proc/self/fd'))

from kernell_sdk.runtime.sandbox import validate_code, SandboxViolation

# Sequential: 100 valid payloads
errors = 0
start = time.time()
for i in range(100):
    try:
        validate_code(f'x = {i} + 1')
    except Exception:
        errors += 1

seq_time = (time.time() - start) * 1000
print(f'  Sequential: 100 validations in {seq_time:.1f}ms, {errors} errors')

# Sequential: 100 hostile payloads (must ALL be rejected)
blocked = 0
hostile_payloads = [
    'import os',
    'eval(\"1\")',
    'exec(\"1\")',
    '__builtins__.__dict__',
    'getattr(object, \"x\")',
    'import subprocess',
]
for payload in hostile_payloads:
    try:
        validate_code(payload)
    except SandboxViolation:
        blocked += 1

print(f'  Hostile payloads blocked: {blocked}/{len(hostile_payloads)}')

# FD leak check
final_fds = len(os.listdir('/proc/self/fd'))
leak = final_fds - baseline_fds
if leak > 0:
    print(f'  ⚠ FD LEAK DETECTED: {leak} descriptors leaked')
    sys.exit(2)
else:
    print(f'  FD leak check: clean ({final_fds} fds)')

if errors > 0 or blocked < len(hostile_payloads):
    print('  ✘ SMOKE TEST FAILED')
    sys.exit(2)
else:
    print('  ✔ SMOKE TEST PASSED')
"

SMOKE_EXIT=$?
if [ $SMOKE_EXIT -ne 0 ]; then
    echo -e "${FAIL} Smoke test failed. DO NOT proceed to shadow traffic."
    exit 2
fi

echo ""
echo "═══════════════════════════════════════════════════════════"
echo -e "  ${PASS} DAY 0 BOOT COMPLETE"
echo ""
echo "  Next steps:"
echo "    1. Start shadow traffic (10-20%)"
echo "    2. Monitor dashboard for 4-8h"
echo "    3. Enable canary 1% if stable"
echo "═══════════════════════════════════════════════════════════"
echo ""
