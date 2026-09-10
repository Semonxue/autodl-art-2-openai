#!/usr/bin/env bash
# End-to-end smoke check against a running gateway instance.
#
# Usage:
#   AUTODL_TOKEN=... BASE_URL=http://127.0.0.1:8000/v1 bash scripts/smoke.sh
#
# Exits 0 on success, non-zero on any unexpected response.

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000/v1}"
TOKEN="${AUTODL_TOKEN:?AUTODL_TOKEN must be set}"

red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
header() { printf "\n=== %s ===\n" "$*"; }

check_status() {
    local expected="$1" actual="$2" label="$3"
    if [[ "$expected" == "$actual" ]]; then
        green "OK   $label -> $actual"
    else
        red   "FAIL $label expected=$expected got=$actual"
        exit 1
    fi
}

header "1. /v1/models (no auth)"
status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/models")
check_status 401 "$status" "models without Authorization"

header "2. /v1/models"
resp=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer $TOKEN" "$BASE_URL/models")
body=$(printf '%s' "$resp" | sed '$d')
status=$(printf '%s' "$resp" | tail -n1)
check_status 200 "$status" "models with Bearer token"
workflow_id=$(printf '%s' "$body" | python3 -c "
import sys, json
data = json.load(sys.stdin)['data']
print(next((m['id'] for m in data if m['id']), ''))
")
if [[ -z "$workflow_id" ]]; then
    red "FAIL no workflow id returned"
    exit 1
fi
green "OK   picked workflow_id=$workflow_id"

header "3. /v1/videos"
resp=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$workflow_id\",\"prompt\":\"smoke test\",\"seconds\":5,\"size\":\"1280x720\"}" \
    "$BASE_URL/videos")
body=$(printf '%s' "$resp" | sed '$d')
status=$(printf '%s' "$resp" | tail -n1)
check_status 202 "$status" "videos create"
task_id=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
green "OK   task_id=$task_id"

header "4. /v1/videos/{id}"
resp=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer $TOKEN" \
    "$BASE_URL/videos/$task_id")
status=$(printf '%s' "$resp" | tail -n1)
check_status 200 "$status" "videos retrieve"

header "5. /v1/videos/{id}/content"
status=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $TOKEN" \
    "$BASE_URL/videos/$task_id/content")
# Could be 302 (success), 200 (already running but somehow has result),
# or 404 (still queued). 502 means upstream/network issue.
case "$status" in
    302|200|404) green "OK   content endpoint -> $status";;
    *)           red "FAIL content endpoint -> $status"; exit 1;;
esac

green "\nAll smoke checks passed."
