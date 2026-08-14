"""検査どうしで使い回す道具(fixture ではないもの)。

pytest の作法として、検査モジュールは互いに import しない(収集の順で
壊れるうえ、どこに何があるか追えなくなる)。共有するものはここか
conftest.py に置く —— fixture は conftest.py、ただの関数とクラスはここ。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
from pathlib import Path

from padcue.project import Project
from padcue.switchsim import handshake_sequence, verify_reply

# ---- プロジェクトの下ごしらえ ----



def make_project(tmp_path, flows: dict, parts: dict | None = None) -> Project:
    tmp_path = pathlib.Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir(parents=True, exist_ok=True)
    (tmp_path / "parts").mkdir(parents=True, exist_ok=True)
    for name, body in flows.items():
        (tmp_path / "procedures" / f"{name}.flow.json").write_text(
            json.dumps({"schema": 1, "name": name, "body": body},
                       ensure_ascii=False), encoding="utf-8")
    for name, text in (parts or {}).items():
        (tmp_path / "parts" / f"{name}.csv").write_text(text, encoding="utf-8")
    return p


# ---- C 実行エンジンのホスト版 ----

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


# ---- プロコン互換の転送層のホスト版 ----

# ホスト検査の装置が名乗る MAC(host/procon_host.c と同じ値)。実機は装置ごとに
# 下位3バイトを自分の MAC から作るので、ここは検査用の固定値でよい
MAC = bytes([0x04, 0x03, 0xD6, 0x00, 0x00, 0x01])

class Device:
    """C 実装のプロセスを「デバイス」として扱うラッパ。"""

    def __init__(self, exe: Path):
        self.proc = subprocess.Popen(
            [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)

    def _cmd(self, line: str) -> str:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline().strip()

    def send_output(self, data: bytes) -> bytes | None:
        r = self._cmd("out " + data.hex())
        if r == "none":
            return None
        assert r.startswith("in "), r
        return bytes.fromhex(r[3:])

    def set_state(self, buttons=0, lx=0, ly=0, rx=0, ry=0,
                  gx=0, gy=0, gz=0, ax=0, ay=0, az=0) -> None:
        r = self._cmd(f"state {buttons} {lx} {ly} {rx} {ry} "
                      f"{gx} {gy} {gz} {ax} {ay} {az}")
        assert r == "ok", r

    def input_report(self) -> bytes:
        r = self._cmd("input")
        assert r.startswith("in "), r
        return bytes.fromhex(r[3:])

    def hidpad_report(self) -> bytes:
        r = self._cmd("hidpad")
        assert r.startswith("in "), r
        return bytes.fromhex(r[3:])

    def breadcrumb(self) -> int:
        r = self._cmd("bc")
        return int(r.split()[1], 16)

    def tx_out(self, data: bytes) -> bool | None:
        """出力レポートを処理し、応答は送出キューへ積む(USB 統合を模した経路)。"""
        r = self._cmd("txout " + data.hex())
        if r == "none":
            return None
        assert r.startswith("queued "), r
        return r.split()[1] == "1"

    def tx_next(self) -> bytes | None:
        r = self._cmd("txnext")
        if r == "empty":
            return None
        assert r.startswith("in "), r
        return bytes.fromhex(r[3:])

    def tx_fail(self) -> int:
        """送出しようとしたが失敗した場合(エンドポイントが空かない等)。"""
        r = self._cmd("txfail")
        assert r.startswith("fail "), r
        return int(r.split()[1])

    def tx_bad(self) -> None:
        """レポートが壊れていて送らずに捨てた場合。"""
        assert self._cmd("txbad") == "discarded"

    def tx_stats(self) -> dict:
        r = self._cmd("txstats").split()
        return {"replies": int(r[1]), "inputs": int(r[2]),
                "dropped": int(r[3]), "pending": int(r[4])}

    def tx_stats2(self) -> dict:
        r = self._cmd("txstats2").split()
        return {"failed_replies": int(r[1]), "dropped_inputs": int(r[2]),
                "bad_reports": int(r[3]), "retry": int(r[4])}

    def descriptor_info(self) -> tuple[int, int, int, int]:
        r = self._cmd("desc").split()
        return int(r[1]), int(r[2]), int(r[3], 16), int(r[4], 16)

    def led(self) -> tuple[int, int]:
        r = self._cmd("led")
        _, val, calls = r.split()
        return int(val, 16), int(calls)

    def pair(self) -> tuple[int, int]:
        """ペアリングの観測値 (受けた回数, 直近フェーズ)。"""
        r = self._cmd("pair")
        _, reqs, step = r.split()
        return int(reqs), int(step, 16)

    def host_info(self) -> bytes | None:
        """本体識別子の控え(取り出すと消える)。無ければ None。"""
        r = self._cmd("hostinfo")
        if r == "none":
            return None
        _, _n, hexs = r.split()
        return bytes.fromhex(hexs)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)

def run_handshake(device: Device) -> None:
    for exp in handshake_sequence(MAC):
        reply = device.send_output(exp.out)
        verify_reply(exp, reply)
