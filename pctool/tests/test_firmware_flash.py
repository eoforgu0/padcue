"""新しい装置のダイアログが書き込むファームウェアの用意を見る。

対象は padcue/firmware.py とその経路である。書き込みそのものは画面(Web Serial)が
行うので、ここで見るのは、何を書くかと、書けたかを照合する MD5 である。自動
(リリース)は GitHub へ行かず、取りに行く関数を差し替える。
"""
import base64
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.request

import pytest

from padcue import firmware, gui, proto
from tests.conftest import REPO
from tests.helpers import get, post, tool_module

# ダミーのイメージは pctool/tools/_fakefw.py にある(画面の検査のツールも同じ物を使う)
_fakefw = tool_module("_fakefw")
APP_AT = _fakefw.APP_AT
fake_firmware = _fakefw.fake_firmware


def test_a_merged_image_tells_its_version():
    """1 つにまとめたイメージは、中のパーティションテーブルからアプリを探して、
    そのバージョンを読むこと。"""
    fw = firmware.merged(fake_firmware("9.8.7"), "file")
    assert (fw.source, fw.version) == ("file", "9.8.7")
    assert [(p.offset, len(p.data)) for p in fw.parts] == [
        (0, APP_AT + 80)]


@pytest.mark.parametrize("image, why", [
    (b"", "ケーブルで書き込むファイルではありません"),
    (b"\x00" * 100, "ケーブルで書き込むファイルではありません"),
    # パーティションテーブルがなく、アプリとしても読めない(アプリとして読めれば、
    # 無線の更新に使うファイルと示す。下の test_a_wireless_update_file_…)
    (b"\xe9" + bytes(200), "ファームウェアのファイルにアプリが見つかりません"),
    (b"\xe9" + bytes(firmware.FLASH_SIZE),
     "ファイルが装置のフラッシュより大きいため、書き込めません"),
    # 名前は短く付ける。付けないと中身のバイト列がテストの名前になり、フラッシュより
    # 大きい中身では、pytest が環境変数に置く名前が Windows の上限を超える
], ids=["empty", "zeros", "no-app", "too-large"])
def test_an_image_that_cannot_be_written_says_why(image, why):
    with pytest.raises(firmware.FirmwareError) as e:
        firmware.merged(image, "file")
    assert str(e.value) == why


@pytest.mark.parametrize("image, why", [
    (b"\x00" * 100,
     "「pademu.bin」はケーブルで書き込むファイルではありません。"
     "名前が padcue-firmware- で始まり、-ota が付いていない"
     "ファイルを選択してください。"),
    (b"\xe9" + bytes(200), "「pademu.bin」にアプリが見つかりません"),
    (b"\xe9" + bytes(firmware.FLASH_SIZE),
     "「pademu.bin」は装置のフラッシュより大きいため、書き込めません"),
], ids=["not-cable", "no-app", "too-large"])
def test_a_chosen_file_that_cannot_be_written_is_named(image, why):
    """利用者が選んだファイルを拒否するときは、ファイルを名前で先に示し、ケーブルで
    書き込むファイルでなければ、どのファイルを選べばよいかを続けること。"""
    with pytest.raises(firmware.FirmwareError) as e:
        firmware.merged(image, "file", "pademu.bin")
    assert str(e.value) == why


def test_a_wireless_update_file_says_which_one_to_choose():
    """無線で更新するファイル(アプリだけのイメージ)を選ぶと、その旨を示し、選ぶ
    ファイルを続けること。

    リリースのページでは 2 つのファイルが隣に並ぶので、最も起きやすい間違い
    である。「アプリが見つかりません」では、どちらを選び直せばよいか分からない
    (逆の間違いは、ファームウェアの更新のダイアログが同じ形で示す)。
    """
    ota = fake_firmware("0.1.0")[APP_AT:]
    with pytest.raises(firmware.FirmwareError) as e:
        firmware.merged(ota, "file", "padcue-firmware-0.1.0-ota.bin")
    assert str(e.value) == ("「padcue-firmware-0.1.0-ota.bin」は無線で更新するファイル"
                            "です。名前に -ota が付いていない方を選択してください。")


def test_a_renamed_wireless_update_file_asks_for_the_cable_file():
    """名前に -ota が付いていない無線用のファイルを選ぶと、ケーブルで書き込む
    ファイルを選ぶよう案内すること。

    「名前に -ota が付いていない方」と書くと、選んだファイル自身もそれに当てはまり、
    どれを選び直せばよいかの手がかりにならない。
    """
    ota = fake_firmware("0.1.0")[APP_AT:]
    with pytest.raises(firmware.FirmwareError) as e:
        firmware.merged(ota, "file", "renamed.bin")
    assert str(e.value) == ("「renamed.bin」は無線で更新するファイルです。"
                            "ケーブルで書き込むファイルを選択してください。")


def test_the_md5_is_of_the_padded_data():
    """照合の MD5 は、esptool-js が書く前に 4 バイトの倍数まで 0xFF で埋めた中身の
    ものであること。埋める前の値で照合すると、正しく書けても一致しない。"""
    assert firmware.Part(0, b"abc").md5 == hashlib.md5(b"abc\xff").hexdigest()
    assert firmware.Part(0, b"abcd").md5 == hashlib.md5(b"abcd").hexdigest()


class FakeGitHub:
    """GitHub の API とダウンロードのダミー(urlopen と同じ呼ばれ方をする)。"""

    def __init__(self, assets=None, status=None, down=False):
        self.assets = assets
        self.status = status
        self.down = down
        self.calls: list[str] = []

    def __call__(self, req, timeout):
        url = req.full_url
        self.calls.append(url)
        if self.down:
            raise urllib.error.URLError("unreachable")
        if self.status:
            raise urllib.error.HTTPError(url, self.status, "x", {}, None)
        if url.startswith("https://api.github.com/"):
            listed = [{k: v for k, v in a.items() if k != "_body"}
                      for a in self.assets]
            body = json.dumps({"assets": listed}).encode()
        else:
            body = self.assets_by_url[url]
        return io.BytesIO(body)

    @property
    def assets_by_url(self):
        return {a["browser_download_url"]: a["_body"] for a in self.assets}


def asset(name, body=b""):
    return {"name": name, "browser_download_url": f"https://example.test/{name}",
            "_body": body}


def test_the_release_gives_the_image_written_by_cable():
    """リリースからは、ケーブルで書き込むイメージ(-ota でない方)を取ること。

    タグは v<PC 側のバージョン> で、ファイルの名前にはファームウェアのバージョンが付く。
    """
    gh = FakeGitHub([asset("padcue-firmware-0.1.0-ota.bin", b"\xe9ota"),
                     asset("padcue-firmware-0.1.0.bin", fake_firmware("0.1.0"))])
    fw = firmware.release("0.2.0", gh)
    assert (fw.source, fw.version) == ("release", "0.1.0")
    assert gh.calls[0].endswith("/releases/tags/v0.2.0")
    assert gh.calls[1].endswith("/padcue-firmware-0.1.0.bin")


@pytest.mark.parametrize("gh, why", [
    (FakeGitHub(status=404), "padcue 0.2.0 のリリースがありません"),
    (FakeGitHub(status=503), r"リリースを取得できません\(HTTP 503\)"),
    (FakeGitHub(down=True), "インターネットに接続しているか確認してください"),
    (FakeGitHub([asset("padcue-firmware-0.1.0-ota.bin")]),
     "ケーブルで書き込むファームウェアがありません"),
])
def test_a_release_that_cannot_be_used_says_why(gh, why):
    with pytest.raises(firmware.FirmwareError, match=why):
        firmware.release("0.2.0", gh)


def test_the_release_is_fetched_once_per_version(monkeypatch):
    """ダイアログを開くたびに取得し直さないこと。書き込む中身を 1 つずつ渡すときも、
    取得した同じリリースから渡す。"""
    monkeypatch.setattr(firmware, "_RELEASE_CACHE", {})
    gh = FakeGitHub([asset("padcue-firmware-0.1.0.bin", fake_firmware("0.1.0"))])
    a = firmware.release_cached("0.2.0", gh)
    assert firmware.release_cached("0.2.0", gh) is a
    assert len(gh.calls) == 2


def test_the_dialog_gets_the_release_and_its_parts(server, monkeypatch):
    """自動は、リリースの説明(バージョンと照合の MD5)と、中身を 1 つずつ返すこと。"""
    fw = firmware.merged(fake_firmware("0.1.0"), "release")
    monkeypatch.setattr(firmware, "release_cached", lambda version: fw)
    got = get(server, "/api/firmware")
    assert got == fw.describe()
    assert got["parts"][0]["md5"] == fw.parts[0].md5
    with urllib.request.urlopen(server + "/api/firmware/part?i=0") as r:
        assert r.headers["Content-Type"] == "application/octet-stream"
        assert r.read() == fw.parts[0].data


def test_the_dialog_says_why_there_is_no_release(server, monkeypatch):
    def none(version):
        raise firmware.FirmwareError(f"padcue {version} のリリースがありません")
    monkeypatch.setattr(firmware, "release_cached", none)
    assert get(server, "/api/firmware")["error"].endswith("のリリースがありません")


def test_the_dialog_uses_the_same_values_as_the_device_and_the_script():
    """新しい装置のダイアログ(flash.js)が、Wi-Fi の値の範囲と拒否の文、設定コンソールの
    プロンプトと行の形を、装置・padcue/proto.py・set_wifi_serial.py と同じに持つこと。
    Wi-Fi の値の範囲と拒否の文は、設定のダイアログと共通で devtools.js が持つ。

    パスワードはサーバーを通らないので、ダイアログは値を自分でチェックする。範囲や文がずれると、
    ダイアログが通した値を装置が拒否するか、ダイアログだけ違う文で拒否する。
    """
    # tools はパッケージではないので、探す場所に足してから読み込む
    sys.path.insert(0, str(REPO / "pctool" / "tools"))
    import set_wifi_serial

    def consts(asset):
        js = gui.web_asset(asset)

        def const(name):
            m = re.search(rf"\b{name} = ([^,;\n]+)", js)
            assert m, f"{asset} に {name} がない"
            return m.group(1).strip()
        return const

    wifi = consts("devtools.js")
    limits = ("WIFI_SSID_MAX", "WIFI_PASS_MIN", "WIFI_PASS_MAX")
    assert [int(wifi(n)) for n in limits] == [
        proto.WIFI_SSID_MAX, proto.WIFI_PASS_MIN, proto.WIFI_PASS_MAX]
    assert wifi("WIFI_SSID_BAD") == f"'{proto.WIFI_SSID_BAD}'"
    assert wifi("WIFI_PASS_BAD") == f"'{proto.WIFI_PASS_BAD}'"
    const = consts("flash.js")
    assert const("ND_PROMPT") == f"'{set_wifi_serial.PROMPT}'"
    assert int(const("ND_VID"), 16) == set_wifi_serial.ESPRESSIF_VID
    assert const("ND_SAVED") == f"/{set_wifi_serial.SAVED_RE.pattern}/"
    assert "」に接続できません: " in const("ND_FAILED")
    assert set_wifi_serial.FAILED_RE.pattern.startswith("「.*」に接続できません: ")
    # 設定コンソールの status が出す装置 ID(Wi-Fi だけを設定し直す装置を判別する)
    provision = (REPO / "firmware/main/app_provision.c").read_text(encoding="utf-8")
    assert '"装置 ID: %s ' in provision
    assert const("ND_ID") == "/装置 ID: ([0-9a-f]{12})/"
    # 書くときだけ読み込む esptool-js は、サーバーが配信する場所にある
    path = const("ND_ESPTOOL").strip("'")
    assert path in gui._STATIC
    assert (gui._WEB / gui._STATIC[path][0]).is_file()


def test_a_chosen_file_is_checked(server):
    """手動で選んだファイルは、書き込めるかとバージョンと照合の MD5 を返すこと。
    使えないファイルは理由を返すこと。"""
    image = fake_firmware("9.8.7")
    got = post(server, "/api/firmware/check",
               {"data": base64.b64encode(image).decode()})
    assert got["version"] == "9.8.7"
    assert got["parts"] == [{"offset": 0, "size": len(image),
                             "md5": firmware.Part(0, image).md5}]
    bad = post(server, "/api/firmware/check",
               {"data": base64.b64encode(b"\x00" * 64).decode(), "name": "pademu.bin"})
    assert bad == {"error": "「pademu.bin」はケーブルで書き込むファイルではありません。"
                            "名前が padcue-firmware- で始まり、-ota が付いていない"
                            "ファイルを選択してください。"}
