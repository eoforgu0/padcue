# コントリビューション

個人用のツールです。issue や pull request は送れますが、返信・レビュー・取り込みは都合次第です。送った変更は、このリポジトリと同じ Apache License 2.0 で提供されます。

## 検証

```powershell
python -m pip install -e "./pctool[dev]"
python -m playwright install chromium

python -m pytest -q                              # テスト
python -m ruff check pctool                      # lint
python pctool/tools/uicheck.py uicheck-out/ui    # 画面を実際に動かすテスト
python pctool/tools/runbook_walk.py uicheck-out/walk   # getting-started.md と runbook.md の、実機なしで試せる節のとおりに操作するテスト
```

- どれも実機なしで動きます。全体は CI が実行するので、開発中は変更に関係するものだけで構いません。`uicheck.py` の後ろに区画の名前を並べると、その区画だけ実行します(例: `python pctool/tools/uicheck.py uicheck-out/ui home`)。区画の名前は、引数なしで実行すると出ます。
- テストは追跡しているファイルを見るので、新しいファイルは `git add` してからテストしてください。
- 利用者に見える変更は、CHANGELOG の先頭の「## 未リリース」の節に 1 行で書いてください(節がなければ作ります)。
- 画面の要素のクラス名や表示の文を変えたときは、`python pctool/tools/shoot.py uicheck-out/shots` も実行してください。文書に掲載するスクリーンショットを撮るツールで、要素をその名前と文で探します。README の画面が変わったときは、出力の `run.png` と `edit.png` を `docs/images/` へコピーします。

次のものがないと、一部のテストが skip されます。CI はすべて入れて実行します。

| ないもの | skip されるテスト | 補足 |
|---|---|---|
| git | 追跡しているファイルを見るテスト(リンク先や節の引用など) | ZIP で取得したときです |
| gcc | ファームウェアの C のテストと、C の実行エンジンと Python の参照実装の比較 | Windows では WinLibs(MinGW-w64)を入れます。`$env:PADCUE_REQUIRE_GCC = "1"` で、skip を失敗にできます |
| numpy と OpenCV | 映像の判定の 2 つの経路の比較 | 映像の判定を変更したら、`python -m pip install numpy opencv-python-headless` で入れてテストします。`opencv-python` とは同時に入れません |
| ファームウェアのビルドの成果物 | IRAM の配置とイメージのバージョンの確認 | `$env:PADCUE_REQUIRE_FIRMWARE = "1"` で、skip を失敗にできます |

## ファームウェア

ESP-IDF は `v6.1` を使ってください。別のバージョンでビルドすると、追跡している `firmware/dependencies.lock` が書き換わり、CI と違うファームウェアになります。入れ方は [ESP-IDF の案内](https://docs.espressif.com/projects/esp-idf/en/v6.1/esp32s3/get-started/)のとおりです。Windows では、インストーラー EIM(`winget install Espressif.EIM`)で v6.1 を入れ、EIM の「Open IDF Terminal」で開いたターミナルで実行します。

```powershell
cd firmware
idf.py build
idf.py -p COM5 flash   # ケーブルで書き込む(最初の 1 回)
```

このターミナルの Python は ESP-IDF 専用で、padcue が入っていません。2 回目からの無線の更新は、通常の PowerShell で実行します。

```powershell
python -m padcue ota   # 既定は firmware/build/pademu.bin
```

- 操作画面の「新しい装置」で自分のビルドを書き込むときは、`idf.py merge-bin` で作った `firmware/build/merged-binary.bin` を「ファイルを指定する」で選びます。
- 入力を送信するタイマー割り込み(`app_engine.c`)から呼ぶ関数には `IRAM_ATTR` を付けます。フラッシュに残った関数があると、手順の保存、OTA、設定の保存と重なったときに、装置が異常終了します。`test_firmware_iram.py` は付けた関数の配置を確認しますが、付け忘れは見つけません。
- `pctool/padcue/web/vendor/` の esptool-js は、npm の配布物を変えずに置いています。更新するときは、npm の `dist.integrity` と照合してから置き換え、ファイル名のバージョン、`gui.py` が配信するファイルの一覧、`flash.js` の読み込む場所、NOTICE とライセンスの文を合わせます。

## テストが実機を動かさないために

`pctool/tools/` のうち、装置や操作画面につなぐテストのツールは、必ず `lock_to_mock()` を通してください(`pctool/tools/_localonly.py`)。探索の候補を模擬装置に固定し、loopback 以外への接続、送信、名前の解決を拒否します。PC のウィンドウ、モニター、映像機器の一覧も、テスト用の一覧に置き換えます。pytest にも同じものを適用しています(`pctool/tests/conftest.py`)。

これがないと、同じ LAN に実機があるとき、テストが実機を選び、つながっている Switch を実際に操作します。

## ツール

`pctool/tools/` のツールです。`_` で始まるファイルは、ツールどうしで使う共通の処理です。

| ツール | 何をするか | 実機 |
|---|---|---|
| `uicheck.py` | 操作画面を実際に動かして確認する。確認の中身は `uicheck_regions/` にある | 使わない |
| `runbook_walk.py` | getting-started.md と runbook.md の、実機なしで試せる節のとおりに操作し、書いてあるとおりに動くかを確認する | 使わない |
| `shoot.py` | README と文書に掲載する画面のスクリーンショットを撮る | 使わない |
| `accept.py` | プロジェクトの手順を、模擬装置 2 台のグループで最後まで実行する | 使わない |
| `accept_device.py` | 実機に、ボタンを押さない手順を送って、受け入れの項目を確認する | 使う |
| `set_wifi_serial.py` | 装置がつなぐ Wi-Fi を、USB のケーブルから装置の保存領域に書く | 使う |

```powershell
python pctool/tools/shoot.py uicheck-out/shots          # スクリーンショットを撮る。--dark でダークの配色
python pctool/tools/accept_device.py uicheck-out/acc 192.0.2.20   # 実機の受け入れ。アドレスは使う装置のものにする
```

`accept_device.py` は、ボタンを 1 つも押さない手順を実機に送るので、Switch から見ると何も操作していないコントローラーのままです。`--two=<2 台目のアドレス>` を付けると、2 台のグループの開始、連動停止、合流点の待ち合わせ、周回の終え方を確認します。

## コマンドの出力

`padcue` のコマンドは、結果を標準出力へ、失敗と警告と進み具合を標準エラーへ出します。終了コードは、成功が 0、手順や引数の誤りなどの失敗が 1、装置が拒否したとき(登録と一致しないときを含む)が 2、装置につながらないときが 3 です。操作画面のサーバーを経由しても同じ終了コードを返します(失敗の種類は、操作画面の API の応答の `failure` で受け取ります)。複数の装置へ送るコマンドは、最初に失敗した装置の失敗の種類で決めます。

`run -w` は、実行が異常停止で終わったときも 1 を返します。`run -w` の途中で Ctrl+C を押すと、装置を停止してから 0 を返します(停止できなければ、その失敗の終了コードです)。ほかのコマンドを Ctrl+C で中断したときは、慣例どおり 130 を返します。

## バージョンと公開

- PC 側のバージョン(`pctool/pyproject.toml` の `version`)は配布全体の番号を兼ね、公開するときに上げます。ファームウェアのバージョン(`firmware/main/app_config.h` の `PADEMU_FW_VERSION`)は、ファームウェアを変えたときだけ上げます。ファームウェアのイメージに含まれるバージョンは、ビルドのときに `PADEMU_FW_VERSION` から付けます。
- PC 側は、使えるファームウェアの範囲を持ちます(`pctool/padcue/client.py` の `FIRMWARE_MIN` 以上 `FIRMWARE_BELOW` 未満)。範囲の外の装置では開始せず、更新を求めます。1.0 より前は、2 つ目の数が上がると互換がなくなります。通信や手順データの読み方を変えたら、ファームウェアのバージョンを上げ、PC 側の範囲を合わせます。
- Python の下限は `pctool/pyproject.toml` の `requires-python` です。同じ下限を `pctool/padcue/__init__.py`、`padcue.bat`、`padcue-demo.bat`、README、getting-started.md、`ci.yml` にも書いていて、一致していることを `pctool/tests/test_version.py` が確認します。
- 装置の情報の読み出し(HELLO・STATUS)と、無線での更新に使う通信(操作権の取得と OTA)の形を変えると、範囲の外の装置を無線で更新できなくなります。
- 形式のバージョン(手順、条件、padcue ファイル、プリセット、`padcue.json` の `format` と、手順データの `schema`)は、同じ中身を別の意味に読む変更と、新しい形を書き始める変更で 1 つ上げます。古い形式は読みません(P25)。
- main には、リリースできる状態だけを入れます。途中の作業は main 以外のブランチで行い、PC 側とファームウェアが合う状態にしてから main にマージします。マージしたら、同じタイミングでリリースを出します。
- 公開するときは、CHANGELOG の「未リリース」をバージョンと日付の見出しにし、テストをすべて通してから、PC 側のバージョンで `v<バージョン>` のタグを打ちます。タグをプッシュすると、CI がファームウェアをビルドし、CHANGELOG のその節を説明にしてリリースを作ります(`release.yml`)。
