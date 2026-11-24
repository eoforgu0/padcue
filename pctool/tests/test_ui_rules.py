"""画面のテスト(用語の別名、title、文章の選択)。

読むのは JS の文字列と index.html で、CSS の `content:` は読まない。文言ではない記号
まで検出し、除外の一覧が増え続けるためである。
"""
import re

from tests.conftest import REPO
from tests.helpers import aliases_in, html_texts

WEB = REPO / "pctool/padcue/web"

# 日本語を含む文字列だけが文言である(識別子・CSS の値・内部名は文言ではない)
JA = re.compile(r"[ぁ-んァ-ヶ一-龥]")
# JS の文字列リテラル 3 種。`\` でエスケープした引用符をまたいで取り出す
JS_STR = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""", re.S)


def screen_texts() -> list[tuple[str, int, str]]:
    """画面に出る文字を (ファイル名, 行, 文字列) ですべて返す。"""
    out = []
    for p in sorted(WEB.glob("*.js")):
        for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            # 行の注釈は読む相手が開発者なので除く。ただし「//」より前に
            # 日本語があれば、その行は文言を持っている
            code = ln.split("//")[0]
            for m in JS_STR.finditer(code):
                if JA.search(m.group(2)):
                    out.append((p.name, i, m.group(2)))
    out += [("index.html", i, s) for i, s in html_texts(WEB / "index.html")]
    return out


def test_the_check_reads_the_screen():
    """このテストが実際に画面の文字を読んでいること。

    取り出せていなければ、下のテストは何も見ずに通る。
    """
    texts = screen_texts()
    assert len(texts) > 300, f"画面の文言が {len(texts)} 件しか取り出せていない"
    names = {name for name, _, _ in texts}
    assert "index.html" in names and "run.js" in names, names
    assert any("手動操作" in s for _, _, s in texts), "既知の文言が取り出せていない"


def test_the_screen_uses_one_name_for_each_thing():
    """同じものを 2 つの名で呼ばないこと。"""
    bad = [f"{name}:{i} {s!r} {hit}" for name, i, s in screen_texts()
           for hit in aliases_in(s)]
    assert not bad, "画面に別名がある: " + " / ".join(bad)


def titled_elements() -> list[tuple[int, str, str]]:
    """title を持つ押す物・欄を (行, 見えている文字, title) で返す。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"<(button|label)\b([^>]*)>(.*?)</\1>", html, re.S):
        t = re.search(r'title="([^"]*)"', m.group(2))
        if not t:
            continue
        text = " ".join(re.sub(r"<[^>]+>", " ", m.group(3)).split())
        out.append((html[:m.start()].count("\n") + 1, text, t.group(1)))
    return out


def test_only_things_without_text_have_titles():
    """文字のある押す物・欄に title を付けないこと(gui-principles.md §7-12)。

    title は、文字のない物(記号だけのボタン)の名前と、押せない理由だけに使う。
    わざわざ説明があると、読み手は「自分の想像と違う挙動だからでは」と深読みする
    (gui-principles.md §7-1)。押せない理由は押せないときに JS が入れるので、
    index.html には書かない。見るのは index.html に書いた button と label だけで、
    JS が作る要素は見ない。
    """
    titled = titled_elements()
    assert any(text == "⚙" for _, text, _ in titled), \
        "title 付きの要素が取り出せていない"
    bad = [f"{i}行目 {text!r} の title {title!r}"
           for i, text, title in titled if JA.search(text)]
    assert not bad, "文字のある物に title がある: " + " / ".join(bad)


def test_only_handles_block_text_selection():
    """文章を選べなくしないこと。外してよいのは、ドラッグして動かすハンドルだけ。

    選べない文章は、コピーして人に見せることができない。例えば、装置へつながらない
    理由の文は一覧の行の中にあるので、行ごと選択を外すと、画面を見て手で
    打ち直すことになる。

    ドラッグするのはハンドル(`.grab` / `.bgrab`)と、それ自体を引っ張り出すパレットの
    項目(`.pal`)だけである。行やブロックはハンドルからドラッグするので、中身の文章は
    選べたままでよい。
    """
    css = (WEB / "app.css").read_text(encoding="utf-8")
    allowed = {".grab", ".bgrab", ".pal"}
    bad = []
    for m in re.finditer(r"^([^{\n]+)\{([^}]*)\}", css, re.M):
        if "user-select:none" not in m.group(2).replace(" ", ""):
            continue
        for sel in (s.strip() for s in m.group(1).split(",")):
            if sel.split()[0].split(":")[0] not in allowed:
                bad.append(sel)
    assert not bad, f"ドラッグするハンドルではないのに選択を外している: {bad}"
