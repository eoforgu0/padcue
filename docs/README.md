# 文書の一覧

## 使う

| 文書 | 中身 |
|---|---|
| [getting-started.md](getting-started.md) | 導入。ファームウェアの書き込みから、手順を作って動かすまで |
| [runbook.md](runbook.md) | 日常の使い方(2 台の同時運用、画面を見て自動で選ぶ使い方、通知、コマンド)。うまくいかないときの対処つき |
| [glossary.md](glossary.md) | このプロジェクトで使う用語(手順・フレーム表・レーン・グループ・プリセットなど) |

## 手順を書く

| 文書 | 中身 |
|---|---|
| [specs/file-format.md](specs/file-format.md) | 手順の保存形式。`procedures/<名前>.json` と `frametables/<名前>.csv`、padcue ファイル |
| [specs/observe-and-decide.md](specs/observe-and-decide.md) | 映像を見て自動で選ぶ仕組みと、スクリプトを実行する仕組み(映像ソース・条件・選択条件) |

## 仕組みを知る

| 文書 | 中身 |
|---|---|
| [design/hardware-design.md](design/hardware-design.md) | 装置の方式とハードウェアを選んだ理由と、入力の時刻の精度の分析 |
| [specs/binary-format.md](specs/binary-format.md) | 装置へ送る手順データの形式と、実行のしかた。何が、いつ、どんな順で送られるか |
| [design/firmware-architecture.md](design/firmware-architecture.md) | 装置のファームウェアの設計。タスクの分担と状態の遷移 |
| [design/procon-protocol.md](design/procon-protocol.md) | 有線の Pro コントローラーとして Switch とやり取りする手順(実測の値つき) |
| [specs/comm-protocol.md](specs/comm-protocol.md) | PC と装置の通信。コマンドの一覧 |
| [specs/coupling.md](specs/coupling.md) | 複数の装置の同時運用とグループの仕様。決定に D1〜D12 の番号を振る |
| [design/gui-decisions.md](design/gui-decisions.md) | 操作画面の個別の判断。慣習から外れている選択とその理由、アクセシビリティ、既知の限界 |
| [design/gui-principles.md](design/gui-principles.md) | 操作画面の見た目・文言・配置を決める原則 |

## 設計の根拠

| 文書 | 中身 |
|---|---|
| [purpose.md](purpose.md) | 何のためにあるか。価値の優先順位もここで決める |
| [requirements.md](requirements.md) | 何を満たすか。満たせないことも書く |
| [principles.md](principles.md) | どう作るか。採用しなかった案も理由つきで残す |
| [concepts.md](concepts.md) | 何を何と呼び、どういう関係にあるか |

ほかの文書は、この 4 つから導いています。判断が分かれたときはここに戻ります。

## コードを読む

PC 側は `pctool/padcue/` にあります。右の列は、その部分の仕様を書いた文書です。仕様と実装が一致していなければ、どちらかの誤りです。

| 場所 | 役割 | 対応する文書 |
|---|---|---|
| `procfmt.py` | `procedures/*.json` の読み込みと検証 | [specs/file-format.md](specs/file-format.md) |
| `procmodel.py` | 手順の中身を表すデータの形(ブロック・選択肢・開始位置)。`procfmt.py` が組み立て、`dsl.py` が変換する | [specs/file-format.md](specs/file-format.md) |
| `frametable.py` | `frametables/*.csv`(フレーム表)の読み込みと検証 | [specs/file-format.md](specs/file-format.md) |
| `dsl.py` | 手順を、装置へ送るレコードの列に変換する。警告もここで出す | [specs/binary-format.md](specs/binary-format.md) |
| `lapplan.py` | 「対象の回」があるくり返しを、どの区間に分けて変換するかを決める | [specs/file-format.md](specs/file-format.md) §5-1 |
| `startpos.py` | 途中から始める位置の一覧と、合流点の数え方 | [specs/file-format.md](specs/file-format.md) §6-2 |
| `binfmt.py` | 装置へ送るバイナリ形式の符号化と復号 | [specs/binary-format.md](specs/binary-format.md) |
| `engine.py` | 実行エンジンの参照実装。C の実装と同じ入力の列を出力することをテストで確認する | [specs/binary-format.md](specs/binary-format.md) |
| `proto.py` / `client.py` | 通信のパケットの組み立てと、装置への接続 | [specs/comm-protocol.md](specs/comm-protocol.md) |
| `discover.py` / `autoscan.py` | LAN にいる装置を探し、登録した装置の接続先を追跡する | [specs/comm-protocol.md](specs/comm-protocol.md) |
| `names.py` / `files.py` | 名前の規則と一覧の並び。途中で失敗しても元の中身が残るファイルの書き込み | [specs/file-format.md](specs/file-format.md) |
| `project.py` | プロジェクトフォルダーの読み書き(手順、フレーム表、登録情報)と変換 | [specs/file-format.md](specs/file-format.md) |
| `sent.py` | 装置へ送った手順のコピーと、実行を開始した時点の定義 | [specs/coupling.md](specs/coupling.md) D5、D12 |
| `runlog.py` | 実行の記録の読み書き | [specs/file-format.md](specs/file-format.md) |
| `presets.py` | プリセットの読み書き | [specs/file-format.md](specs/file-format.md) |
| `procrefs.py` | 手順の JSON が参照する手順、フレーム表、条件を読む | [specs/file-format.md](specs/file-format.md) |
| `pack.py` / `unpack.py` | padcue ファイルのエクスポートとインポート | [specs/file-format.md](specs/file-format.md) |
| `registry.py` / `devicepool.py` / `owner.py` | 装置の登録、接続の管理、操作権 | [specs/coupling.md](specs/coupling.md) |
| `coupler.py` / `joinwait.py` | グループの開始と停止、合流点の待ち合わせ | [specs/coupling.md](specs/coupling.md) |
| `lapends.py` / `lapend.py` | 実行中に周回数を変える | [specs/coupling.md](specs/coupling.md) D11 |
| `sources.py` / `frames.py` | 映像ソースから画像を 1 枚取る。画像の符号化と復号 | [specs/observe-and-decide.md](specs/observe-and-decide.md) |
| `conditions.py` | 条件(判定する映像・基準画像・判定する範囲・しきい値)の保存と照合 | [specs/observe-and-decide.md](specs/observe-and-decide.md) |
| `condwatch.py` | 選択待ちの間の条件の判定と、スクリプトの実行 | [specs/observe-and-decide.md](specs/observe-and-decide.md) |
| `record.py` | 手動操作の記録をフレーム表にする | [runbook.md](runbook.md) §1-8 |
| `notify.py` / `toast.py` / `sleepguard.py` | 通知と、実行中のスリープの抑止 | [runbook.md](runbook.md) §2 |
| `timeline.py` | タイムラインの図(装置へ送るバイト列から作るボタンと軸の横棒) | [specs/binary-format.md](specs/binary-format.md) |
| `positions.py` | 実行の位置(止まる先・停止した位置・開始位置)を、装置の状態と送った手順のコピーから求める | [specs/coupling.md](specs/coupling.md) |
| `gui.py` と `web/` | 操作画面(padcue を動かしている PC からだけ開ける Web アプリ) | [design/gui-principles.md](design/gui-principles.md) |
| `guistate.py` / `condedit.py` | 操作画面のサーバーのうち、画面へ渡す状態の組み立てと、条件の編集画面が呼ぶ API | [design/gui-principles.md](design/gui-principles.md) |
| `firmware.py` | 装置へ書き込むファームウェアを、リリースかファイルから用意する | [runbook.md](runbook.md) §3-1 |
| `cli.py` | コマンド(`python -m padcue`) | [runbook.md](runbook.md) の付録と `python -m padcue --help` |
| `statustext.py` | 装置の状態の項目。`python -m padcue status` と操作画面の「状態を表示」が同じものを出す | [design/gui-principles.md](design/gui-principles.md) §1 |
| `words.py` | 画面とコマンドに出す文のつなぎ方と、値の見せ方 | [design/gui-principles.md](design/gui-principles.md) §7 |
| `mockdevice.py` | 模擬装置(テストとデモに使う) | [principles.md](principles.md) P24 |

装置側の `firmware/` は、ESP-IDF に依存せず PC でもビルドできる `components/pademu_core/`(実行エンジンと Pro コントローラーのやり取り)と、ESP-IDF の上で動く `main/` に分かれています。どちらにも、PC でビルドして確認する部分があります([design/firmware-architecture.md](design/firmware-architecture.md) §7)。

## 一次資料(実測)

| 文書 | 中身 |
|---|---|
| [reference/hid_report_descriptor.txt](reference/hid_report_descriptor.txt) | 純正 Pro コントローラーの HID レポートディスクリプター 203 バイト(注釈つき) |
| [reference/bypass_procon_log.txt](reference/bypass_procon_log.txt) | 純正 Pro コントローラーと Switch 本体の USB 通信を、間に入って採取した記録 |
