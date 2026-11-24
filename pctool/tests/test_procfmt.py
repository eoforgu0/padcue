"""手順のファイルとフレーム表の CSV の、読み込みと変換のテスト。

前半は読み込んで変換した結果を手計算の期待値と照合するもの、後半は
file-format.md §8-1 の「読み込みで拒否する形」を 1 行ずつ確認するものである。
期待値は手計算で出す。
"""
import json
import time

import pytest

from padcue import binfmt, engine, procfmt
from padcue.frametable import FrametableError, load_frametable
from padcue.procfmt import ProcedureError, compile_procedure
from padcue.procmodel import StartPos
from tests.helpers import compile_nodes

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
ZL = 1 << binfmt.BUTTONS["ZL"]

COMBO_CSV = """F,A,B,LX,GP
1,1,,,
2,1,,,
3,1,1,-20,300
4,1,1,-20,300
5,1,1,-20,300
6,1,,-20,300
7,1,,,
8,,,,
"""


def doc(body: list, precondition: str = "") -> dict:
    """手順の文書(file-format.md §3)。"""
    return {"kind": "procedure", "format": procfmt.FORMAT_VERSION,
            "precondition": precondition, "body": body}


def write_project(tmp_path, procedures: dict, frametables: dict | None = None):
    """手順の文書とフレーム表を書いて、プロジェクトのフォルダーを返す。"""
    (tmp_path / "procedures").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frametables").mkdir(parents=True, exist_ok=True)
    for name, document in procedures.items():
        (tmp_path / "procedures" / f"{name}.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8")
    for name, text in (frametables or {}).items():
        (tmp_path / "frametables" / f"{name}.csv").write_text(text, encoding="utf-8")
    return tmp_path


def test_unsaved_documents_are_found_by_the_name_identity(tmp_path):
    """保存する前の中身(docs)は、名前の同一性のキーで探すこと(file-format.md §2)。

    ファイルは大文字と小文字を区別せずに開けるので、「retry」と呼ぶ手順は
    「Retry.json」を読む。保存する前の中身を名前の完全一致で探すと、呼び出しの中だけ
    保存した中身を読み、編集画面の確認が保存した後の変換と一致しない。
    """
    root = write_project(tmp_path, {
        "親": doc([{"type": "call", "id": "cl-1", "ref": "retry"}]),
        "Retry": doc([{"type": "wait", "frames": 10}])})
    got = compile_procedure(root, "親", docs={
        "Retry": doc([{"type": "wait", "frames": 30}])})
    assert got.total_frames == 30, got.total_frames


def test_frametable_inputs_exactly_what_the_table_says(tmp_path):
    """フレーム表は、表のとおりにすべての入力を入れること。

    表に書いていない列(ここでは ZL)は空欄と同じで、離した状態になる。手順の側で
    押し続けていても、表の間は表のとおりで、抜けたら手順の状態に戻る。
    """
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "label", "id": "lb-1", "name": "開始"},
            {"type": "hold", "buttons": ["ZL"]},
            {"type": "frametable", "ref": "combo"},
            {"type": "release", "buttons": ["ZL"]},
            {"type": "wait", "frames": 10},
        ], precondition="テスト"),
    }, {"combo": COMBO_CSV})
    c = compile_procedure(root, "main")
    assert c.precondition == "テスト"
    assert c.labels == [(0, "開始")]
    assert c.total_frames == 18
    ems = engine.run(c.events)
    assert [(e.frame, e.buttons, e.lx, e.gx) for e in ems] == [
        (0, A, 0, 0),             # 表の 1 行目は A だけ(書いていない ZL は離す)
        (2, A | B, -20, 300),     # 3 行目: B を加え、スティック左に少し・ジャイロ上
        (5, A, -20, 300),         # 6 行目: B を離す
        (6, A, 0, 0),             # 7 行目: スティックとジャイロを戻す
        (7, 0, 0, 0),             # 8 行目: 何も押さない
        (8, 0, 0, 0),             # 抜けて ZL に戻り、同じフレームの release ZL で離す
        (18, 0, 0, 0),            # 最後の周の終わり(区切り)
    ]


def test_frametable_in_loop_block_semantics(tmp_path):
    """くり返しの中のフレーム表は、最後の行が空白の表でも、表の長さのぶん回ごとに進むこと。"""
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "loop", "id": "lp-1", "count": 3, "body": [
                {"type": "frametable", "ref": "tap"},
                {"type": "wait", "frames": 7},
            ]},
        ]),
    }, {"tap": "A\n1\n1\n1\n \n"})  # A を 3F 押して 1F 離す(計 4F)
    c = compile_procedure(root, "main")
    assert c.total_frames == 33  # (4+7)×3
    # 先頭と末尾の (·, 0) は周の境の STATE(区切り)
    ems = [(e.frame, e.buttons) for e in engine.run(c.events)]
    assert ems == [(0, 0), (0, A), (3, 0), (11, A), (14, 0), (22, A), (25, 0),
                   (33, 0)]


def test_call_between_procedures(tmp_path):
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "call", "id": "cl-1", "ref": "sub"},
            {"type": "wait", "frames": 5},
        ]),
        "sub": doc([
            {"type": "press", "buttons": ["B"], "frames": 5},
            {"type": "wait", "frames": 5},
        ]),
    })
    c = compile_procedure(root, "main")
    assert c.total_frames == 20
    ems = [(e.frame, e.buttons) for e in engine.run(c.events)]
    assert ems == [(0, A), (5, B), (10, 0), (20, 0)]


def test_call_cycle_between_files(tmp_path):
    root = write_project(tmp_path, {
        "a": doc([{"type": "call", "id": "cl-1", "ref": "b"}]),
        "b": doc([{"type": "call", "id": "cl-1", "ref": "a"}]),
    })
    with pytest.raises(ProcedureError, match="循環しています"):
        compile_procedure(root, "a")


def test_call_target_error_reports_target_file(tmp_path):
    """呼び出し先で起きたエラーが、呼び出し先のファイル名と呼び出し先の中のブロックの番号で
    報告され、文言の先頭に呼び出し元(一番上の手順)の名前が付くこと。"""
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "wait", "frames": 1},
            {"type": "loop", "id": "lp-1", "count": 2, "body": [
                {"type": "call", "id": "cl-1", "ref": "sub"},
            ]},
        ]),
        "sub": doc([
            {"type": "wait", "frames": 1},
            # 1 回が時間を進めないくり返しは変換が拒否する(file-format.md §8-2)。
            # 呼び出し先のファイル名とブロックの番号の報告を
            # 確認する素材として使う
            {"type": "loop", "id": "lp-1", "count": 2, "body": [
                {"type": "hold", "buttons": ["ZL"]},
            ]},
        ]),
    })
    with pytest.raises(ProcedureError) as ei:
        compile_procedure(root, "main")
    e = ei.value
    # 呼び出し元(main)のファイル名ではなく、実際にエラーが書かれている sub
    assert e.file == "procedures/sub.json"
    # sub のファイルの中の通し番号(1=wait 2=loop。手順そのものは 0)。main の
    # 側から通し番号を引き継いでいたら 2 より大きくなっているはず
    assert "2 番目のブロック" in str(e)
    assert "実行時間が 0 フレーム" in str(e)
    # 開いている手順(main)の中では、そこへ入っていく呼び出しのブロックを指す。
    # 番号は sub の中の話なので、main の中の番号として使わせない
    assert e.callee == "sub" and e.block == "cl-1" and e.line is None, vars(e)


def test_block_numbers_in_messages_count_disabled_blocks(tmp_path):
    """変換の失敗と警告が示す「N 番目のブロック」は、無効にしたブロックも数えること。

    編集画面は無効にしたブロックを取り消し線つきで列に見せ、「N 番目」もその順で
    数える(file-format.md §8)。読み込みは無効にしたブロックを取り除くので、取り
    除いた後の通し番号で示すと、コマンドの文と画面とで同じブロックの番号が
    ずれる。中のブロックも数え、呼び出し先の失敗は呼び出し先の中で数える。画面が
    ブロックを見つけるのに使う通し番号(line)は、無効にしたブロックを数えないまま。
    """
    off_loop = {"type": "loop", "id": "lp-0", "count": 2, "disabled": True,
                "body": [{"type": "wait", "frames": 1},
                         {"type": "branch", "id": "br-0", "arms": [
                             {"body": [{"type": "wait", "frames": 1}]}],
                          "otherwise": [{"type": "wait", "frames": 1}]}]}
    root = write_project(tmp_path, {
        # 無効のくり返し(1)と中の 4 つ(2〜5)、押して離す(6)、呼び出し(7)
        "main": doc([off_loop, {"type": "press", "buttons": ["A"], "frames": 1},
                     {"type": "call", "id": "cl-1", "ref": "ない手順"}]),
        # 無効の待つ(1)、待つ(2)、時間を進めないくり返し(3)
        "sub": doc([{"type": "wait", "frames": 1, "disabled": True},
                    {"type": "wait", "frames": 1},
                    {"type": "loop", "id": "lp-1", "count": 2, "body": [
                        {"type": "hold", "buttons": ["ZL"]}]}]),
        "top": doc([{"type": "call", "id": "cl-2", "ref": "sub"}]),
        # 無効の待つ(1)、名前のないラベル(2。警告になる)、待つ(3)
        "warn": doc([{"type": "wait", "frames": 1, "disabled": True},
                     {"type": "label", "id": "lb-1", "name": ""},
                     {"type": "wait", "frames": 1}]),
    })
    with pytest.raises(ProcedureError) as ei:
        compile_procedure(root, "main")
    assert "7 番目のブロック" in str(ei.value) and ei.value.line == 2, vars(ei.value)
    with pytest.raises(ProcedureError) as ei:
        compile_procedure(root, "top")
    assert ei.value.file == "procedures/sub.json"
    assert "3 番目のブロック" in str(ei.value), str(ei.value)
    w = compile_procedure(root, "warn").warnings
    assert [(x.line, x.shown) for x in w] == [(1, 2)], w


def test_a_warning_inside_a_called_procedure_is_numbered_at_the_call(tmp_path):
    """呼び出し先の中の警告は、開いた手順の中の、そこへ入っていく呼び出しのブロックの
    番号(shown)で示すこと(file-format.md §8)。

    編集画面は、呼び出し先の中のブロックを指せないので、呼び出しのブロックを
    「N 番目(別の手順)」と指す。実行の画面とコマンドの番号もそれに揃える。
    呼び出し先の中の番号を示すと、利用者は開いた手順の N 番目を見に行く。
    警告が付くブロックの番号(line)は、そのブロックが書いてある手順の中の番号で
    あること(2 段の呼び出しでも、一番上の手順の番号を混ぜない)。
    """
    def wait(off=False):
        return {"type": "wait", "frames": 1, **({"disabled": True} if off else {})}
    root = write_project(tmp_path, {
        # 無効の待つ(1・2)、待つ(3)、呼び出し cl-1(4)、待つ(5。到達しない)
        "top": doc([wait(True), wait(True), wait(),
                    {"type": "call", "id": "cl-1", "ref": "mid"}, wait()]),
        # 待つ(1〜5)、呼び出し cl-2(6)
        "mid": doc([wait(), wait(), wait(), wait(), wait(),
                    {"type": "call", "id": "cl-2", "ref": "end"}]),
        # 区切りのない無限のくり返し(1)
        "end": doc([{"type": "loop", "id": "lp-1", "count": "infinite", "body": [
            {"type": "press", "buttons": ["A"], "frames": 5}]}]),
    })
    ws = {w.block: w for w in compile_procedure(root, "top").warnings}
    assert set(ws) == {"lp-1", "cl-2"}, ws
    # 呼び出し先の呼び出し先(end)の中の警告
    assert (ws["lp-1"].path, ws["lp-1"].line, ws["lp-1"].shown) == (
        ("cl-1", "cl-2"), 1, 4), ws["lp-1"]
    # mid の中の呼び出し cl-2 に付く警告。line は mid の中の 6 番目
    assert (ws["cl-2"].path, ws["cl-2"].line, ws["cl-2"].shown) == (
        ("cl-1",), 6, 4), ws["cl-2"]


def test_a_bad_lap_number_inside_a_callee_names_the_callee_file(tmp_path):
    """開始位置の回番号が、呼び出し先のくり返しの回数を超えるときの失敗は、
    呼び出し先のファイルと、その中の番号で示すこと(file-format.md §8)。

    番号は呼び出し先のくり返しのものなので、一番上の手順の名前で示すと、
    一番上の手順の別のブロックを指す。
    """
    off = {"type": "wait", "frames": 1, "disabled": True}
    root = write_project(tmp_path, {
        "top": doc([off, {"type": "wait", "frames": 1},
                    {"type": "call", "id": "cl-1", "ref": "sub"}]),
        # 無効の待つ(1)、2 回のくり返し(2)
        "sub": doc([off, {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "label", "id": "lb-1", "name": "中"},
            {"type": "wait", "frames": 3}]}]),
    })
    start = StartPos(path=("cl-1",), block="lb-1", turns=(1, 5))
    with pytest.raises(ProcedureError) as ei:
        compile_procedure(root, "top", start=start)
    assert ei.value.file == "procedures/sub.json", vars(ei.value)
    assert "2 番目のブロック" in str(ei.value), str(ei.value)
    assert ei.value.block == "cl-1", vars(ei.value)


def test_a_call_inside_an_arm_is_found_for_the_error(tmp_path):
    """選択肢の中の呼び出しから入った先の失敗も、その呼び出しのブロックを指すこと。

    呼び出しは、くり返しの本体のほか選択肢の中身にも書ける。たどる所が
    1 つでも欠けると、開いている手順の中で指すブロックがなくなる。
    """
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "branch", "id": "br-1", "otherwise": [], "arms": [
                {"name": "x", "what": {"kind": "manual"}, "body": [
                    {"type": "call", "id": "cl-2", "ref": "sub"}]}]},
        ]),
        "sub": doc([
            {"type": "loop", "id": "lp-1", "count": 2, "body": [
                {"type": "hold", "buttons": ["ZL"]},
            ]},
        ]),
    })
    with pytest.raises(ProcedureError) as ei:
        compile_procedure(root, "main")
    e = ei.value
    assert e.callee == "sub" and e.block == "cl-2", vars(e)


def test_a_broken_callee_file_points_at_the_call(tmp_path):
    """呼び出し先のファイルが読めないときも、呼び出しのブロックを指すこと。"""
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "wait", "frames": 1},
            {"type": "call", "id": "cl-9", "ref": "sub"},
        ]),
        "sub": doc([
            {"type": "wait", "frames": 1},
            {"type": "ないブロック"},
        ]),
    })
    with pytest.raises(ProcedureError) as ei:
        compile_procedure(root, "main")
    e = ei.value
    assert e.file == "procedures/sub.json", e
    assert e.callee == "sub" and e.block == "cl-9" and e.via == "body[1]", vars(e)


def test_frametable_rep_expansion():
    p = load_frametable("p", "A,rep\n1,3\n,2\n")
    assert [r["A"] for r in p.rows] == [1, 1, 1, 0, 0]


def test_a_huge_rep_is_refused_without_expanding_it():
    """上限を超える rep は、展開する前に拒否すること。展開してから数えると、拒否する前に
    その数だけメモリと時間を使う(10 億行の展開は数十秒と数 GB になる)。時間は、
    展開していないことの目安である。展開すれば数十秒かかるので、上限は 5 秒にして
    余裕を持たせてある。"""
    started = time.monotonic()
    with pytest.raises(FrametableError, match="上限"):
        load_frametable("p", "A,rep\n1,1000000000\n")
    assert time.monotonic() - started < 5.0


@pytest.mark.parametrize("text", ["A,\n1,\n", "A\n1,,,\n", "A,\n1\n"])
def test_trailing_commas_from_a_spreadsheet_are_read(text):
    """ヘッダーの末尾の空の列名 1 つと、ヘッダーより後ろの空のセルは取り除いて読むこと
    (file-format.md §9-2)。表計算ソフトは行の末尾にカンマを付けて保存することがある。"""
    assert [r["A"] for r in load_frametable("p", text).rows] == [1]


def test_an_empty_column_name_in_the_middle_is_refused():
    """末尾でない空の列名は拒否すること。どの入力の列か分からない。"""
    with pytest.raises(FrametableError, match="列名が空欄です"):
        load_frametable("p", "A,,B\n1,0,1\n")


def test_frametable_csv_errors():
    with pytest.raises(FrametableError, match="フレーム表の列名ではありません"):
        load_frametable("p", "A,Q\n1,1\n")
    with pytest.raises(FrametableError, match="2 つあります"):
        load_frametable("p", "A,A\n1,1\n")
    with pytest.raises(FrametableError, match="空行"):
        load_frametable("p", "A\n1\n\n1\n")
    with pytest.raises(FrametableError,
                       match="F 列の値は 2 である必要があります: 3"):
        load_frametable("p", "F,A\n1,1\n3,1\n")
    with pytest.raises(FrametableError,
                       match="-2048 から 2047 までの整数である必要があります"):
        load_frametable("p", "LX\n5000\n")
    with pytest.raises(FrametableError, match="データ行がありません"):
        load_frametable("p", "A\n")
    with pytest.raises(FrametableError, match="列がありません"):
        load_frametable("p", "F,rep\n1,1\n")


def test_the_f_column_counts_data_rows_from_one():
    """F 列の値は、ヘッダーを除いたデータの行の番号(1 から)であること。

    失敗の場所はファイルの行の番号(ヘッダーが 1 行目)で示すので、2 つの番号は
    1 ずれる。file-format.md §9-2 はこの数え方を書いている。無効にした行も数える。
    """
    load_frametable("p", "F,A\n1,1\n2,0\n")
    load_frametable("p", "F,A,disabled\n1,1,\n2,0,1\n3,1,\n")
    with pytest.raises(FrametableError,
                       match="ファイルの 2 行目: F 列の値は 1 である必要があります: 2"):
        load_frametable("p", "F,A\n2,1\n3,0\n")


def test_frametable_f_column_errors_say_how_to_fix_them():
    """F 列を拒否するときに、修正方法を付けること。

    F 列を拒否する文は、修正方法(編集画面で保存し直す)を付ける。編集画面は
    保存のたびに行番号を振り直す。
    """
    with pytest.raises(FrametableError, match="保存し直してください"):
        load_frametable("p", "F,A\n1,1\n3,1\n")
    with pytest.raises(FrametableError, match="保存し直してください"):
        load_frametable("p", "F,A\nx,1\n")


def test_stick_with_duration_returns_to_center(tmp_path):
    """スティックに長さを付けると、その長さだけ倒して自動で中央へ戻ること。"""
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "stick", "side": "L", "x": -2048, "y": 0, "frames": 20},
            {"type": "wait", "frames": 10},
        ]),
    })
    c = compile_procedure(root, "main")
    assert c.total_frames == 30
    ems = [(e.frame, e.lx, e.ly) for e in engine.run(c.events)]
    assert ems == [(0, -2048, 0), (20, 0, 0), (30, 0, 0)]


def test_stick_without_duration_keeps_holding(tmp_path):
    """長さ 0(既定)なら時間を消費せず、次に変えるまで倒したままであること。"""
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "stick", "side": "L", "x": 0, "y": 2047},
            {"type": "wait", "frames": 30},
            {"type": "stick", "side": "L", "x": 0, "y": 0},
            {"type": "wait", "frames": 5},
        ]),
    })
    c = compile_procedure(root, "main")
    assert c.total_frames == 35
    ems = [(e.frame, e.lx, e.ly) for e in engine.run(c.events)]
    assert ems == [(0, 0, 2047), (30, 0, 0), (35, 0, 0)]


def test_stick_duration_right_side(tmp_path):
    """右スティックでも同じように戻ること(左右で処理が分かれているため)。"""
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "stick", "side": "R", "x": 1000, "y": -1000, "frames": 12},
            {"type": "wait", "frames": 8},
        ]),
    })
    c = compile_procedure(root, "main")
    ems = [(e.frame, e.rx, e.ry) for e in engine.run(c.events)]
    assert ems == [(0, 1000, -1000), (12, 0, 0), (20, 0, 0)]


def test_frametable_state_does_not_leak_after_frametable(tmp_path):
    """フレーム表の最後の行の入力が、そこを抜けた後まで残らないこと。

    残ると「手順のどこにも書いていない入力」が最後まで押しっぱなしになる。
    """
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "frametable", "ref": "居残り"},
            {"type": "wait", "frames": 10},
            {"type": "press", "buttons": ["B"], "frames": 3},
            {"type": "wait", "frames": 10},
        ]),
    }, {"居残り": "A,LX\n1,1000\n1,1000\n1,1000\n"})
    c = compile_procedure(root, "main")
    assert c.total_frames == 26, "戻すことで手順の長さが変わってはいけない"
    ems = [(e.frame, e.buttons, e.lx) for e in engine.run(c.events)]
    assert ems == [
        (0, A, 1000),   # フレーム表の中: A 押下・スティック倒し
        (3, 0, 0),      # 抜けたら元に戻る(時間は消費しない)
        (13, B, 0),     # 手順が指示した B だけが押される
        (16, 0, 0),
        (26, 0, 0),     # 最後の周の終わり(区切り)
    ]


def test_frametable_returns_to_the_hold_from_procedure_after_the_table(tmp_path):
    """手順の側で押していたもの(hold)は、表の間は表のとおりに離れ、抜けたら戻ること。"""
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "hold", "buttons": ["ZL"]},
            {"type": "frametable", "ref": "居残り"},
            {"type": "wait", "frames": 10},
            {"type": "release", "buttons": ["ZL"]},
            {"type": "wait", "frames": 5},
        ]),
    }, {"居残り": "A,LX\n1,1000\n1,1000\n1,1000\n"})
    c = compile_procedure(root, "main")
    ems = [(e.frame, e.buttons, e.lx) for e in engine.run(c.events)]
    assert ems == [
        (0, A, 1000),   # 表の間は表のとおり(書いていない ZL は離す)
        (3, ZL, 0),     # 抜けたら手順の状態(hold の ZL)に戻る
        (13, 0, 0),     # release ZL
        (18, 0, 0),     # 最後の周の終わり(区切り)
    ]


def test_frametable_sets_the_axes_it_does_not_write_to_their_rest(tmp_path):
    """表に書いていない軸も、表の間は空欄と同じになり、抜けたら手順の状態に戻ること。

    スティックは中央(0)、加速度は静止の値(縦が 4096)である。
    """
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "stick", "side": "L", "x": 0, "y": 2047},
            {"type": "frametable", "ref": "押すだけ"},
            {"type": "wait", "frames": 5},
        ]),
    }, {"押すだけ": "A\n1\n1\n"})
    c = compile_procedure(root, "main")
    ems = [(e.frame, e.buttons, e.ly, e.az)
           for e in engine.run(c.events)]
    assert ems == [
        (0, A, 0, 4096),      # 表の間は、書いていないスティックも中央
        (2, 0, 2047, 4096),   # 抜けたら手順のスティック(上いっぱい)に戻る
        (7, 0, 0, 4096),      # 最後の周の終わり(区切り)
    ]


def test_frametable_in_loop_plays_the_same_each_time(tmp_path):
    """くり返しの中のフレーム表でも、回ごとに同じ形で再生されること。"""
    root = write_project(tmp_path, {
        "main": doc([
            {"type": "loop", "id": "lp-1", "count": 2, "body": [
                {"type": "frametable", "ref": "居残り"},
                {"type": "wait", "frames": 7},
            ]},
        ]),
    }, {"居残り": "A\n1\n1\n1\n"})
    c = compile_procedure(root, "main")
    assert c.total_frames == 20   # (3+7)×2
    # 先頭と末尾の (·, 0) は周の境の STATE(区切り)
    ems = [(e.frame, e.buttons) for e in engine.run(c.events)]
    assert ems == [(0, 0), (0, A), (3, 0), (10, A), (13, 0), (20, 0)]


# ---- 0 フレームの「離す」 ----

def test_release_swallowed_by_next_press_warns():
    """離してすぐ押し直すと「離す」が出力に現れないので、警告すること。"""
    c = compile_nodes([{"type": "press", "buttons": ["A"], "frames": 2},
                       {"type": "release", "buttons": ["A"]},
                       {"type": "press", "buttons": ["A"], "frames": 2},
                       {"type": "wait", "frames": 10}])
    assert any("離す" in w.msg and "打ち消され" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]
    # 実際に 4 フレーム押しっぱなしになる。末尾は最後の周の終わりの STATE で、
    # くり返しを抜けた後の区間なので相対フレームは 0 に戻る
    states = [(e.frame, bool(e.buttons & A)) for e in c.events
              if isinstance(e, binfmt.State)]
    assert states == [(0, True), (2, True), (4, False), (0, False)]


def test_release_with_wait_is_not_warned():
    c = compile_nodes([{"type": "press", "buttons": ["A"], "frames": 2},
                       {"type": "wait", "frames": 2},
                       {"type": "press", "buttons": ["A"], "frames": 2},
                       {"type": "wait", "frames": 10}])
    assert not any("離す" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


# ---- 読み込みは通し、変換で拒否か警告する形(file-format.md §8-2・§8-3) ----

def test_infinite_loop_becomes_u32_max_and_has_no_total(tmp_path):
    """手順のファイルから変換しても、無限のくり返しの結果が変わらないこと。

    SETCNT は u32 最大値になり、総フレーム数は 0 になって、区切りを 1 つも含まない
    警告も出る(test_dsl_engine.py が手順の組み立てから直接変換して見るのと同じ結果)。
    総フレーム数 0 は「総量なし」で、進捗を割合で出せないことを表す。
    """
    root = write_project(tmp_path, {"main": doc([
        {"type": "loop", "id": "lp-1", "count": "infinite",
         "body": [{"type": "wait", "frames": 10}]},
    ])})
    c = compile_procedure(root, "main")
    assert c.total_frames == 0
    counts = [e.value for e in c.events if isinstance(e, binfmt.SetCnt)]
    assert counts == [1, 0xFFFFFFFF]   # 手順全体を囲むくり返し、無限のくり返し
    assert any("区切りがない" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_otherwise_of_a_branch_with_no_arm_warns(tmp_path):
    """選択肢が 0 本の分岐の「それ以外」は、変換に入らないこと。

    黙って除かない(file-format.md §7)。
    """
    root = write_project(tmp_path, {"main": doc([
        {"type": "branch", "id": "br-1", "arms": [],
         "otherwise": [{"type": "press", "buttons": ["A"], "frames": 3}]},
        {"type": "wait", "frames": 10},
    ])})
    c = compile_procedure(root, "main")
    assert c.total_frames == 10
    assert any("選択肢がないため" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_otherwise_hidden_by_an_every_time_arm_warns(tmp_path):
    """「それ以外」の行が消えている分岐でも、中身があれば知らせること
    (file-format.md §7)。

    選択肢の「対象の回」を「毎回」に戻すと、どの回もその選択肢が選ばれ、
    「それ以外」の行は画面から消える。中身はファイルに残るが変換では使わない。
    知らせないと、書いた入力がどこにも現れない。
    """
    root = write_project(tmp_path, {"main": doc([
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "甲", "what": {"kind": "manual"},
                 "body": [{"type": "wait", "frames": 5}]}],
             "otherwise": [{"type": "wait", "frames": 30}]}]},
    ])})
    c = compile_procedure(root, "main")
    got = [w.msg for w in c.warnings]
    assert any("「それ以外」に中身がありますが" in m for m in got), got
    # 行が出ている分岐(「対象の回」で選ばれない回がある)では知らせない
    root2 = write_project(tmp_path / "b", {"main": doc([
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "甲", "what": {"kind": "manual"},
                 "when": {"loop": "lp-1", "kind": "first"},
                 "body": [{"type": "wait", "frames": 5}]}],
             "otherwise": [{"type": "wait", "frames": 30}]}]},
    ])})
    got2 = [w.msg for w in compile_procedure(root2, "main").warnings]
    assert not any("「それ以外」に中身がありますが" in m for m in got2), got2


def test_otherwise_hidden_by_a_plain_every_time_arm_warns(tmp_path):
    """「対象の回」も「選択条件」も持たない選択肢がある分岐でも、「それ以外」の
    中身を知らせること(file-format.md §7)。

    その選択肢はどの回にも残り、待たずに選ばれるので、「それ以外」へ進む回は
    ない。「対象の回」を持つ選択肢がほかにあっても同じである。
    """
    root = write_project(tmp_path, {"main": doc([
        {"type": "loop", "id": "lp-1", "count": 3, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "1 回目", "when": {"loop": "lp-1", "kind": "first"},
                 "body": [{"type": "press", "buttons": ["A"], "frames": 1}]},
                {"name": "ほか",
                 "body": [{"type": "press", "buttons": ["B"], "frames": 1}]}],
             "otherwise": [{"type": "press", "buttons": ["X"], "frames": 5}]}]},
    ])})
    got = [w.msg for w in compile_procedure(root, "main").warnings]
    assert any("「それ以外」に中身がありますが" in m for m in got), got


# ---- 読み込みで拒否する形(file-format.md §8-1) ----
#
# file-format.md §8-1 の表の行を 1 つずつ確認する。文言は一部の一致で見る(値の書き方まで
# 固定すると、言い回しを修正するたびにテストを書き換えることになる)。


def load_error(tmp_path, document, name="p") -> str:
    """手順の文書を 1 つ書いて読み込み、拒否された文言を返す。"""
    root = write_project(tmp_path, {name: document})
    with pytest.raises(ProcedureError) as ei:
        compile_procedure(root, name)
    return str(ei.value)


def block_error(tmp_path, block, name="p") -> str:
    """ブロック 1 つだけの手順を読み込み、拒否された文言を返す。"""
    return load_error(tmp_path, doc([block]), name)


def test_broken_json_is_rejected(tmp_path):
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "frametables").mkdir(exist_ok=True)
    (tmp_path / "procedures" / "p.json").write_text(
        '{"kind": "procedure", "format": 1, "body": [', encoding="utf-8")
    with pytest.raises(ProcedureError, match="ファイルが壊れています"):
        compile_procedure(tmp_path, "p")


def test_non_utf8_file_is_rejected(tmp_path):
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "frametables").mkdir(exist_ok=True)
    # 「あ」を Shift_JIS で書いたバイト列は UTF-8 として読めない
    (tmp_path / "procedures" / "p.json").write_bytes(
        b'{"kind": "procedure", "format": 1, "precondition": "\x82\xa0", "body": []}')
    with pytest.raises(ProcedureError, match="文字コードが UTF-8 ではありません"):
        compile_procedure(tmp_path, "p")


def test_header_is_checked(tmp_path):
    assert "手順ファイルを指定してください" in load_error(
        tmp_path, {"kind": "condition", "format": 1, "body": []})
    assert "新しいバージョンの padcue で作成されています" in load_error(
        tmp_path, {"kind": "procedure", "format": procfmt.FORMAT_VERSION + 1,
                   "body": []})
    # 読めるバージョンより新しいもののほかは、今のバージョンでなければ形式の誤りである
    want = f"format は {procfmt.FORMAT_VERSION} である必要があります"
    for fmt in (str(procfmt.FORMAT_VERSION), float(procfmt.FORMAT_VERSION),
                True, None, procfmt.FORMAT_VERSION - 1):
        assert f"{want}: {json.dumps(fmt)}" in load_error(
            tmp_path, {"kind": "procedure", "format": fmt, "body": []}), fmt
    assert f"{want}: null" in load_error(
        tmp_path, {"kind": "procedure", "body": []})
    # 手順の名前はファイル名そのものなので、文書の中には書けない(file-format.md §2)
    assert "使用できない項目です: name" in load_error(
        tmp_path, {"kind": "procedure", "format": procfmt.FORMAT_VERSION,
                   "name": "p", "body": []})
    assert "body はブロックの配列である必要があります" in load_error(
        tmp_path, {"kind": "procedure", "format": procfmt.FORMAT_VERSION,
                   "body": {}})


def test_unknown_block_type_is_rejected(tmp_path):
    assert "サポートされていないブロックの種類です" in block_error(
        tmp_path, {"type": "dance"})


def test_unknown_key_is_rejected(tmp_path):
    assert "使用できない項目です: frames2" in block_error(
        tmp_path, {"type": "wait", "frames": 5, "frames2": 1})


def test_id_on_a_block_without_one_is_an_unknown_key(tmp_path):
    """識別子を持つのは 6 種のブロックだけであること(file-format.md §6-1)。"""
    assert "使用できない項目です: id" in block_error(
        tmp_path, {"type": "press", "buttons": ["A"], "frames": 3, "id": "x"})


def test_missing_required_key_is_rejected(tmp_path):
    # ファイルを書く人は欄の名前で、実行する人は画面の用語で読む
    assert "長さ(frames)がありません" in block_error(tmp_path, {"type": "wait"})


def test_value_out_of_range_is_rejected(tmp_path):
    assert "frames は 1 から" in block_error(
        tmp_path, {"type": "wait", "frames": 0})
    assert "x は -2048 から 2047 までの整数である必要があります" in block_error(
        tmp_path, {"type": "stick", "side": "L", "x": 3000, "y": 0})


def test_value_outside_the_fixed_choices_is_rejected(tmp_path):
    assert 'wait_input は "release"' in block_error(
        tmp_path, {"type": "branch", "id": "br-1", "wait_input": "hold",
                   "arms": []})


def test_unknown_button_is_rejected(tmp_path):
    assert "使用できないボタン名です" in block_error(
        tmp_path, {"type": "press", "buttons": ["Z"], "frames": 3})


def test_identifier_form_is_checked(tmp_path):
    assert "識別子に使用できるのは英数字と _ と - です" in block_error(
        tmp_path, {"type": "label", "id": "ラベル", "name": "開始"})
    assert "識別子は 1 から 32 文字である必要があります" in block_error(
        tmp_path, {"type": "label", "id": "a" * 33, "name": "開始"})
    assert "@ で始まる識別子は使用できません" in block_error(
        tmp_path, {"type": "label", "id": "@whole", "name": "開始"})
    assert "id がありません" in block_error(
        tmp_path, {"type": "label", "name": "開始"})


def test_duplicate_identifier_is_rejected(tmp_path):
    assert "識別子 lb-1 が 2 か所にあります" in load_error(tmp_path, doc([
        {"type": "label", "id": "lb-1", "name": "開始"},
        {"type": "wait", "frames": 5},
        {"type": "label", "id": "lb-1", "name": "終了"},
    ]))


def test_duplicate_identifier_counts_disabled_blocks_too(tmp_path):
    """識別子の重複は、無効にしたブロックも含めて数えること(file-format.md §4-4)。

    片方を無効にしておくと読めるが、有効に戻した瞬間に読めなくなる、
    という状態を作らないためである。
    """
    assert "識別子 lb-1 が 2 か所にあります" in load_error(tmp_path, doc([
        {"type": "label", "id": "lb-1", "name": "開始"},
        {"type": "wait", "frames": 5},
        {"type": "label", "id": "lb-1", "name": "終了", "disabled": True},
    ]))


def test_ref_must_follow_the_name_rule(tmp_path):
    assert "名前に「/」は使用できません" in block_error(
        tmp_path, {"type": "call", "id": "cl-1", "ref": "../外"})


def test_loop_count_is_checked(tmp_path):
    body = [{"type": "wait", "frames": 5}]
    assert "count は 1 以上である必要があります" in block_error(
        tmp_path, {"type": "loop", "id": "lp-1", "count": 0, "body": body})
    assert 'count は 1 以上の整数か "infinite" である必要があります' in block_error(
        tmp_path, {"type": "loop", "id": "lp-1", "count": "たくさん",
                   "body": body})


def test_values_are_shown_as_written_in_the_file(tmp_path):
    """拒否する文に出す値は、ファイルに書く形(JSON)で見せること。

    手で書く人が見ているのは JSON である。Python の表記(True・'たくさん')で
    見せると、ファイルのどこを修正すればよいかを読み替えることになる。
    """
    msg = block_error(tmp_path, {"type": "wait", "frames": True})
    assert msg.endswith(": true"), msg
    msg = block_error(tmp_path, {"type": "loop", "id": "lp-1", "count": "たくさん",
                                 "body": [{"type": "wait", "frames": 5}]})
    assert msg.endswith(': "たくさん"'), msg


def test_two_lap_loops_are_rejected(tmp_path):
    """周回数を選べるくり返しは、1 つの手順に 1 つまでであること(file-format.md §4-3)。
    """
    body = [{"type": "wait", "frames": 5}]
    assert "周回数を指定できるくり返しは手順の中で 1 つだけです" in load_error(
        tmp_path, doc([
            {"type": "loop", "id": "lp-1", "laps": True, "body": body},
            {"type": "loop", "id": "lp-2", "laps": True, "body": body},
        ]))


def test_two_lap_loops_count_disabled_blocks_too(tmp_path):
    """周回数のくり返しの数も、
    無効にしたブロックを含めて数えること(file-format.md §4-4)。
    """
    body = [{"type": "wait", "frames": 5}]
    assert "周回数を指定できるくり返しは手順の中で 1 つだけです" in load_error(
        tmp_path, doc([
            {"type": "loop", "id": "lp-1", "laps": True, "body": body},
            {"type": "loop", "id": "lp-2", "laps": True, "disabled": True,
             "body": body},
        ]))


def test_allow_value_must_be_in_the_list(tmp_path):
    """allow の値は一覧にあるものだけを受け付けること(file-format.md
    §4-1)。綴りを間違えた allow を通すと、消したはずの警告が出続ける。
    """
    assert "サポートされていない allow の値です" in block_error(
        tmp_path, {"type": "wait", "frames": 5, "allow": ["gyro_const"]})


def test_capture_must_be_names(tmp_path):
    """映像ソースは名前だけを書くこと。実体は PC ごとに違う(file-format.md §4-2)。"""
    assert "映像ソースの名前は空でない文字列である必要があります" in block_error(
        tmp_path, {"type": "script", "id": "sc-1", "cmd": "x", "capture": [1]})


# ---- 分岐と選択肢(file-format.md §5) ----

def branch_error(tmp_path, arm, extra=None) -> str:
    """選択肢 1 つだけの分岐を読み込み、拒否された文言を返す。"""
    return load_error(tmp_path, doc([*(extra or []),
                                     {"type": "branch", "id": "br-1",
                                      "arms": [arm]}]))


def test_what_must_not_be_null(tmp_path):
    """待たない選択肢は、what を書かないことで表すこと(file-format.md §5-2)。"""
    assert "what には kind が必要です" in branch_error(
        tmp_path, {"name": "甲", "what": None})


def test_unknown_what_kind_is_rejected(tmp_path):
    assert "サポートされていない kind の値です" in branch_error(
        tmp_path, {"name": "甲", "what": {"kind": "cond"}})


def test_timeout_frames_must_be_at_least_one(tmp_path):
    assert "時間切れは 1 フレーム以上である必要があります" in branch_error(
        tmp_path, {"name": "甲", "what": {"kind": "timeout", "frames": 0}})


def test_unknown_when_kind_is_rejected(tmp_path):
    assert "サポートされていない kind の値です" in branch_error(
        tmp_path, {"name": "甲", "when": {"loop": "lp-1", "kind": "middle"}})


def test_when_n_is_required_for_some_kinds(tmp_path):
    assert "1 以上の整数の n が必要です" in branch_error(
        tmp_path, {"name": "甲", "when": {"loop": "lp-1", "kind": "nth"}})


def test_when_n_cannot_be_written_for_other_kinds(tmp_path):
    assert 'kind が "first" のときは n を使用できません' in branch_error(
        tmp_path, {"name": "甲",
                   "when": {"loop": "lp-1", "kind": "first", "n": 2}})


def test_when_must_point_at_a_loop(tmp_path):
    """「対象の回」の loop の値は、くり返しの識別子であること(file-format.md §8-1)。"""
    msg = branch_error(
        tmp_path, {"name": "甲", "when": {"loop": "lb-1", "kind": "first"}},
        extra=[{"type": "label", "id": "lb-1", "name": "開始"}])
    assert "「対象の回」に指定できるのはくり返しだけです" in msg


def test_unknown_arm_key_is_rejected(tmp_path):
    assert "使用できない項目です: cond" in branch_error(
        tmp_path, {"name": "甲", "cond": "完了画面"})


# ---- 参照の先(file-format.md §6-3) ----

def test_missing_frame_table_is_rejected(tmp_path):
    """フレーム表だけは、読み込みのときに存在を確認すること(file-format.md §6-3)。"""
    assert "フレーム表「nothing」が見つかりません" in block_error(
        tmp_path, {"type": "frametable", "ref": "nothing"})


def test_missing_procedure_is_rejected_at_compile(tmp_path):
    """呼び出し先の手順がないことは、読み込みでは拒否せず、
    変換が拒否すること(file-format.md §6-3・§8-2)。
    """
    # ブロックの種類の名前(別の手順)は文の頭の「N 番目(別の手順)」が示すので、
    # 文の中では呼び出し先を「手順「X」」と呼ぶ(後ろの「別の手順を選択」と重ねない)
    why = block_error(tmp_path, {"type": "call", "id": "cl-1", "ref": "いない"})
    assert "手順「いない」が見つかりません。別の手順を選択してください。" in why, why
    assert "別の手順「" not in why, why
