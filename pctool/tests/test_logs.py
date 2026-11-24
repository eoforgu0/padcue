"""ログの中身が「実際に起きたこと」を反映しているかを確認する。

固定の文言が出ることだけを見ると、値が常に同じでも通ってしまう。ここでは、条件を
変えると値も変わることを確かめる。
 - 停止するまでの時間を変えると、「N フレーム時点」も変わる
 - 種別ごとに引数の意味が仕様どおりである(完了なら総フレーム、遅延なら回数と最大)
 - PC 側は、受信時刻を付け、世代へ送り、合計の上限を超えたときだけ古い世代を消す
"""
import json
import threading
import time
import urllib.parse

import pytest

from padcue import binfmt, runlog
from padcue.client import DeviceClient
from padcue.mockdevice import MockDevice
from padcue.project import Project
from padcue.runlog import RunLog
from tests.helpers import compile_nodes, get, lap_procedure, new_logs, own, wait_until


@pytest.fixture
def dev():
    d = MockDevice(speed=200.0, host="127.0.0.1")   # 200 倍速で待たずに試す
    d.start()
    yield d
    d.stop()


def _client(dev):
    c = DeviceClient("127.0.0.1", dev.port, timeout=5.0)
    c.connect()
    own(c)          # 転送と実行は操作権の所有者しか行えない
    return c


def _nodes(wait_frames):
    """A を 2F 押して待つだけの手順(押した瞬間と長さだけが違う)。"""
    return [{"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "wait", "frames": wait_frames}]


def _push(c, name, nodes, laps=1):
    """手順を転送する。周回数は手順の中の SETCNT に入るので、
    「何周実行するか」はここで(実行のときではなく)決まる。"""
    comp = compile_nodes(nodes, name, laps=laps)
    blob = binfmt.encode(comp.events, comp.total_frames)
    h = c.put(blob)
    c.commit(h)
    return h, comp.total_frames


def _kinds(entries):
    return [e["kind"] for e in entries]


def _first(entries, kind):
    for e in entries:
        if e["kind"] == kind:
            return e
    raise AssertionError(f"{kind} のログがない: {_kinds(entries)}")


def test_abort_frame_reflects_when_stopped(dev):
    """中断の記録の「N フレーム時点」が、停止するまでに動いた時間に合うこと。

    「最後に状態が変化したフレーム」を記録する作りだと、いつ停止しても同じ値に
    なる。停止するまでの時間を変えて 3 回実行し、記録の値が、測った時間から
    見積もったフレーム数の範囲に入ることを見る。範囲の下は sleep で待った時間、
    上は開始を送る前から停止が返るまでの時間である。sleep が延びても、範囲が
    一緒に広がるだけで外れない。手順は 200 倍速でも約 5 秒かかる長さにして、
    停止する前に完了しないようにする。
    """
    c = _client(dev)
    h, total = _push(c, "長い手順", _nodes(60000))
    new_logs(c)                                   # 起動ログを読み捨てる
    per_s = dev.speed * 1e9 / dev.frame_period_ns    # 実時間 1 秒あたりのフレーム数
    # 時計の分解能の余裕(Python 3.12 までの Windows の time.monotonic は約 16 ミリ秒
    # ごとに進む)。模擬装置も同じ時計で数える
    tick = 0.016
    for wait_s in (0.05, 0.20, 0.40):
        t0 = time.monotonic()
        c.run(h)
        time.sleep(wait_s)
        c.stop()
        ran = time.monotonic() - t0
        a = _first(new_logs(c), "RUN_ABORT")["a"]
        low, high = (wait_s - tick) * per_s, (ran + tick) * per_s
        assert low <= a <= high, (
            f"{wait_s} 秒眠って停止させたのに {a} フレーム時点の記録"
            f"(見込みは {low:.0f}〜{high:.0f})")
        assert a < total, (a, total)


def test_run_done_reports_total_frames(dev):
    """完了ログの a は「その実行の総フレーム」であること。"""
    c = _client(dev)
    h, total = _push(c, "短い手順", _nodes(30), laps=3)
    new_logs(c)
    c.run(h)
    assert wait_until(lambda: not c.status()["running"], 10), \
        "実行が終わらない"
    e = _first(new_logs(c), "RUN_DONE")
    assert total == 32 * 3          # 3 周ぶんが手順の総フレーム数に入っている
    assert e["a"] == total, (e, total)


def test_state_log_records_transition(dev):
    """状態ログの a→b が、実際の遷移(待機中→実行中)になっていること。"""
    c = _client(dev)
    h, _ = _push(c, "短い手順", _nodes(30))
    new_logs(c)
    c.run(h)
    entries = []

    def states():
        entries.extend(new_logs(c))
        return [e for e in entries if e["kind"] == "STATE"]
    st = wait_until(states, 5)
    assert st, _kinds(entries)
    # 2=IDLE, 3=RUNNING(firmware/main/app_state.h の並び)
    assert any(e["a"] == 2 and e["b"] == 3 for e in st), st


def test_log_kinds_are_known_to_the_gui():
    """実機が出しうる種別が、画面の日本語化にすべて含まれていること。

    含まれていない種別は生の英字のまま出てしまう(読めない)。
    """
    import re

    from padcue.gui import web_asset
    js = web_asset("lists.js")
    m = re.search(r"const LOG_JA = \{(.*?)\n\};", js, re.S)
    assert m, "LOG_JA が見つかりません"
    ja = set(re.findall(r"^\s{2}([A-Z_]+):", m.group(1), re.M))
    # 種別はファームウェアが増やす。ここに転記すると、増えた種別が
    # 「転記した表にもない」ので警告なしにテストから漏れる。
    # 定義元(app_log.c の KIND_NAMES)から読む
    from tests.conftest import REPO
    src = (REPO / "firmware" / "main" / "app_log.c").read_text(encoding="utf-8")
    body = re.search(r"KIND_NAMES\[APP_LOG_KIND_MAX\] = \{(.*?)\};", src, re.S)
    assert body, "app_log.c の KIND_NAMES が見つかりません"
    fw = set(re.findall(r'"([A-Z_]+)"', body.group(1)))
    assert len(fw) >= 16, f"種別を読み取れません: {sorted(fw)}"
    assert fw <= ja, f"日本語化されていない種別: {sorted(fw - ja)}"


# ---- PC 側の蓄積 ----

def test_logs_are_kept_and_timestamped(tmp_path):
    """装置は受け取った記録を消すので、PC 側に受信時刻つきで残すこと。"""
    p = Project(tmp_path)
    before = time.time()
    p.logs.append([{"t_ms": 12, "kind": "BOOT", "a": 0, "b": 0}])
    p.logs.append([{"t_ms": 99, "kind": "RUN_START", "a": 0, "b": 0}])
    got = p.logs.read()
    assert _kinds(got) == ["BOOT", "RUN_START"]     # 古い順
    assert all(before <= e["at"] <= time.time() + 1 for e in got), got


def test_clearing_removes_every_generation(tmp_path):
    """消す操作で、世代へ送った分も含めて記録が消えること。

    世代へ送ること自体は test_logs_are_kept_in_generations_instead_of_being_dropped
    が見る。
    """
    p = Project(tmp_path)
    total = p.logs.LOG_KEEP * 2 + 10
    p.logs.append([{"t_ms": i, "kind": "BOOT", "a": i, "b": 0}
                   for i in range(total)])
    gens = p.logs.generations()
    assert gens, "世代へ送られていない"
    p.logs.clear()
    assert p.logs.generations() == []
    assert p.logs.read(limit=10**6) == []


def test_read_logs_limit(tmp_path):
    p = Project(tmp_path)
    p.logs.append([{"t_ms": i, "kind": "BOOT", "a": i, "b": 0}
                   for i in range(50)])
    got = p.logs.read(limit=10)
    assert len(got) == 10
    assert [e["a"] for e in got] == list(range(40, 50)), "末尾 10 件でない"


def test_broken_log_line_is_skipped(tmp_path):
    p = Project(tmp_path)
    p.logs.append([{"t_ms": 1, "kind": "BOOT", "a": 0, "b": 0}])
    with p.logs.path().open("a", encoding="utf-8") as f:
        f.write("これは JSON ではない\n")
    p.logs.append([{"t_ms": 2, "kind": "WIFI_UP", "a": 0, "b": 0}])
    assert _kinds(p.logs.read()) == ["BOOT", "WIFI_UP"]


def test_run_start_records_which_procedure(dev):
    """開始ログに、どの手順か(id)が残ること。

    ログには文字列を含められないため、id は b/c に先頭 64bit(id[:16])だけを
    入れる(ログの枠は 64bit で、id は 128bit のため)。その照合が成立することを見る。
    周回数は手順のレコードの中にあり、装置は周回を数えないので、開始ログには
    含まれない(a は 0)。何周したかは、PC が STATUS のカウンターと対応表から出す。
    """
    c = _client(dev)
    h, _ = _push(c, "短い手順", _nodes(30), laps=7)
    new_logs(c)
    c.run(h)
    c.stop()
    assert wait_until(lambda: not c.status()["running"], 10), \
        "実行が終わらない"
    e = _first(new_logs(c), "RUN_START")
    assert e["a"] == 0, e
    assert f"{e['b']:08x}{e['c']:08x}" == h[:16], (e, h)


def test_finish_logs_carry_the_last_boundary(dev):
    """終了ログが、周回ではなく最後に通った区切りを持つこと。

    何周したかは、PC がこの番号と変換の対応表から出す。周回数を記録に詰めると、
    画面の入力欄が受け取る周回数(70000 周など)を記録の枠で表せない。
    """
    c = _client(dev)
    new_logs(c)
    h3, _ = _push(c, "短い手順3周", _nodes(30), laps=3)
    c.run(h3)
    assert wait_until(lambda: not c.status()["running"], 10), \
        "実行が終わらない"
    e = _first(new_logs(c), "RUN_DONE")
    assert e["a"] > 0, e            # 経過フレーム数は残る
    last = c.status()["last_mark"]
    assert last is not None, "最後に通った区切りが残っていない"
    assert e["c"] == last["index"], (e, last)


def test_a_released_reservation_leaves_no_abort_log(dev):
    """「区切りで停止」の予約を解除したら、その周の境では停止しないこと。

    解除が間に合えば、その先の周の境を越えて動き続け、中断のログも残らない。
    予約し直せば次の周の境で停止し、そのフレームがログに残る。
    """
    lap = 24000                     # 200 倍速なので 1 周が実時間 2 秒
    c = _client(dev)
    h = c.put(lap_procedure(binfmt.INFINITE_COUNT, lap))
    c.commit(h)
    new_logs(c)
    c.run(h)
    c.stop_at_mark(binfmt.MARK_LAP)
    c.stop_at_mark(release=True)

    end = time.time() + 10.0
    while time.time() < end and c.status()["frames_elapsed"] <= lap:
        time.sleep(0.02)
    st = c.status()
    assert st["running"] is True, "解除したのに周の境で停止した"
    assert st["frames_elapsed"] > lap, st
    kinds = _kinds(new_logs(c))
    assert "RUN_ABORT" not in kinds, f"停止していないのに中断のログがある: {kinds}"

    c.stop_at_mark(binfmt.MARK_LAP)
    end = time.time() + 10.0
    while time.time() < end and c.status()["running"]:
        time.sleep(0.02)
    assert c.status()["stop_reason"] == "at_mark"
    e = _first(new_logs(c), "RUN_ABORT")
    assert e["a"] % lap == 0, e     # 周の境で停止している
    assert e["a"] >= lap * 2, e     # 解除したぶん、1 周ぶん先で停止している


def test_logs_are_kept_in_generations_instead_of_being_dropped(tmp_path):
    """記録は捨てずに、世代を分けて残すこと。

    書き込み中のファイルが上限の行数を超えたら、日時を付けた名前へ変えて
    新しいファイルから書き始める。古い行が警告なしに消えると、長時間の実行を
    2 回続けただけで 1 回目の記録が残らない。
    """
    p = Project(tmp_path)
    p.logs.LOG_KEEP = 20                    # テストでは小さくして境目を作る
    for i in range(30):
        p.logs.append([{"t_ms": i, "kind": "BOOT", "a": i, "b": 0}])
    # 世代が 1 つ生まれ、書き込み中のファイルには続きが入る
    gens = p.logs.generations()
    assert len(gens) == 1, gens
    old = [json.loads(x) for x in
           gens[0].read_text(encoding="utf-8").splitlines()]
    now = [json.loads(x) for x in
           p.logs.path().read_text(encoding="utf-8").splitlines()]
    assert [e["a"] for e in old] == list(range(21)), old[:3]
    assert [e["a"] for e in now] == list(range(21, 30)), now[:3]
    # 読むときは世代をまたいで、新しい方から読む(世代を分けた直後でも、
    # 画面に出る記録が数行に減らない)
    assert [e["a"] for e in p.logs.read()] == list(range(30))


def test_old_generations_are_dropped_only_past_the_total_size(tmp_path):
    """世代の合計が上限を超えたときだけ、古い世代から消すこと。"""
    p = Project(tmp_path)
    p.logs.LOG_KEEP = 10
    p.logs.LOG_TOTAL_MAX = 200              # 数世代で超える大きさにする
    for i in range(60):
        p.logs.append([{"t_ms": i, "kind": "BOOT", "a": i, "b": 0}])
    gens = p.logs.generations()
    assert len(gens) == 1, gens        # 最も新しい世代は必ず残る
    # それより古い世代は、合計が上限を超えたぶんだけ消える
    assert sum(f.stat().st_size for f in gens[:-1]) <= p.logs.LOG_TOTAL_MAX


def test_going_back_reads_older_records_page_by_page(tmp_path):
    """「以前のログを表示」は、押すたびに 1 頁ずつ古い方を読むこと。

    書き込み中のファイルは上限の行数まで世代を分けない。世代の数でさかのぼると、
    書き込み中のファイルが 1 頁を超えた後は、押しても中身が変わらない。
    残りがあるかどうかも、実際に古い行が残っているかで答える。
    """
    p = Project(tmp_path)
    p.logs.LOG_KEEP = 40
    for i in range(70):                # 世代 1 つ(41 行)と書き込み中(29 行)
        p.logs.append([{"t_ms": i, "kind": "BOOT", "a": i, "b": 0}])
    assert len(p.logs.generations()) == 1
    got = p.logs.read_page(10)
    assert [e["a"] for e in got.entries] == list(range(60, 70)) and got.more
    assert got.count == 10
    got = p.logs.read_page(10, count=got.count, mark=got.mark, older=True)
    assert [e["a"] for e in got.entries] == list(range(50, 70)) and got.more
    for _ in range(3):
        got = p.logs.read_page(10, count=got.count, mark=got.mark, older=True)
    assert [e["a"] for e in got.entries] == list(range(20, 70)) and got.more
    for _ in range(2):
        got = p.logs.read_page(10, count=got.count, mark=got.mark, older=True)
    assert [e["a"] for e in got.entries] == list(range(70)) and not got.more
    got = p.logs.read_page(70)
    assert len(got.entries) == 70 and not got.more


def test_a_mark_from_another_run_reads_the_newest_records(tmp_path):
    """起動し直した padcue(別の RunLog)は、前の起動の印から足された行を数えず、
    新しい方から同じ行数を読むこと。

    印は、その起動で書いた行の数である。起動をまたいで数えると、読む範囲を
    間違える。
    """
    p = Project(tmp_path)
    p.logs.append([{"kind": "BOOT", "a": i} for i in range(30)])
    first = p.logs.read_page(10)
    p.logs.append([{"kind": "BOOT", "a": i} for i in range(30, 33)])
    got = RunLog(p).read_page(10, count=first.count, mark=first.mark)
    assert [e["a"] for e in got.entries] == list(range(23, 33)), got.entries


def test_reading_from_the_newest_keeps_at_least_a_page(tmp_path):
    """印を渡さずに読むとき(画面の「自動スクロール」が入っている間)は、前に読んだ
    行が 1 頁より少なくても、新しい方から 1 頁は読むこと。

    前に読んだ行の数だけを読むと、記録が少なかったとき(削除した直後など)の
    行数のままになり、その後に足された行が出ない。
    """
    p = Project(tmp_path)
    p.logs.append([{"kind": "BOOT", "a": 0}])
    first = p.logs.read_page(10)
    assert first.count == 1
    p.logs.append([{"kind": "BOOT", "a": i} for i in range(1, 6)])
    got = p.logs.read_page(10, count=first.count)
    assert [e["a"] for e in got.entries] == list(range(6)), got.entries


def _log_page(base, **query):
    """/api/logs を読み、(応答, BOOT の行の (装置, a) の並び) を返す。"""
    got = get(base, "/api/logs?" + urllib.parse.urlencode(
        {k: v for k, v in query.items() if v is not None}))
    rows = [(e.get("dev", ""), e["a"]) for g in got["laps"] for e in g["entries"]
            if e.get("kind") == "BOOT"]
    return got, rows


def test_reading_older_records_keeps_them_when_new_ones_come(proj, server):
    """「以前のログを表示」で読み込んだ古い端は、新しい行が来ても動かないこと。

    画面は、読み込んでいる行の数(count)と、そのときの印(mark)を送る。読み込む
    範囲を新しい方からの行数だけで決めると、新しい行が来るたびに古い端がずれ、
    古いログを読んでいる人の見ている行が上から消える。書き込み中のファイルが
    世代に分かれても(名前が変わっても)同じである。
    """
    proj.logs.LOG_KEEP = 15            # 足す間に世代を分ける
    proj.logs.append([{"kind": "BOOT", "a": i} for i in range(30)])
    first, rows = _log_page(server, limit=10)
    assert [a for _, a in rows] == list(range(20, 30)), rows
    older, rows = _log_page(server, limit=10, count=first.get("count"),
                            mark=first.get("mark"), older=1)
    assert [a for _, a in rows] == list(range(10, 30)), rows
    gens = len(proj.logs.generations())
    proj.logs.append([{"kind": "BOOT", "a": i} for i in range(30, 46)])
    assert len(proj.logs.generations()) > gens, "前提: 世代が分かれていない"
    _later, rows = _log_page(server, limit=10, count=older.get("count"),
                             mark=older.get("mark"))
    assert [a for _, a in rows] == list(range(10, 46)), rows


def test_going_back_with_a_device_filter_reads_until_that_device_has_more(
        proj, server):
    """装置で絞り込んで「以前のログを表示」を押すと、その装置の行が増えるまで(または
    もう前がないところまで)読むこと。絞り込んでいなければ 1 頁だけ読むこと。

    1 頁は装置によらない行数なので、ほかの装置の行ばかりの頁を読むと、絞り込んだ
    欄には何も足されず、押しても何も起きないように見える。
    """
    one, other = "aaaa0000000a", "aaaa0000000b"
    proj.logs.append([{"kind": "BOOT", "a": i} for i in range(5)], dev=one)
    proj.logs.append([{"kind": "BOOT", "a": i} for i in range(5, 45)], dev=other)
    proj.logs.append([{"kind": "BOOT", "a": 45}], dev=one)
    first, rows = _log_page(server, limit=10)
    assert [a for d, a in rows if d == one] == [45], rows
    got, rows = _log_page(server, limit=10, count=first.get("count"),
                          mark=first.get("mark"), older=1, dev_ids=one)
    assert [a for d, a in rows if d == one] == [0, 1, 2, 3, 4, 45], rows
    assert not got["more"], got["more"]
    got, rows = _log_page(server, limit=10, count=first.get("count"),
                          mark=first.get("mark"), older=1)
    assert [a for _, a in rows] == list(range(26, 46)), rows


def _merged(laps, got):
    """画面と同じく、/api/logs の応答 got を、受け取っていたまとまり laps に適用する
    (lists.js の mergeLogs)。

    続きの応答(delta)は、丸ごと変わったまとまりと新しいまとまりを laps に id で持ち
    (同じ id があれば置き換え、なければ後ろに足す)、行を足しただけのまとまりを add に、
    消えたまとまりの id を drop に挙げる。足した行も時刻の順に並べる。続きでない
    応答は、すべてを置き換える。
    """
    if not got.get("delta"):
        return list(got["laps"])
    gone = set(got.get("drop") or [])
    fresh = {g["id"]: g for g in got["laps"]}
    grew = {x["id"]: x["entries"] for x in got.get("add") or []}
    out = []
    for g in laps:
        if g["id"] in gone:
            continue
        if g["id"] in fresh:
            out.append(fresh.pop(g["id"]))
        elif g["id"] in grew:
            entries = sorted([*g["entries"], *grew[g["id"]]],
                             key=lambda x: x.get("at") or 0)
            out.append({**g, "entries": entries, "at": entries[0].get("at")})
        else:
            out.append(g)
    return out + list(fresh.values())


def _sent_rows(got):
    """応答が運んだ BOOT の行の a(新しいまとまりと、まとまりに足した行)。"""
    return [e["a"] for g in [*got["laps"], *(got.get("add") or [])]
            for e in g["entries"] if e.get("kind") == "BOOT"]


def test_each_second_reads_only_the_lines_after_the_last_read(proj, server):
    """毎秒の取得は、自動スクロールのチェックが入っていても外れていても、前に受け取った
    後の新しい行だけを読んで返すこと。読み込み済みの行の数によらないこと。

    読み込んだ範囲をそのたびに読み直すと、「以前のログを表示」でさかのぼった分だけ、
    毎秒の読み込みが重くなる(チェックを外したままなら、新しい行の分だけ増え続ける)。
    続きを読めない応答の印(view)を渡したときは、範囲の全体を読み直して返す。
    """
    proj.logs.LOG_KEEP = 15            # 読み込んだ範囲を世代にまたがらせる
    proj.logs.append([{"kind": "BOOT", "a": i} for i in range(40)])
    first, _rows = _log_page(server, limit=10)
    older, rows = _log_page(server, limit=10, count=first["count"],
                            mark=first["mark"], older=1)
    assert [a for _, a in rows] == list(range(20, 40)) and older.get("view"), rows
    laps, view, mark = older["laps"], older["view"], older["mark"]
    # 画面は、チェックが外れている間だけ印(mark)を送る
    for follow, new in ((False, [40, 41, 42]), (True, [43])):
        proj.logs.append([{"kind": "BOOT", "a": a} for a in new])
        got, _rows = _log_page(server, limit=10, count=older["count"], view=view,
                               mark=None if follow else mark)
        assert got.get("delta") and _sent_rows(got) == new, (follow, got)
        assert got.get("added") == len(new), got
        laps, view, mark = _merged(laps, got), got["view"], got["mark"]
    assert [e["a"] for g in laps for e in g["entries"]] == list(range(20, 44))
    # 続きを読めない印は、範囲の全体を読み直す
    got, rows = _log_page(server, limit=10, count=24, view="知らない印")
    assert not got.get("delta") and [a for _, a in rows] == list(range(20, 44)), rows


# 1 行 1 周のまとめ方の場合分けを通る記録の並び(test_runrecord.py の group_by_lap の
# テストと同じ形)。周の行、同じ周の二度書き、2 台の周、周を持たない行がほかの装置の
# 行で分かれること、終わりの行の並び、前のまとまりの行を周の行のまとまりへ移すこと
# (移されて空になったまとまりは消える)、開始の行が前の実行のまとまりを閉じること
_FOLD_CASES = [
    [{"kind": "PC_LAP", "a": 3, "dev": "A", "at": 1.0},
     {"kind": "LATE_EVENT", "dev": "A", "at": 1.5},
     {"kind": "PC_LAP", "a": 3, "dev": "A", "at": 2.0},
     {"kind": "PC_LAP", "a": 4, "dev": "A", "at": 3.0}],
    [{"kind": "PC_LAP", "a": 1, "dev": "A", "at": 1.0},
     {"kind": "PC_LAP", "a": 1, "dev": "B", "at": 1.1},
     {"kind": "LATE_EVENT", "dev": "A", "at": 1.2},
     {"kind": "PC_LAP", "a": 2, "dev": "B", "at": 1.3},
     {"kind": "BOOT", "dev": "B", "at": 1.4}],
    [{"kind": "BOOT", "dev": "A", "at": 1.0},
     {"kind": "BOOT", "dev": "A", "at": 1.5},
     {"kind": "BOOT", "dev": "B", "at": 2.0},
     {"kind": "PC_SET_START", "dev": "A", "at": 9.0},
     {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 9.5}],
    [{"kind": "PC_LAP", "a": 1, "dev": "A", "at": 1.0},
     {"kind": "STATE", "a": 3, "b": 4, "dev": "A", "at": 2.0},
     {"kind": "STATE", "a": 4, "b": 3, "dev": "A", "at": 3.4},
     {"kind": "RUN_DONE", "dev": "A", "at": 3.4},
     {"kind": "PC_RUN_END", "a": 1, "dev": "A", "at": 3.0},
     {"kind": "USB_UMOUNT", "dev": "A", "at": 3.6}],
    [{"kind": "PC_LAP", "a": 17, "dev": "A", "at": 1.0},
     {"kind": "STATE", "dev": "A", "at": 2.0},
     {"kind": "USB_SUSPEND", "dev": "A", "at": 5.5},
     {"kind": "PC_LAP", "a": 18, "dev": "A", "at": 5.0},
     {"kind": "PC_RUN_END", "a": 17, "dev": "A", "at": 5.4}],
    [{"kind": "BOOT", "dev": "A", "at": 5.0},
     {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 2.0},
     {"kind": "STATE", "dev": "A", "at": 6.0}],
    [{"kind": "PC_LAP", "a": 1, "dev": "A", "at": 1.0},
     {"kind": "STATE", "dev": "A", "at": 2.0},
     {"kind": "PC_SET_START", "dev": "A", "at": 3.0},
     {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 4.0}],
]


@pytest.mark.parametrize("rows", _FOLD_CASES)
def test_reading_on_from_any_line_groups_like_reading_at_once(tmp_path, rows):
    """どの行で区切って続きを読んでも、画面が適用した結果が、全部を一度に読んでまとめた
    結果と同じになること。

    続きの応答は、受け取っていたまとまりのうち、新しい行で変わったものと消えたものと、
    新しいまとまりだけを返す。周の途中の行は前に受け取ったまとまりに入り、周の行は
    前のまとまりの行を自分のまとまりへ移す。新しい行だけをまとめると、そうならない。
    """
    for cut in range(len(rows) + 1):
        (tmp_path / f"cut{cut}").mkdir()
        p = Project(tmp_path / f"cut{cut}")
        p.logs.append(rows[:cut])
        got = p.logs.read_view()
        laps = _merged([], got)
        p.logs.append(rows[cut:])
        got = p.logs.read_view(count=got["count"], view=got["view"])
        assert got.get("delta"), cut
        laps = [{k: v for k, v in g.items() if k != "id"} for g in _merged(laps, got)]
        assert laps == RunLog.group_by_lap(p.logs.read(100)), cut


def test_a_lap_read_later_takes_the_lines_split_by_another_device(tmp_path):
    """周の行を続きで受け取っても、ほかの装置の行で区切られた、その周に入った時刻より
    後の同じ装置の行を自分のまとまりへ移すこと。移されうる行のまとまりは、自動
    スクロールで古い行を落とすときに残すまとまり(open)に入ること。

    グループで、監視が 1 周目を初めて読む前に周回の終え方を選ぶと、1 周目の行は周回数の
    変更の行より後に、周に入った時刻で書かれる。周回数の変更の行は 2 台ぶん続けて
    書かれるので、間に 2P の行が入る。
    """
    rows = [{"kind": "PC_SET_START", "dev": "A", "at": 12.0},
            {"kind": "PC_SET_START", "dev": "B", "at": 12.0},
            {"kind": "RUN_START", "dev": "A", "at": 12.9},
            {"kind": "PC_LAP", "a": 1, "dev": "B", "at": 12.5},
            {"kind": "PC_LAPS", "dev": "A", "at": 14.3},
            {"kind": "PC_LAPS", "dev": "B", "at": 14.3},
            {"kind": "STATE", "dev": "A", "at": 14.5},
            {"kind": "BOOT", "dev": "B", "at": 14.6},
            {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 12.8},
            {"kind": "PC_RUN_END", "a": 1, "dev": "A", "at": 19.0}]
    want = [("A", None, ["PC_SET_START"]), ("B", None, ["PC_SET_START"]),
            ("B", 1, ["PC_LAP", "PC_LAPS", "BOOT"]),
            ("A", 1, ["PC_LAP", "RUN_START", "PC_LAPS", "STATE", "PC_RUN_END"])]
    lap_at = next(i for i, e in enumerate(rows)
                  if e["kind"] == "PC_LAP" and e["dev"] == "A")
    for cut in range(len(rows) + 1):
        (tmp_path / f"cut{cut}").mkdir()
        p = Project(tmp_path / f"cut{cut}")
        p.logs.append(rows[:cut])
        got = p.logs.read_view()
        if cut == lap_at:
            # まだ周の行のない A の行のまとまり(開始の行のほか)は、どれも変わりうる
            loose = {g["id"] for g in got["laps"] if g["dev"] == "A"
                     and g["entries"][0]["kind"] != "PC_SET_START"}
            assert len(loose) == 3 and loose <= set(got["open"]), got
        laps = _merged([], got)
        p.logs.append(rows[cut:])
        got = p.logs.read_view(count=got["count"], view=got["view"])
        laps = _merged(laps, got)
        assert [(g["dev"], g["lap"], [e["kind"] for e in g["entries"]])
                for g in laps] == want, (cut, laps)


def test_dropping_old_generations_makes_the_screen_read_it_all_again(tmp_path):
    """古い世代を消したら、続きを読みに来た欄にも、範囲の全体を読み直させること。

    続きだけを返すと、画面は消えた行を出し続け、先頭の「…より前のログは削除済み
    です」と矛盾する。
    """
    p = Project(tmp_path)
    p.logs.LOG_KEEP = 10
    p.logs.LOG_TOTAL_MAX = 200              # 数世代で超える大きさにする
    for i in range(30):
        p.logs.append([{"kind": "BOOT", "a": i}])
    got = p.logs.read_view(100)
    for i in range(30, 60):
        p.logs.append([{"kind": "BOOT", "a": i}])
    assert p.logs.dropped(), "前提: 古い世代が消えていない"
    got = p.logs.read_view(100, count=got["count"], view=got["view"])
    assert not got.get("delta"), got


@pytest.mark.parametrize("mine", [False, True], ids=["theirs", "both"])
def test_lines_another_process_added_make_the_screen_read_it_all_again(tmp_path, mine):
    """ほかのプロセス(操作画面のサーバーと別に動かした padcue logs --host など)が同じ
    フォルダーの記録に行を足したら、続きを読みに来た欄に範囲の全体を読み直させ、どの
    行も落とさないこと。その後に自分も行を書いたとき(both)も同じである。

    続きは、自分が書いた行の数で読む。ほかのプロセスが書いた行はその数に入らないので、
    続きだけを返すと、その行が欄に出ないか、自分が書いた行の代わりに読まれる。
    """
    p = Project(tmp_path)
    p.logs.append([{"kind": "BOOT", "a": 0}, {"kind": "BOOT", "a": 1}])
    got = p.logs.read_view(100)
    laps = _merged([], got)
    RunLog(p).append([{"kind": "BOOT", "a": 2}, {"kind": "BOOT", "a": 3}])
    if mine:
        p.logs.append([{"kind": "BOOT", "a": 4}])
    got = p.logs.read_view(100, count=got["count"], mark=got["mark"], view=got["view"])
    assert not got.get("delta"), got
    laps = _merged(laps, got)
    assert [e["a"] for g in laps for e in g["entries"]] == list(range(5 if mine else 4))
    # 読み直した後は、また続きを読める
    p.logs.append([{"kind": "BOOT", "a": 9}])
    got = p.logs.read_view(100, count=got["count"], view=got["view"])
    assert got.get("delta") and _sent_rows(got) == [9], got


def test_generations_split_in_the_same_second_keep_their_order(tmp_path, monkeypatch):
    """同じ秒に世代を 2 つ分けても、古い順に並ぶこと。

    世代の名前の時刻の後ろに付ける番号は、桁を揃えて最初の世代から付ける。
    付けない名前(logs-<時刻>.jsonl)と付けた名前(logs-<時刻>-1.jsonl)を名前の
    順に並べると、「-」が「.」より前なので、後に作った世代が前に来る。
    """
    monkeypatch.setattr(runlog.time, "strftime", lambda *a: "20261107-120000")
    p = Project(tmp_path)
    p.logs.LOG_KEEP = 5
    for i in range(18):                # 世代が 2 つできる
        p.logs.append([{"kind": "BOOT", "a": i}])
    assert len(p.logs.generations()) == 2, p.logs.generations()
    assert [e["a"] for e in p.logs.read()] == list(range(18))


def _held_open(path, seconds: float):
    """path をほかのスレッドが読んでいる間(seconds 秒)を作る。Windows は、開いて
    いるファイルの名前の変更と削除を拒否する。"""
    reader = path.open(encoding="utf-8")
    threading.Timer(seconds, reader.close).start()
    return reader


def test_clearing_waits_for_a_reader(tmp_path):
    """ログの削除が、ほかのスレッドの読み出しと重なっても、少し待って消し直すこと。

    やり直さないと、画面の「ログを削除」が想定外のエラーで終わる。
    """
    p = Project(tmp_path)
    p.logs.append([{"kind": "BOOT"}])
    reader = _held_open(p.logs.path(), 0.1)
    try:
        p.logs.clear()
    finally:
        reader.close()
    assert not p.logs.path().exists()


def test_splitting_a_generation_waits_for_a_reader(tmp_path):
    """世代を分ける名前の変更が、ほかのスレッドの読み出しと重なっても、少し待って
    やり直すこと。

    諦めると、次に整理するのは LOG_KEEP 件の後になり、書き込み中のファイルが
    その分だけ大きくなる。
    """
    p = Project(tmp_path)
    p.logs.LOG_KEEP = 5
    for i in range(5):
        p.logs.append([{"kind": "BOOT", "a": i}])
    reader = _held_open(p.logs.path(), 0.1)
    try:
        p.logs.append([{"kind": "BOOT", "a": 5}])  # 6 行目で世代を分ける
    finally:
        reader.close()
    assert len(p.logs.generations()) == 1, p.logs.generations()
