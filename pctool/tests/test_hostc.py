"""C 実行エンジン(firmware/components/pademu_core)と Python 参照実装の一致検証。

同一バイナリに対する送出列(絶対フレーム+全状態12値)の完全一致を、
固定の手順群+乱数生成の手順群で確認する。gcc(WinLibs)が無い環境では skip。
"""
import glob
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from switchctl import binfmt, engine
from switchctl.dsl import compile_source

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "firmware" / "components" / "pademu_core"


def find_gcc() -> str | None:
    if shutil.which("gcc"):
        return "gcc"
    pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\BrechtSanders.WinLibs*\mingw64\bin\gcc.exe")
    hits = glob.glob(pattern)
    return hits[0] if hits else None


@pytest.fixture(scope="session")
def host_exe(tmp_path_factory):
    gcc = find_gcc()
    if gcc is None:
        pytest.skip("gcc が見つかりません")
    del tmp_path_factory   # 使わない(下記の理由でリポジトリ内に置く)
    # ビルド先はリポジトリ内(build/ は非追跡)。%TEMP% に置くと、
    # ウイルス対策ソフトが「一時フォルダに現れた無署名 exe」を誤検知・
    # 隔離して 64 件の検査ごと落とすことがある(2026-08-06 に ESET の
    # 定義更新で実際に発生)。リポジトリを除外設定していれば巻き込まれない
    out = CORE.parents[2] / "build" / "hosttest" / "pademu_host.exe"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [gcc, "-O2", "-std=c11", "-Wall", "-Werror",
           "-I", str(CORE / "include"),
           str(CORE / "pademu_core.c"), str(CORE / "host" / "host_main.c"),
           "-o", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"C ビルド失敗:\n{res.stderr}"
    return out


def run_c(host_exe, blob: bytes, tmp_path, session_loops=1, max_steps=1_000_000):
    p = tmp_path / "proc.bin"
    p.write_bytes(blob)
    res = subprocess.run(
        [str(host_exe), str(p), str(session_loops), str(max_steps)],
        capture_output=True, text=True)
    assert res.returncode == 0, f"C 実行失敗: {res.stdout} {res.stderr}"
    lines = res.stdout.strip().splitlines()
    assert lines[-1] == "DONE"
    out = []
    for ln in lines[:-1]:
        vals = [int(x) for x in ln.split()]
        assert len(vals) == 12
        out.append(tuple(vals))
    return out


def py_emissions(events, total, session_loops=1):
    return [
        (e.frame, e.buttons, e.lx, e.ly, e.rx, e.ry,
         e.gx, e.gy, e.gz, e.ax, e.ay, e.az)
        for e in engine.run(events, total, session_loops)
    ]


FIXED_SOURCES = [
    "proc p\npress A 3\nwait 5\npress B 3\nwait 5\nend\n",
    "proc p\nloop 3 {\npress A 3\nwait 7\n}\nend\n",
    """
proc p
loop 2 {
press A 2  # lint:allow-1f
loop 3 {
press B 1  # lint:allow-1f
wait 4
}
wait 5
}
end
""",
    """
proc p
hold ZL
stick L up
loop 3 {
press A 2  # lint:allow-1f
wait 5
}
stick L neutral
release ZL
wait 10
end
""",
    "proc p\npress A 3\nloop 4 {\nwait 10\n}\npress A 3\nwait 10\nend\n",
    # ジャイロ(長さ付き・回しっぱなし)。C エンジンの gx..az 復元も 12 値
    # 完全一致の対象に載せる(以前はモーションを一度も動かしていなかった)
    "proc p\ngyro 0 2000 0 30\nwait 10\ngyro -500 0 700\nwait 20\n"
    "gyro 0 0 0\nwait 5\nend\n",
    "proc p\nloop 3 {\ngyro 1000 0 0 5\nwait 10\n}\nend\n",
]


@pytest.mark.parametrize("loops", [1, 3])
@pytest.mark.parametrize("i", range(len(FIXED_SOURCES)))
def test_fixed_programs_match(host_exe, tmp_path, i, loops):
    c = compile_source(FIXED_SOURCES[i])
    blob = binfmt.encode(c.name, c.events, c.total_frames)
    assert run_c(host_exe, blob, tmp_path, loops) == \
        py_emissions(c.events, c.total_frames, loops)


def _random_body(rng, depth, lines):
    n = rng.randint(2, 5)
    for _ in range(n):
        kind = rng.randint(0, 5)
        if kind == 0:
            lines.append(f"press {rng.choice(['A','B','X','ZL','DU'])} {rng.randint(3, 20)}")
        elif kind == 1:
            lines.append(f"wait {rng.randint(3, 30)}")
        elif kind == 2:
            lines.append(f"stick {rng.choice(['L','R'])} {rng.randint(-2048, 2047)} {rng.randint(-2048, 2047)}")
        elif kind == 3:
            lines.append(f"hold {rng.choice(['ZR','L'])}")
        elif kind == 4:
            lines.append(f"release {rng.choice(['ZR','L'])}")
        elif kind == 5 and depth < 2:
            lines.append(f"loop {rng.randint(2, 4)} {{  # lint:allow-loop-reset")
            _random_body(rng, depth + 1, lines)
            lines.append("wait 5")
            lines.append("}")
        else:
            lines.append(f"wait {rng.randint(3, 30)}")


@pytest.mark.parametrize("seed", range(15))
def test_random_programs_match(host_exe, tmp_path, seed):
    rng = random.Random(1000 + seed)
    lines = ["proc p"]
    _random_body(rng, 0, lines)
    lines += ["wait 10", "end", ""]
    c = compile_source("\n".join(lines))
    blob = binfmt.encode(c.name, c.events, c.total_frames)
    loops = rng.randint(1, 3)
    assert run_c(host_exe, blob, tmp_path, loops) == \
        py_emissions(c.events, c.total_frames, loops)


def test_japanese_name_roundtrip(host_exe, tmp_path):
    c = compile_source("proc 素材周回テスト\npress A 5\nwait 10\nend\n")
    blob = binfmt.encode(c.name, c.events, c.total_frames)
    run_c(host_exe, blob, tmp_path)  # UTF-8 名の decode が通ること


def test_c_rejects_corruption(host_exe, tmp_path):
    c = compile_source("proc p\npress A 5\nwait 5\nend\n")
    blob = bytearray(binfmt.encode(c.name, c.events, c.total_frames))
    blob[-1] ^= 0xFF
    p = tmp_path / "bad.bin"
    p.write_bytes(bytes(blob))
    res = subprocess.run([str(host_exe), str(p), "1", "1000"],
                        capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 5" in res.stdout  # PADEMU_ERR_CRC
