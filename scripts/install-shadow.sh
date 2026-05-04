#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Kernell OS — Shadow Mode Installer
# ═══════════════════════════════════════════════════════════════════════════
# Usage: curl -sSL https://install.kernell.ai | bash
#
# What this does:
#   1. Installs kernell-os-sdk via pip
#   2. Creates ~/.kernell/shadow/ directory
#   3. Generates agent_id and config.yaml
#   4. Verifies installation
#   5. Shows activation instructions
#
# What this does NOT do:
#   - Touch your production code
#   - Modify any environment variables
#   - Start any background processes
#   - Send any data anywhere (telemetry is opt-in)
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

KERNELL_DIR="$HOME/.kernell"
SHADOW_DIR="$KERNELL_DIR/shadow"
CONFIG_FILE="$KERNELL_DIR/config.yaml"

echo ""
echo -e "${CYAN}${BOLD}⬡ Kernell OS — Shadow Mode Installer${NC}"
echo -e "${DIM}═══════════════════════════════════════${NC}"
echo ""

# ── Step 1: Check Python ─────────────────────────────────────────────────
echo -e "${BLUE}[1/5]${NC} Checking Python environment..."
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python 3 not found. Install Python 3.9+ first.${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; }; then
    echo -e "${RED}❌ Python $PYTHON_VERSION detected. Requires 3.9+${NC}"
    exit 1
fi
echo -e "   ${GREEN}✅${NC} Python $PYTHON_VERSION"

# ── Step 2: Install SDK ──────────────────────────────────────────────────
echo -e "${BLUE}[2/5]${NC} Installing kernell-os-sdk..."
pip install kernell-os-sdk --quiet --upgrade 2>/dev/null || \
    pip3 install kernell-os-sdk --quiet --upgrade 2>/dev/null || \
    python3 -m pip install kernell-os-sdk --quiet --upgrade

echo -e "   ${GREEN}✅${NC} kernell-os-sdk installed"

# ── Step 3: Create config directory ───────────────────────────────────────
echo -e "${BLUE}[3/5]${NC} Creating Kernell directory..."
mkdir -p "$SHADOW_DIR"

# Generate unique agent ID
AGENT_ID=$(python3 -c "import uuid; print(str(uuid.uuid4())[:8])")

# Detect hardware
RAM_GB=$(python3 -c "
import os
try:
    mem = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    print(mem // (1024**3))
except:
    print(0)
")

HAS_GPU="false"
if command -v nvidia-smi &> /dev/null; then
    HAS_GPU="true"
fi

# Write config
cat > "$CONFIG_FILE" << EOF
# Kernell OS — Shadow Mode Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Docs: https://docs.kernell.ai/shadow-mode

mode: shadow
agent_id: "$AGENT_ID"

shadow:
  enabled: true
  execute: false        # NEVER execute — observe only
  log_only: true
  intercept: true

hardware:
  ram_gb: $RAM_GB
  has_gpu: $HAS_GPU

telemetry:
  enabled: false        # Opt-in only
  endpoint: "https://api.kernell.ai/telemetry"
  anonymize: true
  consent_given: false

dashboard:
  url: "https://app.kernell.ai"
EOF

echo -e "   ${GREEN}✅${NC} Config written to $CONFIG_FILE"
echo -e "   ${DIM}   Agent ID: $AGENT_ID${NC}"

# ── Step 4: Verify installation ──────────────────────────────────────────
echo -e "${BLUE}[4/5]${NC} Verifying installation..."

python3 -c "
from kernell_sdk.shadow import ShadowProxy, ShadowConfig
proxy = ShadowProxy(ShadowConfig(agent_id='$AGENT_ID'))
print('   ✅ Shadow Proxy OK')
print(f'   ✅ Log dir: {proxy._config.log_dir}')
" 2>/dev/null || {
    echo -e "${RED}❌ Verification failed. Try: pip install kernell-os-sdk --force${NC}"
    exit 1
}

# ── Step 5: Show activation instructions ─────────────────────────────────
echo -e "${BLUE}[5/5]${NC} Ready!"
echo ""
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✅ Kernell Shadow Mode Installed Successfully${NC}"
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}Agent ID:${NC}  $AGENT_ID"
echo -e "  ${CYAN}Config:${NC}    $CONFIG_FILE"
echo -e "  ${CYAN}Logs:${NC}      $SHADOW_DIR/"
echo -e "  ${CYAN}RAM:${NC}       ${RAM_GB}GB  |  GPU: $HAS_GPU"
echo ""
echo -e "${BOLD}  Activate with ONE line in your code:${NC}"
echo ""
echo -e "  ${DIM}# Add this at the top of your main script:${NC}"
echo -e "  ${GREEN}from kernell_sdk.shadow.proxy import patch_openai${NC}"
echo -e "  ${GREEN}patch_openai()${NC}"
echo ""
echo -e "  ${DIM}# That's it. Your code runs exactly the same.${NC}"
echo -e "  ${DIM}# Kernell observes in parallel and computes savings.${NC}"
echo ""
echo -e "  ${BOLD}View savings report:${NC}"
echo -e "  ${CYAN}kernell shadow report${NC}"
echo ""
echo -e "${DIM}  Expected insights in: ~2-4 hours of normal usage${NC}"
echo -e "${DIM}  Full savings analysis: ~24-48 hours${NC}"
echo ""
