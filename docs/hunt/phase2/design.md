# Phase 2 Design

## 目的

Phase 2は、既存rich graphを変更せずに、Ruby/Railsコードの次の静的監査索引をローカル生成します。

```text
Rails route
→ controller action
→ authorization call
→ optional service calls
→ Sidekiq enqueue
→ worker perform
```

設計の中心は「分からないものを結ばない」です。dynamic dispatch、複数定義、namespace推測が必要な関係は`unresolved`として残します。

## product graphとの分離

CLIは`code_graph_extract.build_graph(target, universe_paths=None)`をexactly once呼びます。これはwhole-repository full buildであり、incremental ingestではありません。

返された`nodes`と`edges`はそのまま`nodes.json`と`edges.json`へ保存します。Phase 2索引は別のJSONへ保存し、次を変更しません。

- graph Node kind
- graph Edge kind
- graph schema
- DB schema
- GitHub App ingestion
- SaaS/UI

DB readbackや`--push`は使用しません。この分離により、DB保存時の縮退を避けつつ、既存製品挙動へ影響を与えません。

## 実行pipeline

```text
CLI validation
├── target absolute/existing/Git root/HEAD
├── Veripsa clean
├── target clean unless explicitly allowed
├── full index state（no assume-unchanged/skip-worktree/gitlink）
└── output outside target and Veripsa
        ↓
build_graph(target, universe_paths=None) exactly once
        ↓
raw rich nodes/edges
        ↓
guarded Ruby file inventory
        ↓
Rails/auth/Sidekiq/service indexes
        ↓
execution path assembly
        ↓
target/Veripsa state recheck
        ↓
atomic JSON/Markdown writes to output only
```

targetとVeripsaのgit state取得では`GIT_OPTIONAL_LOCKS=0`を使用し、`core.fsmonitor=false`、`submodule.recurse=false`をcommand localで指定します。global/system Git config、terminal prompt、system attributesも無効化します。Python bytecode writeもprocess内で無効化します。

索引生成後の再確認でroot、HEAD、dirty booleanが変化していた場合、artifact write前にfail closedします。これはatomic filesystem snapshotではありませんが、通常の並行commitとclean→dirty変化が旧manifestへ混入することを防ぎます。

Git ignored untracked pathの集合も取得し、rich graph Nodeの`path`とEdgeの`src`/`dst`との交差をNFC正規化後に確認します。交差が1件でもあれば、commit SHAから解析入力を再構築できないため、Ruby indexとartifact writeの前にfail closedします。ignored `.gitattributes`はfile selectionを変えられるため、交差の有無にかかわらずgraph build前に拒否します。

normal statusから変更bytesを隠せる`assume-unchanged`と、full checkoutでない`skip-worktree`/sparse indexは、targetとVeripsaの双方でgraph build前に拒否します。gitlink/submoduleも、親commitだけでは実際に利用するworking treeを固定できないため双方で拒否します。

## Ruby inventory

監査索引は既存extractorのABI-safe Ruby grammar loaderとsource-file guardを再利用します。これによりgenerated/vendored、symlink、oversized、binary fileは既存graphと同じ境界で除外されます。

監査索引passでは、guardを通った各Ruby fileをtree-sitterで一度parseし、同じASTから次を収集します。これは先に実行されるrich graph extractionとは独立したlocal index passです。

- class/moduleとfully-qualified lexical name
- instance methodとsingleton method
- method parameter
- call siteのreceiver、method、arguments、enclosing class/method、lexical module stack、file/line/column
- class-level worker metadata call

Ruby inputのread/parse failure、またはparse treeのnamed `ERROR`/anonymous missing
tokenを`root.has_error`で検出した場合はwarningを出します。その1 fileがduplicate
controller/service/worker definitionを隠し得るため、definition universe全体を
incompleteとし、run内のroute→controllerとenqueue→workerの一意解決を停止します。

include/excludeはcall site、route、worker recordの出力対象を選びます。一方、除外fileのclass/method definitionも一意性判定の母集団へ残します。これにより、除外file内のduplicate/shadow definitionを見落として高confidenceに結ぶことを防ぎます。選択外definitionへしか到達できない関係は未解決です。

raw argument expressionはcallのtop-level argumentだけです。receiverまたは1 expressionが320文字を超える場合は本文を保存せず`null`にします。

Ruby/Rails audit indexは`hunt-ruby-rails-v1`をproducer versionとして宣言します。
route/auth/Sidekiq/execution-pathの抽出・解決規則またはrecord schemaを変更する場合に
versionを更新します。docs/testだけの変更やbyte-identical refactorでは更新しません。

manifestのrich graph versionとgraph schema contract versionは、`build_graph()`が返した
producer-declared fieldだけから取得します。commit、package、出力shapeから推測せず、
取得できない場合は`null`にします。

## Rails route index

route scan対象は、選択済みRuby fileのうち次です。

- `config/routes.rb`
- `config/routes/`配下
- filenameが`_routes.rb`で終わるfile

対応するstatic DSL:

```text
get post put patch delete
resources resource
namespace scope
member collection
```

`resources`と`resource`はcanonical REST actionsへ展開します。staticな`only:`、`except:`、`path:`、`controller:`だけを解釈します。それ以外のoptionはroute形状への影響を推測せず、declaration全体をunresolvedにします。member/collection blockはresource contextからpathとcontrollerを構築します。

controller/actionは、fully-qualified controller classとinstance methodの組にexactly one local definitionがある場合だけfile/lineへ接続します。0件または複数件ならroute recordを残し、controller/action hopは未解決にします。

dynamic positional/path/controller/module option、dynamic target、unsupported DSL call/block、bare `draw` block、明示receiver付きdecoyは推測せず、low confidence recordとwarningへ落とします。transparentに展開する`draw`は明示的な`Rails.application.routes.draw`だけです。resource内へ入れ子にしたnamespace/scopeはPhase 2ではpath contextを合成せず、未解決です。

`config/routes.rb`以外の`config/routes/**`/`*_routes.rb`は、`draw`されたscopeを証明できないためroute recordを作ってもinclusion context unknownとしてlow/unresolvedにし、controller actionやexecution pathへ確定接続しません。

## Authorization call index

対象call name:

```text
authorize
authorize!
can?
cannot?
Ability.allowed?
Ability.denied?
allowed?
denied?
policy
pundit_authorize
current_user.can?
current_user.cannot?
```

call-site自体は構文上の事実として記録します。action/resourceは、static symbolと単純なresource expressionの位置が既知の形だけ推定します。

frameworkやhelperごとのsignatureが曖昧な場合、call recordを捨てずにinferred fieldを`null`、confidenceを`low`、`unresolved_reason`を非nullにします。これは認可の存在・欠落・十分性を判定する処理ではありません。

## Sidekiq index

enqueue call:

```text
perform_async
perform_in
perform_at
Sidekiq::Client.push
Sidekiq::Client.push_bulk
delay
```

worker marker:

```text
include Sidekiq::Worker
include ApplicationWorker
include Gitlab::SidekiqMiddleware
ApplicationWorker superclass
Sidekiq::Worker superclass
```

worker classでは、local `perform` definition、parameter、`sidekiq_options`、queue、`feature_category`、`urgency`、`idempotent!`を索引化します。

enqueue receiverまたは`push`の`class` valueがstatic constantで、indexed worker classがexactly one存在し、そのworkerの`perform`も一意な場合だけ接続します。

Rubyの相対constantは、構文上の`Module.nesting` stackに沿うlocal候補とtop-level候補を比較します。複数候補があれば未解決とし、先頭`::`付きconstantだけをabsoluteとして扱います。相対`Sidekiq::Client`がlocal classにshadowされる場合も`push`をSidekiq APIと確定しません。

worker metadata macroはbareまたは`self` receiverだけを採用します。`Helper.include ApplicationWorker`や`Worker.include ApplicationWorker`のような明示receiverは、そのconstantが現在class自身だと推測せずworker markerにしません。singleton methodも`def self.method`だけをlocal class methodとして採用します。

`delay`はproxy先のworker `perform`を証明できないため、call siteは記録しても常にunresolvedです。variable receiver、dynamic `push` class、unknown/duplicate workerも接続しません。

## service call resolution

controller actionからenqueueへ直接到達しない場合だけ、call inventoryを最大3 service transitionまで幅優先で探索します。

許可する静的shape:

1. `Service.new(...).execute`のようなstatic constructor receiverからinstance methodへ
2. `Service.execute(...)`のようなstatic constant receiverからsingleton methodへ
3. bare callまたは`self.helper`から同じclassの同種methodへ

constant receiverはclass名が`Service`で終わるか、terminal methodが`call`、`execute`、`run`、`start`、`schedule`の場合に候補になります。

解決条件:

- class name、method name、instance/singleton種別がexact match
- local definitionがexactly one
- cycle guardに未到達
- service transitionが3以下

exact matchがなくbasenameだけ一致する場合も確定しません。複数namespaceまたはRuby lexical scopeに候補があればambiguous、1候補でもnamespace inferenceが必要ならunprovenとして残します。`class A::B`と`module A; class B`で異なるRuby lexical nestingも別に保持します。

次は解決対象外です。

- receiver変数
- factory/DI
- inheritance/mixin dispatch
- `send` / `public_send`
- `super`
- points-to推論

## execution path assembly

routeごとに、controller actionへ一意に接続できたかを確認します。そのaction内にlexically containedなauthorization callがあり、actionまたは最大3 service transition先にenqueueがある場合、hopを構築します。

relation:

```text
route_to_controller
contains_authorization
calls_service
contains_enqueue
enqueue_to_worker_perform
```

全必要hopが解決し、`unresolved_gaps`が空の場合だけ`status`は`resolved`です。それ以外は`partial`です。

`partial`でも既知のroute→controller、authorization、service、enqueue、worker hopは捨てずに記録します。欠けた関係だけを`unresolved_gaps`へ残します。静的に証明できたenqueue pathと同じmethodに無関係な未解決callがあっても、そのcallはwarningとして保持し、証明済みpath自身のgapには混ぜません。

`resolved`でもCFGによるbranch/order proofはありません。authorizationとenqueueのlexical/static reachabilityだけなので、path confidenceの上限は`medium`です。

path IDはroute、authorization、service transition、enqueue、workerのfile/line/column、最小call identityをcanonical JSON化し、SHA-256へ入力して作ります。探索順の連番には依存せず、同一行に複数callがあってもcolumnで区別します。

execution pathは最大5,000 recordです。5,000件を超えるcandidateがあり、
出力を切り捨てた場合だけwarningを出します。

## determinism

- JSON keyはsort
- index recordとwarningはstable composite keyでsort
- path IDはcanonical content hash
- JSONはUTF-8、2-space indent、末尾newline
- writesは同じoutput directory内のtemporary fileからatomic replace

`run_manifest.json`のtimestamp、elapsed time、maximum RSS、実行pathは意図的にrun依存です。それ以外のstable artifactをdeterminism testの対象にします。

## security boundary

target Rubyをload/eval/executeしません。tree-sitterでbytesをparseするだけです。GitHub App、DB driver、SaaS clientを実行経路へ含めず、output以外へartifactを保存しません。

この設計は監査索引を作るものであり、authorization gapやvulnerabilityを自動判定しません。
