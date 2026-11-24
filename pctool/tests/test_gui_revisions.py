"""リビジョンと、保存していない中身の検証。

守りたい規則:
 - 画面に出す事実は元が変わったら追従できる。そのために、手順とフレーム表と
    条件のリビジョンを配信する。手順のリビジョンは、装置へ送る中身が同じ変更(ラベル名
    だけ)や、参照している条件の有無でも変わる
 - 編集画面が保存する前の中身も、保存と同じ検証で失敗と警告が分かる。
    検証はファイルを変えない
 - 開いた後にほかで変わった物を、黙って上書きしない
 - ない条件を指す選択肢は、変換の警告と同じ場所に警告として出る
 - 図は画面の解像度より細かく作らない(値の変化ごとに区間を作ると、
    長い手順で画面が固まる)
 - 変換と図のキャッシュは、そのフォルダーのいまの中身の分だけを持ち、渡した先で
    書き込まれても変わらない
"""
import base64
import json
import urllib.parse
import urllib.request

import pytest

from padcue import gui, timeline
from padcue.conditions import ConditionStore
from padcue.frames import encode_png
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from tests.helpers import GRAY_PNG, add_images, get, manual_arm, post


def q(name):
    return urllib.parse.quote(name)


def proc(state, name):
    return next(p for p in state["procedures"] if p["name"] == name)


def branch_on(cond):
    arm = {"name": "出た", "what": {"kind": "condition", "ref": cond},
           "body": [{"type": "wait", "frames": 5}]}
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "branch", "id": "br-1", "arms": [arm, manual_arm("出ない")]}]}


def make_condition(proj, name):
    store = ConditionStore(proj.root)
    store.save(name, {"kind": "desktop"}, [(0.0, 0.0, 1.0, 1.0)], 88)
    add_images(store, name, refs=[GRAY_PNG])


# ---- 保存していない中身の検証 ----


def test_an_unsaved_doc_is_checked_without_touching_the_file(server, proj):
    """保存前の中身を、保存と同じ検証にかけられること。ファイルは変わらない。"""
    before = proj.procedure_path("サンプル").read_bytes()
    broken = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 5}, {"type": "frametable", "ref": "ない表"}]}
    r = post(f"{server}/api/procedure/check", {"name": "サンプル", "doc": broken})
    at = r["error_at"]
    assert at["path"] == "body[1]" and "ない表" in at["msg"], r
    assert r["warnings"] == []
    warned = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 30},
        {"type": "gyro", "gp": 0, "gy": 2000, "gr": 0, "frames": 200}]}
    r = post(f"{server}/api/procedure/check", {"name": "サンプル", "doc": warned})
    assert r["error_at"] is None
    assert [w["line"] for w in r["warnings"]] == [2], r
    assert proj.procedure_path("サンプル").read_bytes() == before


def test_the_list_and_the_check_say_whether_the_length_has_waits(server, proj):
    """手順の一覧と保存前の検証が、待ちのある手順かを返すこと。

    待ちのある手順の長さは、待つ時間と、選ばれるまで分からない選択肢の中身を
    数えない下限である。画面は長さに「〜」を付けて下限だと示すので、一覧と、
    編集の間に見出しが使う検証の応答の両方に要る(片方だけだと、編集を始めた
    とたんに見出しの「〜」が消える)。
    """
    waits = branch_on("ない条件")
    plain = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 30}]}
    proj.save_procedure_doc("待つ手順", waits)
    proj.save_procedure_doc("待たない手順", plain)
    st = get(f"{server}/api/state")
    assert proc(st, "待つ手順")["has_waits"] is True
    assert proc(st, "待たない手順")["has_waits"] is False
    r = post(f"{server}/api/procedure/check", {"name": "待つ手順", "doc": waits})
    assert r["error_at"] is None and r["has_waits"] is True, r
    r = post(f"{server}/api/procedure/check", {"name": "待つ手順", "doc": plain})
    assert r["error_at"] is None and r["has_waits"] is False, r


def test_the_list_and_the_check_say_whether_the_procedure_never_ends(server, proj):
    """手順の一覧と保存前の検証が、終わりのない手順(無限のくり返しがある)かを
    返すこと。

    変換は終わりのない手順の長さを 0 で表す。0 は、選択待ちの選択肢の中身だけの
    手順でも起きるので、長さからは区別できない。画面は、終わりのない手順の長さを
    「無限」と出す。
    """
    endless = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 30},
        {"type": "loop", "id": "lp-1", "count": "infinite", "body": [
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 5}]}]}
    arms_only = branch_on("ない条件")
    proj.save_procedure_doc("終わらない手順", endless)
    proj.save_procedure_doc("選ぶだけの手順", arms_only)
    st = get(f"{server}/api/state")
    got = [(proc(st, n)["frames"], proc(st, n).get("endless"))
           for n in ("終わらない手順", "選ぶだけの手順")]
    assert got == [(0, True), (0, False)], got
    for doc, want in ((endless, True), (arms_only, False)):
        r = post(f"{server}/api/procedure/check",
                 {"name": "終わらない手順", "doc": doc})
        assert r["error_at"] is None and r.get("endless") is want, r


def test_an_unsaved_doc_sees_the_saved_procedures_it_calls(server, proj):
    """保存前の中身が呼ぶ手順は、保存してある中身で検証されること。"""
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "call", "id": "cl-1", "ref": "サンプル"}]}
    r = post(f"{server}/api/procedure/check", {"name": "新しい", "doc": doc})
    assert r["error_at"] is None and r["warnings"] == [], r
    assert not proj.procedure_path("新しい").exists()


# ---- ほかで変わった物を黙って上書きしない ----


def test_saving_a_procedure_changed_elsewhere_is_refused(server, proj):
    opened = get(f"{server}/api/procedure?name={q('サンプル')}")
    base = opened["rev"]
    assert base
    # 開いている間に、別のタブが同じ手順を保存した
    other = json.loads(json.dumps(opened["doc"]))
    other["body"].append({"type": "wait", "frames": 7})
    proj.save_procedure_doc("サンプル", other)
    mine = json.loads(json.dumps(opened["doc"]))
    mine["body"].append({"type": "wait", "frames": 9})
    r = post(f"{server}/api/procedure/save",
             {"name": "サンプル", "doc": mine, "base_rev": base})
    assert r.get("conflict") and "別のタブかアプリで変更されています" in r["error"], r
    assert proj.load_procedure_doc("サンプル")["body"][-1]["frames"] == 7
    # 上書きしてよいと確認したリビジョン(拒否したときに返したリビジョン)を送れば書ける。
    # 確認している間にさらに変わっていれば、もう一度拒否する(確認していないリビジョンは
    # 上書きしない)
    seen = r["rev"]
    proj.save_procedure_doc("サンプル", other | {"precondition": "別のタブ"})
    again = post(f"{server}/api/procedure/save",
                 {"name": "サンプル", "doc": mine, "base_rev": seen})
    assert again.get("conflict") and again["rev"] != seen, again
    r = post(f"{server}/api/procedure/save",
             {"name": "サンプル", "doc": mine, "base_rev": again["rev"]})
    assert r.get("ok") and r["rev"], r
    assert proj.load_procedure_doc("サンプル")["body"][-1]["frames"] == 9
    mine["body"].append({"type": "wait", "frames": 11})
    r2 = post(f"{server}/api/procedure/save",
              {"name": "サンプル", "doc": mine, "base_rev": r["rev"]})
    assert r2.get("ok"), r2


def test_saving_a_frametable_changed_elsewhere_is_refused(server, proj):
    opened = get(f"{server}/api/frametable?name={q('サンプル')}")
    base = opened["rev"]
    proj.save_frametable_grid("サンプル", opened["header"],
                              opened["rows"] + [[""] * len(opened["header"])])
    r = post(f"{server}/api/frametable/save",
             {"name": "サンプル", "header": opened["header"],
              "rows": opened["rows"], "base_rev": base})
    assert r.get("conflict"), r
    r = post(f"{server}/api/frametable/save",
             {"name": "サンプル", "header": opened["header"],
              "rows": opened["rows"], "base_rev": r["rev"]})
    assert r.get("ok"), r
    now = get(f"{server}/api/frametable?name={q('サンプル')}")
    assert now["rows"] == opened["rows"] and now["rev"] == r["rev"], now


# ---- 手順のリビジョン ----


def test_the_procedure_rev_follows_changes_the_device_never_sees(server, proj):
    """ラベル名だけの変更でもリビジョンが変わること(装置へ送る中身は同じ)。

    画面はリビジョンが変わったときだけ図と選択肢を作り直す。中身の id をキーにすると、
    ラベル名や前提条件だけの変更が画面に送られない。
    """
    st = get(f"{server}/api/state")
    before = proc(st, "サンプル")
    doc = proj.load_procedure_doc("サンプル")
    doc["body"][0]["name"] = "はじめ"
    proj.save_procedure_doc("サンプル", doc)
    after = proc(get(f"{server}/api/state"), "サンプル")
    assert after["hash"] == before["hash"]
    assert after["rev"] != before["rev"]
    assert after["file_rev"] != before["file_rev"]


def test_a_missing_condition_is_a_warning_and_its_arrival_changes_the_rev(
        server, proj):
    """ない条件を指す選択肢は警告になり、条件ができると警告が消えること。

    条件は PC ごとの物なので、なくても変換は通る(その選択肢が自動では
    選ばれないだけ)。手順のファイルは変わらないが、リビジョンは変わる。
    """
    proj.save_procedure_doc("分岐", branch_on("完了画面"))
    st = get(f"{server}/api/state")
    p = proc(st, "分岐")
    assert p["warnings"] == 1, p
    tl = get(f"{server}/api/timeline?name={q('分岐')}")
    assert any("条件「完了画面」が見つかりません" in w["msg"]
               for w in tl["warnings"]), tl["warnings"]
    opened = get(f"{server}/api/procedure?name={q('分岐')}")
    # 選択肢の名前は、画面が場所として前に付ける(選択肢の中の失敗と同じ形)
    assert [(w["arm"], w["msg"]) for w in opened["check"]["warnings"]] == [
        ("出た", "条件「完了画面」が見つかりません。"
                 "この選択肢は自動で選択されません。")]
    make_condition(proj, "完了画面")
    p2 = proc(get(f"{server}/api/state"), "分岐")
    assert p2["warnings"] == 0 and p2["rev"] != p["rev"], p2
    assert p2["file_rev"] == p["file_rev"]


def test_lists_of_frametables_and_conditions_carry_their_revs(server, proj):
    make_condition(proj, "完了画面")
    st = get(f"{server}/api/state")
    frametable = next(x for x in st["frametables"] if x["name"] == "サンプル")
    cond = next(x for x in st["conditions"] if x["name"] == "完了画面")
    assert frametable["rev"] and frametable["in_use"] == []
    assert cond["rev"] == get(
        f"{server}/api/condition/get?name={q('完了画面')}")["rev"]
    store = ConditionStore(proj.root)
    store.save("完了画面", {"kind": "desktop"}, [(0.0, 0.0, 0.5, 0.5)], 80)
    add_images(store, "完了画面", refs=[GRAY_PNG])
    st2 = get(f"{server}/api/state")
    cond2 = next(x for x in st2["conditions"] if x["name"] == "完了画面")
    assert cond2["rev"] != cond["rev"]


# ---- 図の解像度 ----


def test_spans_finer_than_the_screen_are_joined():
    """細かい区間はつなぎ、長い区間は値が違えば別に残すこと。"""
    total = 10000
    # 1 フレームごとに値が変わる軸(画面では 1 画素に満たない)
    tiny = [[i, i + 1, (i % 7) * 100] for i in range(total)]
    out = timeline._coalesce(tiny, total, True)
    assert len(out) <= timeline._RESOLUTION, len(out)
    assert out[0][0] == 0 and out[-1][1] == total
    assert out[0][2] == "0〜600", out[0]
    # 長い区間どうしは、隣り合っていても値が違えば別のまま
    wide = [[0, 5000, 100], [5000, 10000, 200]]
    assert timeline._coalesce(wide, total, True) == wide
    # 離れた短い区間はつながない(押していない間が見える)
    apart = [[0, 1], [5000, 5001]]
    assert timeline._coalesce(apart, total, False) == apart


def test_a_long_busy_procedure_draws_a_bounded_timeline(server, proj):
    # 1 フレームごとに左右へ倒すのを 3000 回くり返す(値の変化が 6000 回)
    (proj.root / "frametables" / "揺らし.csv").write_text(
        "F,LX\n1,1000\n2,-1000\n", encoding="utf-8")
    proj.save_procedure_doc("揺らし", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "loop", "id": "lp-1", "count": 3000, "body": [
                {"type": "frametable", "ref": "揺らし"}]}]})
    tl = get(f"{server}/api/timeline?name={q('揺らし')}")
    spans = sum(len(t["spans"]) for t in tl["tracks"])
    assert 0 < spans <= 2 * timeline._RESOLUTION, spans


def test_writing_into_a_handed_out_timeline_leaves_the_kept_one_alone(proj):
    """図のキャッシュから渡した図の区切りに書き込んでも、キャッシュは変わらないこと。

    呼び出し元は区切りの 1 つずつに選択肢の名前などを書き込む。キャッシュまで書き換わる
    と、同じ id で選択肢の名前だけが違う図を描いたとき、前の図の名前が混ざる。
    """
    with timeline._CACHE_LOCK:
        timeline._CACHE.clear()
    proj.save_procedure_doc("分かれる", branch_on("見る"))
    r = proj.build("分かれる")
    first = timeline.build_cached(r.hash, r.blob, None)
    assert first["cuts"], first
    first["cuts"][0]["name"] = "前の図の名前"
    again = timeline.build_cached(r.hash, r.blob, None)
    assert "name" not in again["cuts"][0], again["cuts"]


def test_saving_a_condition_changed_elsewhere_is_refused(server, proj):
    """条件も、開いた後にほかで変わっていたら黙って上書きしないこと。

    画像の追加と削除も保存で確定するので、画像を追加するだけの保存でもリビジョンが進む。
    同じリビジョンから開いた別のタブの保存は、上書きせずに拒否する。
    """
    make_condition(proj, "完了画面")
    opened = get(f"{server}/api/condition/get?name={q('完了画面')}")
    png = encode_png(4, 4, bytes((10, 10, 10)) * 16)
    added = post(f"{server}/api/condition/image/add",
                 {"name": "完了画面", "image": base64.b64encode(png).decode()})
    # 設定は開いたときのままにして、画像の並びだけを変える
    body = {"name": "完了画面", "source": opened["source"],
            "regions": opened["regions"], "threshold": opened["threshold"],
            "refs": [*opened["refs"], added["file"]],
            "negatives": opened["negatives"], "base_rev": opened["rev"]}
    saved = post(f"{server}/api/condition/save", body)
    assert saved.get("ok") and saved["rev"] != opened["rev"], saved
    # 同じリビジョンから開いた別のタブが、画像を追加する前の並びで保存しようとする
    stale = dict(body, threshold=70, refs=opened["refs"])
    r = post(f"{server}/api/condition/save", stale)
    assert r.get("conflict"), r
    got = ConditionStore(proj.root).load("完了画面")
    assert got.threshold == opened["threshold"]
    assert len(got.ref_files) == len(opened["refs"]) + 1


def test_a_failure_inside_a_called_procedure_points_at_the_call(server, proj):
    """呼び出し先の中の失敗は、開いている手順の呼び出しのブロックを指すこと。

    呼び出し先のファイルの中の番号をそのまま使うと、開いている手順の無関係な
    ブロックが赤くなり、文を押すとそこへ跳ぶ。文には呼び出し先の名前が入る。
    """
    proj.save_procedure_doc("下請け", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "wait", "frames": 1},
            {"type": "frametable", "ref": "ない表"}]})
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 1},
        {"type": "wait", "frames": 1},
        {"type": "call", "id": "cl-7", "ref": "下請け"}]}
    at = post(f"{server}/api/procedure/check",
              {"name": "上", "doc": doc})["error_at"]
    assert at["block"] == "cl-7" and at["line"] is None, at
    assert at["msg"].startswith("「下請け」の中: "), at
    # 修正するのは呼び出し先の中なので、呼び出し先を開いて修正するよう示す。中の文の
    # 対処(「別の〜を選択してください」)は、このブロックの選び直しと読めるので付けない
    assert at["msg"].endswith("。「下請け」を開いて修正してください。"), at
    assert "選択してください" not in at["msg"], at
    # 手順の状態は、文が開いて修正するよう示す手順を持つ(実行・監視のレーンの操作が、
    # 呼び出し元ではなくその手順を開く)
    proj.save_procedure_doc("上", doc)
    st = {p["name"]: p for p in get(f"{server}/api/state")["procedures"]}
    assert st["上"].get("error_callee") == "下請け", st["上"]
    assert "error_callee" not in st["下請け"], st["下請け"]


def test_a_call_cycle_names_the_path_once(server, proj):
    """呼び出しが循環している文は、呼び出し先の名前を前に付けないこと。

    循環の経路(A → B → A)が手順の名前を示すので、「「B」の中: 」を付けると
    コロンが重なり、同じ名前を 2 回読ませる。
    """
    proj.save_procedure_doc("循環の相手", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "call", "id": "cl-1", "ref": "循環の元"}]})
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "call", "id": "cl-2", "ref": "循環の相手"}]}
    proj.save_procedure_doc("循環の元", doc)
    at = post(f"{server}/api/procedure/check",
              {"name": "循環の元", "doc": doc})["error_at"]
    assert at["msg"].startswith("別の手順の呼び出しが循環しています"), at
    assert "の中: " not in at["msg"], at
    # 循環の文は、開く手順を示さない。レーンの操作も呼び出し元を開く
    st = {p["name"]: p for p in get(f"{server}/api/state")["procedures"]}
    assert "error_callee" not in st["循環の元"], st["循環の元"]


# ---- 待たせたくない要求は、別の装置の長い操作の後ろに並ばない ----


def test_urgent_requests_skip_the_queue_only_for_other_devices(server):
    """停止の要求は、別の装置の長い操作がロックを持っていても待たないこと。

    同じ装置と通信する操作がロックを待っているか持っているときは、その後ろで待つ
    (先に停止して、後から来た開始に上書きされることがないように)。
    """
    held = gui._Handler.lock
    mark = object()
    held.acquire()
    with gui._Handler._busy_lock:
        gui._Handler._busy[mark] = frozenset({"1P"})
    try:
        # 待つ作りなら、ロックが空かないので応答が返らず、post の時間切れで失敗する
        post(f"{server}/api/stop_lanes", {"mode": "immediate", "devs": ["2P"]})
        # 同じ装置の停止は、ロックが空くまで返らない。1 秒は、待たない作りなら
        # 十分に返る長さである
        req = urllib.request.Request(
            f"{server}/api/stop_lanes",
            data=json.dumps({"mode": "immediate", "devs": ["1P"]}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with pytest.raises(OSError):
            urllib.request.urlopen(req, timeout=1.0)
    finally:
        with gui._Handler._busy_lock:
            gui._Handler._busy.pop(mark, None)
        held.release()


def test_which_devices_a_request_touches():
    t = gui._targets
    assert t("/api/run", {"plan": [{"dev": "1P"}, {"dev": "2P"}]}) == {"1P", "2P"}
    assert t("/api/run", {"plan": [{"dev": ""}]}) is None
    assert t("/api/procedure/save", {"name": "x"}) == frozenset()
    assert t("/api/select", {"dev": "2P"}) == {"2P"}
    assert gui._overlap(frozenset({"1P"}), frozenset({"2P"})) is False
    assert gui._overlap(None, frozenset()) is False      # ファイルだけの保存は待たない
    assert gui._overlap(None, frozenset({"2P"})) is True


def test_a_transient_write_failure_is_not_remembered(tmp_path, monkeypatch):
    """書き込みの一時的な失敗はキャッシュせず、次は変換し直すこと。

    中身から決まる失敗はキャッシュする(同じ中身なら何度でも同じに失敗する)。書き込み先が
    一時的に使えないだけの失敗までキャッシュすると、原因が解消しても手順がエラーのままになる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    real = proj.build
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("書き込めません")
        return real(*a, **k)

    monkeypatch.setattr(proj, "build", flaky)
    got, err = proj.build_detail("サンプル")
    assert got is None and err, err
    got, err = proj.build_detail("サンプル")
    assert got is not None and err is None, err
    assert len(calls) == 2


def test_only_the_latest_failure_of_a_procedure_is_remembered(tmp_path):
    """失敗する手順を何度修正しても、キャッシュする失敗はいまの中身の 1 件だけで
    あること。

    中身が変わるとキーの指紋が変わるので、古い中身の失敗はもう使われない。捨てないと、
    失敗する中身を保存するたびにキャッシュが増え続ける。
    """
    proj = Project(tmp_path)
    for n in range(5):
        proj.save_procedure_doc("直す途中", {
            "kind": "procedure", "format": FORMAT_VERSION, "precondition": f"{n} 回目",
            "body": [{"type": "wait", "frames": 0}]})
        got, err = proj.build_detail("直す途中")
        assert got is None and err, err
    assert len(proj._fail_cache) == 1, list(proj._fail_cache)


def test_a_project_does_not_answer_for_what_another_folder_sent(tmp_path):
    """別のフォルダーの Project が装置へ送った手順を、id で探さないこと。

    送った手順のコピーはそのフォルダーの中身から作った物である。同じ id の手順でも、別の
    フォルダーでは名前も送った記録も違う。
    """
    here = Project(tmp_path / "ここ")
    here.init_sample()
    other = Project(tmp_path / "よそ")
    other.init_sample()
    r = other.build("サンプル")
    other.sent.remember(r)
    assert other.sent.built(r.hash, "") is not None
    assert here.sent.built(r.hash, "") is None
    assert r.hash not in here.sent.names()


def test_the_content_and_its_revision_come_from_the_same_moment(tmp_path, monkeypatch):
    """開くときの中身とリビジョンは、同じ時点のものであること。

    中身を読んだ直後に別のタブが保存すると、リビジョンだけ新しい組になりうる。その組を
    受け取った画面は、新しい変更を見ないまま最新のリビジョンを持つので、次の保存で
    ほかの変更を確認なしで上書きできてしまう。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    gui._Handler.project = proj
    h = gui._Handler.__new__(gui._Handler)
    doc = proj.load_procedure_doc("サンプル")
    changed = []

    def read_then_someone_saves():
        got = proj.load_procedure_doc("サンプル")
        if not changed:          # 1 回目の読みの直後に、別のタブが保存する
            changed.append(1)
            other = json.loads(json.dumps(doc))
            other["body"].append({"type": "wait", "frames": 7})
            proj.save_procedure_doc("サンプル", other)
        return got

    got, rev = h._read_with_rev("procedure", "サンプル", read_then_someone_saves)
    assert rev == proj.file_rev("procedure", "サンプル")
    assert got == proj.load_procedure_doc("サンプル"), \
        "古い中身に新しいリビジョンが付いている"


def test_a_read_that_never_settles_is_refused(tmp_path):
    """読む間の書き込みが続いて中身とリビジョンが揃わないときは、_read_with_rev が
    ValueError を送出すること。

    揃わないまま読む前のリビジョンを付けて返すと、そのリビジョンが空(読む間にファイルが
    なかった)のとき、保存で照合しない値になる。受け取った画面は、ほかの変更を確認なしで
    上書きできてしまう。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    h = gui._Handler.__new__(gui._Handler)
    h.project = proj
    doc = proj.load_procedure_doc("サンプル")
    n = []

    def someone_keeps_saving():
        got = proj.load_procedure_doc("サンプル")
        n.append(1)
        other = json.loads(json.dumps(doc))
        other["body"].append({"type": "wait", "frames": len(n)})
        proj.save_procedure_doc("サンプル", other)
        return got

    with pytest.raises(ValueError):
        h._read_with_rev("procedure", "サンプル", someone_keeps_saving)


def test_the_check_comes_from_the_content_it_is_sent_with(tmp_path):
    """手順を開くとき、付ける失敗と警告は、一緒に返す中身のものであること。

    別々に読むと、間に入った保存の失敗が、開いた中身のブロックに付く(正しい
    中身に「1 以上にしてください」と出る)。
    """
    proj = Project(tmp_path)
    name = "開くテスト"

    def doc(frames):
        return {"kind": "procedure", "format": FORMAT_VERSION,
                "body": [{"type": "wait", "frames": frames}]}

    proj.save_procedure_doc(name, doc(60))
    real = proj.proc_status
    saved = []

    def someone_saves_first(n):
        if not saved:            # 中身を読んだ後、失敗と警告を出す直前に保存が入る
            saved.append(1)
            proj.save_procedure_doc(name, doc(0))
        return real(n)

    proj.proc_status = someone_saves_first
    h = gui._Handler.__new__(gui._Handler)
    h.project = proj
    h._guard = lambda **kwargs: ""        # 最初のチェック(Host など)は見ない
    h._json = lambda data, *args: data
    h.path = "/api/procedure?name=" + urllib.parse.quote(name)
    got = h._get()
    broken = got["doc"]["body"][0]["frames"] == 0
    assert broken == (got["check"]["error_at"] is not None), got
