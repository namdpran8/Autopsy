#!/usr/bin/env bash
# ============================================================
#  seed_bug.sh — AutoDebug Agent hackathon demo script
# ============================================================
#  Runs the intentionally-buggy service, captures its output,
#  feeds the logs to the /debug API, and prints the results.
# ============================================================
set -e

API_URL="${AUTODEBUG_API_URL:-http://localhost:8080}"
DEMO_LOG="demo/demo_log.txt"
CODE_DIR="demo/buggy_service"

echo "============================================================"
echo "  AutoDebug Agent — Live Demo"
echo "============================================================"
echo ""

# ------------------------------------------------------------------
# Step 1: Run the buggy service and capture stderr + stdout
# ------------------------------------------------------------------
echo "[1/4] Running buggy service to generate crash logs ..."
echo ""

# The service exits with code 1, so we allow that failure
python3 demo/buggy_service/app.py > "$DEMO_LOG" 2>&1 || true

echo "  -> Crash log saved to: $DEMO_LOG"
echo "  -> $(wc -l < "$DEMO_LOG") lines captured"
echo ""

# Show a preview of the log
echo "--- Log preview (last 15 lines) ---"
tail -n 15 "$DEMO_LOG"
echo "-----------------------------------"
echo ""

# ------------------------------------------------------------------
# Step 2: POST the logs to the /debug endpoint
# ------------------------------------------------------------------
echo "[2/4] Sending logs to AutoDebug API ($API_URL/debug) ..."
echo ""

# Build the JSON payload — use python to safely escape the log content
PAYLOAD=$(python3 -c "
import json, sys
with open('$DEMO_LOG') as f:
    logs = f.read()
print(json.dumps({'logs': logs, 'code_directory': '$CODE_DIR'}))
")

RESPONSE=$(curl -s -X POST "$API_URL/debug" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

echo "  -> Response received."
echo ""

# ------------------------------------------------------------------
# Step 3: Pretty-print the full JSON response
# ------------------------------------------------------------------
echo "[3/4] Full API response:"
echo ""
echo "$RESPONSE" | python3 -m json.tool
echo ""

# ------------------------------------------------------------------
# Step 4: Extract and display key URLs
# ------------------------------------------------------------------
echo "[4/4] Results summary:"
echo ""

STATUS=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','unknown'))")
echo "  Status:        $STATUS"

ISSUE_URL=$(echo "$RESPONSE" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('issue', {}).get('url', 'N/A'))
" 2>/dev/null || echo "N/A")
echo "  GitLab Issue:  $ISSUE_URL"

MR_URL=$(echo "$RESPONSE" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('mr', {}).get('url', 'N/A'))
" 2>/dev/null || echo "N/A")
echo "  Merge Request: $MR_URL"

PATCH_SUMMARY=$(echo "$RESPONSE" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('patch_summary', 'N/A'))
" 2>/dev/null || echo "N/A")
echo "  Patch Summary: $PATCH_SUMMARY"

BRANCH=$(echo "$RESPONSE" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('branch', 'N/A'))
" 2>/dev/null || echo "N/A")
echo "  Branch:        $BRANCH"

echo ""
echo "============================================================"
echo "  Demo complete!"
echo "============================================================"
