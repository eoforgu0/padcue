"""長い手順の通し: プロジェクトの手順を、模擬装置 2 台のグループで最後まで実行する。

    python pctool/tools/accept.py <出力先> <プロジェクト> <1P の手順> <2P の手順>

手順は名前で指し、2 台のグループで 50 周まわす。渡したプロジェクトのフォルダーには
書かない。出力先の中の _proj へコピーしてから、そのコピーに模擬装置の登録情報を書く。
_proj は実行のたびに作り直し、出力先のほかの物は変えない。

pytest が見るのは仕組みのひとつひとつである。ここでは、渡した手順そのものが 50 周を
最後まで通るかを見る。テストのダミーの手順では出ない長さ(例えば 17 万フレーム)と
大きさ(2 キロバイト)で通すためである。

実機は必要ない。模擬装置は仮想の時計を早送りするので、17 万フレーム
(実時間で 50 分)が数秒で終わる。

手順のスクリプト(スクリプトのブロックと、選択肢の「スクリプトの結果」)のコマンドは
実行しない。どれも終了コード 0 で終えたものとして進め、実行しなかったコマンドを
最後に出す(skip_scripts)。選択待ちでは(合流点を除き)、人の代わりに先頭の選択肢を
選ぶ。区切りで停止して続きから実行する項目は、1P の手順で、人が選ぶ選択待ち
(選択肢がどれも「手動」の分岐)に 2 回以上着くときだけ行う。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _localonly import lock_to_mock
from _workdir import rebuild_workdir

from padcue import condwatch, engine, gui
from padcue.mockdevice import MockDevice
from padcue.procfmt import compile_procedure
from padcue.project import Project

# 模擬装置の早送り。17 万フレームの手順を数秒で通すために大きく取る
SPEED = 4000.0
# 重い要求(転送など)を待つ上限。確定がこれに収まることを見る
HEAVY_WAIT_S = 15.0


class _NotStarted:
    """起動しなかったコマンドの代わり。すぐに終了コード 0 で終わったことにする。"""

    pid = 0
    returncode = 0

    def wait(self, timeout=None) -> int:
        return 0

    def poll(self) -> int:
        return 0


class _NoCommands:
    """判定の監視(condwatch)が使う subprocess の代わり。

    コマンドを起動する Popen だけを差し替え、起動せずに覚える。例外の型などほかの
    名前は本物の subprocess のものを返す。
    """

    def __init__(self) -> None:
        self.skipped: list[str] = []

    def __getattr__(self, name: str):
        return getattr(subprocess, name)

    def Popen(self, cmd, *_args, **_kwargs) -> _NotStarted:
        self.skipped.append(str(cmd))
        return _NotStarted()


def skip_scripts() -> _NoCommands:
    """手順のスクリプトのコマンドを実行せず、終了コード 0 で終えたことにする。

    返すのは、起動しなかったコマンドを覚えるオブジェクトである。スクリプトのブロックと
    選択肢の「スクリプトの結果」は、PC で任意のコマンドを実行する(runbook.md
    §6-3)。コマンドは別のプロセスになるので、lock_to_mock の安全策が及ばない。
    実行すると、コピーの外のファイルに書いたり、本物の画面・映像機器を使ったり、
    外と通信したりしうる。
    終了コードは 0 にするので、「スクリプトの結果」の選択肢は成立したものとして
    進む。
    """
    fake = _NoCommands()
    condwatch.subprocess = fake
    return fake


class Checker:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def check(self, name: str, fn) -> None:
        try:
            note = fn() or ""
            self.rows.append(("OK", name, str(note)))
        except AssertionError as e:
            self.rows.append(("NG", name, str(e)))
        except Exception as e:                       # noqa: BLE001  項目の失敗として記録し、次の項目へ進む
            self.rows.append(("ERR", name, f"{type(e).__name__}: {e}"))
        st, _, note = self.rows[-1]
        print(f"  {st:3s}  {name}" + (f"    {note}" if note else ""),
              flush=True)

    def skip(self, name: str, why: str) -> None:
        """渡した手順に前提がないので行わない項目。問題には数えず、理由を出す。"""
        self.rows.append(("--", name, why))
        print(f"  --   {name}    {why}", flush=True)

    def summary(self) -> int:
        bad = [r for r in self.rows if r[0] not in ("OK", "--")]
        skipped = sum(r[0] == "--" for r in self.rows)
        done = len(self.rows) - skipped
        print(f"\n===== {len(self.rows)} 項目: OK {done - len(bad)} "
              f"/ 問題 {len(bad)}"
              + (f" / 行わない {skipped}" if skipped else "") + " =====")
        for st, name, note in bad:
            print(f"{st}  {name}\n     {note}")
        return 1 if bad else 0


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read())


def post(base, path, obj):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def wait_until(fn, timeout=60.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def devs(base):
    return {d["name"]: d for d in get(base, "/api/state")["devices"]}


# 人の代わりに押した回数(最後に出す)
PICKED: list[str] = []
# 区切りで停止の項目が使う。ARMED を立てると、人が選ぶ選択待ちを次に 1 つ選んだ
# ところで HELD を立て、それからは選ばない(停止させる前に先へ進めない)
ARMED = threading.Event()
HELD = threading.Event()


def by_hand(kind: str, arm_kinds) -> bool:
    """人が選ぶまで進まない選択待ちか。選択肢がどれも「手動」の分岐である。

    合流点、スクリプトのブロック、条件・スクリプトの結果・時間切れで進む選択肢の
    ある分岐は、判定の監視か装置が進めることがある。kind と arm_kinds は、
    画面の状態の await_kind と arm_kinds と同じ形である。
    """
    return kind == "branch" and all(k == "manual" for k in arm_kinds or [])


def first_arm_path(c) -> tuple[list[str], int | None]:
    """模擬装置で先頭から実行し、選択待ちではどれも先頭の選択肢へ進むときの経路。

    返すのは、着いた選択待ちの種類の並びと、終わりのフレーム数である。種類は
    "hand"(人が選ぶ選択待ち。by_hand)、"join"(合流点)、"other"(そのほか)の
    どれかである。終わりに着かなければ、フレーム数は None にする。

    c は変換の成果物である。装置と同じ解釈(engine.walk)でたどる。
    """
    metas = {a.get("index"): a for a in c.awaits}
    counters: dict = {}
    idx = base = shift = 0
    met: list[str] = []
    for _ in range(100_000):
        try:
            p = engine.walk(c.events, idx, base, shift, counters, c.total_frames)
        except engine.WalkError:
            return met, None
        if p.kind == "end":
            return met, p.frame
        if p.kind == "await":
            awt = metas.get(p.index) or {}
            kind = ("join" if awt.get("join") else "script" if awt.get("script")
                    else "branch")
            met.append("join" if kind == "join"
                       else "hand" if awt and by_hand(kind, condwatch.arm_kinds(awt))
                       else "other")
            targets = c.events[p.index].targets
            if not targets:
                return met, None
            idx = targets[0]
        elif p.kind == "mark":
            idx = p.index + 1
        else:
            return met, None
        base, shift = p.base, p.shift
    return met, None


def hand_waits(c, limit: int = 2) -> int:
    """先頭の選択肢へ進む経路(first_arm_path)で、人が選ぶ選択待ちに何回着くか。
    limit 回まで数える。
    """
    return min(limit, first_arm_path(c)[0].count("hand"))


def expected_frames(c) -> int | None:
    """人の代わりに先頭の選択肢を押して最後まで実行したときの、経過フレーム数。

    変換器の総フレーム数は、選択肢を長さ 0 として数えてある。選んだ選択肢の長さは、
    実行した経路をたどらないと分からない。人が選ぶ選択待ちと合流点のほかに選択待ちが
    あれば None にする。条件・スクリプトの結果・時間切れで進む選択待ちは、早送りでは
    人の代わりに押すより先に進むことがあり、先頭の選択肢へ進むとは限らない。
    """
    met, end = first_arm_path(c)
    return None if "other" in met else end


def pick_when_waiting(base: str, stop: threading.Event) -> None:
    """選択待ちで先頭の選択肢を押す。人の役である。

    サーバーは代わりに選ばない。手順に「手動」の選択肢しかない選択待ちがあれば、
    誰かが押さないと先へ進まない。合流点の待ちは
    選択肢ではないので押さない。HELD が立っている間は押さない(ARMED を参照)。
    """
    while not stop.is_set():
        try:
            for name, d in devs(base).items():
                if HELD.is_set():
                    break
                if not d.get("awaiting") or d.get("join"):
                    continue
                post(base, "/api/select",
                     {"dev": name, "arm": 0, "gen": d.get("await_gen")})
                PICKED.append(name)
                if ARMED.is_set() and by_hand(d.get("await_kind"),
                                              d.get("arm_kinds")):
                    ARMED.clear()
                    HELD.set()
        except Exception:                            # noqa: BLE001  停止させた直後の失敗は数えない
            pass
        stop.wait(0.2)


# 登録する模擬装置の ID(記録は名前ではなく識別子で追う。P23)
DEV_IDS = {"1P": "aaaa00000001", "2P": "bbbb00000002"}


# 名前のない位置の言い方(画面の lists.js の UNNAMED_PLACE と同じ)
UNNAMED_PLACE = {"label": "名前のないラベル", "branch": "名前のない分岐",
                 "join": "名前のない合流点", "script": "スクリプト"}


def place_of(at: dict) -> str:
    """停止した位置の呼び方。画面(lists.js の stoppedPlace)と同じく、周の境は
    「5 周目の先頭」、ほかは「5 周目の「後半の頭」」と表す(名前は鉤括弧で囲む)。"""
    lap = at.get("lap")
    if at.get("kind") == "lap":
        end = (at.get("start") or {}).get("after")
        return f"{lap} 周目の{'終わり' if end else '先頭'}"
    name = at.get("name")
    place = (f"「{name}」" if name
             else UNNAMED_PLACE.get(at.get("kind"), UNNAMED_PLACE["label"]))
    return f"{lap} 周目の{place}" if lap else place


def laps_done(base: str, dev: str) -> int | None:
    """その装置の最後の実行で、終えた周の数(画面の前回の結果と同じ値)。"""
    return (devs(base)[dev].get("last_result") or {}).get("done")


def last_done_frames(base: str, dev: str) -> int | None:
    """その装置が最後に「最後まで終わりました」と記録したフレーム数。"""
    want = DEV_IDS[dev]
    laps = get(base, "/api/logs?limit=8000").get("laps", [])
    for g in reversed(laps):
        for e in reversed(g.get("entries", [])):
            if e.get("dev") == want and e.get("kind") == "RUN_DONE":
                return e.get("a")
    return None


def run_checks(c: Checker, base: str, proc1: str, proc2: str,
               laps: int, root: Path) -> None:
    assert wait_until(lambda: all(d.get("visible") for d in devs(base).values())), \
        "模擬装置が見えない"
    # 1P の手順を先頭から最後まで実行したときの経過フレーム数。続きから実行した
    # 長さと比べる
    full: dict = {"frames": None}

    def t_confirm_is_quick():
        """確定(転送と照合)が「重い要求の待ち」に収まること。

        渡した手順の大きさで測る。テストのダミーの短い手順では測れない。
        """
        t0 = time.monotonic()
        r = post(base, "/api/push", {"name": proc1, "dev": "1P"})
        dt = time.monotonic() - t0
        assert r.get("ok"), r
        assert dt < HEAVY_WAIT_S, f"確定に {dt:.1f} 秒かかった"
        return f"{dt * 1000:.0f} ms(上限 {HEAVY_WAIT_S:.0f} 秒)"
    c.check("渡した手順の確定が、重い要求の待ちに収まる",
            t_confirm_is_quick)

    def t_one_lane_runs_to_the_end():
        r = post(base, "/api/run",
                 {"plan": [{"dev": "1P", "name": proc1, "laps": laps}]})
        assert r.get("ok"), r
        assert wait_until(lambda: devs(base)["1P"].get("running"), 20.0), \
            "実行中にならない"
        done = wait_until(
            lambda: not devs(base)["1P"].get("running"), 300.0, 0.25)
        assert done, "最後まで終わらない"
        d = devs(base)["1P"]
        assert d.get("state") in ("IDLE", "READY"), d.get("state")
        assert not d.get("error"), d.get("error")
        # 停止せずに終わったことだけでは、1 周で終わっても通る。終えた周の数と、
        # 装置が記録したフレーム数を見る
        wait_until(lambda: laps_done(base, "1P") == laps, 10.0)
        done = laps_done(base, "1P")
        assert done == laps, f"終えたのは {done} 周"
        built = compile_procedure(str(root), proc1, laps=laps)
        got = last_done_frames(base, "1P")
        full["frames"] = got
        want = expected_frames(built)
        if want is None:
            # 進む選択肢が決まらない選択待ちがある。選択肢の長さは 0 以上なので、
            # 選択肢を長さ 0 として数えた変換器の総フレーム数が下限になる
            assert got is not None and got >= built.total_frames, (
                f"実行したのは {got} フレーム(選択肢を除いて {built.total_frames})")
            return f"{laps} 周 = {got} フレーム"
        assert got == want, (
            f"実行したのは {got} フレーム(先頭の選択肢へ進むと {want})")
        return f"{laps} 周 = {want} フレーム"
    c.check(f"1P の手順が {laps} 周を最後まで実行できる",
            t_one_lane_runs_to_the_end)

    def t_stop_at_mark_and_resume():
        """区切りで停止して、「停止した位置」から続きを実行できること。

        人が選ぶ選択待ちを 1 つ代わりに選び、次の選択待ちは選ばずに置いたまま
        区切りで停止を送る。早送りでは、置いておかないと停止させる前に最後まで
        終わる。装置は、その選択待ちか、そこまでの区切りで停止する。どちらも停止した
        位置から始め直せる(選択待ちで停止したときは、その選択待ちからやり直す)。
        人が選ぶ選択待ちに 2 回以上着かない手順では行わない(この項目を呼ぶ所)。
        """
        ARMED.set()
        try:
            r = post(base, "/api/run",
                     {"plan": [{"dev": "1P", "name": proc1, "laps": laps}]})
            assert r.get("ok"), r
            assert wait_until(lambda: devs(base)["1P"].get("running"), 20.0)
            assert wait_until(HELD.is_set, 60.0), "人が選ぶ選択待ちに着かない"
            assert post(base, "/api/stop",
                        {"mode": "graceful", "dev": "1P"}).get("ok")
            assert wait_until(lambda: not devs(base)["1P"].get("running"),
                              120.0, 0.25), "区切りで停止しない"
        finally:
            ARMED.clear()
            HELD.clear()
        # 止まり方まで見る。ここを見ないと、予約せずに即時で停止させても通る
        assert devs(base)["1P"].get("stop_reason") == "at_mark", devs(base)
        at = devs(base)["1P"].get("stopped_at")
        assert at, f"停止した位置がない: {devs(base)['1P']}"
        # 開始位置は、停止した位置に付けてある開始位置の項目そのものである
        assert at.get("start"), f"そこから始め直せない: {at}"
        r = post(base, "/api/run",
                 {"plan": [{"dev": "1P", "name": proc1, "laps": laps,
                            "start": at["start"]}]})
        assert r.get("ok"), r
        assert wait_until(lambda: devs(base)["1P"].get("running"), 20.0)
        assert wait_until(lambda: not devs(base)["1P"].get("running"),
                          300.0, 0.25), "続きが最後まで終わらない"
        # 続きなので、先頭から始めたときより短いはずである。ここを見ないと、
        # 開始位置を捨てて先頭から実行し直しても通ってしまう
        whole = full["frames"]
        assert whole is not None, "先頭から実行したときの長さがない"
        got = last_done_frames(base, "1P")
        assert got is not None and got < whole, (
            f"続きが {got} フレーム実行した(先頭からは {whole})")
        return f"{place_of(at)}から {got} フレーム(先頭からは {whole})"
    # 早送りでは、17 万フレームでも数秒で終わるので、区切りで停止を送る前に最後まで
    # 終わる。停止を間に合わせるには、人の代わりに 1 つ選んだ後に、選ばずに
    # 置いておける選択待ちがもう 1 つ必要である(人が選ぶ選択待ちに 2 回以上着くこと)。
    # 選択待ちが最後の周にしかない手順や、1 周目にしかない手順では足りない
    stop_name = "区切りで停止し、「停止した位置」から続きを実行できる"
    if hand_waits(compile_procedure(str(root), proc1, laps=laps)) >= 2:
        c.check(stop_name, t_stop_at_mark_and_resume)
    else:
        c.skip(stop_name, "1P の手順で、人が選ぶ選択待ちに 2 回以上着かないため"
                          "行わない(早送りでは、停止させる前に最後まで終わる)")

    def t_two_lanes_as_a_group():
        r = post(base, "/api/run", {"plan": [
            {"dev": "1P", "name": proc1, "laps": laps},
            {"dev": "2P", "name": proc2, "laps": laps}]})
        assert r.get("ok"), r
        assert isinstance(r.get("skew_ms"), int), r
        assert wait_until(
            lambda: all(devs(base)[n].get("running") for n in ("1P", "2P")),
            20.0), devs(base)
        g = get(base, "/api/state")["groups"]
        assert len(g) == 1 and sorted(g[0]["lanes"]) == ["1P", "2P"], g
        return f"開始のずれ {r['skew_ms']}ms"
    c.check("2 台を 1 つのグループとして開始できる", t_two_lanes_as_a_group)

    def t_a_group_stop_stops_both():
        """グループの見出しの停止(グループの全レーンを指す)で、両方が停止すること。

        レーン 1 本の停止がそのレーンだけを停止させることは、
        pytest(test_lane_stop.py)が見る。ここで片方を先に停止させると、手順によっては
        相手がすぐ最後まで終わり、相手が動き続けたかを区別できない。
        """
        assert post(base, "/api/stop_lanes",
                    {"mode": "immediate", "devs": ["1P", "2P"]}).get("ok")
        assert wait_until(
            lambda: not any(d.get("running") or d.get("awaiting")
                            for d in devs(base).values()), 30.0), devs(base)
    c.check("グループの見出しの停止で、両方が停止する", t_a_group_stop_stops_both)


def main(argv: list) -> int:
    if len(argv) != 5:
        print(__doc__.strip())
        return 2
    out = Path(argv[1])
    src = Path(argv[2])
    proc1, proc2 = argv[3], argv[4]
    if not (src / "procedures").is_dir():
        print(f"{src} に procedures がありません", file=sys.stderr)
        return 2
    missing = [n for n in (proc1, proc2) if n not in Project(src).procedure_names()]
    if missing:
        print(f"{src} に手順がありません: {'、'.join(missing)}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_proj"
    # 作り直すのは、前回このツールが作ったコピーだけである
    if not rebuild_workdir(work):
        return 2
    # 元のフォルダーには書かない。コピーの上で動かす
    shutil.copytree(src, work, dirs_exist_ok=True)

    proj = Project(work)
    d1 = MockDevice(speed=SPEED, device_id=DEV_IDS["1P"])
    d2 = MockDevice(speed=SPEED, device_id=DEV_IDS["2P"])
    d1.start()
    d2.start()
    cfg = proj.load_config()
    cfg["devices"] = [
        {"id": DEV_IDS["1P"], "name": "1P", "host": "127.0.0.1",
         "port": d1.port},
        {"id": DEV_IDS["2P"], "name": "2P", "host": "127.0.0.1",
         "port": d2.port}]
    proj.save_config(cfg)
    # 【重要】実機と、この PC の画面・映像機器を使わない(理由は tools/_localonly.py)。
    # 渡した手順は、本物の映像機器やウィンドウを名前で指していることがある
    lock_to_mock(d1.port, d1.device_id)
    # 手順のスクリプトも実行しない。別のプロセスになるので、上の安全策が及ばない
    skipped = skip_scripts()

    srv = gui.make_server(proj)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    c = Checker()
    stop = threading.Event()
    picker = threading.Thread(target=pick_when_waiting, args=(base, stop),
                              daemon=True)
    picker.start()
    print(f"手順: {src}\n模擬装置: 早送り {SPEED:.0f} 倍\n")
    try:
        run_checks(c, base, proc1, proc2, 50, work)
    finally:
        stop.set()
        picker.join(timeout=3.0)
        srv.shutdown()
        srv.server_close()
        d1.stop()
        d2.stop()
    print(f"\n人の代わりに選択待ちで押した回数: {len(PICKED)}")
    commands = sorted(set(skipped.skipped))
    if commands:
        print(f"実行しなかったスクリプトのコマンド({len(skipped.skipped)} 回):")
        for cmd in commands:
            print(f"  {cmd}")
    return c.summary()


if __name__ == "__main__":
    # 日本語を出す。コンソールの文字コードで表せないと print が異常終了するので、
    # UTF-8 で出す(CI の英語の Windows がこれに当たる)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv))
