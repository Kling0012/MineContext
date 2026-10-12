#!/usr/bin/env bash
# E2E test for MineContext sync-server (<memory-host>, docker).
# Usage: sudo ./scripts/e2e.sh
# Requires: docker compose, curl, jq, python3.
set -u
cd "$(dirname "$0")/.."

BASE="http://127.0.0.1:${API_PORT:-18741}"
DC="sudo docker compose"
PASS=0
FAIL=0
DAY="$(date +%F)"

ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }
check(){ if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (expected [$2], got [$1])"; fi; }

req() { # req METHOD PATH [JSON]
  local m=$1 p=$2 d=${3:-}
  if [ -n "$d" ]; then
    curl -s -X "$m" "$BASE$p" -H 'Content-Type: application/json' -d "$d"
  else
    curl -s -X "$m" "$BASE$p"
  fi
}

uuid() { python3 -c "import uuid;print(uuid.uuid4())"; }

cleanup() {
  echo "--- cleanup: compose down -v"
  $DC down -v >/dev/null 2>&1
}
trap cleanup EXIT

echo "=== 1. compose up (EMBED_PROVIDER=fake)"
$DC down -v >/dev/null 2>&1
if ! sudo env EMBED_PROVIDER=fake $DC up -d --build >/dev/null 2>&1; then
  bad "docker compose up"
  echo "results: PASS=$PASS FAIL=$FAIL"; exit 1
fi
ok "docker compose up"

echo "=== 2. healthz"
HZ=""
for i in $(seq 1 60); do
  HZ=$(curl -s "$BASE/healthz" || true)
  echo "$HZ" | jq -e '.ok == true and .db == "ok"' >/dev/null 2>&1 && break
  sleep 1
done
if echo "$HZ" | jq -e '.ok == true and .db == "ok"' >/dev/null 2>&1; then
  ok "healthz ok (embed_pending=$(echo "$HZ" | jq .embed_pending))"
else
  bad "healthz: $HZ"
fi

echo "=== 3. register 2 devices"
R=$(req PUT /v1/devices/mac-1 '{"name":"MacBook Pro","os":"darwin"}')
check "$(echo "$R" | jq -r .name)" "MacBook Pro" "device mac-1 upsert"
R=$(req PUT /v1/devices/win-1 '{"name":"Windowsデスクトップ","os":"windows"}')
check "$(echo "$R" | jq -r .os)" "windows" "device win-1 upsert"

echo "=== 4. ingest events from 2 devices (ja+en), idempotent re-ingest"
E1=$(uuid); E2=$(uuid); E3=$(uuid); E4=$(uuid)
mk() { # mk id device kind started text title
  printf '{"id":"%s","device_id":"%s","kind":"%s","started_at":"%s","text":%s,"title":%s}' \
    "$1" "$2" "$3" "$4" "$(jq -Rn --arg v "$5" '$v')" "$(jq -Rn --arg v "$6" '$v')"
}
B1="{\"device_id\":\"mac-1\",\"events\":[$(mk $E1 mac-1 note "${DAY}T09:00:00+09:00" '四半期財務レポートのレビューをした' 'Q3 finances'),$(mk $E3 mac-1 activity "${DAY}T11:00:00+09:00" 'VS Code で Python のコードを書いた' 'coding')]}"
B2="{\"device_id\":\"win-1\",\"events\":[$(mk $E2 win-1 screen_summary "${DAY}T10:00:00+09:00" '会議の議事録: 予算について議論' 'meeting notes')]}"
R=$(req POST "/v1/events:batch" "$B1")
check "$(echo "$R" | jq -r '"\(.accepted)/\(.updated)"')" "2/0" "batch mac-1 accepted/updated"
R=$(req POST "/v1/events:batch" "$B2")
check "$(echo "$R" | jq -r '"\(.accepted)/\(.updated)"')" "1/0" "batch win-1 accepted/updated"
# same ids again with updated text -> updated, not duplicated
B1U="{\"device_id\":\"mac-1\",\"events\":[$(mk $E1 mac-1 note "${DAY}T09:00:00+09:00" '四半期財務レポートのレビューを完了した' 'Q3 finances v2')]}"
R=$(req POST "/v1/events:batch" "$B1U")
check "$(echo "$R" | jq -r '"\(.accepted)/\(.updated)"')" "0/1" "re-ingest same id -> updated"
T=$(req GET "/v1/events?device_id=mac-1&limit=10" | jq -r '.events[] | select(.id=="'$E1'") | .text')
check "$T" "四半期財務レポートのレビューを完了した" "event content updated by re-ingest"

echo "=== 5. wait for fake embeddings then healthz pending=0"
for i in $(seq 1 20); do
  P=$(curl -s "$BASE/healthz" | jq .embed_pending)
  [ "$P" = "0" ] && break
  sleep 1
done
check "$P" "0" "embed_pending drains to 0 (fake provider)"

echo "=== 6. days: both devices chronological + kind counts"
D=$(req GET "/v1/days/${DAY}?tz=Asia/Tokyo")
N=$(echo "$D" | jq '.events | length')
check "$N" "3" "days returns 3 events"
ORDER=$(echo "$D" | jq -r '[.events[].device_id] | join(",")')
check "$ORDER" "mac-1,win-1,mac-1" "days chronological across devices"
T0=$(echo "$D" | jq -r '.events[0].device_name')
check "$T0" "MacBook Pro" "days includes device name"
C=$(echo "$D" | jq -r '[.counts[].kind] | sort | join(",")')
check "$C" "activity,note,screen_summary" "days kind counts"

echo "=== 7. events cursor paging"
P1=$(req GET "/v1/events?limit=2")
N1=$(echo "$P1" | jq '.events | length'); CUR=$(echo "$P1" | jq -r .next_cursor)
P2=$(req GET "/v1/events?limit=2&cursor=$CUR")
N2=$(echo "$P2" | jq '.events | length')
NC2=$(echo "$P2" | jq -r .next_cursor)
if [ "$N1" = "2" ] && [ "$N2" = "1" ] && [ "$NC2" = "null" ]; then
  ok "cursor paging 2+1, last page has no cursor"
else
  bad "cursor paging (page1=$N1 page2=$N2 next=$NC2)"
fi

echo "=== 8. hybrid search: ja partial match + en semantic-ish match"
S=$(req POST /v1/search '{"query":"議事録","k":10}')
H=$(echo "$S" | jq -r '[.hits[].event.id] | index("'$E2'")')
[ "$H" != "null" ] && ok "ja partial match (trgm) finds meeting notes" || bad "ja partial match: $S"
S=$(req POST /v1/search '{"query":"quarterly financial report review","k":10,"kinds":["note"]}')
H=$(echo "$S" | jq -r '[.hits[].event.id] | index("'$E1'")')
[ "$H" != "null" ] && ok "en vector-side match finds Q3 note (kind filter ok)" || bad "en match: $S"
BASIS=$(req POST /v1/search '{"query":"予算","k":10}' | jq -r '.hits[0] | (.vector_rank!=null) or (.trgm_rank!=null)')
check "$BASIS" "true" "hit includes score basis (vector/trgm rank)"

echo "=== 9. delete single event"
R=$(req DELETE "/v1/events/$E3")
check "$(echo "$R" | jq 'has("deleted")')" "true" "delete returns deleted"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE/v1/events/$E3")
check "$CODE" "404" "second delete -> 404"
N=$(req GET "/v1/events?device_id=mac-1&limit=10" | jq '[.events[].id] | index("'$E3'") != null')
check "$N" "false" "deleted event gone from list"

echo "=== 10. embedding failure (openai_compat unreachable): ingest+search keep working"
sudo env EMBED_PROVIDER=openai_compat EMBED_BASE_URL=http://10.255.255.1:1/v1 EMBED_MODEL=test $DC up -d api >/dev/null 2>&1 || bad "recreate api with openai_compat"
HZ=""
for i in $(seq 1 60); do
  HZ=$(curl -s "$BASE/healthz" || true)
  echo "$HZ" | jq -e '.ok == true' >/dev/null 2>&1 && break
  sleep 1
done
echo "$HZ" | jq -e '.ok == true' >/dev/null 2>&1 && ok "healthz ok with failing embedder" || bad "healthz with failing embedder: $HZ"
E5=$(uuid)
R=$(req POST "/v1/events:batch" "{\"device_id\":\"win-1\",\"events\":[$(mk $E5 win-1 note "${DAY}T12:00:00+09:00" '障害時でも登録できるメモ' 'resilient note')]}")
check "$(echo "$R" | jq -r .accepted)" "1" "ingest works while embedder fails"
S=$(req POST /v1/search '{"query":"障害時でも登録","k":10}')
H=$(echo "$S" | jq -r '[.hits[].event.id] | index("'$E5'")')
[ "$H" != "null" ] && ok "search (trgm) works while embedder fails" || bad "search during embed failure: $S"
P=$(curl -s "$BASE/healthz" | jq .embed_pending)
if [ "$P" -ge 1 ] 2>/dev/null; then ok "embed_pending grows ($P) but service stays up"; else bad "embed_pending=$P"; fi

echo
echo "results: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
