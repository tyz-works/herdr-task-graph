# Herdr Task Graph

[English](README.md)

Herdrのエージェント状態とタスク依存関係を、同じターミナル上で表示するTUIプラグインです。Python標準ライブラリだけで動作します。

## 主な機能

- タスク依存関係をDAG表示
- `RUN / READY / WAIT / BLOCK / DONE`を色分け
- 複数の`READY`タスクを並列実行候補として表示
- Herdrの状態変更をSocket APIから反映
- 段が幅に入り切らないときは折り返し、グラフが縦に長いときは選択に追従してスクロール
- Enterで対応するエージェントペインへ移動
- `tasks.json`の変更を検知して自動で再読込（`r`で手動再読込、`q`で終了）
- Herdr未起動時のデモ表示

`session.snapshot`と`events.subscribe`は別々のSocket接続で行います。Herdr 0.9.0は購読以外のリクエストに1回応答すると接続を閉じるためです。購読をつなぎ直すたびにsnapshotも取り直すので、後から起動したエージェントも反映されます。

起動時に`session.snapshot`のHerdrバージョンとSocket APIプロトコルを検査します。Herdr 0.8系のprotocol 19から0.9.0／0.9.1のprotocol 22までを確認済み範囲として扱います。protocol 19未満は接続を停止し、22より新しい未知のプロトコルは警告を表示したうえで読み取りを継続します。

## インストール

GitHubからインストールする場合：

```bash
herdr plugin install tyz-works/herdr-task-graph
```

ローカル開発の場合：

```bash
git clone https://github.com/tyz-works/herdr-task-graph.git
cd herdr-task-graph
herdr plugin link .
```

Herdr起動後、ダッシュボードを開きます。

```bash
herdr plugin action invoke open-task-graph \
  --plugin io.github.tyz-works.task-graph
```

## デモ

```bash
python3 task_graph.py --demo
```

静的な1画面だけなら次を実行します。

```bash
python3 task_graph.py --demo --once
```

## タスク設定

同梱の`tasks.json`を設定ディレクトリへコピーします。

```bash
config_dir="$(herdr plugin config-dir io.github.tyz-works.task-graph)"
cp tasks.json "$config_dir/tasks.json"
```

タスクとHerdrペインの対応付けには、次のどちらかを指定できます。

- `pane_id`: Herdrの完全なペインID
- `pane_match`: ペインID、エージェント名、ペインタイトルに対する部分一致

表示名として`label`（文字列、任意）を指定すると、箱には`id`の代わりに`label`が表示されます。`20260925-my-mission:t004`のような長い自動生成idに向いています。`label`は表示専用で、`depends_on`・一意性の検査など識別はすべて`id`のままです。`label`がなければ`id`を表示し、文字列以外の`label`は読み込み時にエラーになります。

箱は3行で、`[STATE] label`、タイトルだけの行、待機先または担当エージェントの行です。箱の幅はペインに合わせて28〜48桁まで広がり、タイトルを読めるようにします。任意の`group`（文字列）を指定すると、1行目の右端にその末尾が`· …task-graph`のように表示され、複数のmissionで`label`が重なるタスクを見分けられます。pluginにとって意味のない汎用の欄で、`label`と同じく表示専用です。長い`group`でも`label`は押し出されません。文字列以外の`group`は読み込み時にエラーになります。

固定状態には`status`を指定できます。値は`done`、`running`、`blocked`、`ready`、`waiting`、`failed`です。省略するとHerdr状態と依存関係から自動判定します。

別の設定ファイルは`HERDR_TASKS_FILE`環境変数または`--config`で指定できます。指定されているものが優先され（`--config`、`HERDR_TASKS_FILE`、設定ディレクトリの`tasks.json`の順）、どれも指定されていないときだけ同梱のサンプルを表示します。

設定ファイルは監視されており、保存・`os.replace`での差し替え・設定ディレクトリのsymlinkの指す先の差し替えを、1秒以内に反映します。ファイルが存在しない・読めない・内容が不正なときは、最後に読めたタスク（起動時は空のグラフ）を保ったままエラーを表示し、直るまで表示し続けます。指定された設定が読めないときに同梱サンプルへ切り替わることはありません。

## 大きなグラフ

タスクは依存の段ごとに描かれ、依存のないタスクはすべて最初の段に並びます。そのため大きな計画では1つの段に何十個も箱が並びます。ペインの幅に入り切らない段は、同じ列の格子で**複数の行に折り返し**、`-- level 1 · 23 tasks · 12 rows ---`のような区切り線を段の上に引きます。幅がいくつでも箱は重なりません。

ペインの高さを超えるグラフは**縦にスクロール**します。`j` / `k`はすべてのタスクを順に選択し、表示は選択に追従します。グラフの上下の`↑ N more`・`↓ N more`は、画面に収まっていないタスクの数です。接続線も箱と一緒にスクロールします。

折り返した段の内側では、読み違えない接続線だけを描きます（段の最下行の箱から次の段の最上行の箱へ）。それ以外は描きません。間に積まれた箱を突き抜ける線は、それらの箱への依存に見えてしまうためです。待機中の箱は`waiting: ...`の行で待っている相手を確認してください。

## 操作

- `j` / `k` または上下矢印：タスク選択（必要に応じて表示をスクロール）
- `Enter`：対応するエージェントペインへ移動
- `r`：`tasks.json`を今すぐ再読込（変更は自動でも反映されます）
- `q` / `Esc`：終了

## テスト

```bash
python3 -m py_compile task_graph.py
python3 -m unittest discover -s tests -v
```

## ライセンス

Apache-2.0
