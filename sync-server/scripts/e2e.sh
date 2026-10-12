#!/usr/bin/env bash
# E2E test for MineContext sync-server (<memory-host>, docker).
# Usage: sudo ./scripts/e2e.sh
# Requires: docker compose, curl, jq, python3.
set -u
cd "$(dirname "$0")/.."

BASE="http://127.0.0.1:${API_PORT:-18741}"
# Fixed project name so dev/e2e stacks never collide with the production
# deployment (mcsync-prod, see docker-compose.prod.yml).
DC="sudo docker compose -p mcsync-e2e"
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
  $DC --profile e2e down -v >/dev/null 2>&1
}
trap cleanup EXIT

echo "=== 1. compose up (EMBED_PROVIDER=fake, LLM_PROVIDER=fake, short judge/idle)"
$DC --profile e2e down -v >/dev/null 2>&1
# NOTE: env must reach `docker compose` directly (a nested sudo would env-reset it).
if ! sudo env EMBED_PROVIDER=fake LLM_PROVIDER=fake SESSION_JUDGE_INTERVAL=3 IDLE_MINUTES=0.2 docker compose -p mcsync-e2e up -d --build >/dev/null 2>&1; then
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

echo "=== 10. work sessions: declare -> related events auto-link"
S1=$(req POST /v1/sessions '{"title":"財務レポート","note":"Q3の数値確認","device_id":"mac-1"}')
SID1=$(echo "$S1" | jq -r .session.id)
check "$(echo "$S1" | jq -r .session.status)" "active" "declare session -> active"
check "$(echo "$S1" | jq -r .session.superseded)" "null" "first declare supersedes nothing"
R1=$(uuid); R2=$(uuid); R3=$(uuid)
RT1=$(date -u -d '+1 seconds' +%Y-%m-%dT%H:%M:%SZ)
RT2=$(date -u -d '+2 seconds' +%Y-%m-%dT%H:%M:%SZ)
RT3=$(date -u -d '+3 seconds' +%Y-%m-%dT%H:%M:%SZ)
RB="{\"device_id\":\"mac-1\",\"events\":[$(mk $R1 mac-1 activity "$RT1" '財務レポートの数値を確認している' 'finance'),$(mk $R2 mac-1 activity "$RT2" '財務レポートのグラフを作った' 'chart'),$(mk $R3 mac-1 screen_summary "$RT3" '画面:財務レポートのスプレッドシート' 'screen')]}"
R=$(req POST "/v1/events:batch" "$RB")
check "$(echo "$R" | jq -r .accepted)" "3" "related events ingested"
LNK=$(req GET "/v1/events?device_id=mac-1&limit=10" | jq -r ".events[] | select(.id==\"$R3\") | .session_id")
check "$LNK" "$SID1" "activity/screen_summary auto-linked to active session"
sleep 5
A=$(req GET /v1/sessions/active | jq -r .session.id)
check "$A" "$SID1" "session stays active while related events continue"

echo "=== 11. unrelated events -> ai_detected end at last related event"
U1=$(uuid); U2=$(uuid); U3=$(uuid)
UT1=$(date -u +%Y-%m-%dT%H:%M:%SZ)
UT2=$(date -u -d '+1 seconds' +%Y-%m-%dT%H:%M:%SZ)
UT3=$(date -u -d '+2 seconds' +%Y-%m-%dT%H:%M:%SZ)
UB="{\"device_id\":\"mac-1\",\"events\":[$(mk $U1 mac-1 activity "$UT1" '家族の旅行の写真を見ている' 'photos'),$(mk $U2 mac-1 activity "$UT2" '音楽を聴きながらWebを見る' 'browse'),$(mk $U3 mac-1 screen_summary "$UT3" '画面:動画サイト' 'video')]}"
R=$(req POST "/v1/events:batch" "$UB")
check "$(echo "$R" | jq -r .accepted)" "3" "unrelated events ingested"
ST=""
for i in $(seq 1 20); do
  ST=$(req GET "/v1/sessions/$SID1" | jq -r .status)
  [ "$ST" = "ended" ] && break
  sleep 1
done
check "$ST" "ended" "session ended after unrelated events (ai_detected)"
S=$(req GET "/v1/sessions/$SID1")
check "$(echo "$S" | jq -r .end_reason)" "ai_detected" "end_reason=ai_detected"
check "$(echo "$S" | jq -r .confirmed)" "false" "ai end leaves confirmed=false"
check "$(echo "$S" | jq -r '.end_evidence[0].event_id')" "$R3" "evidence points at last related event"
ET=$(req GET "/v1/events?device_id=mac-1&limit=50" | jq -r ".events[] | select(.id==\"$R3\") | .started_at")
check "$(echo "$S" | jq -r .ended_at)" "$ET" "ended_at = last related event time (not judge time)"

echo "=== 12. supersede / idle end / PATCH / cancel"
S2=$(req POST /v1/sessions '{"title":"メール処理","device_id":"win-1"}')
SID2=$(echo "$S2" | jq -r .session.id)
S3=$(req POST /v1/sessions '{"title":"資料読み","device_id":"mac-1"}')
SID3=$(echo "$S3" | jq -r .session.id)
check "$(echo "$S3" | jq -r .superseded.id)" "$SID2" "new declare returns superseded session"
S2R=$(req GET "/v1/sessions/$SID2")
check "$(echo "$S2R" | jq -r .status)" "ended" "old active session superseded -> ended"
check "$(echo "$S2R" | jq -r .end_reason)" "superseded" "end_reason=superseded"
check "$(echo "$S2R" | jq -r .ended_at)" "$(echo "$S3" | jq -r .session.started_at)" "superseded ended_at = new started_at"
A=$(req GET /v1/sessions/active | jq -r .session.id)
check "$A" "$SID3" "newest declaration is the active session"
RL=$(uuid); RLT=$(date -u -d '+5 seconds' +%Y-%m-%dT%H:%M:%SZ)
# +5s: keeps RL strictly later than section 11's U3 (+3s there), so the
# judge's "last event" (idle evidence / ended_at) is always RL regardless
# of how fast the intermediate poll loops finish.
R=$(req POST "/v1/events:batch" "{\"device_id\":\"mac-1\",\"events\":[$(mk $RL mac-1 activity "$RLT" '資料読み:設計書の章を読み進めた' 'reading')]}")
LNK=$(req GET "/v1/events?device_id=mac-1&kind=activity&limit=50" | jq -r ".events[] | select(.id==\"$RL\") | .session_id")
check "$LNK" "$SID3" "event links to the new active session"
ST=""
for i in $(seq 1 40); do
  ST=$(req GET "/v1/sessions/$SID3" | jq -r .status)
  [ "$ST" = "ended" ] && break
  sleep 1
done
check "$ST" "ended" "session idle-ended after events stop"
S=$(req GET "/v1/sessions/$SID3")
check "$(echo "$S" | jq -r .end_reason)" "idle" "end_reason=idle"
check "$(echo "$S" | jq -r '.end_evidence[0].event_id')" "$RL" "idle evidence = last event"
RLET=$(req GET "/v1/events?device_id=mac-1&kind=activity&limit=50" | jq -r ".events[] | select(.id==\"$RL\") | .started_at")
check "$(echo "$S" | jq -r .ended_at)" "$RLET" "idle ended_at = last event time"
NEWEND=$(date -u -d "@$(( $(date -u -d "$RLT" +%s) + 300 ))" +%Y-%m-%dT%H:%M:%SZ)
P=$(req PATCH "/v1/sessions/$SID3" "{\"title\":\"資料読み(修正済み)\",\"ended_at\":\"$NEWEND\",\"confirmed\":true}")
check "$(echo "$P" | jq -r .title)" "資料読み(修正済み)" "PATCH updates title"
check "$(echo "$P" | jq -r .confirmed)" "true" "PATCH confirms session"
check "$(date -u -d "$(echo "$P" | jq -r .ended_at)" +%s)" "$(date -u -d "$NEWEND" +%s)" "PATCH sets ended_at"
S4=$(req POST /v1/sessions '{"title":"中止する宣言"}')
SID4=$(echo "$S4" | jq -r .session.id)
C=$(req POST "/v1/sessions/$SID4/cancel")
check "$(echo "$C" | jq -r .status)" "cancelled" "cancel -> cancelled"
A=$(req GET /v1/sessions/active | jq -r .session)
check "$A" "null" "no active session remains"

echo "=== 13. timeline: sessions + events + ai_work chronological, per-session totals"
AW=$(uuid); AWT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
R=$(req POST "/v1/events:batch" "{\"device_id\":\"win-1\",\"events\":[{\"id\":\"$AW\",\"kind\":\"ai_work\",\"started_at\":\"$AWT\",\"text\":\"Claude が P2B セッション機能を実装した\",\"title\":\"P2B 実装\",\"source\":\"claude\",\"meta\":{\"agent\":\"claude\"}}]}")
check "$(echo "$R" | jq -r .accepted)" "1" "ai_work event ingested"
TL=$(req GET "/v1/timeline?date=${DAY}&tz=Asia/Tokyo")
# compare at second precision: t may mix micro (sessions) and second (events) forms
check "$(echo "$TL" | jq -r '[.items[].t | sub("\\.[0-9]+";"")] == ([.items[].t | sub("\\.[0-9]+";"")]|sort)')" "true" "timeline items are chronological"
check "$(echo "$TL" | jq -r '[.items[] | select(.type=="session") | .session.id] | index("'$SID1'") != null')" "true" "timeline includes sessions"
check "$(echo "$TL" | jq -r '.items[] | select(.type=="ai_work") | .event.source')" "claude" "ai_work item carries source"
check "$(echo "$TL" | jq -r '.items[] | select(.type=="ai_work") | .event.device_name')" "Windowsデスクトップ" "timeline events carry device name"
check "$(echo "$TL" | jq -r '.session_totals[] | select(.session_id=="'$SID3'") | .minutes > 4')" "true" "per-session total minutes (s3 ~5min)"
check "$(echo "$TL" | jq -r '.total_minutes > 0')" "true" "total_minutes present"

echo "=== 14. embedder + LLM unreachable: ingest, sessions and search keep working"
sudo env EMBED_PROVIDER=openai_compat EMBED_BASE_URL=http://10.255.255.1:1/v1 EMBED_MODEL=test \
  LLM_PROVIDER=openai_compat LLM_BASE_URL=http://10.255.255.1:1/v1 LLM_TIMEOUT_SEC=5 SESSION_JUDGE_INTERVAL=5 \
  docker compose -p mcsync-e2e up -d api >/dev/null 2>&1 || bad "recreate api with unreachable providers"
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
S5=$(req POST /v1/sessions '{"title":"障害時の作業"}')
SID5=$(echo "$S5" | jq -r .session.id)
check "$(echo "$S5" | jq -r .session.status)" "active" "declare works while LLM unreachable"
# an event is needed so the judge reaches the LLM call (no-event sessions idle
# out before the LLM is consulted); +1s keeps it after the session's start
E6=$(uuid); E6T=$(date -u -d '+1 seconds' +%Y-%m-%dT%H:%M:%SZ)
R=$(req POST "/v1/events:batch" "{\"device_id\":\"win-1\",\"events\":[{\"id\":\"$E6\",\"kind\":\"activity\",\"started_at\":\"$E6T\",\"text\":\"障害時でも活動を記録する\",\"title\":\"resilient activity\"}]}")
check "$(echo "$R" | jq -r .accepted)" "1" "ingest works while LLM unreachable"
sleep 8
check "$(curl -s "$BASE/healthz" | jq -r .ok)" "true" "healthz still ok with failing judge"
A=$(req GET /v1/sessions/active | jq -r .session.id)
check "$A" "$SID5" "session stays active when judge fails (fail-soft)"
JLOG=""
for i in $(seq 1 25); do
  if $DC logs api 2>&1 | grep -q "session judge failed"; then JLOG=1; break; fi
  sleep 1
done
if [ -n "$JLOG" ]; then
  ok "judge failure logged (skips cycle only, server keeps running)"
else
  bad "no judge failure log found"
fi

echo "=== 15. agent-memory import: ai_work timeline, idempotent, superseded, fail-soft"
AM1=00000000-0000-4000-8000-00000000a001
AM2=00000000-0000-4000-8000-00000000a002
AM3=00000000-0000-4000-8000-00000000a003
AM4=00000000-0000-4000-8000-00000000a004
AM5=00000000-0000-4000-8000-00000000a005
# fake agent-memory runs as an e2e-profile compose service (same network as
# the api, so no host firewall is involved); page-size 2 forces cursor paging
$DC --profile e2e up -d fake-agent-memory >/dev/null 2>&1 || bad "start fake agent-memory"
# judge interval 600s: no judging races during this section
if ! sudo env EMBED_PROVIDER=fake LLM_PROVIDER=fake SESSION_JUDGE_INTERVAL=600 IDLE_MINUTES=15 \
     AM_BASE_URL=http://fake-agent-memory:8377 AM_IMPORT_INTERVAL=3 \
     docker compose -p mcsync-e2e up -d api >/dev/null 2>&1; then
  bad "recreate api with agent-memory import"
fi
N=0; ERR="x"
for i in $(seq 1 40); do
  IMP=$(curl -s "$BASE/v1/importers" || true)
  N=$(echo "$IMP" | jq -r '.importers[] | select(.name=="agent_memory") | (.imported_count // 0)')
  ERR=$(echo "$IMP" | jq -r '.importers[] | select(.name=="agent_memory") | (.last_error // "")')
  [ "${N:-0}" -ge 4 ] 2>/dev/null && [ -z "$ERR" ] && break
  sleep 1
done
if [ "${N:-0}" -ge 4 ] 2>/dev/null; then
  ok "importer pulled all 4 records (page-size 2 -> cursor paging worked)"
else
  bad "importer did not finish (count=${N:-none} err=$ERR)"
fi
check "$(echo "$IMP" | jq -r '.importers[] | select(.name=="agent_memory") | .enabled')" "true" "importer reports enabled"
check "$(echo "$IMP" | jq -r '.importers[] | select(.name=="agent_memory") | .cursor_ts != null')" "true" "importer cursor advanced"
EVC=$(req GET "/v1/events?kind=ai_work&limit=100" | jq '[.events[] | select(.device_id | startswith("agent:"))] | length')
check "$EVC" "4" "4 ai_work events stored from agent devices"
am_event() { req GET "/v1/events?kind=ai_work&limit=100" | jq -c ".events[] | select(.id==\"$1\")"; }
M1=$(am_event $AM1)
check "$(echo "$M1" | jq -r .device_id)" "agent:claude" "memory -> device agent:claude"
check "$(echo "$M1" | jq -r .title)" "decision · minecontext" "title from type + first tag"
check "$(echo "$M1" | jq -r .source)" "e2e:/fake/project" "source = source.uri"
check "$(echo "$M1" | jq -r .meta.memory_type)" "decision" "meta.memory_type kept"
check "$(echo "$M1" | jq -r .meta.superseded_by)" "$AM4" "superseded record marked via supersedes link"
check "$(am_event $AM2 | jq -r .device_id)" "agent:codex" "second agent -> device agent:codex"
TL=$(req GET "/v1/timeline?date=${DAY}&tz=Asia/Tokyo")
AWD=$(echo "$TL" | jq -r '[.items[] | select(.type=="ai_work") | select(.event.device_id | startswith("agent:"))] | length')
check "$AWD" "3" "timeline hides superseded ai_work by default"
ORD=$(echo "$TL" | jq -r '[.items[] | select(.type=="ai_work") | select(.event.device_id | startswith("agent:")) | .event.id] | join(",")')
check "$ORD" "$AM2,$AM3,$AM4" "ai_work items chronological in timeline"
DN=$(echo "$TL" | jq -r ".items[] | select(.type==\"ai_work\") | select(.event.id==\"$AM2\") | .event.device_name")
check "$DN" "codex" "timeline ai_work carries agent device name"
AWS=$(req GET "/v1/timeline?date=${DAY}&tz=Asia/Tokyo&include_superseded=true" | jq -r '[.items[] | select(.type=="ai_work") | select(.event.device_id | startswith("agent:"))] | length')
check "$AWS" "4" "include_superseded=true shows all 4"
RUN=$(curl -s -X POST "$BASE/v1/importers/agent-memory/run")
check "$(echo "$RUN" | jq -r .ok)" "true" "manual import run ok"
EVC2=$(req GET "/v1/events?kind=ai_work&limit=100" | jq '[.events[] | select(.device_id | startswith("agent:"))] | length')
check "$EVC2" "4" "re-run does not duplicate (id = memory_id upsert)"
S6=$(req POST /v1/sessions "{\"title\":\"AIの作業記録\",\"started_at\":\"$(date -u -d '-2 hours' +%Y-%m-%dT%H:%M:%SZ)\"}")
SID6=$(echo "$S6" | jq -r .session.id)
curl -s -X POST "$BASE/v1/importers/agent-memory/run" >/dev/null
LNK=$(am_event $AM4 | jq -r .session_id)
check "$LNK" "$SID6" "ai_work links to session active at its time"
$DC --profile e2e stop fake-agent-memory >/dev/null 2>&1
sleep 1
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/importers/agent-memory/run")
check "$CODE" "502" "manual run reports 502 while agent-memory down"
check "$(curl -s "$BASE/healthz" | jq -r .ok)" "true" "server stays up while agent-memory down"
ERR=""
for i in $(seq 1 10); do
  ERR=$(req GET /v1/importers | jq -r '.importers[] | select(.name=="agent_memory") | (.last_error // "")')
  [ -n "$ERR" ] && break
  sleep 1
done
[ -n "$ERR" ] && ok "last_error recorded while down" || bad "no last_error recorded"
if sudo env FAKE_AM_ARGS=--extra docker compose -p mcsync-e2e --profile e2e up -d fake-agent-memory >/dev/null 2>&1; then
  ok "fake agent-memory restarted with an extra record"
else
  bad "fake agent-memory restart failed"
fi
EVC3=""
for i in $(seq 1 40); do
  EVC3=$(req GET "/v1/events?kind=ai_work&limit=100" | jq '[.events[] | select(.device_id | startswith("agent:"))] | length')
  ERR=$(req GET /v1/importers | jq -r '.importers[] | select(.name=="agent_memory") | (.last_error // "")')
  [ "$EVC3" = "5" ] && [ -z "$ERR" ] && break
  sleep 1
done
check "$EVC3" "5" "background import recovers after agent-memory returns"
check "$(req GET /v1/importers | jq -r '.importers[] | select(.name=="agent_memory") | .last_success_at != null')" "true" "last_success_at set after recovery"
AWF=$(req GET "/v1/timeline?date=${DAY}&tz=Asia/Tokyo" | jq -r '[.items[] | select(.type=="ai_work") | select(.event.device_id | startswith("agent:"))] | length')
check "$AWF" "4" "timeline shows recovered record (superseded still hidden)"

echo "=== 16. stats/daily: empty days, tz boundary, 5-min rounding, session day-split"
D1="$(date -d '+1 day' +%F)"; D2="$(date -d '+2 day' +%F)"; D3="$(date -d '+3 day' +%F)"
# DAY 19:00Z == DAY+1 04:00+09:00 -> belongs to DAY+1 in Asia/Tokyo, DAY in UTC
EA=$(uuid)
R=$(req POST "/v1/events:batch" "{\"device_id\":\"win-1\",\"events\":[$(mk $EA win-1 note "${DAY}T19:00:00Z" 'tz境界のメモ' 'tz edge')]}")
check "$(echo "$R" | jq -r .accepted)" "1" "boundary event ingested"
# clean day D2: 09:01/09:04 same 5-min bucket, 09:07 next bucket; ai_work both in one bucket
B1=$(uuid); B2=$(uuid); B3=$(uuid); AI1=$(uuid); AI2=$(uuid)
R=$(req POST "/v1/events:batch" "{\"device_id\":\"mac-1\",\"events\":[$(mk $B1 mac-1 note "${D2}T09:01:00+09:00" '丸め1' 'r1'),$(mk $B2 mac-1 note "${D2}T09:04:00+09:00" '丸め2' 'r2'),$(mk $B3 mac-1 note "${D2}T09:07:00+09:00" '丸め3' 'r3'),$(mk $AI1 mac-1 ai_work "${D2}T10:01:00+09:00" 'AI作業1' 'ai1'),$(mk $AI2 mac-1 ai_work "${D2}T10:03:00+09:00" 'AI作業2' 'ai2')]}")
check "$(echo "$R" | jq -r .accepted)" "5" "bucket-rounding events ingested"
# session crossing midnight JST: DAY 23:00 -> DAY+1 01:00 (60min on each day)
SS=$(req POST /v1/sessions "{\"title\":\"日跨ぎ作業\",\"device_id\":\"mac-1\",\"started_at\":\"${DAY}T23:00:00+09:00\"}")
SSID=$(echo "$SS" | jq -r .session.id)
SE=$(req POST "/v1/sessions/$SSID/end" "{\"ended_at\":\"${D1}T01:00:00+09:00\"}")
check "$(echo "$SE" | jq -r .status)" "ended" "cross-midnight session ended"

ST=$(req GET "/v1/stats/daily?from=${DAY}&to=${D3}")
check "$(echo "$ST" | jq -r .tz)" "Asia/Tokyo" "default tz is Asia/Tokyo"
check "$(echo "$ST" | jq '.days | length')" "4" "all days in range returned (incl. empty)"
J1=$(echo "$ST" | jq -c ".days[] | select(.date==\"$D1\")")
check "$(echo "$J1" | jq -r '[.events,.mine,.ai_work,.active_minutes,.ai_active_minutes] | @tsv')" "1	1	0	5	0" "D1(JST): boundary event counts (mine bucket)"
check "$(echo "$J1" | jq '.session_minutes == 60')" "true" "D1(JST): 60min session part after midnight"
J2=$(echo "$ST" | jq -c ".days[] | select(.date==\"$D2\")")
check "$(echo "$J2" | jq -r '[.events,.mine,.ai_work,.active_minutes,.ai_active_minutes] | @tsv')" "5	3	2	10	5" "D2(JST): 5-min bucket rounding (2 mine + 1 ai buckets)"
check "$(echo "$J2" | jq '.session_minutes == 0')" "true" "D2(JST): no session overlap"
check "$(echo "$J2" | jq -r .first_at)" "${D2}T00:01:00Z" "D2 first_at (09:01 JST in UTC)"
check "$(echo "$J2" | jq -r .last_at)" "${D2}T01:03:00Z" "D2 last_at (10:03 JST in UTC)"
J3=$(echo "$ST" | jq -c ".days[] | select(.date==\"$D3\")")
check "$(echo "$J3" | jq -r '[.events,.mine,.ai_work,.active_minutes,.ai_active_minutes,(.session_minutes==0),(.first_at==null),(.last_at==null)] | @tsv')" "0	0	0	0	0	true	true	true" "day without records is all zeros/nulls"
JD=$(echo "$ST" | jq -c ".days[] | select(.date==\"$DAY\")")
check "$(echo "$JD" | jq '.session_minutes >= 60')" "true" "cross-midnight session gives DAY its 60min part"
STU=$(req GET "/v1/stats/daily?from=${DAY}&to=${D1}&tz=UTC")
check "$(echo "$STU" | jq -r ".days[] | select(.date==\"$D1\") | [.events,.mine] | @tsv")" "0	0" "tz=UTC: boundary event not on DAY+1"
check "$(echo "$STU" | jq '.days[] | select(.date=="'$DAY'") | .mine >= 4')" "true" "tz=UTC: boundary event counted on DAY instead"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/stats/daily?from=${DAY}&to=$(date -d '+401 day' +%F)")
check "$CODE" "422" "range over 400 days -> 422"

echo
echo "results: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
