# Veripsa Hunt Phase 2

Phase 2は、任意のローカルGit repositoryに対して既存のrich graphをfull rebuildし、Ruby/Railsコードから次の監査索引を作るためのオフライン専用CLIです。

```text
Rails route
→ controller action
→ authorization call
→ 最大3段の静的service call
→ Sidekiq enqueue
→ worker perform
```

このCLIは脆弱性を検出・判定するものではありません。`resolved` pathも、静的かつ一意な定義をfile/lineで結べたことだけを表します。認可の十分性、実行時の分岐、exploitability、severityは証明しません。

## 境界

`python -m tools.hunt` は次を行いません。

- GitHub App、product ingest、DB、SaaS、UIの呼び出し
- 外部通信
- incremental ingest
- target repositoryの実行または変更
- GitLab clone、GitLab起動、GitLab.comへの通信
- 脆弱性候補評価、PoC作成、HackerOne報告

出力は指定したoutput directoryだけに書き込みます。output directoryがtarget repositoryまたはVeripsa worktreeの内側にある場合は、symlinkを解決した後のpathでfail closedします。

target/Veripsaのgit stateは索引生成後、最初のartifact write前にも再確認します。通常の並行commitまたはclean→dirty変化を検出した場合、mixed snapshotを保存せず失敗します。repository localの`core.fsmonitor`は無効化し、global/system Git configも状態確認へ取り込みません。

Gitがdirtyとして表示しないignored untracked fileでも、rich graphのNode pathまたはEdge endpointへ現れた場合はcommit SHAから再現できないため、Ruby index開始前・artifact作成前にfail closedします。ignored `.gitattributes`は解析対象そのものを変え得るため、graphへ現れなくても拒否します。解析対象外のignored fileは許容します。

`assume-unchanged` / `skip-worktree` index flag、sparse checkout、gitlink/submoduleを含むtargetまたはVeripsa worktreeも、recorded SHAだけでfull working-tree inputを保証できないため拒否します。

## 前提

- Phase 2実装を含むVeripsa checkoutがcleanである
- Veripsa checkoutに`assume-unchanged` / `skip-worktree` pathやgitlink/submoduleがない
- targetがlocalのcleanかつfull Git checkoutで、HEAD commitを持つ
- targetに`assume-unchanged` / `skip-worktree` pathやgitlink/submoduleがない
- `--target`はabsolute pathかつGit top-levelそのもの
- `--output`はabsolute pathで、targetとVeripsaの外側にある
- `requirements.txt`のtree-sitter関連packageがローカル環境に導入済み

Veripsa worktreeがdirtyな場合のoverrideはありません。targetがdirtyな場合だけ、`--allow-dirty-target`で明示的に許可できます。その場合、manifestのcommit SHAは未commit bytesを識別しないため、再現性は低下します。

## 基本実行

同じcommitから再現する場合は、最初に両repositoryのSHAとclean状態を確認します。

```bash
HUNT_VERIPSA_ROOT=/absolute/path/to/Veripsa-hunt-phase2
HUNT_TARGET_ROOT=/absolute/path/to/local/repository
HUNT_OUTPUT_ROOT=/absolute/path/to/dedicated-output

git -C "$HUNT_VERIPSA_ROOT" rev-parse HEAD
GIT_OPTIONAL_LOCKS=0 git -C "$HUNT_VERIPSA_ROOT" status --porcelain=v1 --untracked-files=normal

git -C "$HUNT_TARGET_ROOT" rev-parse HEAD
GIT_OPTIONAL_LOCKS=0 git -C "$HUNT_TARGET_ROOT" status --porcelain=v1 --untracked-files=normal

cd "$HUNT_VERIPSA_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
  python3 -m tools.hunt \
  --target "$HUNT_TARGET_ROOT" \
  --output "$HUNT_OUTPUT_ROOT"
```

成功時は次の9成果物が作られます。

```text
run_manifest.json
nodes.json
edges.json
rails_routes.json
authorization_calls.json
sidekiq_jobs.json
execution_paths.json
warnings.json
summary.md
```

専用の空output directoryをrunごとに使ってください。CLIは上記成果物をatomic replaceしますが、output directoryに存在する無関係な古いファイルは削除しません。

## include/exclude

`--include-pattern`と`--exclude-pattern`はPython `fnmatch`形式のrepeatableなcase-sensitive globです。repository-relativeかつ`/`区切りのRuby file pathへ適用され、`*`は`/`にも一致します。

- includeが0件なら、既存extractorのfile guardを通過した全Ruby fileが監査索引の対象
- includeが複数なら、いずれかに一致したRuby fileが対象
- excludeはincludeより優先
- 先頭の`**/`はroot直下にも一致
- patternは`rails_routes.json`、authorization、Sidekiq、execution pathだけに作用
- `nodes.json`と`edges.json`はpatternに関係なく常にtarget全体のfull rich graph
- 一意性の誤証明を避けるため、除外Ruby fileのclass/method定義もdefinition universeには残す
- 除外fileのcall site、route、worker recordは出力せず、除外先の定義へ到達する関係は`partial`にする

例:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
  python3 -m tools.hunt \
  --target "$HUNT_TARGET_ROOT" \
  --output "$HUNT_OUTPUT_ROOT" \
  --include-pattern 'config/routes.rb' \
  --include-pattern 'config/routes/*.rb' \
  --include-pattern 'app/controllers/*export*.rb' \
  --include-pattern 'app/controllers/*import*.rb' \
  --include-pattern 'app/services/*export*.rb' \
  --include-pattern 'app/services/*import*.rb' \
  --include-pattern 'app/workers/*export*.rb' \
  --include-pattern 'app/workers/*import*.rb' \
  --exclude-pattern 'spec/*'
```

必要なcontroller、service、workerをpatternから外すと、routeやexecution pathは意図どおり`partial`になります。patternはgraph buildの負荷を減らすoptionではありません。

## dirty targetを明示的に解析する

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
  python3 -m tools.hunt \
  --target "$HUNT_TARGET_ROOT" \
  --output "$HUNT_OUTPUT_ROOT" \
  --allow-dirty-target
```

`run_manifest.json`の`target_dirty_state`は`true`になります。第三者再現用の証拠にはclean targetを使用してください。

## synthetic fixtureのオフライン再現

fixtureはGitLabコードをコピーせずに作成した小さなRails/Ruby例です。CLIはGit repository rootを要求するため、一時directoryへcopyしてlocal commitを作ります。

```bash
HUNT_VERIPSA_ROOT=/absolute/path/to/Veripsa-hunt-phase2
HUNT_FIXTURE_REPO="$(mktemp -d /tmp/veripsa-hunt-fixture.XXXXXX)"
HUNT_FIXTURE_OUTPUT="$(mktemp -d /tmp/veripsa-hunt-output.XXXXXX)"

cp -R "$HUNT_VERIPSA_ROOT/tests/fixtures/hunt_phase2/." "$HUNT_FIXTURE_REPO/"
git -C "$HUNT_FIXTURE_REPO" init -q
git -C "$HUNT_FIXTURE_REPO" add .
git -C "$HUNT_FIXTURE_REPO" \
  -c user.name='Veripsa Hunt Fixture' \
  -c user.email='hunt-fixture@example.invalid' \
  -c commit.gpgsign=false \
  commit --no-verify -q -m 'synthetic hunt fixture'

cd "$HUNT_VERIPSA_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
  python3 -m tools.hunt \
  --target "$HUNT_FIXTURE_REPO" \
  --output "$HUNT_FIXTURE_OUTPUT"

ls -1 "$HUNT_FIXTURE_OUTPUT"
```

fixtureには、正常なroute→authorize→service→`perform_async`→worker pathに加え、namespace/resources、member/collection、`perform_in`、unknown worker、dynamic route/auth、ambiguous/lexically-shadowed constant、外部route file、明示receiver decoy、保守的に未解決とする`delay`が含まれます。GitLab固有コードはコピーしていません。

## offline tests

```bash
cd "$HUNT_VERIPSA_ROOT"

PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
  python3 tests/test_hunt_indexes.py

PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
  python3 tests/test_hunt_cli.py
```

CLI gateは隔離したclean Veripsa copyから、`PYTHONDONTWRITEBYTECODE`を設定しないbare `python -m tools.hunt`もsubprocess実行します。targetの解析対象外ignored fileを含むdigest、Veripsaのdigest、git status、`__pycache__`/`.pyc`不在を確認します。また、ignored Ruby/edge-only input、ignored `.gitattributes`、target/Veripsaの`assume-unchanged` / `skip-worktree`、gitlink、DB/networkのPython API trap、repository-local `core.fsmonitor` decoy、解析中state changeのfail-closedを検証します。

関連する既存graph regression:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_extractor.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_extractor_determinism.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_resolve_go_ruby.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_routes_coverage.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_routes_redos.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_cross_tier_route_probe.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_job_queue_contract.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_dos_pathological_inputs.py
```

## 読み方

最初に`run_manifest.json`でSHA、dirty state、extractor/schema/package version、
countを確認し、次に`warnings.json`と`execution_paths.json`の`partial`を確認します。
各hopのfile/lineを元コードで必ず再確認してください。

`summary.md`は件数の便宜的な要約であり、security finding reportではありません。

Phase 2はここで停止します。GitLab実Repo解析、Phase 3、脆弱性候補評価には、別途明示的な承認があるまで進みません。
