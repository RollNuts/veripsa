# Phase 2 Output Schema

## 共通規約

- JSONはUTF-8、2-space indent、sorted keys、末尾newline
- source fileはtarget repository-relativeの`/`区切りpath
- source lineは1-based
- source columnがあるindexでは1-based
- confidenceは`high`、`medium`、`low`
- warning severityは監査診断の`info`、`warning`、`error`であり、脆弱性severityではない
- 取得・静的解決できないscalarは`null`
- 解決不能なrecordは捨てず、`unresolved_reason`または`unresolved_gaps`へ理由を残す
- 空のcollectionは`[]`または`{}`

## nullとunresolvedの方針

`null`を推測値で置換しません。

例:

- dynamic route path → `path: null`
- controller actionが一意でない → `controller_file: null`, `action_line: null`
- dynamic authorization action → `inferred_action: null`
- workerが一意でない → `resolved_worker_class: null`, `worker_file: null`
- maximum RSSを取得できない → `maximum_rss_bytes: null`
- aggregate graph warningで個別fileが不明 → `file: null`, `line: null`

raw receiver/argument expressionはcallに必要な最小expressionだけです。320文字を超えるかNULを含むexpressionは`null`にし、unresolved warningを出します。

## 1. `run_manifest.json`

単一JSON objectです。

| Field | Type | 意味 |
|---|---|---|
| `target_absolute_path` | string | symlink解決後のGit top-level |
| `target_repository_name` | string | target root basename |
| `target_commit_sha` | string | `HEAD` SHA |
| `target_dirty_state` | boolean | 実行前のtarget dirty状態 |
| `veripsa_commit_sha` | string | 実行したVeripsa `HEAD` SHA |
| `veripsa_dirty_state` | boolean | successful runでは`false` |
| `utc_timestamp` | string | timezone-aware ISO-8601 UTC |
| `exact_command` | array[string]/null | 取得できた場合のPython executableを含み得るargv |
| `exact_command_capture` | string | command取得方法またはprogrammatic callで取得不能だった理由 |
| `python_version` | string | runtime Python version |
| `extractor_versions` | object | producer名→version stringまたは`null` |
| `graph_schema_contract_version` | integer/null | rich graph producerが宣言したschema contract version |
| `relevant_package_versions` | object | package名→version stringまたは`null` |
| `elapsed_time_seconds` | number | validationからartifact生成まで |
| `maximum_rss_bytes` | integer/null | process最大RSSをbyteへ正規化 |
| `files_parsed` | integer/null | rich graph aggregate |
| `files_failed` | integer/null | rich graph aggregate |
| `files_skipped` | integer/null | rich graph aggregate |
| `node_count` | integer | `nodes.json`件数 |
| `edge_count` | integer | `edges.json`件数 |
| `warning_count` | integer | `warnings.json`件数 |
| `include_patterns` | array[string] | 指定されたaudit-index include |
| `exclude_patterns` | array[string] | 指定されたaudit-index exclude |
| `artifacts` | array[string] | このrunが生成する9成果物名 |

`target_dirty_state: true`の場合、SHAだけでは解析bytesを再現できません。

`exact_command_capture`は次のいずれかです。

```text
caller_supplied
sys.orig_argv
reconstructed_module_argv
reconstructed_script_argv
unavailable_programmatic_call
```

通常の`python -m tools.hunt`ではcommandを取得または再構築します。programmatic APIからcaller supplied commandなしで呼んだ場合は、推測せず`exact_command: null`とします。

`extractor_versions`は次の2 keyを持ちます。

```json
{
  "rich_graph": "cg2",
  "ruby_rails_audit_index": "hunt-ruby-rails-v1"
}
```

`rich_graph`と`graph_schema_contract_version`は、実際の`build_graph()`戻り値に
producerが宣言した値だけを記録します。commit SHA、package version、Node/Edge shape
から補完しません。producer metadataが取得できなければ`null`です。
`ruby_rails_audit_index`は索引の抽出・解決規則とrecord schemaのversionです。

## 2. `nodes.json`

既存`build_graph()`が返した`nodes` arrayを変換せず保存します。Phase 2固有fieldやNode kindは追加しません。

共通的なfield:

```json
{
  "id": "path-or-symbol-id",
  "kind": "existing-graph-node-kind",
  "path": "repository/relative/path",
  "name": "optional-symbol-name",
  "language": "optional-language",
  "start_line": 1,
  "end_line": 2
}
```

Node kindごとのoptional fieldは既存rich graph producerに従います。

## 3. `edges.json`

既存`build_graph()`が返した`edges` arrayを変換せず保存します。Phase 2固有Edge kindは追加しません。

```json
{
  "src": "source-coordinate",
  "dst": "destination-coordinate",
  "kind": "existing-graph-edge-kind"
}
```

## 4. `rails_routes.json`

route recordのarrayです。

| Field | Type |
|---|---|
| `source_file` | string |
| `source_line` | integer |
| `source_column` | integer |
| `http_verb` | string/null |
| `path` | string/null |
| `controller` | string/null |
| `action` | string/null |
| `namespace_stack` | array[string] |
| `controller_file` | string/null |
| `action_line` | integer/null |
| `confidence` | string |
| `unresolved_reason` | string/null |

`resources`/`resource` expansionでは、一つのDSL declarationから複数recordが同じsource lineで生成されます。

## 5. `authorization_calls.json`

authorizationに関係し得るcall-site recordのarrayです。

| Field | Type |
|---|---|
| `source_file` | string |
| `source_line` | integer |
| `source_column` | integer |
| `enclosing_class_or_module` | string/null |
| `enclosing_method` | string/null |
| `receiver` | string/null |
| `call_name` | string |
| `raw_arguments` | array[string/null] |
| `inferred_action` | string/null |
| `inferred_resource` | string/null |
| `confidence` | string |
| `unresolved_reason` | string/null |

このrecordはcall site索引です。authorization checkが十分または不足しているという判定ではありません。

## 6. `sidekiq_jobs.json`

次のtop-level objectです。

```json
{
  "workers": [],
  "enqueue_sites": []
}
```

### `workers[]`

| Field | Type |
|---|---|
| `class_name` | string |
| `class_file` | string |
| `class_line` | integer |
| `worker_markers` | array[string] |
| `perform_method_line` | integer/null |
| `perform_parameters` | array[string] |
| `worker_file` | string/null |
| `queue_metadata` | object |
| `confidence` | string |
| `unresolved_reason` | string/null |

`queue_metadata`:

```json
{
  "queue": "project_export",
  "feature_category": "import_export",
  "urgency": "low",
  "idempotent": true,
  "sidekiq_options": {
    "queue": "project_export"
  }
}
```

取得できないmetadata valueは`null`、`sidekiq_options`は空object、`idempotent!`がなければ`false`です。

### `enqueue_sites[]`

| Field | Type |
|---|---|
| `enqueue_source_file` | string |
| `enqueue_line` | integer |
| `enqueue_column` | integer |
| `enclosing_class_or_module` | string/null |
| `enclosing_method` | string/null |
| `receiver_class_name` | string/null |
| `enqueue_method` | string |
| `raw_argument_expressions` | array[string/null] |
| `resolved_worker_class` | string/null |
| `worker_file` | string/null |
| `perform_method_line` | integer/null |
| `perform_parameters` | array[string] |
| `queue_metadata` | object/null |
| `confidence` | string |
| `unresolved_reason` | string/null |

worker未解決時、`perform_parameters`は空arrayです。`delay`は索引されますが、worker/performは常に未解決です。

## 7. `execution_paths.json`

execution path recordのarrayです。

| Field | Type |
|---|---|
| `path_id` | string |
| `status` | `resolved`または`partial` |
| `route` | object |
| `hops` | array[hop] |
| `confidence` | string |
| `unresolved_gaps` | array[string] |

`route`:

```json
{
  "file": "config/routes.rb",
  "line": 4,
  "verb": "POST",
  "path": "/api/v1/project_exports",
  "controller": "Api::V1::ProjectExportsController",
  "action": "create"
}
```

`hop`:

| Field | Type |
|---|---|
| `source_file` | string |
| `source_line` | integer |
| `destination_file` | string |
| `destination_line` | integer |
| `relation` | string |
| `confidence` | string |
| `evidence` | string |

relationは次のいずれかです。

```text
route_to_controller
contains_authorization
calls_service
contains_enqueue
enqueue_to_worker_perform
```

`resolved`は`unresolved_gaps: []`かつ必要hopが静的に接続されたことを表します。runtime branch/orderやauthorization sufficiencyは表しません。route自体が未解決なら`hops`は空になり得ます。`partial`でも既知のcontroller/auth/service/enqueue hopは保持します。

## 8. `warnings.json`

warning recordのarrayです。

```json
{
  "file": "repository/relative/file.rb",
  "line": 10,
  "category": "sidekiq_enqueue_unresolved",
  "reason": "worker class has 0 indexed definitions: UnknownWorker",
  "severity": "info"
}
```

Field:

```text
file: string|null
line: integer|null
category: string
reason: string
severity: info|warning|error
```

代表的なcategory:

```text
ruby_grammar_unavailable
ruby_read_failed
ruby_parse_failed
ruby_parse_error
ruby_definition_unresolved
rails_route_unresolved
rails_route_dynamic_context
rails_route_unsupported_dsl
rails_route_inclusion_context_unresolved
rails_controller_action_unresolved
authorization_call_unresolved
sidekiq_worker_unresolved
sidekiq_enqueue_unresolved
service_call_unresolved
execution_path_cap_reached
graph_parse_failures_aggregate
graph_skipped_files_aggregate
```

## 9. `summary.md`

人が最初に確認する件数要約です。

- target名、commit、dirty state
- graph Node/Edge count
- route/auth/worker/enqueue count
- resolved/partial path count
- warning count
- 静的索引でありsecurity findingではない旨

機械的な再現証拠には`run_manifest.json`と各JSONを使用してください。
