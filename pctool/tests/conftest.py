"""検査で共有する下ごしらえ(fixture)。

pytest の作法として、検査モジュールは互いに import しない。共有するものは
**fixture はここ、ただの関数とクラスは helpers.py** に置く。以前は
test_procon / test_resume / test_wait_branch / test_integration が
test_hostc から借りていて、gcc ビルドの手順・skip の条件・出力先が
一箇所で読めなかった。
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


def page_assets(base: str) -> str:
    """画面を構成する資産(HTML + CSS + JS)をつなげた文字列。

    資産が gui.py の中の1本の文字列だった頃は、`/` を取れば全部入っていた。
    実ファイルへ分けたので、「画面のどこかにこの語があること」を見る検査は
    ここを通す(どのファイルにあるかまで問うなら web_asset を直接読む)。
    """
    import urllib.request

    from padcue.gui import _SCRIPTS
    parts = []
    for path in ("/", "/app.css", *(f"/{s}" for s in _SCRIPTS)):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            parts.append(r.read().decode("utf-8"))
    return "\n".join(parts)


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
    状態になる(この skip で飛ぶのは 68 件。2026-08-14 に実測)。CI では
    要求フラグを立てる。
    """
    found = find_gcc()
    if found:
        return found
    if os.environ.get("PADCUE_REQUIRE_GCC"):
        pytest.fail("gcc が見つかりません(PADCUE_REQUIRE_GCC が立っています)")
    pytest.skip("gcc が見つかりません")


def _build_host(gcc: str, name: str, sources: list[str]) -> Path:
    """pademu_core のソースをホストでビルドし、実行ファイルの場所を返す。

    出力先はリポジトリ直下の .hosttest/(非追跡)。%TEMP% に置くと、ウイルス
    対策ソフトが「一時フォルダに現れた無署名の exe」を誤検知・隔離して 64 件
    の検査ごと落とすことがある(2026-08-06 に ESET の定義更新で実際に発生)。
    リポジトリを除外設定していれば巻き込まれない。build/ に置かないのは、
    そこが利用者の手順のコンパイル結果を入れる場所だから(このリポジトリ
    自身をプロジェクトフォルダとして使える)。
    """
    out = REPO / ".hosttest" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    # 警告の厳しさはファーム本体(firmware/main/CMakeLists.txt)に合わせる。
    # ここだけ緩いと、実機のビルドで初めて止まる
    cmd = [gcc, "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
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
