"""検査で共有する下ごしらえ。

fixture はここに置く。pytest の作法(テストモジュール同士を import しない)に
従うため。以前は test_procon / test_resume / test_wait_branch /
test_integration の4ファイルが test_hostc から fixture を借りていて、
gcc ビルドの手順・skip の条件・出力先が一箇所で読めなかった。
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "firmware" / "components" / "pademu_core"


def find_gcc() -> str | None:
    """ホスト用の gcc を探す。WinLibs を winget で入れた場所も見る。"""
    if shutil.which("gcc"):
        return "gcc"
    pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\BrechtSanders.WinLibs*\mingw64\bin\gcc.exe")
    hits = glob.glob(pattern)
    return hits[0] if hits else None


@pytest.fixture(scope="session")
def gcc() -> str:
    """ホスト用の gcc。無ければ skip する。

    ただし環境変数 PADCUE_REQUIRE_GCC が立っていれば失敗させる。C 実装と
    Python 参照実装の送出列が完全一致することはこのプロジェクトの中核的な
    主張で、gcc の無い環境で黙って skip すると「緑なのに何も検証していない」
    状態になる(この skip で飛ぶのは約120件)。CI では要求フラグを立てる。
    """
    found = find_gcc()
    if found:
        return found
    if os.environ.get("PADCUE_REQUIRE_GCC"):
        pytest.fail("gcc が見つかりません(PADCUE_REQUIRE_GCC が立っています)")
    pytest.skip("gcc が見つかりません")


def _build_host(gcc: str, name: str, sources: list[str]) -> Path:
    """pademu_core のソースをホストでビルドし、実行ファイルの場所を返す。

    出力先はリポジトリ内(build/ は非追跡)。%TEMP% に置くと、ウイルス対策
    ソフトが「一時フォルダに現れた無署名の exe」を誤検知・隔離して 64 件の
    検査ごと落とすことがある(2026-08-06 に ESET の定義更新で実際に発生)。
    リポジトリを除外設定していれば巻き込まれない。
    """
    out = REPO / "build" / "hosttest" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [gcc, "-O2", "-std=c11", "-Wall", "-Werror",
           "-I", str(CORE / "include")]
    cmd += [str(CORE / s) for s in sources]
    cmd += ["-o", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"C ビルド失敗:\n{res.stderr}"
    return out


@pytest.fixture(scope="session")
def host_exe(gcc: str) -> Path:
    """実行エンジン(pademu_core)のホスト版。"""
    return _build_host(gcc, "pademu_host.exe",
                       ["pademu_core.c", "host/host_main.c"])


@pytest.fixture(scope="session")
def procon_exe(gcc: str) -> Path:
    """プロコン互換の転送層のホスト版。"""
    return _build_host(gcc, "procon_host.exe",
                       ["pademu_procon.c", "pademu_hidpad.c", "pademu_tx.c",
                        "pademu_usb_desc.c", "pademu_usb_desc_hidpad.c",
                        "host/procon_host.c"])
