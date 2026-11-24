"""バージョンの定義元と、それを転記する場所が合っていることを確認する。

PC 側のバージョンの定義元は pctool/pyproject.toml の `version` 1 つ(P26)。
padcue は起動時にそこを読むので(`padcue.__version__`)、書き換える場所はそこだけで
済む。
CHANGELOG の最新の見出しは人が書き換えるので、PC 側のバージョンとの一致を求める。
最新の見出しは、番号を付ける前の「未リリース」でもよい(番号と日付は公開する
ときに決める)。

ファームウェアのバージョン(firmware/main/app_config.h)は PC 側と別に、変えた
ときだけ上げる。組み合わせは、PC 側が持つ使える範囲(client.FIRMWARE_MIN)で
決まるので、このツリーのファームウェアがその範囲に入ることを求める。
"""
import re

import pytest

import padcue
from tests.conftest import REPO

PYPROJECT = REPO / "pctool" / "pyproject.toml"
APP_CONFIG = REPO / "firmware" / "main" / "app_config.h"
CHANGELOG = REPO / "CHANGELOG.md"


def _source_version() -> str:
    """定義元(pyproject.toml の `[project] version`)の値。"""
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  PYPROJECT.read_text(encoding="utf-8"), re.M)
    assert m, "pyproject.toml に version がない"
    return m.group(1)


PKG_VERSION = _source_version()


def _python_floor() -> str:
    """対応する Python の下限(pyproject.toml の requires-python)。"""
    m = re.search(r'^requires-python\s*=\s*">=(\d+\.\d+)"',
                  PYPROJECT.read_text(encoding="utf-8"), re.M)
    assert m, "pyproject.toml の requires-python が「>=X.Y」の形でない"
    return m.group(1)


PY_FLOOR = _python_floor()


def _firmware_version() -> str:
    m = re.search(r'#define PADEMU_FW_VERSION "([^"]+)"',
                  APP_CONFIG.read_text(encoding="utf-8"))
    assert m, "app_config.h に PADEMU_FW_VERSION がない"
    return m.group(1)


def _changelog_version() -> str | None:
    """CHANGELOG の一番上の見出しが指すバージョン。「## 未リリース」なら None。

    ほかの見出しは `## vX.Y.Z (日付)` の形で書く。
    """
    m = re.search(r'^## (.+)$', CHANGELOG.read_text(encoding="utf-8"), re.M)
    assert m, "CHANGELOG.md に `## ` の見出しがない"
    top = m.group(1).strip()
    if top == "未リリース":
        return None
    v = re.match(r'v(\d+\.\d+\.\d+)(?: |$)', top)
    assert v, ("CHANGELOG.md の一番上の見出しが「未リリース」でも `vX.Y.Z` でも"
               f"ない: {top}")
    return v.group(1)


def test_the_package_reads_the_version_from_the_source_of_truth():
    """実行している padcue のバージョンが、定義元に書いてある値そのものであること。

    読む経路が壊れる(pyproject を見つけられず、入っていない配布物の情報を
    読んでしまう)と、画面と CLI が古いバージョンを報告したまま動き続ける。
    """
    assert padcue.__version__ == PKG_VERSION, \
        (f"padcue が示す {padcue.__version__} が pyproject.toml の "
         f"{PKG_VERSION} とずれています")


def test_an_old_python_is_told_the_version_it_needs(monkeypatch):
    """下限より前の Python で開くと、必要なバージョンを表示して終了すること。

    確認しないと、新しい書き方を読み込んだ所で、意味の分からないエラーで終了する。
    確認する所(padcue/__init__.py)は、3.9 でも読める書き方であること。
    """
    import ast
    import importlib
    import sys
    major, minor = map(int, PY_FLOOR.split("."))
    src = (REPO / "pctool" / "padcue" / "__init__.py").read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 9))
    monkeypatch.setattr(sys, "version_info", (major, minor - 1, 0))
    with pytest.raises(SystemExit) as e:
        importlib.reload(padcue)
    assert f"Python {PY_FLOOR} 以上が必要です" in str(e.value)


@pytest.mark.parametrize("rel, pattern", [
    ("pctool/padcue/__init__.py", r"Python (\d+\.\d+) 以上が必要です"),
    ("padcue.bat", r"Install Python (\d+\.\d+) or later"),
    ("padcue-demo.bat", r"Install Python (\d+\.\d+) or later"),
    ("README.md", r"Python (\d+\.\d+) 以上"),
    ("docs/getting-started.md", r"Python (\d+\.\d+) 以上"),
    # pytest の 1 つ目のバージョンが下限、2 つ目が最新
    (".github/workflows/ci.yml", r'python-version: \["(\d+\.\d+)"'),
])
def test_the_python_floor_is_the_same_everywhere(rel, pattern):
    """対応する Python の下限を書いた場所が、どれも requires-python と同じであること。

    一部だけ上げ忘れると、起動時の確認が通す Python と、
    案内や CI が示す Python が一致しない。
    """
    got = re.findall(pattern, (REPO / rel).read_text(encoding="utf-8"))
    assert got, f"{rel} に下限が書かれていない"
    assert set(got) == {PY_FLOOR}, f"{rel} の下限 {got} が {PY_FLOOR} でない"


def test_the_firmware_of_this_tree_can_be_used_by_this_padcue():
    """このツリーのファームウェアが、この padcue の使える範囲に入ること。

    同じリリースに添付するファームウェアを、同じリリースの padcue が拒否すると、利用者は
    どの組み合わせでも動かせない。
    """
    from padcue.client import firmware_problem
    fw = _firmware_version()
    assert firmware_problem(fw) == "", firmware_problem(fw)


def test_the_changelog_names_the_current_version():
    """CHANGELOG の最新の見出しが、実装のバージョンと揃っていること。

    バージョンを上げたときに書き換え忘れても、実装だけは動いてしまうので、気づく
    きっかけがどこにもない。CHANGELOG の最新の見出しが「未リリース」のときは、
    比べるバージョンがない。
    """
    got = _changelog_version()
    assert got in (None, PKG_VERSION), \
        f"CHANGELOG の最新 v{got} が実装 {PKG_VERSION} とずれています"


@pytest.mark.parametrize("where", ["package", "firmware"])
def test_version_looks_like_a_release(where):
    """開発中の目印(-dev など)を付けたまま公開しないこと。"""
    v = PKG_VERSION if where == "package" else _firmware_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), \
        f"{where} のバージョンが公開できる形ではありません: {v}"
