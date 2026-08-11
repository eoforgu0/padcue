# PC⇔マイコン通信プロトコル v0(ドラフト)

作成: 2026-07-29 ／ 状態: 実装開始用ドラフト。実装中の変更は本文書に追記して同期する。

## 設計原則

- 遅延不問・利用者1名・LAN 内限定(設計文書の許容条件)。HTTP/MQTT 等は使わず最小の自作パケット形式
- 実行中のタイミング経路に PC を関与させない(D2)。実行中に許されるのは RAM フラグ操作(停止指示)と状態読み出しのみ
- 実行中のフラッシュ書き込み禁止(7.4)。転送データは RAM 受け、COMMIT は停止中のみ

## トランスポート

| 経路 | 用途 |
|---|---|
| TCP :5555(マイコン=サーバ) | コマンド/応答。パケットの形: `len u16 | type u8 | payload | crc32`(**この「パケット」はゲームの1描画周期を指す「フレーム」とは別物**。2026-08-12 に語を分けた) |
| UDP :5556(マイコン→PC) | ログストリーム。実行中は詳細ログを RAM リングバッファに蓄積し停止後に回収(A-4) |

デバイスの IP は DHCP 固定割当(ルーター側設定)を前提とし、PC ツールは設定ファイルに保持する。

**ペイロード形式**: `json_len u16 | JSON(UTF-8) | blob`。制御情報は JSON(タイミング経路外のため拡張性・可読性を優先。MCU 側は cJSON)、手順データ等の大きなバイト列は blob に載せる。応答の type は「要求 type | 0x80」、エラー応答は 0xFF(code, message)。参照実装: `pctool/switchctl/proto.py`

## コマンド一覧

| コマンド | 実行中可否 | 応答 |
|---|---|---|
| HELLO | 可 | **id**(個体識別子 = WiFi MAC 12桁hex。2026-08-04 追加。PC は接続のたびに登録簿と照合し、IP の入れ替わりによる取り違えを防ぐ)/ FW 版 / 手順スキーマ版 / ビルドバリアント(転送層モード, bInterval)/ 稼働パーティション(A/B)/ 前回リセット理由 / **imu_enabled**(本体がサブコマンド 0x40 で IMU を有効化したか。ジャイロ不動作の切り分け用) |
| PUT name,data | **不可** | ACK(受信データの crc32)→ PC 側で照合 |
| COMMIT name | **不可** | RAM→フラッシュ確定 |
| LIST | 可 | 保存済み手順(name, hash, size, 受信日時) |
| RUN name, expected_hash, loop_n, resume={segment,index,base}(省略時は先頭) | 不可(既実行中) | ハッシュ不一致・再開点不正・実行中は拒否+理由。セグメント表(AWAIT の腕対応・SEGEND 連鎖)は転送時メタとして登録済みであること |
| SELECT arm[, gen] | AWAITING のみ | 待機分岐の腕選択。gen(任意。2026-08-04 追加)= STATUS の await_gen(この実行で何回目の駐機か)。渡すと装置が照合し、別の駐機に宛てた古い選択を STALE_SELECT で拒否する(2台の自動合流用。省略時は従来どおり無条件)。遷移は firmware-architecture.md §6 の表に従う |
| STOP mode=immediate\|graceful\|cancel | 可 | immediate: 即時全ニュートラル+破棄。**冪等**: エンジン停止済みでも状態機械が RUNNING/AWAITING に残っていれば IDLE へ戻す(固着からの復帰口)。graceful: セッション境界で終了処理へ(AWAITING 中は選択待ちを維持)。cancel: graceful の**予約だけを取り消す**(2026-08-04 追加。既に止まっていたら何も起きない=取り消しが間に合わなかった扱い。予約中かは STATUS の stop_graceful で分かる) |
| CLEAR_ERROR | ERROR のみ | ラッチ解除→IDLE(ログ回収前の自動解除はしない) |
| STATUS | 可 | 状態機械 / 周回カウンタ / 現在イベント index / 開始からの経過フレーム / **今回の総フレーム(total_frames)と指定周回数(loop_n)** ※進捗表示に必須 / await_gen(何回目の駐機か。SELECT の gen に渡す)/ imu_enabled(HELLO と同じ) |
| LOGS | 可(送信はコア0) | RAM 保持分の回収。各エントリは `t_ms, kind, a, b, c`(2026-08-04 に c を追加)。RUN_START: a=指定周回数(0=無限)、b/c=手順ハッシュ上位/下位32bit(PC が LIST のハッシュと突き合わせて名前に戻す)。RUN_DONE/RUN_ABORT: a=経過フレーム、b=遅れ回数、c=周回(上位16bit=完了周、下位16bit=指定周、65535で飽和)。ENGINE_FAULT: a=イベント index、b/c は同上。AWAIT_TIMEOUT(2026-08-06): a=待ったフレーム、b=on_timeout。HOST_INFO(同): a/b=ペアリング(サブコマンド 0x01)引数の先頭8バイト(本体識別子の調査用) |
| MODE procon\|hidpad | **不可** | 転送層モード切替(NVS 保存、要 USB 再列挙) |
| CONFIG key,value | **不可** | frame_period_us 等。NVS 保存 |
| OTA size + データ | **不可** | 検証→再起動。次回 HELLO でロールバック有無を報告 |

## デバイス状態機械(LED 表示と対応)

**正本は firmware-architecture.md §6**(AWAITING の遷移表・ERROR 解除を含む)。概要: BOOT → WIFI_CONNECTING → IDLE ⇄ RUNNING(⇄ AWAITING)/ ERROR(ラッチ、CLEAR_ERROR で解除)/ OTA

- プレイヤー番号(Switch からのサブコマンド 0x30)受信時は LED に反映(1P/2P 以降の判別)
- USB 切断・サスペンドを RUNNING 中に検出した場合: 中断+全ニュートラル+位置(周回・index)記録 → ERROR

## 未決事項(実装中に確定し本文書へ反映)

- OTA の分割転送サイズとレジューム要否
- パススルーモード導入時の入力ストリーム形式(バックログ機能)
