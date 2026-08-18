# Next Phase

Phase 2の完了時点では、synthetic fixtureでローカル監査索引の構造と保守的なunresolved behaviorを検証しただけです。

GitLab実Repo解析、Phase 3、脆弱性候補評価には進みません。

## Phase 3へ進む最小条件

1. Phase 2実装を専用branch上のclean commitへ固定する。
2. Phase 2 testと関連graph regressionがofflineでpassし、terminal evidenceが保存されている。
3. Node/Edge kindとDB/GitHub App/SaaS/UIが変更されていないことをdiff reviewで確認する。
4. ユーザーが用意したcleanなlocal GitLab checkout pathと対象commit SHAを受け取る。
5. GitLab checkoutを自動cloneせず、Git top-levelとclean stateを確認する。
6. outputをGitLab/Veripsa双方の外側に用意する。
7. GitLab規模のfull graphに必要な時間・memoryを安全なlocal環境で計測できる。
8. Project export/importに必要なinclude/exclude scopeと、除外による未検出riskを合意する。
9. Phase 3実行について明示的な承認を得る。

いずれかが欠ける場合は、GitLab解析を開始せずblockerとして報告します。

## 承認後の最小Phase 3

Phase 3を実施する場合も、最初の作業はlocal source indexの生成と人手確認だけです。

1. GitLabとVeripsa双方のSHA、clean state、commandを記録する。
2. `python -m tools.hunt`でfull rich graphとPhase 2索引を生成する。
3. `warnings.json`を先に確認し、parse failure、dynamic route、unknown worker、ambiguous serviceを整理する。
4. Project export/importに関係するroute、controller、authorization、enqueue、workerのfile/lineを元コードで確認する。
5. graph/index edgeを事実と盲信せず、GitLabのroute定義、Ability/policy、service、worker、既存testと突合する。
6. coverage gapと誤結合を記録する。

この段階でも脆弱性、severity、exploitabilityを自動判定しません。

## extractor変更を検討できる条件

Phase 3で新しいextractor/index relationを追加できるのは、次をすべて満たす場合だけです。

1. GitLabの具体的なProject export/import経路を確認するために必要
2. ripgrep、元コード、既存indexでは解決できない
3. 他の監査対象でも再利用可能
4. 実装とfixture/testを小規模に保てる
5. existing graph schemaと製品挙動を変更しない

大規模な設計変更が必要なら実装せず、必要性、代替案、変更範囲を報告して停止します。

## 引き続き禁止する操作

- GitLab.com、本番資産、他人のdataへのrequest
- account操作、scan、attack
- GitLabの自動clone
- PoCの本番送信
- HackerOne自動報告
- DB、GitHub App、SaaS、UIへの統合
- 汎用CFG、SSA、taint、points-to engine

次の行動は、Phase 2のclean handoffとユーザーによるPhase 3承認を待つことです。
