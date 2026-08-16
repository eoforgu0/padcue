"""版番号が5箇所で一致していることを確かめる。

版は5つの場所に書いてある。実装が3つ(PC 側のパッケージ・装置のファーム
ウェア・模擬デバイス)、公開物が2つ(CHANGELOG の最新見出し・SECURITY が
名指ししている「直す版」)。機械が見ていないと、0.1.0 / 0.1.0-dev /
0.1.0-mock のように書き換え漏れで容易にずれる。

これは飾りではない。ファームの版は HELLO と探索の応答に載り、画面と CLI が
そのまま表示し、OTA の前後比較にも使われる。ずれていると「更新したのに
版が変わらない」ように見える。公開物の2つは、読む人が「自分の持っている
版は直してもらえるのか」を判断する根拠になる。

同期の仕組み(git describe から導出するなど)は、年に数回しか動かない規模には
過剰なので入れない。代わりに、ずれたら検査が落ちるようにしておく。
"""
import re

import pytest

from padcue.mockdevice import FW_VERSION as MOCK_FW
from tests.conftest import REPO

PYPROJECT = REPO / "pctool" / "pyproject.toml"
APP_CONFIG = REPO / "firmware" / "main" / "app_config.h"
CHANGELOG = REPO / "CHANGELOG.md"
SECURITY = REPO / "SECURITY.md"


def _pyproject_version() -> str:
    m = re.search(r'^version = "([^"]+)"',
                  PYPROJECT.read_text(encoding="utf-8"), re.M)
    assert m, "pyproject.toml に version が無い"
    return m.group(1)


def _firmware_version() -> str:
    m = re.search(r'#define PADEMU_FW_VERSION "([^"]+)"',
                  APP_CONFIG.read_text(encoding="utf-8"))
    assert m, "app_config.h に PADEMU_FW_VERSION が無い"
    return m.group(1)


def _changelog_version() -> str:
    """CHANGELOG の一番上の版見出し(`## vX.Y.Z — 日付`)。"""
    m = re.search(r'^## v(\d+\.\d+\.\d+)', CHANGELOG.read_text(encoding="utf-8"),
                  re.M)
    assert m, "CHANGELOG.md に `## vX.Y.Z` の見出しが無い"
    return m.group(1)


def _security_version() -> str:
    """SECURITY が「直す」と宣言している版(`最新のタグ(現在 `v0.3.0`)`)。"""
    m = re.search(r'最新のタグ\(現在 `v(\d+\.\d+\.\d+)`\)',
                  SECURITY.read_text(encoding="utf-8"))
    assert m, "SECURITY.md が対応する版を名指ししていない"
    return m.group(1)


def test_versions_agree():
    """PC 側・ファーム・模擬デバイスの版が同じであること。"""
    pkg, fw = _pyproject_version(), _firmware_version()
    assert fw == pkg, f"ファーム {fw} と PC 側 {pkg} がずれています"
    assert MOCK_FW == f"{pkg}-mock", \
        f"模擬デバイス {MOCK_FW} が PC 側 {pkg} とずれています"


def test_published_documents_name_the_current_version():
    """CHANGELOG の最新見出しと SECURITY の対応版が、実装と揃っていること。

    版を上げたときに書き換え忘れても、実装だけは動いてしまう。読む人には
    「この版は直してもらえる対象か」が分からなくなるだけなので、気づく
    きっかけがどこにも無い。
    """
    pkg = _pyproject_version()
    assert _changelog_version() == pkg, \
        f"CHANGELOG の最新版 v{_changelog_version()} が実装 {pkg} とずれています"
    assert _security_version() == pkg, \
        f"SECURITY の対応版 v{_security_version()} が実装 {pkg} とずれています"


@pytest.mark.parametrize("value", ["pyproject", "firmware"])
def test_version_looks_like_a_release(value):
    """開発中の目印(-dev など)を付けたまま公開しないこと。"""
    v = _pyproject_version() if value == "pyproject" else _firmware_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), \
        f"{value} の版が公開できる形ではありません: {v}"
