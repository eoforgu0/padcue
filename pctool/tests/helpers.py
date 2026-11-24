"""テストどうしで使い回すツール(fixture ではないもの)。

pytest の慣習として、テストモジュールは互いに import しない(収集の順で import の
結果が変わるうえ、どこに何があるか追えなくなる)。fixture は conftest.py、ダミーの装置は
fakedev.py、Switch の応答の表は switchsim.py、それ以外の関数とクラスはここに置く。
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path

import pytest

from padcue import binfmt, engine, pack, procfmt, proto, unpack
from padcue.client import DeviceClient, LogReader
from padcue.conditions import FORMAT_VERSION as COND_FORMAT
from padcue.conditions import ConditionStore
from padcue.dsl import Compiled, Proc, compile_procs
from padcue.frames import encode_png
from padcue.project import Project
from padcue.proto import Message
from tests.switchsim import handshake_sequence, verify_reply

# ---- 同じものの別名 ----

# 概念(concepts.md)と用語集にない別名と、それに当たる用語。同じものを指す語が 2 つ
# あると、読む人は別のものだと思う(P25)
ALIASES = {
    "操作待ち": "選択待ち",
    "区切り停止": "区切りで停止",
    "即時停止": "停止",
    "一時無効化": "無効にする",
    "映像デバイス": "映像機器",
}


def aliases_in(line: str) -> list[str]:
    """行の中の別名を「別名 → 用語」の形で返す。"""
    return [f"{old!r} → {new}" for old, new in ALIASES.items() if old in line]


# ---- 操作画面のサーバー ----


def get(base: str, path: str = "", timeout: float = 10.0):
    """base + path を GET し、返った JSON を読む。"""
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read())


def post(base: str, path, obj=None, timeout: float = 20.0):
    """base + path へ obj を JSON で POST し、返った JSON を読む。

    path を省いて post(url, obj) とも書ける。
    """
    if obj is None:
        path, obj = "", path
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def select(base: str, dev: str, arm: int):
    """画面と同じく、いま見えている選択待ちの世代(await_gen)を付けて選ぶ。"""
    d = next(x for x in get(base, "/api/state")["devices"] if x["name"] == dev)
    return post(base, "/api/select",
                {"dev": dev, "arm": arm, "gen": d.get("await_gen")})


def lane(base, name="1P"):
    """/api/state の装置のうち、name のレーンを返す。"""
    return next(d for d in get(base, "/api/state")["devices"] if d["name"] == name)


def wait_until(fn, timeout: float = 20.0):
    """fn が真の値を返すまで 0.1 秒ごとに呼び、その値を返す。

    timeout 秒たっても偽なら、最後にもう 1 回呼んだ値を返す。呼び出し側の
    assert は、そのときの値を見て失敗する。
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def lose_device(proj, base, name: str) -> int:
    """装置 name の接続先を、つなぐと拒否されるポートに替え、操作画面のサーバーが
    そのレーンを未検出として返すまで待つ。装置は動き続ける(Wi-Fi が切れただけの
    装置と同じ)。元のポートを返す(登録の port に戻すと、また見つかる)。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        unused = sock.getsockname()[1]
    cfg = proj.load_config()
    at = next(i for i, d in enumerate(cfg["devices"]) if d["name"] == name)
    port = cfg["devices"][at]["port"]
    proj.update_device(cfg, at, port=unused)
    assert wait_until(lambda: lane(base, name).get("gone"), timeout=20), \
        lane(base, name)
    return port


@contextmanager
def gui_server(proj):
    """操作画面のサーバーを、この PC の中(127.0.0.1)の空いたポートで起動する。

    渡すのは画面の URL である。終了させた後に drop_handler_state を呼び、監視と
    装置プールを次のテストへ残さない(前のテストが残したものは、そのテストの
    後始末が消している)。終了させるときは、先にサーバーを終了させてから消す。逆に
    すると、消した後に受け取った要求が装置プールを作り直す。
    """
    from padcue import gui
    srv = gui.make_server(proj)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        drop_handler_state()


# ---- プロジェクトの準備 ----


def compile_nodes(procedures, name: str | None = None, *, root=None,
                  laps: int | str | None = 1, start=None, lanes: int = 1,
                  finals: bool = True) -> Compiled:
    """手順のブロックの配列(正式な形式)から手順をコンパイルする。

    procedures: {手順名: ブロックの配列}。ブロックの配列だけを渡したときは、それを
    name(既定 "p")という名の手順 1 つとして扱う。name はコンパイルする手順。
    root は frametable のブロックが読む frametables/*.csv を置くフォルダー。
    laps は実行のときに決まる周回数(1 以上の整数か "infinite")で、変換器が
    手順の中の SETCNT に入れる。None は周回数を決めない変換(編集画面と一覧の
    変換。dsl.compile_procs)である。start は実行を始める位置で、途中の
    位置なら変換器がそこから続く手順を合成する。lanes は一緒に
    開始するグループのレーンの数で、合流点が選択待ちになるかを決める。
    finals は dsl.compile_procs と同じ意味である。

    ファイルを介さないので、文書の頭(kind と format)は必要ない。dsl の
    CompileError はそのまま送出される(procfmt.compile_procedure はこれをファイル名
    つきの ProcedureError に包む)。
    """
    if isinstance(procedures, list):
        procedures = {name or "p": procedures}
    project = _NodesOnly(pathlib.Path(root or "."))
    for proc_name in procedures:
        project.procs[proc_name] = Proc(proc_name, 0)
    for proc_name, body in procedures.items():
        project._ordinal = 0
        proc = project.procs[proc_name]
        proc.line = project.next_ordinal()
        proc.body.extend(
            procfmt.parse_procedure_body(body, proc_name, project))
    return compile_procs(project.procs, name or next(iter(procedures)), laps, start,
                         lanes, finals)


class _NodesOnly(procfmt._Project):
    """手順をファイルからでなく引数から取るプロジェクト(compile_nodes 用)。"""

    def load_procedure(self, name: str, *, missing_ok: bool = False) -> None:
        return   # 手順はすべて登録済み。未定義の呼び出し先は compile_procs が報告する


def manual_arm(name: str, body: list | None = None) -> dict:
    """人が選ぶ選択肢を 1 つ作る(file-format.md §5-2)。

    「選択条件」を書かない選択肢は待たずに選ばれるので、選択待ちにしたいテストでは
    どの選択肢にも「選択条件」が必要である。人が選ぶだけの選択肢は多くのテストで出てくる
    ので、ここにまとめる。
    """
    return {"name": name, "what": {"kind": "manual"}, "body": body or []}


def make_project(tmp_path, procedures: dict,
                 frametables: dict | None = None) -> Project:
    tmp_path = pathlib.Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frametables").mkdir(parents=True, exist_ok=True)
    for name, body in procedures.items():
        (tmp_path / "procedures" / f"{name}.json").write_text(
            json.dumps({"kind": "procedure", "format": procfmt.FORMAT_VERSION,
                        "body": body}, ensure_ascii=False), encoding="utf-8")
    for name, text in (frametables or {}).items():
        (tmp_path / "frametables" / f"{name}.csv").write_text(text, encoding="utf-8")
    return p


# ---- 対応表 ----


def lap_of(lap_map: list, index: int, values: dict) -> int | None:
    """対応表と装置のカウンターの値から、本来の回番号を出す。

    project.Project.lap_of と同じ計算だが、ホスト検証ハーネスの read_c_marks が
    返す形(レコード番号と、カウンター番号ごとの値の辞書)をそのまま受ける。
    """
    for entry in lap_map:
        if entry["start"] <= index < entry["end"]:
            lap = entry["first"]
            for c in entry["counters"]:
                lap += (c["total"] - values[c["counter"]]) * c["step"]
            return lap
    return None


# ---- 条件の画像 ----


def add_images(store, name: str, refs=(), negatives=()) -> list[str]:
    """条件 name に画像を追加して保存し、追加した画像の位置を追加した順に返す。

    画面と同じ経路(ConditionStore.add_image で置き、save で並びに入れる)を
    通す。設定は保存してあるままにする。
    """
    doc = json.loads(store.doc_path(name).read_text(encoding="utf-8"))
    added = [*(store.add_image(name, png, "基準画像") for png in refs),
             *(store.add_image(name, png) for png in negatives)]
    store.save(name, doc["source"], [tuple(r) for r in doc["regions"]],
               doc["threshold"], refs=[*doc["refs"], *added[:len(refs)]],
               negatives=[*doc["negatives"], *added[len(refs):]])
    return added


def place_images(store, name: str, refs=None, negatives=None) -> None:
    """条件 name に、名前を決めた画像を置いて並びに入れる({位置: PNG})。

    追加した画像の名前は乱数になるので、テストが名前で指したいときに使う。手で書いた
    条件やインポートした条件と同じく、画像の名前は images/ の中なら何でもよい。
    """
    doc = json.loads(store.doc_path(name).read_text(encoding="utf-8"))
    for rel, png in {**(refs or {}), **(negatives or {})}.items():
        path = store.image_path(name, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
    store.save(name, doc["source"], [tuple(r) for r in doc["regions"]],
               doc["threshold"], refs=[*doc["refs"], *(refs or {})],
               negatives=[*doc["negatives"], *(negatives or {})])


# ---- padcue ファイル(エクスポートとインポート) ----

# 条件に置く基準画像(4x4 の灰色)
GRAY_PNG = encode_png(4, 4, bytes((128, 128, 128)) * 16)


def procedure_doc(body) -> dict:
    """ファイルに書く形の手順(文書の頭つき)。"""
    return {"kind": "procedure", "format": procfmt.FORMAT_VERSION, "body": body}


def pack_project(tmp_path, procs=None, frametables=None, conds=None,
                 sub="proj") -> Project:
    """tmp_path / sub に、渡した手順・フレーム表・条件を持つプロジェクトを作る。

    procs は {名前: 手順の文書}、frametables は {名前: CSV}、conds は条件の名前の
    並びである。条件は、画像のファイルを 1 枚(GRAY_PNG)持つ。
    """
    root = pathlib.Path(tmp_path) / sub
    (root / "procedures").mkdir(parents=True, exist_ok=True)
    for name, doc in (procs or {}).items():
        (root / f"procedures/{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (root / "frametables").mkdir(parents=True, exist_ok=True)
    for name, text in (frametables or {}).items():
        (root / f"frametables/{name}.csv").write_text(text, encoding="utf-8")
    store = ConditionStore(root)
    for name in conds or []:
        store.save(name, {"kind": "file", "path": "x.png"}, [(0.0, 0.0, 1.0, 1.0)], 88)
        place_images(store, name, refs={"images/0001.png": GRAY_PNG})
    (root / "padcue.json").write_text('{"devices": []}', encoding="utf-8")
    return Project(root)


def zip_pack(tmp_path, entries: dict, name="悪い.padcue"):
    """entries({zip の中の名前: 中身})をそのまま入れた zip を作る。

    書き出し(pack.write)を通さないので、形の崩れた padcue ファイルを作れる。
    """
    dest = pathlib.Path(tmp_path) / name
    with zipfile.ZipFile(dest, "w") as zf:
        for n, data in entries.items():
            zf.writestr(n, data)
    return dest


def pack_manifest(*entries) -> str:
    """padcue ファイルの構成情報。entries は (種類, 名前) の並び。"""
    return json.dumps({"kind": "pack", "format": pack.PACK_FORMAT,
                       "entries": [{"kind": k, "name": n} for k, n in entries]},
                      ensure_ascii=False)


def condition_doc(**over) -> dict:
    """基準画像を 1 枚持つ条件の condition.json。over で欄を差し替える。"""
    doc = {"kind": "condition", "format": COND_FORMAT,
           "source": {"kind": "file", "path": "x.png"},
           "regions": [[0.0, 0.0, 1.0, 1.0]], "threshold": 88,
           "refs": ["images/0001.png"], "negatives": []}
    doc.update(over)
    return doc


def export_pack(tmp_path, tops, **kw):
    """インポート先とは別のプロジェクト(tmp_path / "src")に作って、padcue ファイルに
    書き出し、読み込んだ結果を返す。kw は pack_project に渡す。"""
    src = pack_project(tmp_path, sub="src", **kw)
    dest = pathlib.Path(tmp_path) / f"出したもの{pack.SUFFIX}"
    pack.write(src, tops, dest)
    return unpack.read(dest)


# ---- 装置への要求 ----


def own(client, client_id: str = "tests", force: bool = False) -> None:
    """接続に操作権を持たせる(識別子を送ってから取得する)。

    装置は、実行・転送・選択のように装置を動かす要求を、操作権の所有者からしか
    受け付けない。force を立てると、別の所有者から取り上げる。
    """
    client.hello(client_id)
    client.acquire(force)


def new_logs(client) -> list[dict]:
    """前に読んだ後に溜まった装置の記録を、すべて読む。

    読んだ分は受け取ったと知らせるので、装置から消える。次に呼ぶと、その後に
    溜まった記録だけが返る。
    """
    got: list[dict] = []
    LogReader().read(client.logs, got.extend)
    return got


# 別の padcue のふりをする接続の識別子
OTHER_SETUP = "0123456789abcdef0123456789abcdef"


def other_setup(dev) -> DeviceClient:
    """別の padcue の識別子を送り、操作権を強制的に取得した接続。"""
    c = DeviceClient("127.0.0.1", dev.port)
    c.connect()
    c.hello(OTHER_SETUP)
    c.acquire(force=True)
    return c


def wait_awaiting(c, timeout: float = 20.0) -> dict:
    """選択待ちに入るか実行が終わるまで 0.01 秒ごとに STATUS を読み、最後に読んだ
    STATUS を返す。

    timeout 秒たっても選択待ちにならず実行も続いていれば、そのときの STATUS を
    返す。呼び出し側の assert は、それを見て失敗する。
    """
    end = time.monotonic() + timeout
    st = c.status()
    while time.monotonic() < end and st.get("running") and not st.get("awaiting"):
        time.sleep(0.01)
        st = c.status()
    return st


def serve_wrong_magic() -> int:
    """マジックの違う HELLO 応答を 1 回だけ返すダミーのサーバーを起動し、ポートを返す。

    生のソケットで、形式は正しいがこのツールの装置ではない機器のふりをする。
    1 回の接続に答えたら、ソケットを閉じて終わる。
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.recv(4096)
            conn.sendall(proto.pack(Message(
                proto.T_HELLO | proto.T_RESP, {"magic": "other", "fw": "x"})))
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def require_firmware_build(path: Path, label: str) -> None:
    """ファームウェアのビルドの成果物(path)がなければ、skip する。

    環境変数 PADCUE_REQUIRE_FIRMWARE があれば、skip せずに失敗させる(CI が設定する。
    ci.yml と CONTRIBUTING.md)。skip のまま通ると、確認していないことに気づけない。
    label はない物の呼び名(失敗の文に入る)。
    """
    if path.is_file():
        return
    if os.environ.get("PADCUE_REQUIRE_FIRMWARE"):
        pytest.fail(f"ファームウェアの{label}がありません: {path}")
    pytest.skip(
        f"ファームウェアをビルドしていません(firmware/build/{path.name} がない)")


def write_marker(folder, base: str) -> None:
    """serve() が書くのと同じ稼働の目印(gui_server.json。動いているサーバーの
    トークンつき)を、folder に書く。base は画面の URL。"""
    from padcue import gui
    port = int(base.rsplit(":", 1)[1])
    (folder / "gui_server.json").write_text(
        json.dumps({"port": port, "pid": 0, "token": gui._Handler.server_token}),
        encoding="utf-8")


def tool_module(name: str):
    """pctool/tools/ の module を読み込む(tools はパッケージではないので、探す場所に
    足してから読む)。"""
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module(name)


# ---- 手順のバイナリ(レコードから直接組む) ----
# 変換器(dsl.py)を通さずに組むのは、区切りのマークをどこに置くかをテストが
# 決めるためである。


def lap_procedure(count: int, lap: int = 60) -> bytes:
    """周回のくり返しだけの手順。

        0 SETCNT  周回のカウンター(count)
        1 STATE   回の先頭。A を押す(周の境のマーク)
        2 STATE   同じ回の中で A を離す
        3 DJNZ    1 へ戻り、1 回ぶん(lap フレーム)だけ時刻を進める
        4 STATE   くり返しを抜けた直後(周の境のマーク)
        5 END

    count に binfmt.INFINITE_COUNT を渡すと、総フレーム数 0(総量なし)の
    無限のくり返しになる。
    """
    events = [
        binfmt.SetCnt(0, count),
        binfmt.State(0, buttons=1 << binfmt.BUTTONS["A"], mark=binfmt.MARK_LAP),
        binfmt.State(lap // 2),
        binfmt.Djnz(0, 1, lap),
        binfmt.State(0, mark=binfmt.MARK_LAP),
        binfmt.End(),
    ]
    total = 0 if count == binfmt.INFINITE_COUNT else lap * count
    return binfmt.encode(events, total)


def record_byte(index: int, offset: int) -> int:
    """レコード index の中の offset バイト目の、手順データの先頭からの位置。"""
    return binfmt.HEADER_SIZE + index * binfmt.RECORD_SIZE + offset


def rewrite_byte(blob: bytes, offset: int, value: int) -> bytes:
    """手順データの 1 バイトを書き換え、CRC を付け直す。

    符号化(binfmt.encode)は壊れた値を拒否するので、復号が拒否することを確認する壊れた
    手順データは、正しく符号化してからこれで作る。CRC は encode と同じく、ヘッダーの
    CRC の欄を除く 14 バイトとレコード部から計算する。
    """
    out = bytearray(blob)
    out[offset] = value
    struct.pack_into("<I", out, binfmt.HEADER_SIZE - 4,
                     zlib.crc32(bytes(out[:binfmt.HEADER_SIZE - 4])
                                + bytes(out[binfmt.HEADER_SIZE:])))
    return bytes(out)


# ---- C 実行エンジンのホスト版 ----

def run_c(core_host_exe, blob: bytes, tmp_path, max_steps=1_000_000, *,
          choices=None, await_frames=0):
    """ホスト検証ハーネスへバイナリを渡し、出力列(12 値の組)を返す。

    choices を渡すと、選択待ちでその順に選び(PADEMU_CHOICES)、選ぶまでに
    await_frames フレーム待つ(PADEMU_AWAIT_FRAMES)。渡さなければ、ハーネスの
    既定のまま実行する。周回数は手順の中の SETCNT に入っている(変換器が手順全体を
    くり返しで囲む)ので、ハーネスへは渡さない。「MARK …」で始まる行は区切りのマークの
    注釈で、出力そのものではないので読み飛ばす(マークを見るテストは
    read_c_marks を使う)。
    """
    return [em for em, _mark in _run_c_lines(core_host_exe, blob, tmp_path, max_steps,
                                             choices, await_frames)]


def run_py(events, choices=None, await_frames=0):
    """Python の実行エンジンで実行し、run_c と同じ形(12 値の組)の出力列を返す。"""
    return [(e.frame, e.buttons, e.lx, e.ly, e.rx, e.ry,
             e.gx, e.gy, e.gz, e.ax, e.ay, e.az)
            for e in engine.run(events, choices=choices, await_frames=await_frames)]


def read_c_marks(core_host_exe, blob: bytes, tmp_path, max_steps=1_000_000):
    """出力ごとの区切りのマークを返す [(レコード番号, マーク, {カウンター: 値})]。

    マークの付いていない出力は含めない(ハーネスは注釈の行を出さない)。
    """
    return [mark for _em, mark in _run_c_lines(core_host_exe, blob, tmp_path, max_steps)
            if mark is not None]


def read_c_mark_frames(core_host_exe, blob: bytes, tmp_path, max_steps=1_000_000):
    """区切りのマークの付いた出力を [(フレーム, マーク)] で返す。

    区切りが何フレーム目にあるかを、先頭から通した実行と、途中の位置から
    始める合成した手順とで照合するために使う。レコード番号は
    合成で変わるので、read_c_marks が返す番号どうしは比べられない。
    """
    return [(em[0], mark[1])
            for em, mark in _run_c_lines(core_host_exe, blob, tmp_path, max_steps)
            if mark is not None]


def _run_c_lines(core_host_exe, blob: bytes, tmp_path, max_steps,
                 choices=None, await_frames=0):
    p = tmp_path / "proc.bin"
    p.write_bytes(blob)
    env = None
    if choices is not None:
        env = dict(os.environ, PADEMU_CHOICES=",".join(str(c) for c in choices),
                   PADEMU_AWAIT_FRAMES=str(await_frames))
    res = subprocess.run(
        [str(core_host_exe), str(p), str(max_steps)], capture_output=True, text=True,
        env=env)
    assert res.returncode == 0, f"C 実行失敗: {res.stdout} {res.stderr}"
    lines = res.stdout.strip().splitlines()
    assert lines[-1] == "DONE"
    out = []
    pending = None
    for ln in lines[:-1]:
        if ln.startswith("MARK "):
            parts = ln.split()
            counters = {int(k): int(v)
                        for k, v in (t.split("=") for t in parts[3:])}
            pending = (int(parts[1]), int(parts[2]), counters)
            continue
        vals = [int(x) for x in ln.split()]
        assert len(vals) == 12
        out.append((tuple(vals), pending))
        pending = None
    assert pending is None, "MARK の後ろに出力の行がない"
    return out


# ---- Pro コントローラー互換の転送層のホスト版 ----

# ホストテストの装置が報告する MAC(host/procon_host.c と同じ値)。実機は装置ごとに
# 下位 3 バイトを自分の MAC から作るので、ここはテスト用の固定値でよい
MAC = bytes([0x04, 0x03, 0xD6, 0x00, 0x00, 0x01])


class Device:
    """C 実装のホスト版の装置(procon_host)を、標準入出力で動かすラッパー。"""

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

    def breadcrumb(self) -> int:
        r = self._cmd("bc")
        return int(r.split()[1], 16)

    def tx_out(self, data: bytes) -> bool | None:
        """出力レポートを処理し、応答は送信キューへ入れる(USB 統合を模した経路)。"""
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
        """送信しようとしたが失敗した場合(エンドポイントが空かない等)。"""
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

    def descriptor_info(self) -> tuple[int, int, int]:
        r = self._cmd("desc").split()
        return int(r[1]), int(r[2], 16), int(r[3], 16)

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
        """本体識別子の記録(取り出すと消える)。なければ None。"""
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


# ---- 操作画面(gui._Handler)の後始末 ----

def drop_handler_state():
    """操作画面のサーバーが持つ監視と装置プールを消す(gui.reset_handler_state)。前の
    テストのものを次のテストへ持ち込まないため、テストの前後に呼ぶ。"""
    from padcue import gui
    gui.reset_handler_state()


# ---- 画面の文言(index.html) ----

HTML_ATTR = re.compile(r'\b(?:title|placeholder|alt|aria-label)="([^"]*)"')
HTML_TEXT = re.compile(r">([^<>{}]+)<")
_JA = re.compile(r"[ぁ-んァ-ヶ一-龥]")
# 文言ではない所。注釈は読む相手が開発者で、script と style は文言を持たない
_HTML_NOT_TEXT = re.compile(r"<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>",
                            re.S)


def html_texts(path) -> list[tuple[int, str]]:
    """HTML の中の、日本語を含む文言を (行, 文字列) で返す。

    title・placeholder・alt・aria-label の値と、要素の中の文字である。行を
    またぐ文字は、空白を 1 つに詰めてひとまとまりで返す。
    """
    src = path.read_text(encoding="utf-8")
    # 行の番号がずれないよう、改行だけ残して消す
    src = _HTML_NOT_TEXT.sub(lambda m: "\n" * m.group(0).count("\n"), src)
    out = []
    for rx in (HTML_ATTR, HTML_TEXT):
        for m in rx.finditer(src):
            s = " ".join(m.group(1).split())
            if _JA.search(s):
                out.append((src.count("\n", 0, m.start(1)) + 1, s))
    return sorted(out)


def page_assets(base: str) -> str:
    """画面を構成するファイル(HTML、CSS、JS)をつなげた文字列。

    ファイルは分かれているので、「画面のどこかにこの語があること」を
    見るテストはここを通す(どのファイルにあるかまで問うなら web_asset を
    直接読む)。
    """
    from padcue.gui import _SCRIPTS
    parts = []
    for path in ("/", "/app.css", *(f"/{s}" for s in _SCRIPTS)):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            parts.append(r.read().decode("utf-8"))
    return "\n".join(parts)
