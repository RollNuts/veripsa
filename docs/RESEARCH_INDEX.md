# Research index and evidence boundaries

Start with the existing experiments, then follow their implementation and limits.
This index organizes public material and its evaluation boundaries; it adds no
hosted service or vulnerability finding. Veripsa Core is
[source-available under BSL 1.1](../LICENSE), not an OSI-approved open-source
license. The hosted GitHub App remains suspended since 2026-08-18.

日本語：既存の実装・再現例・限界への索引です。一般的な検出精度、処理時間、
脆弱性発見を実証した資料とは区別しています。まず[日本語の入口](README.ja.md)
から無料のローカルデモを試せます。

To contribute a reproduction, counterexample, measurement, or focused fix, use
the [participation guide](../CONTRIBUTING.md). External use or contribution is an
observation to record, not a result established by preparing this index.

## Explore the existing assets

| Area | Entry points | What to inspect |
| --- | --- | --- |
| Architecture | [Engineering choices](../ARCHITECTURE.md), [implementation inventory](ANALYSIS_ASSETS.md) | Extraction, offline evidence, and the separate event-driven App runtime. |
| Code graph | [Static graph entry points](ANALYSIS_ASSETS.md#existing-implementation-map), [schema contract](GRAPH_SCHEMA_CONTRACT.md) | Reference kinds, coverage, resource identities, uncertainty, and optional parser dependencies. Language support does not imply complete semantic resolution. |
| Co-change | [Static and historical evidence](ANALYSIS_ASSETS.md#static-and-historical-evidence-complement-each-other), [history update limits](ANALYSIS_ASSETS.md#history-update-limits), [producer](../_cg_cochange.py) | Repeated joint edits, rename handling, large-commit filtering, thresholds, and the distinction between offline set comparisons and App SQL policy. |
| Evaluation | [Synthetic merge comparison](../examples/structural-overlap/README.md), [pinned Flask history evaluation](OFFLINE_EXAMPLE.md), [isolated SQL verification record](ANALYSIS_ASSETS.md#verification-record-for-this-change) | Inputs, commands, observations, counterexamples, and what each experiment actually measures. |
| Security research | [Public local audit-index prototype](hunt/phase2/README.md), [its limitations](hunt/phase2/limitations.md) | Conservative Rails route / authorization-call / job links. A resolved static path is not proof of authorization, a vulnerability, or exploitability. |
| Examples | [Git + Python local demo](../examples/structural-overlap/README.md), [expected JSON](../examples/structural-overlap/expected-output.json), [Flask reproduction](OFFLINE_EXAMPLE.md) | Run free local examples before adding a database or an AI-agent integration. |
| Limitations | [Evidence status below](#what-has-and-has-not-been-measured), [demo limits](../examples/structural-overlap/README.md), [data boundary](../SECURITY.md#content-free-posture), [current product scope](PRODUCT_FACTS.md) | Dynamic references, incidental co-edits, history-poor inputs, sensitive metadata, and unavailable hosted operation. |
| Development history | [Origin and execution triggers](../ARCHITECTURE.md#origin-and-execution-triggers), [author-provided background draft](PUBLISHING_KIT.ja.md#背景記事の下書き), [changelog](../CHANGELOG.md) | Frequent Git-conflict friction, the App/MCP trigger choice, later experiments, and the distinction between the author's account and measured results. |

## What has and has not been measured

| Question | Existing public evidence | Boundary |
| --- | --- | --- |
| Can a clean Git merge combine two individually valid changes into a failure? | Four synthetic contrasts compare real Git merges, individual/combined Python execution, graph/history candidates, and offline union/intersection. | Constructed behavioral examples, including compatible related changes and incidental co-edits; not a representative accuracy dataset or the production PR verdict. |
| Do structural pairs correspond to past joint edits? | The pinned Flask evaluation records sampled historical controls and a repeat run with the same report. | One retrospective public checkout. Historical correlation is not a bug, vulnerability, future-failure, or productivity label. |
| Do selected implementation contracts hold? | The existing demo has 13 tests; the analysis inventory records ten targeted isolated PostgreSQL gate scripts. | Named regression observations, not a full release-suite result or current hosted deployment evidence. This index does not rerun or expand that record. |
| Can review-pair evidence be scored on a fixed small population? | The [four-pair synthetic benchmark](../examples/structural-overlap/README.md#a-shared-labeled-regression-benchmark) has separate labels, TP/FP/FN/TN and precision/recall for graph, history, union and corroboration. | Constructed population only. Union recovers one dynamic miss while keeping one incidental-history false positive; these are not general accuracy or vulnerability labels. |
| What are general precision, recall, and false-positive / false-negative rates? | **Not measured by these experiments.** The demo exposes concrete misses and non-failing relationships. | Those examples are not an independently labelled, exhaustive reference set. Do not convert them or the Flask lift ratios into general accuracy percentages. |
| What are processing time and peak memory? | The synthetic benchmark can write separate wall-time and platform-unit `RUSAGE_CHILDREN` RSS measurements, with source identity and dirty state. No cross-tool or representative performance study is published. | The recorded child RSS is not aggregate concurrent process-tree peak memory. Compare timing/RSS separately from stable scores, and record clean source commits, environment and scope. |
| Does the graph rediscover vulnerabilities or reduce security-review time? | **Not established by the experiments indexed here.** The public audit-index prototype documents static links and unresolved cases. | No vulnerability-detection accuracy, authorized live-target result, bounty outcome, or comparison of researcher effort follows from a graph edge. |

An absent edge is **not observed**, not proof that a dependency is absent or a
change is safe. More candidates can also mean more review work. Offline union
and intersection are ways to compare evidence, not established accuracy gains.

## Keep new results inspectable

For each additional experiment, record the public or synthetic input and exact
commit, analyzer commit, environment and optional parsers, command, expected
review points and how they were selected, complete candidate counts, observed
results, and failure cases. For a historical change-review experiment, analyze
the parent revision so the changed code and future history cannot leak into its
inputs. A source-checked review checklist is not an exhaustive ground truth;
report checklist coverage and unreviewed candidates separately instead of
inventing precision or recall. Keep elapsed time and peak memory separate from
stable output when measuring reproducibility.

Only publish material permitted by the [data and reporting boundary](../SECURITY.md).
The public audit-index prototype's raw artifacts have their own
[output schema](hunt/phase2/output-schema.md); their paths, expressions, and
metadata need review before sharing. Their existence does not make arbitrary
analysis output suitable for public release.
