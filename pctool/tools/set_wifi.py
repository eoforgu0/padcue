"""WiFi の接続情報を入れる(この端末の中だけで完結する)。

なぜ要るか: WiFi の SSID/パスワードは ESP-IDF のビルド設定 `sdkconfig` の中に
あるが、このファイルは作り直されると中身が消える。消えると
マイコンがネットワークから見えなくなる。

そこで、消えない場所(`firmware/sdkconfig.defaults.local`)に置く。
このファイルは git に入らず(.gitignore 済み)、sdkconfig を作り直しても
ビルドのたびに自動で読み込まれる。

使い方(リポジトリ直下から):
    python pctool/tools/set_wifi.py
    (画面の指示どおり SSID とパスワードを入れる。パスワードは表示されない)

入力した値はこの PC の中のファイルに書かれるだけで、どこにも送信されない。
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

FW = Path(__file__).resolve().parents[2] / "firmware"
LOCAL = FW / "sdkconfig.defaults.local"


def main() -> int:
    print("マイコンがつなぐ WiFi の情報を入れます。")
    print("(2.4GHz の WiFi にしてください。5GHz にはつながりません)")
    print()
    ssid = input("WiFi の名前(SSID): ").strip()
    if not ssid:
        print("SSID が空です。中止しました。")
        return 1
    pw = getpass.getpass("パスワード(入力は表示されません): ")
    if not pw:
        print("パスワードが空です。中止しました。")
        return 1
    pw2 = getpass.getpass("もう一度パスワード: ")
    if pw != pw2:
        print("2回のパスワードが一致しません。中止しました。")
        return 1
    for name, v in (("SSID", ssid), ("パスワード", pw)):
        if '"' in v or "\\" in v:
            print(f'{name} に " または \\ が含まれています。この形式では扱えません。')
            return 1

    LOCAL.write_text(
        "# WiFi の接続情報(この PC の中だけ。git には入らない)\n"
        "# sdkconfig を作り直しても、ここから毎回読み込まれる\n"
        f'CONFIG_PADEMU_WIFI_SSID="{ssid}"\n'
        f'CONFIG_PADEMU_WIFI_PASS="{pw}"\n',
        encoding="utf-8")
    print()
    print(f"保存しました: {LOCAL}")
    print(f"  SSID: {ssid}  /  パスワード: {len(pw)} 文字")
    print()
    print("次にビルドし直すと反映されます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
