#!/usr/bin/env bash
# V10 CDK pre-deploy gate. Exits non-zero on any failure.
# Run from cdk_deploy_v10/ via: bash preflight.sh

set -e

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "Bot dir: $BOT_DIR"

# Detect Python 3.11 runner (py launcher on Windows; python3.11 on Linux/Mac)
if command -v py >/dev/null 2>&1; then
    PY="py -3.11"
elif command -v python3.11 >/dev/null 2>&1; then
    PY="python3.11"
else
    echo "x Python 3.11 not found. Install python3.11 first."
    exit 1
fi
echo "Using: $PY"

echo ""
echo "-- 1/4: Python 3.11 syntax check --------------------------------"
find "$BOT_DIR" \
    -name '*.py' \
    -not -path '*/__pycache__/*' \
    -not -path '*/.venv/*' \
    -not -path '*/venv/*' \
    -not -path '*/cdk.out/*' \
    -not -path '*/cdk_deploy/*' \
    -not -path '*/cdk_deploy_v10/*' \
    -not -path '*/tests/*' \
    -not -path '*/.pytest_cache/*' \
    -not -path '*/local/*' \
    -not -path '*/.claude/*' \
    -print0 | xargs -0 $PY -m py_compile
echo "ok All .py files parse under Python 3.11"

echo ""
echo "-- 2/4: Full test suite (must be 251 passing) -------------------"
cd "$BOT_DIR"
$PY -m pytest tests/ -q 2>&1 | tail -3
echo "ok Tests passed"

echo ""
echo "-- 3/4: pip install dry-run on requirements.txt -----------------"
$PY -m pip install --dry-run -r "$BOT_DIR/requirements.txt" >/dev/null 2>&1 || {
    echo "x pip dry-run failed. Run manually for details:"
    echo "  $PY -m pip install --dry-run -r $BOT_DIR/requirements.txt"
    exit 1
}
echo "ok requirements.txt resolves cleanly"

echo ""
echo "-- 4/4: Asset bundle size estimate ------------------------------"
# Rough estimate — du minus the excludes. Doesn't perfectly mirror CDK exclude semantics.
SIZE_KB=$(du -sk "$BOT_DIR" \
    --exclude=__pycache__ \
    --exclude=.git \
    --exclude=.venv \
    --exclude=venv \
    --exclude=.pytest_cache \
    --exclude=cdk.out \
    --exclude=cdk_deploy \
    --exclude=cdk_deploy_v10 \
    --exclude=tests \
    --exclude=local \
    --exclude=.claude \
    --exclude='*.log' \
    --exclude='*.jsonl' \
    --exclude='*.docx' \
    --exclude='*.zip' \
    --exclude='data/materialized_results' \
    --exclude='data/confluence_cache' \
    2>/dev/null | awk '{print $1}')
SIZE_MB=$((SIZE_KB / 1024))
echo "Estimated asset bundle: ~${SIZE_MB} MB"
if [ "$SIZE_MB" -gt 50 ]; then
    echo "WARN Bundle is large (>50 MB). Review excludes if surprising."
fi

echo ""
echo "================================================================="
echo "ok preflight passed -- ready to deploy"
echo "================================================================="
