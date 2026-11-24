"""「使用している場所」と、プリセットの開始位置の照合。

Project の usages・referrers・usage_and_joins・rename_condition_refs と、
PresetStore の lanes・lost_starts を確かめる。
"""
import json
import sys

import pytest

from padcue.names import name_key
from padcue.presets import PRESET_FORMAT
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project


def _procedure(p: Project, name: str, body: list) -> None:
    (p.root / "procedures").mkdir(parents=True, exist_ok=True)
    p.procedure_path(name).write_text(json.dumps(
        {"kind": "procedure", "format": FORMAT_VERSION, "body": body},
        ensure_ascii=False), encoding="utf-8")


def _preset(p: Project, name: str, lanes: list) -> None:
    (p.root / "presets").mkdir(exist_ok=True)
    (p.root / "presets" / f"{name}.json").write_text(json.dumps(
        {"format": PRESET_FORMAT, "lanes": lanes},
        ensure_ascii=False), encoding="utf-8")


# 呼び出し 3 か所(下ごしらえ)、フレーム表 2 か所(連続)、条件の分岐 1 つ(確認画面)
CALLER_BODY = [
    {"type": "call", "id": "cl-1", "ref": "下ごしらえ"},
    {"type": "loop", "id": "lp-1", "count": 2, "body": [
        {"type": "call", "id": "cl-2", "ref": "下ごしらえ"},
        {"type": "frametable", "ref": "連続"}]},
    {"type": "branch", "id": "br-1", "arms": [
        {"name": "出た", "what": {"kind": "condition", "ref": "確認画面"},
         "body": [{"type": "call", "id": "cl-3", "ref": "下ごしらえ"}]},
        {"name": "まだ", "what": {"kind": "condition", "ref": "確認画面"},
         "body": []}],
     "otherwise": [{"type": "frametable", "ref": "連続"}]}]


def test_usages_count_every_place_inside_the_blocks(tmp_path):
    """くり返し・選択肢・「それ以外」の中も数え、手順ごとにか所の数を返すこと。

    数え漏らすと、使っている手順があるのに「使用している場所」が 0 と表示し、
    消してよいと思わせる。
    """
    p = Project(tmp_path)
    _procedure(p, "呼ぶ側", CALLER_BODY)
    _procedure(p, "使わない", [{"type": "wait", "frames": 5}])
    assert p.usages("call", "下ごしらえ") == [{"proc": "呼ぶ側", "count": 3}]
    assert p.usages("frametable", "連続") == [{"proc": "呼ぶ側", "count": 2}]
    # 条件は分岐のブロック 1 つを 1 か所と数える(選択肢が 2 つ指していても)
    assert p.usages("condition", "確認画面") == [{"proc": "呼ぶ側", "count": 1}]
    assert p.usages("call", "どこにもない") == []
    assert p.referrers("call", "下ごしらえ") == ["呼ぶ側"]


def test_usage_counts_match_the_places_the_list_shows(tmp_path):
    """一覧の行のメニューに出す数が、「使用している場所」の一覧の「か所」の合計と
    同じであること。

    数は、手順の中のか所の数(くり返し・選択肢・「それ以外」の中も)と、手順なら
    それを直接使うプリセットのレーンの数の和である。メニューの数と開いた一覧の数が
    一致しないと、どちらを信じて消してよいか分からない。読めない手順は、一覧と同じく
    数えない。
    """
    p = Project(tmp_path)
    _procedure(p, "呼ぶ側", CALLER_BODY)
    _procedure(p, "もう1つ", [{"type": "frametable", "ref": "連続"}])
    (p.root / "procedures" / "壊れた.json").write_text("{", encoding="utf-8")
    _preset(p, "いつもの", [
        {"id": "a1", "name": "1P", "proc": "下ごしらえ", "laps": None, "start": None},
        {"id": "b2", "name": "2P", "proc": "下ごしらえ", "laps": None, "start": None},
        {"id": "c3", "name": "3P", "proc": "呼ぶ側", "laps": None, "start": None}])
    got = p.usage_and_joins()[0]
    for kind, name in (("call", "下ごしらえ"), ("call", "呼ぶ側"),
                       ("frametable", "連続"),
                       ("condition", "確認画面"), ("call", "どこにもない")):
        listed = sum(u["count"] for u in p.usages(kind, name))
        if kind == "call":
            listed += len(p.presets.lanes(name))
        assert got.get((kind, name), 0) == listed, (kind, name, got)
    assert got[("call", "下ごしらえ")] == 5       # 手順の中の 3 か所とレーン 2 つ
    assert got[("frametable", "連続")] == 3
    assert got[("condition", "確認画面")] == 1


def test_usage_counts_survive_malformed_procedures_and_presets(tmp_path):
    """形の崩れた手順やプリセットがあっても、数えるのをやめないこと。

    数は毎秒の状態が作る。1 本の壊れた手順で失敗すると、状態がエラーだけに
    なり、すべての一覧とレーンが消える(変換はその手順だけをエラーにする)。
    形の崩れたプリセットは、一覧では読めない行なので数えない。
    """
    p = Project(tmp_path)
    _procedure(p, "本体が数値", 5)
    _procedure(p, "参照が配列", [
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "x", "what": {"kind": "condition", "ref": ["x"]}, "body": 5}]},
        {"type": "branch", "id": "br-2", "arms": 5},
        {"type": "frametable", "ref": "連続"}])
    _preset(p, "レーンが数値", 5)
    _preset(p, "ID が配列", [{"id": ["x"], "name": "1P", "proc": "参照が配列",
                             "laps": None, "start": None}])
    _preset(p, "いつもの", [{"id": "a1", "name": "1P", "proc": "参照が配列",
                           "laps": None, "start": None}])
    got = p.usage_and_joins()[0]
    assert got == {("frametable", "連続"): 1, ("call", "参照が配列"): 1}, got


def test_join_names_are_listed_once_in_list_order(tmp_path):
    """合流点に付けた名前を、手順の一覧の順・手順の中の順に、重ねずに返すこと。

    編集の画面は、これを合流点の名前の欄の候補に出す。相手の手順と 1 文字でも
    違うと(大文字と小文字も)対応しないので、打ち直させずに選ばせる。中身の
    中(くり返し・選択肢)の合流点も入れ、名前のない合流点は入れない。
    """
    p = Project(tmp_path)
    _procedure(p, "あ", [
        {"type": "join", "name": "合図"},
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "join", "name": "Round"}]},
        {"type": "join", "name": ""}])
    _procedure(p, "い", [
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "x", "what": {"kind": "manual"},
             "body": [{"type": "join", "name": "round"}]}]},
        {"type": "join", "name": "合図"}])
    _procedure(p, "壊れた", 5)
    assert p.usage_and_joins()[1] == ["合図", "Round", "round"]


# True は FORMAT_VERSION が 1 のときに、Python の等号で今の版と同じになる値
@pytest.mark.parametrize("fmt", [True, float(FORMAT_VERSION)])
def test_a_procedure_of_another_format_is_not_counted_or_rewritten(tmp_path, fmt):
    """バージョンが整数の FORMAT_VERSION でない手順(True や 1.0 のように、Python では
    今の版と等しく見える値を含む)は、使用している場所に数えず、参照の書き換えでも
    変えないこと。

    読み込みはこの手順を形式の誤りとして拒否するので、一覧ではエラーの手順である。
    数えると「使用している場所」に開けない手順が出て、書き換えると読めない手順の
    中身をツールが変える。
    """
    body = [{"type": "branch", "id": "br-1", "arms": [
        {"name": "出た", "what": {"kind": "condition", "ref": "確認画面"},
         "body": []}]}]
    p = Project(tmp_path)
    _procedure(p, "見て選ぶ", body)
    odd = p.procedure_path("バージョン違い")
    odd.write_text(json.dumps({"kind": "procedure", "format": fmt, "body": body},
                              ensure_ascii=False), encoding="utf-8")
    before = odd.read_bytes()
    assert p.usages("condition", "確認画面") == [{"proc": "見て選ぶ", "count": 1}]
    assert p.rename_condition_refs("確認画面", "終わった画面") == 1
    assert odd.read_bytes() == before


def test_preset_lanes_name_the_lane_by_the_current_device_name(tmp_path):
    """手順を直接使うプリセットのレーンを、今の装置の名前と開始位置で返すこと。

    プリセットは装置 ID で持つので、装置の名前を変えても今の名前で示す。
    """
    p = Project(tmp_path)
    p.save_config({"devices": [{"id": "a1", "name": "左の台", "host": "192.0.2.1",
                                "port": 5555}]})
    _procedure(p, "親", [{"type": "label", "id": "lb-1", "name": "後半の頭"},
                         {"type": "wait", "frames": 5}])
    _preset(p, "いつもの", [
        {"id": "a1", "name": "1P", "proc": "親", "laps": 3,
         "start": {"name": "後半の頭", "path": [], "block": "lb-1", "turns": []}},
        {"id": "b2", "name": "2P", "proc": "親", "laps": None, "start": None},
        {"id": "c3", "name": "3P", "proc": "別の手順", "laps": 1, "start": None}])
    assert p.presets.lanes("親") == [
        {"preset": "いつもの", "lane": "左の台", "start": "後半の頭", "lost": False},
        {"preset": "いつもの", "lane": "2P", "start": None, "lost": False}]
    assert p.presets.lanes("使わない手順") == []


def test_preset_lanes_say_the_current_label_name_and_a_lost_start(tmp_path):
    """開始位置は手順の今のラベルの名前で示し、見つからなければ lost が真になること。

    プリセットは開始位置の名前を保存した時点のまま持つ。ラベルの名前を変えた後に
    古い名前で示すと、手順の中で探せない。見つからない開始位置は、実行の画面で
    そのレーンを開始できない(「名前(見つかりません)」になる)。
    """
    p = Project(tmp_path)
    _procedure(p, "親", [{"type": "label", "id": "lb-1", "name": "後半の始め"},
                         {"type": "wait", "frames": 5}])
    _preset(p, "いつもの", [
        {"id": "a1", "name": "1P", "proc": "親", "laps": 1,
         "start": {"name": "後半の頭", "path": [], "block": "lb-1", "turns": []}}])
    assert p.presets.lanes("親") == [
        {"preset": "いつもの", "lane": "1P", "start": "後半の始め", "lost": False}]
    _procedure(p, "親", [{"type": "wait", "frames": 5}])
    assert p.presets.lanes("親") == [
        {"preset": "いつもの", "lane": "1P", "start": "後半の頭", "lost": True}]


def test_callers_follow_the_calls_up_to_the_top(tmp_path):
    """手順を、呼び出しをたどって使っている手順を、直接呼ぶ手順から順にすべて返すこと。

    手順を消すと、それを呼ぶ手順も、さらにそれを呼ぶ手順も実行できなくなる。削除の
    確認が直接呼ぶ手順だけを挙げると、いつも使う上の手順が動かなくなることに、消した
    後で気づく。循環した呼び出しでもたどり終え、同じ手順を 2 度挙げない。
    """
    p = Project(tmp_path)

    def call(ref, cid="cl-1"):
        return {"type": "call", "id": cid, "ref": ref}
    _procedure(p, "呼ぶA", [call("中継B"), call("中継B (3)", "cl-2")])
    _procedure(p, "中継B", [call("呼ばれC")])
    _procedure(p, "中継B (3)", [call("呼ばれC")])
    _procedure(p, "呼ばれC", [{"type": "wait", "frames": 5}])
    _procedure(p, "別", [{"type": "wait", "frames": 5}])
    assert p.callers("呼ばれC") == ["中継B", "中継B (3)", "呼ぶA"]
    assert p.callers("中継B") == ["呼ぶA"]
    assert p.callers("呼ぶA") == []
    # 循環している呼び出し(変換できない手順)でもたどり終え、消す手順そのものは挙げない
    _procedure(p, "呼ばれC", [call("呼ぶA")])
    assert p.callers("呼ばれC") == ["中継B", "中継B (3)", "呼ぶA"]


def _two_level(p: Project) -> None:
    """「親」が「子」を呼び、プリセットの開始位置が「子」のラベルを指す形。"""
    _procedure(p, "親", [{"type": "call", "id": "cl-1", "ref": "子"}])
    _procedure(p, "子", [{"type": "label", "id": "lb-1", "name": "後半の頭"},
                         {"type": "wait", "frames": 5}])
    _procedure(p, "別", [{"type": "wait", "frames": 5}])
    _preset(p, "後半から", [
        {"id": "a1", "name": "1P", "proc": "親", "laps": 1,
         "start": {"name": "後半の頭", "path": ["cl-1"], "block": "lb-1",
                   "turns": []}}])


def _doc(body: list) -> dict:
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": body}


LOST = [{"preset": "後半から", "lane": "1P", "label": "後半の頭"}]


def test_lost_starts_find_every_way_a_save_loses_a_start(tmp_path):
    """保存すると開始位置が見つからなくなる形を、どれも見つけること。

    ラベルを消すほかに、ラベルや途中の呼び出しを無効にしたときと、呼び出す手順を
    替えたときにも見つからなくなる。識別子が残っているかだけを見ると、ラベルの無効、
    呼び出しの無効、呼ぶ手順の替えを見逃す。
    """
    p = Project(tmp_path)
    _two_level(p)
    wait = {"type": "wait", "frames": 5}
    label = {"type": "label", "id": "lb-1", "name": "後半の頭"}
    call = {"type": "call", "id": "cl-1", "ref": "子"}
    # ラベルを消す
    assert p.presets.lost_starts("子", _doc([wait])) == LOST
    # ラベルを無効にする
    assert p.presets.lost_starts(
        "子", _doc([{**label, "disabled": True}, wait])) == LOST
    # 途中の呼び出しを消す・無効にする・呼ぶ手順を替える
    assert p.presets.lost_starts("親", _doc([wait])) == LOST
    assert p.presets.lost_starts("親", _doc([{**call, "disabled": True}])) == LOST
    assert p.presets.lost_starts("親", _doc([{**call, "ref": "別"}])) == LOST


def test_lost_starts_name_the_lanes_that_start_there(tmp_path):
    """実行・監視のレーンが開始位置に選んでいるラベルも、プリセットと同じく見つけること。

    レーンの開始位置は画面だけが持つので、画面が送る。プリセットが使っていない
    ラベルでも、レーンが使っていれば挙げる(挙げないと、保存した後にレーンが開始
    できなくなっていることに、実行・監視へ戻るまで気づかない)。
    """
    p = Project(tmp_path)
    _two_level(p)
    lane = {"lane": "2P", "proc": "親",
            "start": {"name": "後半の頭", "path": ["cl-1"], "block": "lb-1"}}
    wait = {"type": "wait", "frames": 5}
    assert p.presets.lost_starts("子", _doc([wait]), [lane]) == [
        *LOST, {"lane": "2P", "label": "後半の頭"}]
    (p.root / "presets" / "後半から.json").unlink()
    assert p.presets.lost_starts("子", _doc([wait]), [lane]) == [
        {"lane": "2P", "label": "後半の頭"}]
    assert p.presets.lost_starts("別", _doc([]), [lane]) == []


def test_lost_starts_leave_what_a_save_keeps(tmp_path):
    """見つかったままの保存と、既に見つからない開始位置は挙げないこと。

    既に見つからない開始位置を保存のたびに聞くと、確認する意味が薄れる。
    """
    p = Project(tmp_path)
    _two_level(p)
    wait = {"type": "wait", "frames": 5}
    label = {"type": "label", "id": "lb-1", "name": "後半の頭"}
    # 並べ替えと名前の変更では見つかったまま(識別子で照合する)
    assert p.presets.lost_starts(
        "子", _doc([wait, {**label, "name": "中盤"}])) == []
    # 関係のない手順の保存
    assert p.presets.lost_starts("別", _doc([])) == []
    # 既に見つからない開始位置は挙げない
    _procedure(p, "子", [wait])
    assert p.presets.lost_starts("子", _doc([])) == []


@pytest.mark.parametrize("start, why", [
    # プリセットを呼び出した後に、手順から消したラベル。画面の欄は
    # 「後半の頭(見つかりません)」で、その位置のまま保存を押せる
    ({"name": "後半の頭", "path": [], "block": "lb-9", "turns": []},
     "開始位置「後半の頭」が手順「親」に見つかりません。"
     "開始位置を選び直してください。"),
    # 停止した位置は、回番号がどのくり返しの回か(loops)か、周回数のくり返しの
    # 直後(after)を持つ(positions.stopped_at)
    ({"name": "停止した位置", "path": [], "block": "br-1", "turns": [],
      "loops": []},
     "「停止した位置」はプリセットに保存できません"),
    ({"name": "停止した位置", "path": [], "block": "lp-1", "turns": [],
      "after": True},
     "「停止した位置」はプリセットに保存できません"),
])
def test_a_start_that_cannot_be_saved_says_why(tmp_path, start, why):
    """保存できない開始位置は、停止した位置かどうかで理由を分けて書くこと。

    消したラベルを指す開始位置に「停止した位置は保存できません」と書くと、
    理由が事実と違い、欄を選び直せば保存できることにたどり着けない。
    """
    p = Project(tmp_path)
    _procedure(p, "親", [{"type": "label", "id": "lb-1", "name": "前半"},
                         {"type": "wait", "frames": 5}])
    with pytest.raises(ValueError) as e:
        p.presets.save("いつもの", {"lanes": [
            {"id": "a1", "name": "1P", "proc": "親", "laps": 1, "start": start}]})
    assert str(e.value) == why
    assert p.presets.names() == []


# ---- 綴りの違う参照(名前は NFC と大文字小文字を区別せずに比べる) ----

def _spelled_differently(p: Project) -> None:
    """手順「Retry」を「retry」と呼ぶ手順と、条件「Clear」を「clear」と指す手順。

    Windows はファイル名の大文字と小文字を区別しないので、変換も実行も通る。
    """
    _procedure(p, "Retry", [{"type": "label", "id": "lb-1", "name": "後半の頭"},
                            {"type": "wait", "frames": 5}])
    _procedure(p, "呼ぶ側", [
        {"type": "call", "id": "cl-1", "ref": "retry"},
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "出た", "what": {"kind": "condition", "ref": "clear"},
             "body": []}]}])
    (p.root / "conditions" / "Clear").mkdir(parents=True)
    (p.root / "conditions" / "Clear" / "condition.json").write_text("{}",
                                                                  encoding="utf-8")
    _preset(p, "後半から", [
        {"id": "a1", "name": "1P", "proc": "retry", "laps": 1,
         "start": {"name": "後半の頭", "path": [], "block": "lb-1", "turns": []}}])


# 綴りの違う名前で同じファイルを開けるのは、ファイル名の大文字と小文字を区別しない
# ファイルシステム(Windows の既定)だけである。PC 側の対応は Windows なので
# (README の「必要なもの」)、Windows で確かめる
_CASE_FOLDING_FS = pytest.mark.skipif(
    sys.platform != "win32",
    reason="ファイル名の大文字と小文字を区別しない Windows で確かめる")


@_CASE_FOLDING_FS
def test_a_reference_spelled_differently_is_a_use(tmp_path):
    """綴りの違う参照も、使用している場所と削除の確認に数えること。

    数えないと、「Retry」を確認なしで消せて、呼んでいる手順が変換できなくなる。
    改名(_rewrite_refs)は綴りの違う参照も書き換えるので、削除と矛盾する。
    """
    p = Project(tmp_path)
    _spelled_differently(p)
    assert p.usages("call", "Retry") == [{"proc": "呼ぶ側", "count": 1}]
    assert p.referrers("condition", "Clear") == ["呼ぶ側"]
    assert p.presets.lanes("Retry") == [
        {"preset": "後半から", "lane": "1P", "start": "後半の頭", "lost": False}]
    got = p.usage_and_joins()[0]
    # 手順の中の 1 か所とプリセットのレーン 1 つ
    assert got[("call", name_key("Retry"))] == 2, got
    assert got[("condition", name_key("Clear"))] == 1, got


def test_a_condition_spelled_differently_is_not_missing(tmp_path):
    """条件を綴りの違う名前で指していても、「見つかりません」と警告しないこと。

    開始のときの保存と判定はパスで開くので、Windows では見つかって使われる。
    警告が事実と逆になる。
    """
    p = Project(tmp_path)
    _spelled_differently(p)
    awaits = [{"block": "br-1", "conds": ["clear"], "names": ["出た"]}]
    assert p.missing_conditions(awaits) == []
    assert p.condition_warnings(awaits) == []
    assert p.missing_conditions([{"conds": ["ない"]}]) == ["ない"]


@_CASE_FOLDING_FS
def test_saving_a_procedure_spelled_differently_finds_lost_starts(tmp_path):
    """プリセットが綴りの違う名前で手順を指していても、保存で見つからなくなる
    開始位置を見つけること。

    保存前の中身を名前の完全一致で差し替えると、保存する前のファイルを見たままになる。
    """
    p = Project(tmp_path)
    _spelled_differently(p)
    assert p.presets.lost_starts("Retry", _doc([{"type": "wait", "frames": 5}])) == [
        {"preset": "後半から", "lane": "1P", "label": "後半の頭"}]


def test_the_order_follows_a_name_spelled_differently(tmp_path):
    """並び順、非表示、フォルダーも、綴りの違う名前で照合すること。

    エクスプローラーでファイル名の大文字と小文字だけを変えると、手順は一覧の末尾へ
    移り、非表示とフォルダーの所属が警告なしに外れていた。
    """
    p = Project(tmp_path)
    for n in ("a", "b", "Retry"):
        _procedure(p, n, [{"type": "wait", "frames": 5}])
    (p.root / "order.json").write_text(json.dumps({
        "procedures": ["retry", "b", "a"], "hidden": ["retry"],
        "folders": [{"name": "よく使う", "items": ["RETRY", "a"]}]},
        ensure_ascii=False), encoding="utf-8")
    assert p.procedure_names() == ["Retry", "b", "a"]
    assert p.load_proc_org() == {
        "folders": [{"name": "よく使う", "items": ["Retry", "a"]}],
        "hidden": ["Retry"]}
    # 改名と削除も、綴りの違う項目に付いていく
    p.rename_procedure("Retry", "やり直し")
    org = p.load_proc_org()
    assert org["hidden"] == ["やり直し"]
    assert org["folders"][0]["items"] == ["やり直し", "a"]
    assert p.procedure_names() == ["やり直し", "b", "a"]
