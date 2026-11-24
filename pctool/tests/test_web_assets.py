"""画面のファイル(HTML・CSS・JS)と Python 側の表が矛盾していないこと。

JS からは Python の定数を読めないので、同じ内容が 2 か所に書かれる所がある。
そこは必ずここで照合する。片方だけ修正された状態を通さないため。
あわせて、画面のファイルのうち 1 か所にまとめた処理を、ほかのファイルが直接
書いていないことも見る。
"""
import math
import re

from padcue import binfmt
from padcue.conditions import AFTER_COUNT
from padcue.dsl import _FRAMETABLE_AXES, _GYRO_AXIS_JA
from padcue.gui import _SCRIPTS, web_asset
from padcue.procfmt import _BLOCK_KEYS
from padcue.timeline import BUTTON_ORDER
from tests.conftest import REPO
from tests.helpers import compile_nodes


def test_gui_button_bits_match_binfmt():
    """GUI(JS)のビット表が送信データのビット順と一致すること。

    表示順(BUTTONS)から作ると、DU が PLUS になるなど、ビット 8 以降の
    全ボタンが別ボタンとして送られる。
    """
    js = web_asset("manual.js")
    m = re.search(r"const BTN_BITS = \[(.*?)\];", js, re.S)
    assert m, "BTN_BITS が見つかりません"
    names = re.findall(r"'([A-Z]+)'", m.group(1))
    expect = [name for name, _bit in
              sorted(binfmt.BUTTONS.items(), key=lambda kv: kv[1])]
    assert names == expect, f"\nJS : {names}\n正 : {expect}"


def test_all_button_lists_cover_the_same_buttons():
    """ボタンの種類が、形式・タイムライン・編集画面で揃っていること。

    ボタンを追加するときに変更する所は 3 つある(binfmt.BUTTONS、timeline.BUTTON_ORDER、
    web/core.js の BTN_GROUPS)。1 つ忘れると、そのボタンだけ横棒が出ない、
    フレーム表に列がない、といった抜けが生じる。順序は用途ごとに違ってよい
    (送信のビット順・横棒の並び・画面のグループ)ので種類だけを見る。
    """
    m = re.search(r"const BTN_GROUPS = \[(.*?)\];", web_asset("core.js"), re.S)
    assert m, "BTN_GROUPS が見つかりません"
    js = re.findall(r"'([A-Z]+)'", m.group(1))
    assert len(js) == len(set(js)), f"JS のボタンに重複があります: {js}"
    assert set(js) == set(binfmt.BUTTONS), (
        f"\n編集画面: {sorted(js)}\n形式    : {sorted(binfmt.BUTTONS)}")
    assert set(BUTTON_ORDER) == set(binfmt.BUTTONS), (
        f"\nタイムライン: {sorted(BUTTON_ORDER)}\n形式    : {sorted(binfmt.BUTTONS)}")


def test_palette_kinds_match_the_saved_format():
    """パレットのブロックの種類が、手順の保存形式の種類と一致すること。

    種類を追加するときに変更する所は 2 つある(core.js の PALETTE_COLUMNS と
    procfmt.py の _BLOCK_KEYS)。パレットにだけ追加すると、置いたとたんに保存が
    「知らない種類」として拒否する。形式にだけ追加すると、その種類は画面から置けない。
    並びは画面の都合で決めてよいので種類だけを見る。
    """
    m = re.search(r"const PALETTE_COLUMNS = \[(.*?)\n\];", web_asset("core.js"), re.S)
    assert m, "PALETTE_COLUMNS が見つかりません"
    kinds = re.findall(r"\['([a-z]+)'", m.group(1))
    assert len(kinds) == len(set(kinds)), f"PALETTE_COLUMNS に重複があります: {kinds}"
    assert set(kinds) == set(_BLOCK_KEYS), (
        f"\nパレット: {sorted(kinds)}\n保存形式: {sorted(_BLOCK_KEYS)}")


def test_gyro_axis_labels_agree_between_compiler_and_screen():
    """ジャイロ 3 軸の呼び名と並びが、コンパイラーと画面で一致していること。

    軸の対応は、GP が gx でひねり(ロール)、GY が gy で縦(ピッチ)、GR が gz で
    横(ヨー)である。
    実機確認済みは GY と GR(docs/glossary.md §2)。この対応はワイヤの並びで
    決まっていて入れ替えられないのに、呼び名は手順のファイルの欄と画面で別々に
    書かれている。片方が gp と gy を混同したまま残ると、そのとおりに書いた人は
    「縦に振る」つもりで未確認のロール軸を動かす。

    呼び名そのものは欄の名前(core.js の AXIS)が正で、ブロックの 1 行の要約と
    変換の警告はそこから取る。3 か所に直書きすると、1 つだけ修正したときに
    同じ軸が 2 つの名で呼ばれる。
    """
    # 手順のファイルの欄 gp / gy / gr が、それぞれ生値の gx / gy / gz になる
    c = compile_nodes([{"type": "gyro", "gp": 11, "gy": 22, "gr": 33},
                       {"type": "wait", "frames": 1}])
    st = next(e for e in c.events if isinstance(e, binfmt.State))
    assert (st.gx, st.gy, st.gz) == (11, 22, 33)
    # フレーム表の列名も同じ対応
    assert [_FRAMETABLE_AXES[c] for c in ("GP", "GY", "GR")] == ["gx", "gy", "gz"]

    # 呼び名の正は欄の名前。「ひねり -32768〜32767」の先頭語
    m = re.search(r"const AXIS = \{(.*?)\};", web_asset("core.js"), re.S)
    assert m, "AXIS が見つかりません"
    axis = dict(re.findall(r"(\w+): '([^ ']+)", m.group(1)))
    want = [axis["GP"], axis["GY"], axis["GR"]]
    # 変換の警告(ゆらぎ幅が縮んだときに軸を指定する)
    assert list(_GYRO_AXIS_JA) == want, f"\n変換: {_GYRO_AXIS_JA}\n欄  : {want}"
    # 画面のブロックの 1 行の要約。並びは画面の欄と同じ縦・横・ひねり
    js = re.search(r"const v = \[(.+?)\]\s*\n?\s*\.filter", web_asset("procedure.js"),
                   re.S)
    assert js, "gyro ブロックの表示が見つかりません"
    pairs = re.findall(r"\['(.+?)', n\.(g[pyr])\]", js.group(1))
    assert pairs == [(axis["GY"], "gy"), (axis["GR"], "gr"),
                     (axis["GP"], "gp")], pairs


def test_local_storage_is_used_only_through_the_guarded_helpers():
    """ブラウザーの保存領域(localStorage)は、core.js の readStore と writeStore だけが
    読み書きすること。

    保存を拒否するブラウザー(保存を許していない設定、容量の不足)では、getItem と
    setItem が例外を投げる。直接呼ぶ所が 1 つでもあると、そのファイルは読み込みの
    途中で中断し、後に読むファイルも含めて画面全体が動かない。index.html の head は、
    最初の描画の前に配色を適用するために core.js より先に動くので、そこだけは自分で
    例外を受け取る(このテストの外)。
    """
    def code_lines(name):
        for i, line in enumerate(web_asset(name).splitlines(), 1):
            code = line.split("//")[0]
            if "localStorage" in code:
                yield i, code.strip()

    bad = [f"{name}:{i}: {code}" for name in _SCRIPTS if name != "core.js"
           for i, code in code_lines(name)]
    assert not bad, "localStorage を直に使っている: " + "; ".join(bad)
    core = [code for _i, code in code_lines("core.js")]
    assert len(core) == 2 and all(c.startswith("try {") for c in core), core


def test_the_screen_modules_depend_one_way():
    """画面のモジュールの依存が、main.js に並べた順に下から上へ向かうこと。

    循環すると、読み込みの順によって初期化の前の名前を読み、どの画面の状態がどこで
    変わるのかも追えなくなる。上の層を呼ぶときは core.js の links を通す。
    """
    order = re.findall(r"^import '\./([a-z]+\.js)';", web_asset("main.js"), re.M)
    assert sorted(order) == sorted(s for s in _SCRIPTS if s != "main.js")
    bad = []
    for i, name in enumerate(order):
        for dep in re.findall(r"^(?:import \{[^}]*)?\} from '\./([a-z]+\.js)';",
                              web_asset(name), re.M):
            if order.index(dep) >= i:
                bad.append(f"{name} -> {dep}")
    assert not bad, bad


def test_the_runbook_waits_as_long_as_the_screen_shoots_after_a_reference():
    """runbook.md §6-1 の「待つ秒数」が、基準画像の直後の画面を取得する間隔と枚数に
    合うこと。

    直後の画面は、基準画像を追加した時から condition.js の AFTER_GAP_MS ずつ間を
    空けて、conditions.py の AFTER_COUNT 枚取得する。runbook.md の秒数がそれより短いと、
    書かれたとおりに場面を変えた人の 2 枚目が別の場面になり、しきい値の目安が甘く
    出る。画面は取得し終えるまで小さな絵に「取得中」を出すが、runbook.md の秒数も
    同じ値に揃える。runbook.md が引くバッジの名前も、画面が出す名前と同じにする。
    """
    m = re.search(r"const AFTER_GAP_MS = (\d+);", web_asset("condition.js"))
    assert m, "AFTER_GAP_MS が見つかりません"
    want = math.ceil(AFTER_COUNT * int(m.group(1)) / 1000)
    badge = re.search(r"\bcondshooting', '([^']+)'", web_asset("condition.js"))
    assert badge, "小さな絵のバッジ(condshooting)の文字が見つかりません"
    runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
    step = runbook.split("### 6-1.")[1].split("\n4. ")[0]
    assert f"「{badge.group(1)}」" in step, (badge.group(1), step)
    got = re.findall(r"(\d+) 秒ほど", step)
    assert got == [str(want)], (got, want)


def test_the_procedure_panel_heading_starts_as_it_is_drawn():
    """手順の編集の右の欄の見出しは、読み込んだときの文字が、手順を開いていない
    ときに表示する文字と同じであること。

    違うと、開ける手順がないまま画面に入ったときと、開いていた手順を消した
    ときとで、同じ状態が 2 通りの見出しで出る。
    """
    m = re.search(r'<h2 id="propshead">(.*?)</h2>', web_asset("index.html"))
    assert m, "右の欄の見出しが見つかりません"
    js = web_asset("procprops.js")
    drawn = re.findall(r"getElementById\('propshead'\)\.textContent = '(.*?)'", js)
    assert drawn == [m.group(1)], (drawn, m.group(1))
