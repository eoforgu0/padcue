"""文書のテスト。読む人が先へ進めなくなる誤りを見る。

引用(>)の中のフェンスは数え漏らしやすく、表は区切り行の 1 セルが空なだけで表として
描画されない。リンクと節の引用は、移動や並べ替えで切れても読んでいて気づけない。
案内したコマンドがなければ、困っている人に使えない手段を案内することになる。
"""
from __future__ import annotations

import ast
import contextlib
import io
import re
import shlex
import subprocess
import unicodedata

import pytest

from padcue import cli
from padcue.project import Project
from tests.conftest import REPO
from tests.helpers import aliases_in

# 追跡している文書だけを見る(取得した依存ライブラリの README は対象外)
SKIP = ("managed_components", ".pytest_cache", "node_modules")


# 別名の一覧を定義しているファイル。コードの照合から外す
ALIASES_DEFINED_IN = "pctool/tests/helpers.py"
# コードとして照合するファイル(注釈・docstring・文字列を含む)
CODE_SUFFIXES = (".py", ".js", ".css", ".html", ".c", ".h")


def tracked_paths() -> set | None:
    """追跡しているファイルと、それを含むフォルダーのパス。git が使えなければ None。"""
    try:
        got = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return None
    paths = set(got)
    for p in got:
        parts = p.split("/")
        paths.update("/".join(parts[:i]) for i in range(1, len(parts)))
    return paths


def docs() -> list:
    """追跡している Markdown の一覧。

    git から取得する。フォルダーを探すと、テストのツールの出力先や追跡していない
    作業ファイルまで検出し、リポジトリの中身と関係なく失敗する。git が使えないとき
    (zip で取得したとき)だけ、フォルダーを探す。
    """
    try:
        got = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files", "*.md"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        got = []
    if not got:
        got = [p.relative_to(REPO).as_posix() for p in REPO.rglob("*.md")]
    return [REPO / rel for rel in sorted(got)
            if not any(s in rel for s in SKIP)]


def lines_of(p) -> list:
    return p.read_text(encoding="utf-8").split("\n")


def _fence(s: str) -> bool:
    """引用の中のフェンスも数える(`> ```py` のような行)。"""
    return s.lstrip("> ").startswith("```")


def _fence_lang(s: str) -> str:
    return s.lstrip("> ")[3:].strip()


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_code_fences_declare_a_language(path):
    """コードフェンスに言語が付いていること(引用の中も含む)。

    色分けされるだけでなく、「貼り付けるコマンドか、画面に出る文字か」を
    読む人が区別できる。
    """
    bad, inside = [], False
    for i, ln in enumerate(lines_of(path), 1):
        if not _fence(ln.strip()):
            continue
        if inside:
            inside = False
            continue
        inside = True
        if not _fence_lang(ln.strip()):
            bad.append(i)
    assert not bad, f"言語のないコードフェンス: {path.name} の {bad} 行目"


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_tables_are_valid_gfm(path):
    """表が GFM の表として成立していること。

    見出し行の次に区切り行(セルが `-` と `:` だけの行)があるとき、その列数が
    見出し行と同じであること。列数が違うと、表として描画されない。区切り行が
    ない塊や、セルが空の区切り行は、次のテストが見る。
    """
    def cells(s: str) -> list:
        s = s.strip().lstrip(">").strip()
        return [c.strip() for c in s.split("|")[1:-1]]

    src, inside, bad = lines_of(path), False, []
    for i, ln in enumerate(src):
        s = ln.strip()
        if _fence(s):
            inside = not inside
            continue
        if inside or not s.lstrip(">").strip().startswith("|"):
            continue
        nxt = src[i + 1].strip() if i + 1 < len(src) else ""
        if not nxt.lstrip(">").strip().startswith("|"):
            continue
        sep = cells(nxt)
        if not sep or not all(re.fullmatch(r":?-{2,}:?", c) for c in sep):
            continue                      # ここは区切り行ではない(本文の行)
        head = cells(s)
        if len(head) != len(sep):
            bad.append((i + 1, f"見出し {len(head)} 列 / 区切り {len(sep)} 列"))
    assert not bad, f"{path.name}: 表の区切り行が見出しと合っていない {bad}"


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_every_pipe_block_has_a_separator(path):
    """パイプで始まる行の塊には、必ず区切り行があること。

    上のテストは「区切り行が見出しと同じ列数か」を見るので、区切り行が最初から
    ない形には気づけない。表の途中に空行が入ると、そこから下の行は区切り行を
    持たない孤立した塊になり、GFM では生のパイプ文字のまま表示される。
    """
    src, inside, blocks, cur = lines_of(path), False, [], []
    for i, ln in enumerate(src):
        s = ln.strip()
        if _fence(s):
            inside = not inside
            cur = []
            continue
        if not inside and s.lstrip(">").strip().startswith("|"):
            cur.append((i + 1, s))
            continue
        if cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    def is_sep(s: str) -> bool:
        cs = [c.strip() for c in s.strip().lstrip(">").strip().split("|")[1:-1]]
        return bool(cs) and all(re.fullmatch(r":?-{2,}:?", c) for c in cs)

    bad = [b[0][0] for b in blocks if len(b) > 1 and not is_sep(b[1][1])]
    assert not bad, (
        f"{path.name}: 区切り行を持たないパイプ行の塊({bad} 行目)。"
        "表の途中に空行が入っていないか確認")


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_relative_links_resolve(path):
    """文書中の相対リンクの指す先が実在すること。"""
    text = path.read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = m.group(1).split("#")[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (path.parent / target).resolve().exists():
            bad.append(target)
    assert not bad, f"{path.name}: 指す先がないリンク {bad}"


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_links_point_to_tracked_files(path):
    """公開する文書のリンクが、リポジトリが追跡している物を指していること。

    上のテストは「指す先が手元に実在するか」しか見ない。追跡していないファイルは
    書いた人の手元には実在するので、そこへのリンクは手元では通り、リポジトリを
    取得した人の手元でだけ切れる。
    """
    tracked = tracked_paths()
    if tracked is None:
        pytest.skip("git が使えない(zip で取得したとき)")
    text = path.read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = m.group(1).split("#")[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        rel = (path.parent / target).resolve()
        try:
            rel = rel.relative_to(REPO).as_posix()
        except ValueError:
            bad.append(f"{target}(リポジトリの外)")
            continue
        if rel not in tracked:
            bad.append(f"{target} -> {rel}")
    assert not bad, (
        f"{path.name}: 追跡していない場所へのリンク {bad}。"
        "取得した人の手元では切れる")


# ---- 節の引き方 ----

# 節の記号(文字そのものをここに書くと、このテストが自分を検出する)
SECTION_MARK = "\u00a7"
# 節の番号。`3`、`4-4`
SECTION_NO = r"(\d+(?:-\d+)?)"
# 引いている文書の名前。`file-format.md §2`、`[file-format.md](file-format.md) §2`。
# 番号を「・」で並べた続きも、同じ文書の節とみなす。注釈の中で行が折り返されていてもよい
CITED_DOC = re.compile(
    r"([\w-]+)\.md\)?\]?(?:\([^)]*\))?"
    r"(?:\s*(?:#|//|/\*|\*|<!--)?\s*" + SECTION_MARK + r"[\w-]+\s*・)*"
    r"\s*(?:#|//|/\*|\*|<!--)?\s*$")
# ほかの規格の節(このリポジトリの文書ではない)
FOREIGN_SPEC = ("PNG 仕様 ",)


def _anchors(path) -> set:
    """文書の節の番号。見出しの番号と、節の中の番号付きの項目(`7-12`)。"""
    got, top = set(), None
    for s in lines_of(path):
        m = re.match(r"#{2,4} +" + SECTION_NO + r"[. ]", s)
        if m:
            got.add(m.group(1))
            if s.startswith("## "):
                top = m.group(1)
            continue
        if s.startswith("## "):
            top = None
        m = re.match(r"(\d+)\. ", s)
        if m and top:
            got.add(f"{top}-{m.group(1)}")
    return got


def _cited_sections():
    """追跡しているファイルの、節を引いている所。(ファイル, 行, 文書の名前, 番号)。

    文書の名前が前になければ、名前は None である。
    """
    tracked = tracked_paths() or set()
    for rel in sorted(tracked):
        if not rel.endswith((".md", *CODE_SUFFIXES)) or rel == "CHANGELOG.md":
            continue
        if any(s in rel for s in SKIP) or not (REPO / rel).is_file():
            continue
        text = (REPO / rel).read_text(encoding="utf-8")
        for m in re.finditer(SECTION_MARK + SECTION_NO, text):
            before = text[max(0, m.start() - 300):m.start()]
            if before.endswith(FOREIGN_SPEC):
                continue
            doc = CITED_DOC.search(before)
            line = text.count("\n", 0, m.start()) + 1
            yield rel, line, doc.group(1) if doc else None, m.group(1)


def test_cited_sections_exist():
    """`文書の名前.md` と節の番号で引いた先の節が、その文書にあること。

    節の番号は、節を追加したり並べ替えたりすると変わる。引いている側は文書の外にも
    あり(コードの注釈など)、番号がずれても読んでいる人には分からない。
    """
    if tracked_paths() is None:
        pytest.skip("git が使えない(zip で取得したとき)")
    by_name: dict = {}
    for p in docs():
        by_name.setdefault(p.stem, []).append(p)
    anchors = {}
    bad = []
    for rel, line, name, no in _cited_sections():
        if name is None:
            continue
        paths = by_name.get(name, [])
        if len(paths) != 1:
            bad.append(f"{rel}:{line} {name}.md(文書が {len(paths)} つ)")
            continue
        if paths[0] not in anchors:
            anchors[paths[0]] = _anchors(paths[0])
        if no not in anchors[paths[0]]:
            bad.append(f"{rel}:{line} {name}.md の {no}")
    assert not bad, f"引いた先の節がない: {bad}"


def test_sections_are_cited_with_the_document_name():
    """節の記号の前には、引いている文書の名前があること。

    同じ文書の中は番号だけ(`3 節`、`4-4`)で引き、記号は付けない。コードの注釈に
    名前のない記号があると、どの文書の節かが分からない。
    """
    if tracked_paths() is None:
        pytest.skip("git が使えない(zip で取得したとき)")
    bad = [f"{rel}:{line} {no}" for rel, line, name, no in _cited_sections()
           if name is None]
    assert not bad, f"文書の名前がない節の記号: {bad}"


# ---- 要件・制約・方針・決定の記号 ----

# 記号を定める場所。要件と制約は requirements.md、方針は principles.md、決定は
# coupling.md にある(glossary.md §6)
SYMBOL_DEFS = (
    ("docs/requirements.md", r"- \*\*([RC]\d+)[*\s]"),
    ("docs/principles.md", r"### (P\d+) "),
    ("docs/specs/coupling.md", r"- \*\*(D\d+) "),
)
SYMBOL = re.compile(r"(?<![\w-])([RCPD]\d{1,2})(?![\w-])")
# 記号と同じ形のほかの名前。C11 は C 言語の規格、D65 は色の計算の標準の光
NOT_SYMBOLS = {"C11", "D65"}


def _defined_symbols() -> set:
    got = set()
    for rel, rx in SYMBOL_DEFS:
        for s in lines_of(REPO / rel):
            m = re.match(rx, s)
            if m:
                got.add(m.group(1))
    return got


def test_cited_symbols_are_defined():
    """文書とコードが引く R・C・P・D の記号が、定める文書にあること。

    方針や要件を追加したり外したりすると、番号が変わる。注釈は文書の外にあるので、
    消えた番号を引いたままでも誰も気づかない。
    """
    if tracked_paths() is None:
        pytest.skip("git が使えない(zip で取得したとき)")
    defined = _defined_symbols()
    bad = []
    for rel in sorted(tracked_paths()):
        if not rel.endswith((".md", *CODE_SUFFIXES)) or rel == "CHANGELOG.md":
            continue
        if any(s in rel for s in SKIP) or not (REPO / rel).is_file():
            continue
        for i, s in enumerate(lines_of(REPO / rel), 1):
            for m in SYMBOL.finditer(s):
                if m.group(1) not in defined | NOT_SYMBOLS:
                    bad.append(f"{rel}:{i} {m.group(1)}")
    assert not bad, f"定めていない記号を引いている: {bad}"


def test_the_glossary_states_the_range_of_each_symbol():
    """用語集が書く記号の範囲(R1〜R28 など)が、定めている記号と一致すること。"""
    defined = _defined_symbols()
    text = (REPO / "docs/glossary.md").read_text(encoding="utf-8")
    for kind in "RCPD":
        nums = sorted(int(s[1:]) for s in defined if s[0] == kind)
        assert nums == list(range(1, len(nums) + 1)), \
            f"{kind} の番号が飛んでいる: {nums}"
        want = f"{kind}1\u301c{kind}{nums[-1]}"
        assert want in text, f"用語集に {want} がない"


# ---- 図 ----


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_figures_use_only_fixed_width_characters(path):
    """図に、幅が環境で変わる文字を使っていないこと。

    East Asian Width が Ambiguous の文字(罫線素片・矢印・記号)は、環境に
    よって 1 桁にも 2 桁にも見える。桁を揃えて描く図に混ぜると崩れる。
    表は崩れても読めるので対象外である。
    """
    bad, inside = [], False
    for i, ln in enumerate(lines_of(path), 1):
        if _fence(ln.strip()):
            inside = not inside
            continue
        if not inside:
            continue
        for c in ln:
            if ord(c) > 127 and unicodedata.east_asian_width(c) == "A":
                bad.append((i, c))
    assert not bad, f"{path.name}: 図に幅の変わる文字 {bad}"


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_documents_use_one_name_for_each_thing(path):
    """公開する文書が、同じものの別名(helpers.ALIASES)で書かれていないこと。

    同じものを指す語が 2 つあると、読む人は別のものだと思う(P25)。
    """
    bad = [f"{i}行目 {hit}" for i, ln in enumerate(lines_of(path), 1)
           for hit in aliases_in(ln)]
    assert not bad, f"{path.name}: 別名 {bad}"


def test_code_uses_one_name_for_each_thing():
    """コードの注釈・docstring・文字列が、同じものの別名で書かれていないこと。"""
    if tracked_paths() is None:
        pytest.skip("git が使えない(zip で取得したとき)")
    bad = []
    for rel in sorted(tracked_paths()):
        if not rel.endswith(CODE_SUFFIXES) or rel == ALIASES_DEFINED_IN:
            continue
        p = REPO / rel
        if not p.is_file():
            continue
        for i, ln in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            bad += [f"{rel}:{i} {hit}" for hit in aliases_in(ln)]
    assert not bad, f"別名: {bad}"


CLI = REPO / "pctool" / "padcue" / "cli.py"


def cli_names() -> tuple:
    """CLI が実際に持つ下位コマンドと選択肢。

    `device` の下位だけは add_parser ではなく cmd_device の中の文字列比較で
    分けているので、そちらも対象にする。
    """
    src = CLI.read_text(encoding="utf-8")
    subs = set(re.findall(r'sub\.add_parser\(\s*"([a-z-]+)"', src))
    # --help は argparse が自分で追加する
    opts = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', src)) | {"--help"}
    devs = set(re.findall(r'a == "([a-z-]+)"', src))
    assert subs and opts and devs, "cli.py の書き方が変わった(このテストを見直すこと)"
    return subs, opts, devs


def test_documented_commands_exist():
    """文書が案内するコマンドと選択肢が、実在すること。

    先へ進めなくなった人が最後に頼るのが手順書である。そこに書いてあるコマンドが
    ないと、最も困っている人に、使えない手段を案内することになる。
    """
    subs, opts, devs = cli_names()
    bad = []
    for path in docs():
        for i, ln in enumerate(lines_of(path), 1):
            for m in re.finditer(r"padcue device ([a-z][a-z-]*)", ln):
                if m.group(1) not in devs:
                    bad.append(f"{path.name}:{i} `padcue device {m.group(1)}`")
            for m in re.finditer(r"padcue(?:\.bat)? ([a-z][a-z-]*)", ln):
                w = m.group(1)
                if w not in subs:
                    bad.append(f"{path.name}:{i} `padcue {w}`")
            # padcue のコマンドの行だけを見る(esptool など、ほかのツールの選択肢は
            # 見ない)
            if re.search(r"\bpadcue(?:\.bat)? ", ln):
                for m in re.finditer(r"(--[a-z][a-z-]+)", ln):
                    if m.group(1) not in opts:
                        bad.append(f"{path.name}:{i} `{m.group(1)}`")
    assert not bad, f"CLI にないものを案内している: {bad}"


# 打って確認しないもの。選択の余地がある書き方(かっこ・並び・省略)は、
# そのままでは 1 本のコマンドにならない
_NOT_A_COMMAND_LINE = ("[", "]", "...", "…", "|", "$", "%")

# f 文字列の置換フィールドの代わりに置く目印。名前かアドレスか数なので、並びを見る
# だけなら中身は問わない(引数がまるごと置換フィールドなら、数としても名前としても
# 通る 1 に、引数の一部なら x に替える)
_HOLE = "\u0001"
# 値の決まった欄の <…> は、そのままでは通らないので、実在する値に替えて並びを見る
_FILLED = {"<番号>": "1", "<終え方>": "end"}


def _text_of(node):
    """文字列の中身。f 文字列は置換フィールドを目印に替えて 1 本に戻す。

    ここで戻さないと、置換フィールドの前後で切れて並びの誤りを見逃す。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant)
            and isinstance(part.value, str) else _HOLE
            for part in node.values)
    return None


JAPANESE = re.compile("[ぁ-ゖァ-ヺー一-鿿]")


def _command_at(text, start):
    """text の start から始まる `padcue ...` を、命令として読める所まで取る。"""
    words = text[start:].split("\n", 1)[0].split(" ")
    out = [words[0]]
    for raw in words[1:]:
        w = raw.strip("`\u3002\u3001\uff09)\uff08(")
        if not w:
            break
        # <名前> のようなプレースホルダーは、中身が日本語でも命令の一部である
        if not (w.startswith("<") and w.endswith(">")) and JAPANESE.search(w):
            break
        out.append(w)
    if len(out) < 2:
        return None
    # バージョンの表記(`padcue 0.1.0`)などは命令ではない。命令は下位命令か選択肢で始まる
    if not re.match(r"^(--?[a-z]|[a-z])", out[1]):
        return None
    return " ".join(out)


def command_lines():
    """文書と、利用者に見せる文字列から、`padcue ...` の 1 本を取り出す。"""
    out = []
    for path in docs():
        for i, ln in enumerate(lines_of(path), 1):
            for m in re.finditer(r"`(padcue [^`]+)`", ln):
                out.append((f"{path.name}:{i}", m.group(1)))
    for path in sorted((REPO / "pctool" / "padcue").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # f 文字列の中の断片は見ない。見ると、置換フィールドの手前で切れた
        # 「padcue --device」だけが 1 本の命令として取り出される
        inner = {id(part) for n in ast.walk(tree)
                 if isinstance(n, ast.JoinedStr) for part in n.values}
        for node in ast.walk(tree):
            if id(node) in inner:
                continue
            text = _text_of(node)
            if text is None or "padcue " not in text:
                continue
            for m in re.finditer(r"padcue ", text):
                cmd = _command_at(text, m.start())
                if cmd:
                    out.append((f"{path.name}:{node.lineno}", cmd))
    return out


def test_documented_commands_actually_parse():
    """案内しているコマンドが、そのまま打って通ること。

    語が実在するだけでは足りない。例えば `padcue device <IP> --device 1P` は、
    語はすべて実在するが、`--device` は主パーサーの選択肢なので、サブコマンドの
    後ろに置くと「使用できない引数があります」で拒否される。語の実在を見るテストでは
    見つからない。困っている人に、通らないコマンドを打たせてはならない。

    見るのは並びだけである。文章の中では `padcue run -w` のように要点
    だけを書くことがあるので、書き足りない(位置引数の省略)は除かない。
    """
    bad = []
    for where, line in command_lines():
        if any(c in line for c in _NOT_A_COMMAND_LINE):
            continue
        try:
            argv = ["1" if a == _HOLE else _FILLED.get(a, a).replace(_HOLE, "x")
                    for a in shlex.split(line)[1:]]
        except ValueError:
            continue
        if not argv or "--help" in argv:
            continue          # --help は argparse が自分で受けて終わる
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                cli.build_parser().parse_args(argv)
        except SystemExit:
            msg = err.getvalue()
            if "引数が足りません" in msg:
                continue          # 要点だけを書いた引用
            # 使用法の行と、使い方の案内の行を除いた、誤りの文
            why = [s for s in msg.splitlines()
                   if s.strip() and not s.startswith(("使用法", " "))]
            bad.append(f"{where} `{line}`: {why[0] if why else msg.strip()}")
    assert not bad, f"案内しているのに打つと通らない: {bad}"


def test_the_readme_example_compiles(tmp_path):
    """README に掲載している手順とフレーム表が、そのまま通ること。

    最初の例が動かないと、読む人は自分の環境を疑う。形式を変えたときに、README の
    例だけが前の形のまま残りやすい。
    """
    src = (REPO / "README.md").read_text(encoding="utf-8")
    procedure = re.search(r'```json\n(\{\n  "kind".*?)\n```', src, re.S)
    frametable = re.search(r"```csv\n(F,.*?)\n```", src, re.S)
    assert procedure and frametable, "README の例が見つからない(このテストを見直すこと)"
    (tmp_path / "procedures").mkdir()
    (tmp_path / "frametables").mkdir()
    (tmp_path / "procedures" / "サンプル.json").write_text(
        procedure.group(1), encoding="utf-8")
    (tmp_path / "frametables" / "サンプル.csv").write_text(
        frametable.group(1) + "\n", encoding="utf-8")
    built = Project(tmp_path).build("サンプル")
    assert built.total_frames > 0 and built.blob


def test_the_help_lists_every_command():
    """`padcue --help` が、実在する下位コマンドをすべて記載していること。

    文書がないコマンドを案内するのと同じくらい、あるのに案内されないのも
    困る。
    """
    subs, _opts, _devs = cli_names()
    head = CLI.read_text(encoding="utf-8").split('"""')[1]
    missing = sorted(w for w in subs if f"padcue {w}" not in head)
    assert not missing, f"--help に出てこないコマンド: {missing}"
