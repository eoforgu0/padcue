"""版番号が3箇所で一致していることを確かめる。

PC 側のパッケージ・装置のファームウェア・模擬デバイスが、それぞれ別の
場所に版を書いている。以前は 0.1.0 / 0.1.0-dev / 0.1.0-mock と、README の
「v0.2.0」まで含めて三重にずれていた。

これは飾りではない。ファームの版は HELLO と探索の応答に載り、画面と CLI が
そのまま表示し、OTA の前後比較にも使われる。ずれていると「更新したのに
版が変わらない」ように見える。

同期の仕組み(git describe から導出するなど)は、年に数回しか動かない規模には
過剰なので入れない。代わりに、ずれたら検査が落ちるようにしておく。
"""
import re

import pytest

from padcue.mockdevice import FW_VERSION as MOCK_FW
from tests.conftest import REPO

PYPROJECT = REPO / "pctool" / "pyproject.toml"
APP_CONFIG = REPO / "firmware" / "main" / "app_config.h"


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


def test_versions_agree():
    """PC 側・ファーム・模擬デバイスの版が同じであること。"""
    pkg, fw = _pyproject_version(), _firmware_version()
    assert fw == pkg, f"ファーム {fw} と PC 側 {pkg} がずれています"
    assert MOCK_FW == f"{pkg}-mock", \
        f"模擬デバイス {MOCK_FW} が PC 側 {pkg} とずれています"


@pytest.mark.parametrize("value", ["pyproject", "firmware"])
def test_version_looks_like_a_release(value):
    """開発中の目印(-dev など)を付けたまま公開しないこと。"""
    v = _pyproject_version() if value == "pyproject" else _firmware_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), \
        f"{value} の版が公開できる形ではありません: {v}"
