# padctl — Nintendo Switch 2 自動操作システム

PC で書いた手順を、マイコン(M5Stack AtomS3 Lite)が Switch 2 のコントローラーとして
高い時間精度で実行します。長時間の周回作業の代行と、フレーム単位の精密な挙動検証が目的です。

**現在の状態**: v0.2.0。1台運用に加えて **2台同時運用**(装置ごとのレーン画面、
「連結」でのまとめて開始・自動合流・連動停止、編成の保存、どの Switch に
つながっているかの自動表示)まで実機検証済み。初代 Switch でも動作します。
**セットアップは [docs/runbook.md](docs/runbook.md)**(2台目の追加は §8、連結の使い方は §9)。

> 固有用語は [docs/glossary.md](docs/glossary.md) にまとまっています。

---

## 使い方(3分)

**`padctl.bat` をダブルクリック**すると操作画面が開きます。
実機が無いうちは **`padctl-練習.bat`**(模擬デバイス付き)で全機能を試せます。

手順は `procedures/`、部品は `parts/` に保存されます(バッチと同じ場所)。

コマンドから使う場合:

```bash
set PYTHONPATH=pctool
python -m switchctl gui          # 操作画面
python -m switchctl mock         # 模擬デバイス(別の端末で)
```

操作画面で手順を選び、タイムライン(いつ何のボタンを押すか)を確認して実行します。
手順の作成・編集も同じ画面で行います(ファイルを直接いじる必要はありません)。

**実機がある場合**は `switchctl device auto`(LAN 内を探して覚える)に変えるだけで、
同じ操作が実機に対して行われます。はじめての一台は [docs/runbook.md](docs/runbook.md) を参照。

---

## 手順の書き方

**ふだんは操作画面で作ります。**以下はその保存形式です(git で差分を見たいとき、
まとめて書き換えたいとき、他のツールと行き来したいときに直接編集できます)。

手順は2階層です。**流れ**は JSON、**高密度な同時入力**は CSV(1行=1フレーム)で書きます。

`procedures/素材周回.flow.json` — 流れ

```json
{
  "schema": 1, "name": "素材周回", "pre": "拠点前・メニュー閉",
  "body": [
    {"type": "label", "text": "移動"},
    {"type": "stick", "side": "L", "x": 0, "y": 2047},
    {"type": "wait", "frames": 120},
    {"type": "loop", "count": 3, "body": [
      {"type": "part", "ref": "コンボ"},
      {"type": "wait", "frames": 25}
    ]}
  ]
}
```

`parts/コンボ.csv` — 同時入力(1行=1フレーム)

```csv
F,A,B,ZL,LX,GP
1,1,,,,
2,1,,,,
3,1,1,1,-1200,300
4,,,,,
```

**ルール**

- 数値はすべて**生値**。スティックは中心 0 の -2048〜+2047(左と下が負)。`+1` が送信分解能の最小刻み
- ジャイロ(GP/GY/GR)は毎フレームの回転速度(生値)
- CSV の**空セル**は「離す/0」。操作画面で作った部品は**全ての入力を明示**します(手書きの CSV で列そのものを省いた場合だけ「直前の状態を継続」)
- **くり返しは書いた区間の正確な再生**。押しっぱなしを周回にまたがせたい場合は loop の前で押す
- 危ない書き方(1フレームの押下=まったく現れないことがある、0フレームで消える操作、周回の継ぎ目の衝突)は変換時に警告

詳細は [docs/specs/flow-format.md](docs/specs/flow-format.md)。

---

## 構成

```
docs/           設計文書(下記)
firmware/       マイコン側(ESP-IDF)
  components/padctl_core/   移植可能な純粋C(手順の解釈・実行・プロトコル)
  main/                     ESP32 固有(USB・タイマー・通信・保存・状態機械)
pctool/         PC 側(Python。コンパイラ・通信・CLI・GUI・模擬デバイス)
```

### 読む順(設計を追う場合)

0. [glossary.md](docs/glossary.md) — 固有用語
1. [hardware-design.md](docs/hardware-design.md) — 何を作るのか、なぜこの構成か(第一原理は「入力精度」)
2. [specs/procedure-format.md](docs/specs/procedure-format.md) — 手順データの形式と実行モデル
3. [specs/flow-format.md](docs/specs/flow-format.md) — 手順の書き方(正本形式)
4. [specs/comm-protocol.md](docs/specs/comm-protocol.md) — PC ⇔ マイコンの通信
5. [design/firmware-architecture.md](docs/design/firmware-architecture.md) — マイコン内部の設計
6. [design/procon-protocol.md](docs/design/procon-protocol.md) — コントローラーとして名乗るためのプロトコル(実測値つき)

---

## 開発

```bash
# PC 側のテスト(実機不要。C 実装の検証も含む)
python -m pytest -q

# GUI を実際にブラウザで操作して想定と突き合わせる
cd pctool && python tools/uicheck.py <出力先>

# 手順書(docs/runbook.md)のとおりになぞって、ずれていないか確認する
cd pctool && python tools/runbook_walk.py <出力先>

# 画面の見た目を確認(明暗テーマのスクリーンショット)
cd pctool && python tools/shoot.py <出力先> [--dark]

# マイコン側のビルド(Windows は PowerShell から)
cd firmware && idf.py build

# 実機への無線更新(2回目以降。ケーブル不要)
python -m switchctl ota
```

テストは実機なしで次を検証します:

- 手順のコンパイル結果(タイミング・警告)
- **C 実行エンジンと Python 参照実装の送出列が完全一致すること**
- **模擬 Switch に対してプロトコル応答が仕様どおりであること**
- 転送・実行・停止・ログ回収の一連(模擬デバイス)
- 手順名・部品名の検証(プロジェクトの外に書かない・消さない)
- GUI サーバーと実機の接続の扱い(1本を持ち回す・切れたら繋ぎ直す)
- **通し検証**: 手順を書いてから Switch が受け取るバイト列まで

`tools/uicheck.py` はブラウザを実際に動かし、実行・停止・部分実行・待機分岐・
編集・取り消し・未保存警告・未接続時の抑止などを「こうしたらこうなるはず」で
照合します(失敗した項目はスクリーンショットを残します)。
