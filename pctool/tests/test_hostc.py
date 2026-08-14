"""C 実行エンジン(firmware/components/pademu_core)と Python 参照実装の一致検証。

同一バイナリに対する送出列(絶対フレーム+全状態12値)の完全一致を、
固定の手順群+乱数生成の手順群で確認する。C のビルドと gcc が無いときの
扱いは conftest.py の host_exe / gcc fixture にある。
"""
import random
import subprocess

import pytest

from padcue import binfmt, engine
from padcue.dsl import compile_source
from tests.helpers import run_c


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
            lines.append(
                f"press {rng.choice(['A','B','X','ZL','DU'])} "
                f"{rng.randint(3, 20)}")
        elif kind == 1:
            lines.append(f"wait {rng.randint(3, 30)}")
        elif kind == 2:
            lines.append(
                f"stick {rng.choice(['L','R'])} "
                f"{rng.randint(-2048, 2047)} {rng.randint(-2048, 2047)}")
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


def test_c_rejects_overflowing_event_count(host_exe, tmp_path):
    """イベント数が桁あふれを起こす値でも、長さ検査が弾くこと。

    実機の size_t は 32bit なので、`count * 32` と掛けると count が 2^27 で
    積が回り込んで 0 になる。レコード部が空(50B ちょうど)のデータが検査を
    通り抜け、直後のループが1億3千万回ぶん確保外を読んでいた。crc の対象は
    ヘッダだけになるので、値も合わせられる。

    この検査自体はホスト(64bit)で走るので、掛け算のままでも通ってしまう
    ——桁あふれを再現できない。**割り算で照合する形に保つ**ための錠として
    置く(掛け算に戻すと実機だけが壊れ、ここでは気づけない)。
    """
    import struct
    import zlib
    head = struct.pack("<4sH32sII", b"PDT0", binfmt.SCHEMA_VERSION,
                       b"x".ljust(32, b"\x00"), 0x08000000, 10)
    blob = head + struct.pack("<I", zlib.crc32(head))
    assert len(blob) == 50
    p = tmp_path / "overflow.bin"
    p.write_bytes(blob)
    res = subprocess.run([str(host_exe), str(p), "1", "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 4" in res.stdout   # PADEMU_ERR_LENGTH
    # PC 側は多倍長なので元から弾く(両実装が同じ判定であることの確認)
    with pytest.raises(binfmt.DecodeError):
        binfmt.decode(blob)


def test_c_rejects_zero_loop_count(host_exe, tmp_path):
    """くり返し回数 0 のバイナリを、装置側も実行前に弾くこと。

    実行は「1 減らしてから 0 か見る」ので、0 から引くと C 側(uint32)が
    回り込んで約42億周する。Python 側は任意精度なので -1 になって素通り
    する。両実装の送出列が一致しない唯一の既知の穴だったので、decode の
    段階で両方が弾くようにした(PC 側は test_binfmt が見ている)。
    """
    blob = binfmt.encode("p", [binfmt.SetCnt(0, 0), binfmt.State(0),
                               binfmt.Djnz(0, target=1, advance=3),
                               binfmt.End()], 3)
    p = tmp_path / "zeroloop.bin"
    p.write_bytes(blob)
    res = subprocess.run([str(host_exe), str(p), "1", "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 8" in res.stdout   # PADEMU_ERR_ZERO_CYCLE
