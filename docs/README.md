# 文書の地図

何を知りたいときにどれを読むか。

## 使う

| 文書 | 中身 |
|---|---|
| [runbook.md](runbook.md) | **開封から、自分の手順で自動操作するまで**の手順書。うまくいかないときの分岐つき |
| [glossary.md](glossary.md) | このプロジェクト固有の言葉(手順・部品・レーン・連結・プリセットなど) |

## 手順を書く

| 文書 | 中身 |
|---|---|
| [specs/flow-format.md](specs/flow-format.md) | 手順の保存形式(**正本**)。`procedures/<名前>.flow.json` と `parts/<名前>.csv` |
| [specs/procedure-format.md](specs/procedure-format.md) | 手順の実行モデル。何が・いつ・どんな順で送られるか |

## 仕組みを知る

| 文書 | 中身 |
|---|---|
| [hardware-design.md](hardware-design.md) | 何を作るのか、なぜこの構成なのか。**第一原理は「入力精度」(R1)** |
| [design/firmware-architecture.md](design/firmware-architecture.md) | マイコン内部の設計。状態機械はここが正本 |
| [design/procon-protocol.md](design/procon-protocol.md) | コントローラーとして名乗るためのプロトコル(実測値つき) |
| [specs/comm-protocol.md](specs/comm-protocol.md) | PC とマイコンの通信。コマンドの一覧 |
| [design/gui-principles.md](design/gui-principles.md) | 画面のあらゆる見た目・文言・配置を、ここから説明できる状態に保つための原則 |
| [design/multi-device-plan.md](design/multi-device-plan.md) | 2台同時運用の設計と決定事項。D1〜D10 はコードが参照する契約 |

## 一次資料(実測)

| 文書 | 中身 |
|---|---|
| [reference/hid_report_descriptor.txt](reference/hid_report_descriptor.txt) | 純正 Pro コントローラーの HID レポートディスクリプタ 203 バイト(注釈つき) |
| [reference/bypass_procon_log.txt](reference/bypass_procon_log.txt) | 純正機と Switch 本体の USB 通信を中継して採ったログ |

---

作者ローカルの作業記録(`docs/notes/`)はリポジトリに含めていません。
