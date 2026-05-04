#!/bin/bash
# Kernell OS SDK — Pre-commit Security Gate
# Runs Adapter Compliance Checker before allowing commits.
# Install: cp pre-commit-security.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

set -e

PYTHON="${VIRTUAL_ENV:-/home/anny/kernell-os/.venv}/bin/python"
CHECKER="/home/anny/kernell-os/verifier/adapter_compliance_checker.py"
ADAPTERS="/home/anny/kernell-os/kernell-os-sdk/kernell_sdk/adapters"

echo "🔍 [pre-commit] Running Adapter Compliance Checker..."

if ! $PYTHON "$CHECKER" "$ADAPTERS"; then
    echo ""
    echo "🔴 COMMIT BLOCKED: Adapter Security Contract v1.0 violated."
    echo "   Fix all CRITICAL violations before committing."
    exit 1
fi

echo "✅ [pre-commit] Security gate passed."
exit 0
