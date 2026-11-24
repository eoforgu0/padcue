"""ケーブルで書き込むファームウェアを用意する(操作画面の「新しい装置」)。

書き込むものは、利用者が画面のラジオボタンで選ぶ。

- 自動: この padcue と同じバージョンのリリースに添付した、ケーブルで書き込むイメージ
  (GitHub)。サーバーが取ってきて、画面へ渡す
- 手動: 利用者が選んだファイル。画面が読み、サーバーは中身を確認する(形、バージョン、
  照合の MD5)だけで、保存しない

どちらも 0x0 から書く 1 つにまとめたイメージである(release.yml が作る。自分で
ビルドしたものは `idf.py merge-bin` でまとめる)。書き込みそのものは画面(ブラウザーの
Web Serial)が行う。サーバーは装置のポートを使わない。
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .words import sentences

# リリースの情報(GitHub の API)。タグは v<PC 側のバージョン>(release.yml)
RELEASE_API = "https://api.github.com/repos/eoforgu0/padcue/releases/tags/v{version}"
# ケーブルで書き込むイメージの名前(release.yml)。-ota.bin は無線の更新に使う
# アプリだけのイメージで、0x0 から書くものではない。バージョンには - も入りうるので
# -ota.bin もこの形に当たる。取るときに名前の終わりで除く(release)
IMAGE_NAME = re.compile(r"padcue-firmware-([0-9][0-9A-Za-z.+-]*)\.bin")
# 装置のフラッシュの大きさ。これより大きいイメージは書けない
FLASH_SIZE = 8 * 1024 * 1024
# 更新に使うパーティションの大きさ(firmware/partitions.csv)。無線で書けるイメージの上限
OTA_MAX = 3 * 1024 * 1024
# ESP32 のイメージ(ブートローダーとアプリ)の先頭のバイト
IMAGE_MAGIC = 0xE9
# パーティションテーブルの位置と、項目の形(esp_partition_info_t)
PARTITION_TABLE = 0x8000
PARTITION_MAGIC = 0x50AA
# アプリのイメージの頭(24 バイト)と最初のセグメントの頭(8 バイト)の後ろに
# esp_app_desc_t があり、その中のバージョンは magic・secure_version・予約(合わせて
# 16 バイト)の後ろの 32 バイト(tests/test_firmware_image.py と同じ)
_DESC = 24 + 8
_DESC_MAGIC = 0xABCD5432
# リリースを取りに行くときの待ち時間(秒)
_TIMEOUT = 20
_NO_INTERNET = ("リリースを取得できません。インターネットに接続しているか"
                "確認してください。")


class FirmwareError(Exception):
    """書き込むファームウェアを用意できない。文はそのまま画面に出す。"""


@dataclass
class Part:
    """フラッシュの offset から書く 1 つのファイル。"""
    offset: int
    data: bytes

    @property
    def md5(self) -> str:
        """書いた後に装置のフラッシュと照合する値(esptool と同じく MD5)。

        esptool-js は、書く前に長さを 4 バイトの倍数まで 0xFF で埋め、埋めた後の
        中身で照合する。フラッシュの設定は書き換えさせない(画面は keep を渡す)。
        """
        pad = -len(self.data) % 4
        return hashlib.md5(self.data + b"\xff" * pad).hexdigest()


@dataclass
class Firmware:
    source: str                  # "release"(自動)か "file"(手動)
    version: str                 # アプリが持つバージョン(装置が報告するもの)
    parts: list[Part] = field(default_factory=list)

    def describe(self) -> dict:
        """画面に渡す説明(中身は /api/firmware/part で 1 つずつ渡す)。"""
        return {"source": self.source, "version": self.version,
                "parts": [{"offset": p.offset, "size": len(p.data), "md5": p.md5}
                          for p in self.parts]}


def _named(name: str, particle: str, otherwise: str = "") -> str:
    """選んだファイルを文の頭で名前で示す(「「x.bin」は」)。名前を知らない
    (リリースから取ったファイル、コマンドで渡したファイル)ときは otherwise。

    画面はこの文をそのまま出す。どのファイルを拒否したのかを、名前で先に示す。
    """
    return f"「{name}」{particle}" if name else otherwise


def app_version(app: bytes, name: str = "") -> str:
    """アプリのイメージが持つバージョン。読めなければ FirmwareError。

    name は利用者が選んだファイルの名前で、拒否の文の頭で示す。
    """
    if len(app) < _DESC + 48 or app[0] != IMAGE_MAGIC:
        raise FirmwareError(_named(name, "は")
                            + "ファームウェアのファイルではありません")
    (magic,) = struct.unpack_from("<I", app, _DESC)
    if magic != _DESC_MAGIC:
        raise FirmwareError(_named(name, "から")
                            + "ファームウェアのバージョンを読み込めません")
    return app[_DESC + 16:_DESC + 48].split(b"\0")[0].decode("ascii", "replace")


def _app_offset(image: bytes, name: str = "") -> int:
    """1 つにまとめたイメージの中の、最初のアプリのパーティションの位置。"""
    pos = PARTITION_TABLE
    while pos + 32 <= len(image):
        magic, ptype, _sub, offset = struct.unpack_from("<HBBI", image, pos)
        if magic != PARTITION_MAGIC:
            break
        if ptype == 0:          # app
            return offset
        pos += 32
    raise FirmwareError(_named(name, "に", "ファームウェアのファイルに")
                        + "アプリが見つかりません")


def merged(image: bytes, source: str, name: str = "") -> Firmware:
    """0x0 から書く、1 つにまとめたイメージ(リリースの padcue-firmware-*.bin)。

    name は利用者が選んだファイルの名前で、拒否の文の頭で示す。
    """
    if not image or image[0] != IMAGE_MAGIC:
        # 無線の更新に使うファイル(-ota.bin)を選んだときと同じ文型で、選び直す
        # ファイルを示す(ota_version)
        raise FirmwareError(sentences(
            _named(name, "は") + "ケーブルで書き込むファイルではありません",
            "名前が padcue-firmware- で始まり、-ota が付いていないファイルを"
            "選択してください" if name else ""))
    if len(image) > FLASH_SIZE:
        raise FirmwareError(_named(name, "は", "ファイルが")
                            + "装置のフラッシュより大きいため、書き込めません")
    try:
        at = _app_offset(image, name)
    except FirmwareError as missing:
        # パーティションテーブルがなく、アプリとして読めるなら、無線の更新に使う
        # ファイル(-ota.bin)である。リリースでは 2 つのファイルが隣に並び、最も
        # 起きやすい間違いなので、逆の間違い(ota_version)と同じ文型で、選び直す
        # ファイルを示す。名前に -ota が付いていない(名前を変えた)ファイルでは、
        # 名前では選び直す方を示せないので、「ケーブルで書き込むファイル」と書く
        try:
            app_version(image, name)
        except FirmwareError:
            raise missing from None
        again = ("名前に -ota が付いていない方を選択してください" if "-ota" in name
                 else "ケーブルで書き込むファイルを選択してください")
        raise FirmwareError(sentences(
            _named(name, "は") + "無線で更新するファイルです",
            again if name else "")) from None
    return Firmware(source, app_version(image[at:], name), [Part(0, image)])


def _get(url: str, opener) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "padcue"})
    with opener(req, timeout=_TIMEOUT) as r:
        return r.read()


def _release_image(version: str, ota: bool, opener) -> bytes:
    """この padcue と同じバージョンのリリースに添付したイメージを取ってくる。

    ota が真なら無線の更新に使うイメージ(名前が -ota.bin で終わる)、偽なら
    ケーブルで書き込むイメージである。
    """
    what = "無線の更新に使う" if ota else "ケーブルで書き込む"
    try:
        info = json.loads(_get(RELEASE_API.format(version=version), opener))
        asset = next((a for a in info.get("assets") or []
                      if IMAGE_NAME.fullmatch(a.get("name", ""))
                      and a["name"].endswith("-ota.bin") == ota), None)
        if asset is None:
            raise FirmwareError(
                f"padcue {version} のリリースに、{what}ファームウェアがありません")
        return _get(asset["browser_download_url"], opener)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FirmwareError(f"padcue {version} のリリースがありません") from None
        raise FirmwareError(f"リリースを取得できません(HTTP {e.code})") from None
    except OSError:
        raise FirmwareError(_NO_INTERNET) from None
    except (ValueError, KeyError):
        raise FirmwareError("リリースの情報を読み込めません") from None


def release(version: str, opener=urllib.request.urlopen) -> Firmware:
    """この padcue と同じバージョンのリリースに添付した、ケーブルで書き込むイメージ。"""
    return merged(_release_image(version, False, opener), "release")


def ota_version(app: bytes, name: str = "") -> str:
    """無線の更新に使うイメージ(アプリだけ)のバージョン。使えなければ FirmwareError。

    name は利用者が選んだファイルの名前で、拒否の文の頭で示す。
    """
    if len(app) > OTA_MAX:
        raise FirmwareError(_named(name, "は", "ファイルが")
                            + "更新のパーティションより大きいため、更新できません")
    try:
        _app_offset(app)
    except FirmwareError:
        # パーティションテーブルがない: アプリだけのイメージ
        return app_version(app, name)
    raise FirmwareError(sentences(
        _named(name, "は") + "ケーブルで書き込むファイルです",
        "名前が -ota.bin で終わるファイルを選択してください"))


def ota_release(version: str, opener=urllib.request.urlopen) -> bytes:
    """この padcue と同じバージョンのリリースに添付した、無線の更新に使うイメージ。"""
    image = _release_image(version, True, opener)
    ota_version(image)
    return image


# 取得したイメージ(バージョン -> 中身)。同じバージョンでは取得し直さない
_OTA_CACHE: dict[str, bytes] = {}
_RELEASE_CACHE: dict[str, Firmware] = {}


def ota_release_cached(version: str, opener=urllib.request.urlopen) -> bytes:
    """ota_release と同じ。取得したものは覚えておく。"""
    if version not in _OTA_CACHE:
        _OTA_CACHE[version] = ota_release(version, opener)
    return _OTA_CACHE[version]


def release_cached(version: str, opener=urllib.request.urlopen) -> Firmware:
    """release と同じ。取得したものは覚えておく。"""
    if version not in _RELEASE_CACHE:
        _RELEASE_CACHE[version] = release(version, opener)
    return _RELEASE_CACHE[version]
