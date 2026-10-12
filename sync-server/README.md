# MineContext sync-server

複数端末（Mac/Windows）の MineContext 作業記録（イベント）を集約し、
時系列参照とハイブリッド検索（pgvector + pg_trgm）を提供する中央サーバ。
認証なし（tailnet 内限定運用を想定）。

## 構成

- Python 3.12 / FastAPI / SQLAlchemy 2 + psycopg 3 / Alembic
- PostgreSQL 17 + pgvector（`pgvector/pgvector:pg17` イメージ、拡張 `vector` `pg_trgm`）
- docker-compose.yml: `db` と `api` の2サービス。api は起動時に `alembic upgrade head` を自動実行。
- 依存は `pyproject.toml` + `uv.lock` で固定（Docker ビルドも lock ファイルから）。

## 起動方法（このリポジトリ）

```sh
cd sync-server
# 開発用（決定的な疑似埋め込み、外部通信なし）
sudo docker compose up -d --build
curl -s http://127.0.0.1:18741/healthz
```

環境変数（compose 経由。既定値は docker-compose.yml 参照）:

| 変数 | 既定 | 説明 |
|---|---|---|
| `API_PORT` | `18741` | ホスト側ポート |
| `BIND_HOST` | `127.0.0.1` | ホスト側バインド（tailnet で公開する場合は tailscale IP 等） |
| `EMBED_PROVIDER` | `fake` | `fake`（開発用・決定的・外部通信なし）、`openai_compat`、または `cloudflare`（Workers AI。agent-memory と同じ形式） |
| `EMBED_BASE_URL` | 空 | OpenAI 互換のベース URL（`.../v1` まで）。`openai_compat` 時必須 |
| `EMBED_MODEL` | `fake-embed` | 埋め込みモデル名（cloudflare は `@cf/qwen/qwen3-embedding-0.6b`） |
| `EMBED_DIM` | `1024` | 埋め込み次元。**マイグレーション時に vector 列の次元として固定される** |
| `EMBED_API_KEY_FILE` | 空 | API キーのファイルパス（秘密はファイル参照のみ。`openai_compat` 用） |
| `EMBED_CF_ENV_FILE` | 空 | `cloudflare` 用。`CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN` を含む env 形式ファイルのパス |
| `EMBED_CF_TIMEOUT_SEC` | `60` | `cloudflare` 通信のタイムアウト秒 |
| `EMBED_BATCH` | `64` | 1回の埋め込みリクエストに載せるイベント数の上限 |
| `EMBED_INTERVAL_SEC` | `5` | バックグラウンド埋め込みの周期 |
| `TZ_DEFAULT` | `Asia/Tokyo` | `GET /v1/days/{date}`・`GET /v1/timeline`・`GET /v1/stats/daily` の tz 既定値 |
| `LLM_PROVIDER` | `openai_compat` | 作業終了判定の LLM。`openai_compat`（`/chat/completions`）または `fake`（開発用・決定的・外部通信なし） |
| `LLM_BASE_URL` | 空 | OpenAI 互換のベース URL（`.../v1` まで）。`openai_compat` 時必須（未設定なら判定をスキップしてログのみ） |
| `LLM_MODEL` | `glm-5.3-flash` | 終了判定に使うモデル名 |
| `LLM_API_KEY_FILE` | 空 | API キーのファイルパス（秘密はファイル参照のみ） |
| `LLM_TIMEOUT_SEC` | `30` | LLM 通信のタイムアウト秒 |
| `SESSION_JUDGE_INTERVAL` | `120` | 終了判定の周期（秒） |
| `SESSION_END_THRESHOLD` | `0.75` | LLM 判定で終了扱いにする確信度のしきい値 |
| `IDLE_MINUTES` | `15` | 全端末でイベントが無い状態がこの分数続くと idle 終了 |
| `AM_IMPORT_ENABLED` | `1` | agent-memory からの取り込み有効/無効 |
| `AM_BASE_URL` | 空 | agent-memory のベース URL（例 `http://<memory-host>.<tailnet>.ts.net:8377`）。空なら取り込みはアイドル |
| `AM_IMPORT_INTERVAL` | `300` | 取り込み周期（秒） |
| `AM_IMPORT_LOOKBACK_HOURS` | `48` | 初回（カーソル無し）の遡り時間（時） |
| `AM_TIMEOUT_SEC` | `15` | agent-memory 通信のタイムアウト秒 |

注意: `EMBED_DIM` を変えると既存 DB の vector 列と不一致になるため、
新規ボリューム（`docker compose down -v`）で初期化すること。

## データモデル

- `devices(id PK, name, os, last_seen_at)`
- `events(id UUID PK=クライアント生成, device_id FK, kind, started_at, ended_at, app, title, text, meta jsonb, source, created_at, updated_at)`
  - kind は `activity|screen_summary|note|todo|ai_work|report` のみ（CHECK 制約）
  - インデックス: started_at / (device_id, started_at) / kind / GIN(text gin_trgm_ops) / GIN(meta)
- `event_embeddings(event_id PK FK CASCADE, model, embedding vector(EMBED_DIM), embedded_at)`
  - 埋め込みキューは持たず、「event_embeddings に無い events」を未処理とみなしてバックグラウンドで埋め込み。
    失敗しても events の保存・検索は止まらない（fail-soft。次の周期で再試行）。
- `work_sessions(id uuid PK, device_id（宣言した端末）, title, note, started_at, ended_at, status active/ended/cancelled, end_reason manual/ai_detected/idle/superseded, end_confidence, end_evidence jsonb（根拠にした event id と理由）, confirmed, created_at, updated_at)`
  - 「これから○○をする」の宣言。同時に active は1件だけで、新しい宣言は旧 active を
    `superseded` で終了させる（終了時刻 = 新しい宣言の開始時刻）。
  - 終了判定はバックグラウンド（`SESSION_JUDGE_INTERVAL` 秒ごと）。全端末の
    activity/screen_summary を最大30件集め、(1) `IDLE_MINUTES` 分どの端末からも
    イベントが無ければ `idle` 終了、(2) それ以外は LLM が JSON
    `{ongoing, ended_at_event_id, confidence, reason}` で判定し
    `ongoing=false` かつ `confidence >= SESSION_END_THRESHOLD` なら `ai_detected` 終了。
    終了時刻は「作業に関係した最後のイベント」の時刻。判定結果は `confirmed=false` のまま
    で、後から PATCH で修正・確定できる。LLM 失敗・設定なしはその回の判定スキップのみ
    （fail-soft。サーバ全体は止まらない）。`LLM_PROVIDER=fake` は title の単語が
    直近の記録から消えたら終了とみなす決定的ロジック（開始後3件未満は継続扱い）。
- `events.session_id uuid null`（FK, ON DELETE SET NULL）
  - `events:batch` で受けた activity/screen_summary は、その時刻に active な
    work_session があれば自動で紐づける。
- `importer_state(name PK, cursor_ts timestamptz, last_success_at, last_error, imported_count, updated_at)`
  - バックグラウンド取り込み（agent-memory → ai_work イベント）の進捗管理。
    cursor_ts は「最後に取り込んだ created_at（含む）」で、ページ単位で commit される
    （途中失敗しても続きから再試行）。

- `importer_state`（上記）: agent-memory 取り込みのカーソルと状態
- agent-memory からの取り込み（P3-A、バックグラウンド）:
  `GET {AM_BASE_URL}/v1/memories`（created_at 昇順・カーソルページング、クエリの
  ISO 時刻の「+」は %2B エンコード）を読み、各記録を events に upsert する。
  id=memory_id、kind='ai_work'、device_id='agent:<source.agent>'（devices に
  name=<agent>, os='agent' で自動登録）、started_at=created_at、text=content、
  title=type+先頭タグの短い見出し（例 "decision · MineContext"）、source=source.uri、
  meta={memory_type, tags, refs, source_session, valid_from, supersedes, superseded_by}。
  取り込み時刻帯に active な work_session があれば session_id を紐づける
  （events:batch と同じ規則。無ければ紐づけない）。supersede された記録は
  meta.superseded_by を付けた上で DB に残し、timeline では
  include_superseded=false（既定）で隠す。失敗（agent-memory 停止・タイムアウト）は
  ログと importer_state.last_error に記録するだけで次の周期で再試行する
  （fail-soft。止まるのは取り込みだけ）。初回は AM_IMPORT_LOOKBACK_HOURS から遡る。
  注意: api コンテナから agent-memory への経路はホストファイアウォールの影響を受ける
  （<memory-host> は ufw の既定 DROP で、コンテナ→ホスト/テールネット IP が遮断された実績あり。
  到達できない場合は ufw の許可か network_mode の調整が必要）。

## API 例（curl）

時刻は ISO8601 タイムゾーン付き。レスポンスの時刻は UTC (Z)。

```sh
B=http://127.0.0.1:18741

# ヘルスチェック
curl -s $B/healthz
# -> {"ok":true,"db":"ok","embed_pending":0}

# 端末登録（upsert。last_seen_at 更新）
curl -s -X PUT $B/v1/devices/mac-1 -H 'Content-Type: application/json' \
  -d '{"name":"MacBook Pro","os":"darwin"}'

# イベント一括投入（最大500。id 冪等 upsert: 同じ id は内容更新）
curl -s -X POST "$B/v1/events:batch" -H 'Content-Type: application/json' -d '{
  "device_id":"mac-1",
  "events":[{
    "id":"0192a0c0-0000-7000-8000-000000000001",
    "kind":"note",
    "started_at":"2026-10-12T09:00:00+09:00",
    "ended_at":"2026-10-12T09:30:00+09:00",
    "app":"Obsidian",
    "title":"週次メモ",
    "text":"四半期財務レポートのレビューをした",
    "meta":{"project":"minecontext"},
    "source":"obsidian-plugin"
  }]
}'
# -> {"accepted":1,"updated":0}   accepted=新規挿入, updated=同一id更新

# 一覧（started_at 昇順、カーソルページング）
curl -s "$B/v1/events?from=2026-10-12T00:00:00Z&device_id=mac-1&kind=note&limit=100"
curl -s "$B/v1/events?limit=100&cursor=<next_cursor>"

# ある日の全端末の時系列（端末名付き）と kind 別件数
curl -s "$B/v1/days/2026-10-12?tz=Asia/Tokyo" | jq '.counts, [.events[]|{t:.started_at,d:.device_name,txt:.text}]'

# ハイブリッド検索（pgvector cosine 上位50 と pg_trgm similarity 上位50 を RRF k=60 で統合）
# 埋め込み無しのイベントも trgm 側で拾う。各結果に score と根拠（vector_rank / trgm_rank）付き。
curl -s -X POST $B/v1/search -H 'Content-Type: application/json' -d '{
  "query":"財務レポート",
  "from":"2026-10-01T00:00:00Z", "to":"2026-11-01T00:00:00Z",
  "kinds":["note","report"], "device_ids":["mac-1"], "k":20
}'

# 削除（単体のみ）
curl -s -X DELETE $B/v1/events/0192a0c0-0000-7000-8000-000000000001

# 作業宣言（新しい宣言は旧 active を superseded 終了させる）
curl -s -X POST $B/v1/sessions -H 'Content-Type: application/json' -d '{
  "title":"財務レポート", "note":"Q3のレビュー", "device_id":"mac-1"
}'
# -> {"session":{"id":"...","status":"active",...},"superseded":null}

# 現在の active な宣言
curl -s $B/v1/sessions/active

# 手動終了（manual・confirmed=true）
curl -s -X POST $B/v1/sessions/<id>/end -H 'Content-Type: application/json' \
  -d '{"ended_at":"2026-10-12T11:30:00+09:00"}'

# 修正・確定（title/note/started_at/ended_at/confirmed。AI 終了の確定に使う）
curl -s -X PATCH $B/v1/sessions/<id> -H 'Content-Type: application/json' \
  -d '{"title":"財務レポート（訂正）","ended_at":"2026-10-12T11:15:00+09:00","confirmed":true}'

# 取り消し（宣言を撤回）
curl -s -X POST $B/v1/sessions/<id>/cancel

# 一覧
curl -s "$B/v1/sessions?from=2026-10-12T00:00:00Z&status=ended"

# 1日分の統合タイムライン: sessions + events（端末名付き）+ ai_work を時刻順1本、
# セッション別の合計時間（cancelled を除く）付き
curl -s "$B/v1/timeline?date=2026-10-12&tz=Asia/Tokyo" | \
  jq '{total_minutes, totals: [.session_totals[]|{title,minutes,status}], items: [.items[]|{type,t,title:(.session.title // .event.title)}]}'
# supersede された ai_work（meta.superseded_by あり）は既定で隠れる。表示するには:
curl -s "$B/v1/timeline?date=2026-10-12&tz=Asia/Tokyo&include_superseded=true"

# 日別集計（ヒートマップ用）: 期間内の全日を返す（記録の無い日も 0 で）。
# events=全件数 / mine=kind != ai_work / ai_work / active_minutes・ai_active_minutes=
# started_at を5分刻みに丸めた異なる区間の数 x5 / session_minutes=その日に重なる
# work_sessions（cancelled 以外）を日境界で切って合計（active は now まで）。
# 期間は最大400日。日付の境界は tz で計算。1クエリで集計。
curl -s "$B/v1/stats/daily?from=2026-09-13&to=2026-10-12&tz=Asia/Tokyo" | jq '.days[-1]'

# agent-memory 取り込み（ai_work イベント）の状態と手動実行
curl -s $B/v1/importers
# -> {"importers":[{"name":"agent_memory","enabled":true,"cursor_ts":"...",
#     "last_success_at":"...","last_error":null,"imported_count":12,...}]}
curl -s -X POST $B/v1/importers/agent-memory/run
# -> {"ok":true,"fetched":3,"upserted":3,"pages":1,"cursor_ts":"..."}
```

## E2E テスト

```sh
sudo ./scripts/e2e.sh
```

docker compose up（fake 埋め込み + fake 終了判定、判定周期3秒・idle 12秒）→ healthz →
端末2台登録 → 2台分イベント投入（ja/en、同 id 再投入で更新）→ days で時系列・件数 →
カーソルページング → 検索（日本語部分一致とベクトル側一致）→ 削除 → 作業宣言と
activity/screen_summary の自動紐付け → 無関係なイベントが続くと ai_detected 終了
（終了時刻は最後の関係イベント時刻）→ 新しい宣言で旧 active が superseded →
イベント途絶えで idle 終了 → PATCH で修正と confirmed → cancel → timeline で
sessions・events・ai_work が時刻順に並びセッション別合計が出る → 埋め込みと LLM
到達不能時の投入・検索・宣言の継続 → 偽 agent-memory（scripts/fake_agent_memory.py を
compose の e2e プロファイルサービス `--profile e2e` として起動、2ページ・ページサイズ2、
同じネットワーク内なので本物には触らない）から ai_work 取り込み（タイムライン時刻順・superseded
既定で非表示・再実行で重複なし・active セッションへ紐付け）→ 偽サーバ停止中も
サーバは動き続け（run は 502・last_error 記録）復帰後にバックグラウンドで再取り込み、
→ 日別集計 stats/daily（記録の無い日が 0・tz 境界の日付・active_minutes の丸め・
セッションの日跨ぎ分割・期間超過 422）、を curl+jq で検証し PASS/FAIL を出す。
終了時に `compose down -v` で環境を消す。

`fake` provider はテキストの単語・文字2-gram を特徴ハッシュした決定的ベクトルで、
「意味検索」は n-gram 共有による近似（外部通信なし）。本番相当の意味検索には
`openai_compat` で実埋め込みモデルを指定すること。

e2e.sh の compose project 名は `mcsync-e2e` に固定している（本番 `mcsync-prod` との分離）。

## `cloudflare` provider（Workers AI 埋め込み）

agent-memory（記憶システム）と同じ構成: OpenAI 互換エンドポイント
`POST https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1/embeddings` に
`{"model","input"}` を送る（Bearer トークン認証）。クエリ・文書のどちらにも
instruction（前置き）は付けない — gateway は graphiti の OpenAI クライアントを
同じ `/ai/v1` に向けており、embedded に前置きを追加していない
（`agent-memory/gateway/app/graph_store.py` の `build_cloudflare_embedder_config`、
graphiti `graphiti_core/embedder/openai.py` の `create`/`create_batch`）。
アカウント ID とトークンは env 形式ファイル（`EMBED_CF_ENV_FILE`）から読み、
環境変数・コマンドラインには載せない。失敗時は既存の fail-soft
（events の保存・trgm 検索は止まらず、次周期で再試行）。

## 本番配置（<sync-host>）

本番は <sync-host>（Ubuntu 24.04, tailnet <sync-server-ip>）の `/opt/minecontext-sync/` で
`docker-compose.prod.yml` を重ねて動く。compose project 名 `mcsync-prod`
（override の `name:` と `-p` の両方で固定）、ボリューム `mcsync-prod-pgdata`。

- api のバインドは `<sync-server-ip>:18740` と `127.0.0.1:18740` のみ
  （`ports: !override` で base の開発用マッピングを置換）。**認証がないため
  0.0.0.0 や公開 IP には絶対に bind しない。**
- 埋め込み: `EMBED_PROVIDER=cloudflare`（`@cf/qwen/qwen3-embedding-0.6b`, 1024 次元）。
  認証は `/root/.config/minecontext/cloudflare.env`（mode 600、
  `CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN`）を読み取り専用マウント。
- LLM（作業終了判定）: `/root/.config/minecontext/zhipu-key`（600）を読み取り専用
  マウント、`LLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4`、
  `LLM_MODEL=glm-5.3-flash`。
- agent-memory 取り込み: `AM_BASE_URL=http://<memory-host>.<tailnet>.ts.net:8377`
  （`extra_hosts` で `<memory-host>.<tailnet>.ts.net:<memory-host-ip>` を固定）。
- メモリ上限 api 512m / db 768m、`restart: unless-stopped`（ホスト再起動後に自動復帰）。
- db のポートは公開しない。<sync-host> 既存の Caddy・1Panel 等には触れない。

### 初回構築（<memory-host> から）

```sh
# 1) 秘密ファイルを置く（値は出力しない）
ssh <sync-host> 'install -d -m 700 /root/.config/minecontext'
scp -p ~/.config/minecontext/zhipu-key <sync-host>:/root/.config/minecontext/
scp -p <(printf 'CLOUDFLARE_ACCOUNT_ID=%s\nCLOUDFLARE_API_TOKEN=%s\n' \
  "$(grep '^CLOUDFLARE_ACCOUNT_ID=' ~/agent-memory/.env | cut -d= -f2-)" \
  "$(grep '^CLOUDFLARE_API_TOKEN='   ~/agent-memory/.env | cut -d= -f2-)") \
  <sync-host>:/root/.config/minecontext/cloudflare.env
ssh <sync-host> 'chmod 600 /root/.config/minecontext/cloudflare.env /root/.config/minecontext/zhipu-key'

# 2) コードを配置して起動
rsync -a --delete --exclude .venv --exclude __pycache__ \
  --exclude backup.sh --exclude backups/ --exclude README.backup.md \
  ~/dev/MineContext/sync-server/ <sync-host>:/opt/minecontext-sync/
ssh <sync-host> 'cd /opt/minecontext-sync && docker compose -f docker-compose.yml \
  -f docker-compose.prod.yml -p mcsync-prod up -d --build'
```

### 運用（<sync-host> 上）

```sh
cd /opt/minecontext-sync
docker compose -f docker-compose.yml -f docker-compose.prod.yml -p mcsync-prod ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml -p mcsync-prod logs -f api
# 停止 / 再開
docker compose -f docker-compose.yml -f docker-compose.prod.yml -p mcsync-prod stop
docker compose -f docker-compose.yml -f docker-compose.prod.yml -p mcsync-prod start
```

### 更新手順（<memory-host> から）

```sh
rsync -a --delete --exclude .venv --exclude __pycache__ \
  --exclude backup.sh --exclude backups/ --exclude README.backup.md \
  ~/dev/MineContext/sync-server/ <sync-host>:/opt/minecontext-sync/
ssh <sync-host> 'cd /opt/minecontext-sync && docker compose -f docker-compose.yml \
  -f docker-compose.prod.yml -p mcsync-prod up -d --build'
```

`--delete` は `/opt/minecontext-sync/` 配下のみに効く（秘密は `/root/.config/` 別置き）。
`backup.sh` / `backups/` / `README.backup.md` は <sync-host> 固有のため exclude してある（消すと timer が壊れる）。
データ（`mcsync-prod-pgdata` ボリューム）は `down -v` しない限り消えない。

### バックアップ

<sync-host> では systemd timer（`minecontext-backup.timer`、毎日 03:20 JST）が
`/opt/minecontext-sync/backup.sh` を実行し、`pg_dump -Fc` を
`/opt/minecontext-sync/backups/mcsync-YYYYMMDD-HHMM.dump` に保存する。
14 日より古い `*.dump` は自動削除。詳細・復元手順は <sync-host>:`/opt/minecontext-sync/README.backup.md` 参照。

```sh
# 状態確認・手動実行（<sync-host> 上）
systemctl list-timers minecontext-backup.timer
systemctl start minecontext-backup.service
# ダンプの中身を確認
docker exec -i mcsync-prod-db-1 pg_restore -l \
  < /opt/minecontext-sync/backups/mcsync-XXXXXXXX-HHMM.dump | less
```

復元手順（<sync-host> 上。DB を作り直すため実行前に確認すること）:

```sh
cd /opt/minecontext-sync
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml -p mcsync-prod"

# 1. api を止める
$C stop api
# 2. DB を作り直す（sync を DROP して CREATE）
$C exec -T db psql -U sync -d postgres \
  -c "DROP DATABASE sync;" -c "CREATE DATABASE sync OWNER sync;"
# 3. リストア（vector / pg_trgm 拡張もダンプから作成される）
$C exec -T db pg_restore -U sync --no-owner -d sync \
  < /opt/minecontext-sync/backups/mcsync-XXXXXXXX-HHMM.dump
# 4. api を戻して確認
$C start api
curl http://127.0.0.1:18740/healthz   # {"ok":true,"db":"ok"}
```

ボリューム `mcsync-prod-pgdata` ごと消えた場合は `$C up -d db` で空の DB を初期化してから 3 を行う。
pgdata ボリュームの tar 退避（`down -v` 前の全退避）は代替手段として使える:
`docker run --rm -v mcsync-prod-pgdata:/data -v /root:/backup alpine \
  tar czf /backup/minecontext-sync-$(date +%F).tar.gz -C /data .`

## Lab（tailnet 内 Linux）への移行手順 — 未実施

以下は想定手順のメモであり、**今回実施していない**（Lab には触っていない）。

1. Lab にこのディレクトリをコピー（git 管理後 `git pull` か scp）。
2. `docker compose` と pgvector イメージが動くことを確認。
3. ホスト側バインドを tailnet に向ける: `BIND_HOST=<tailscale IP> API_PORT=18741 docker compose up -d --build`
   （認証はないため、tailscale/ファイアウォール以外で公開しないこと）。
4. 実埋め込みを使う場合:
   `EMBED_PROVIDER=openai_compat EMBED_BASE_URL=https://.../v1 EMBED_MODEL=... EMBED_DIM=<モデル次元> EMBED_API_KEY_FILE=/path/to/key`
   を設定してから初回起動（EMBED_DIM は vector 列の次元に固定されるため、初回起動前に確定させる）。
   API キーはファイルマウントで渡す（compose に追記が必要）。
5. 各端末（MineContext）のアップロード先を `http://<tailscale IP>:18741` に設定。
6. バックアップ: `db-data` ボリューム（または `pg_dump`）を定期退避。
