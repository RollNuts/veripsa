# Phase 2 Limitations

Phase 2はfile/lineへ戻れる保守的な監査索引です。脆弱性検出器、認可証明器、runtime tracerではありません。

## static linkageの意味

`execution_paths.json`の`resolved`は、対応するroute、controller method、authorization call、service call、enqueue、worker `perform`がstaticかつ一意に結べたことを表します。

次は証明しません。

- 実際にそのbranchが実行される
- authorizationがenqueueより前に実行される
- authorization resultが強制される
- authorization action/resourceが正しい
- worker実行時のactor権限
- exploitabilityまたはsecurity severity

CFGを作らないため、同一method内の異なるbranchにあるauthorizationとenqueueもlexical reachabilityとして現れ得ます。元コードで必ず確認してください。

## Rails routes

対応はstaticな一般DSLのsubsetです。

未対応または限定対応:

- `match`
- `mount`
- Rails engine
- `constraints`
- `concerns`
- external `draw` fileの展開
- `controller` / `defaults` block
- runtime loopやmetaprogrammingで作るroute
- interpolated/dynamic pathまたはtarget
- `shallow`, custom `param`, `path_names`
- custom inflector、acronym、不規則なpluralization
- inheritanceで提供されるcontroller action
- resource block内に入れ子にしたnamespace/scopeのpath合成
- 明示receiver経由のroute mapper

`resources`/`resource`のpath、controller、`only`、`except`はstatic valueに限定されます。それ以外のoptionが存在するdeclarationは、無視してhigh confidenceにせず全体をunresolvedにします。単純なcamelize/singularize/pluralizeでRails behavior全体を再現しません。

`config/routes.rb`以外のroute fileは、`draw`時のnamespace/scope inclusion contextを復元しません。そのfile内のrouteは索引に残りますが、low/unresolvedでありexecution pathへ確定接続しません。

## Authorization

authorization indexはmethod nameと限定的なargument positionを基にします。同名のapplication methodも索引され得ます。

未対応:

- `before_action`やcontroller callbackからのauthorization接続
- Ability ruleやpolicy classの意味解析
- macroで宣言するauthorization
- action/resourceのdata flow
- return valueの強制有無
- alias、delegation、dynamic dispatch
- authorization欠落判定

`policy(resource)`はactionを含まないため、actionは`null`です。dynamic argumentもcall-site自体は残しますが、inferenceは未解決です。

## Sidekiq

worker/enqueue resolutionはstatic constantとunique local definitionに限定されます。

未対応:

- ActiveJob
- `.set(...).perform_async`
- variableまたはfactoryから得たworker class
- `constantize`
- inheritance/mixinによる`perform` dispatch
- string形式のdynamic `push` class
- custom enqueue wrapper
- Sidekiq middlewareのruntime behavior
- retry、deduplication、transaction、queue routingのruntime semantics

`delay`はproxy先を静的に証明できないため、意図的にworker `perform`へ接続しません。

相対constantは構文上のlexical module stackとtop-levelのlocal definitionを比較します。複数候補、選択外definition、相対`Sidekiq::Client`のlocal shadowは未解決です。ただしautoload順、runtime constant replacement、external gem側のreopenまで証明するものではありません。

## service traversal

最大3 transitionで、static constructor/constant receiverまたは同一class callだけを追います。

未対応:

- local variableへ代入したservice object
- dependency injection
- factory
- inheritance
- module mixin
- delegation
- `send` / `public_send`
- `super`
- points-to analysis

exact class/method definitionが0件または複数件なら未解決です。basenameが一意でもnamespace inferenceが必要なら確定しません。

## Ruby parsingとfile selection

- tree-sitter Ruby grammarがなければRuby索引全体を生成できない
- Ruby read/parse failureまたは`ERROR`/missing nodeが1件でもあれば、隠れた
  duplicate definitionを否定できないため、そのrun全体でroute/controller/workerの
  一意解決を停止
- generated/vendored、symlink、oversized、binary fileは既存extractor guardで除外
- include/excludeで必要なdefinitionを外すとpartialになる
- raw receiver/argument expressionは320文字上限

include/excludeはRuby監査索引の出力対象だけに作用します。一意性判定のため、guardを通過した選択外Ruby fileもparseし、そのclass/method definitionを母集団に残します。このためpatternはfull graphだけでなくRuby definition scanの時間・memory・file数も削減しません。

## rich graph diagnostics

既存`build_graph()`が返すparse failure/skipped情報はaggregate countだけです。そのため個別file/reasonを復元できず、`warnings.json`では`file`と`line`が`null`のaggregate warningになります。

direct full graphはrepository全体をmemoryへ保持します。GitLab規模でのruntimeと最大RSSは、固定したlocal checkoutで実測するまで不明です。

## reproducibility

clean targetではmanifest SHAと解析bytesを対応付けられます。

Git ignored untracked fileがrich graphのNodeまたはEdgeへ入る場合はsuccessful runにせずfail closedします。ignored `.gitattributes`も解析対象を変え得るため拒否します。解析対象外のignored fileはgraph/index bytesへ影響しないため許容します。

`assume-unchanged` / `skip-worktree` path、sparse checkout、gitlink/submoduleを含むtargetまたはVeripsa worktreeはfull inputをrecorded SHAだけで固定できないため拒否します。Phase 2はこれらを再現可能に記録するmanifest拡張を行いません。

CLIは索引生成後、artifact write前にtarget/VeripsaのHEADとdirty booleanを再確認します。通常のclean→dirty変化やcommitは検出しますが、これはfilesystem snapshotではありません。`--allow-dirty-target`で既にdirtyなfileが解析中に別のdirty contentへ変わっても、dirty booleanが変わらなければ検出できません。また、再確認後からread/write完了までの極小race windowも残ります。高い再現性が必要なrunはimmutableなclean checkoutを使用してください。

`--allow-dirty-target`を使ったrunでは:

- `target_dirty_state`は`true`
- commit SHAは記録される
- 未commit bytesのpatch/hashは保存されない
- 第三者はSHAだけから同じ解析を再現できない

timestamp、elapsed time、maximum RSS、temporary/output pathはrunごとに変わり得ます。

Git clean判定はworking treeの全byte hashではありません。custom clean/smudge filter、Git LFS、platform依存のline-ending変換のように、Gitがcleanとみなすworking bytesを別環境でSHAだけから復元できない場合があります。高い証拠性が必要なrunでは同じGit config/toolingを固定した通常checkoutを用い、元コードのfile/lineを再確認してください。

## 明示的なPhase 2対象外

- Ruby GraphQL mutation/resolver
- resource ID propagation
- taint analysis
- ActiveRecord model/table convention resolution
- archive/filesystem/object-storage sink解析
- authorization欠落判定
- vulnerability candidate ranking
- severity判定
- PoC
- GitLab cloneまたはGitLab起動
- GitLab.comや本番環境への通信
- HackerOne API
- DB schema、GitHub App、SaaS、UIの変更
- 汎用CFG、SSA、points-to analysis

これらをPhase 2出力から推測して補ってはいけません。
