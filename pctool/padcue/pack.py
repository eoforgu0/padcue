"""padcue ファイル(エクスポート)。手順の一式を 1 つのファイルにまとめる
(file-format.md §10)。

出すものが 1 件で、それが 1 つのファイルなら、そのファイルをそのまま出す。
それ以外は zip にして拡張子を `.padcue` にする。中の並びはプロジェクトと同じ
なので、展開すればそのまま読める。

    <名前>.padcue
      pack.json
      procedures/<名前>.json
      frametables/<名前>.csv
      conditions/<名前>/condition.json
      conditions/<名前>/images/3f2a9c0d1b7e4a55.png

入れないもの: 登録情報、プリセット、実行記録、表示順、変換の結果、操作権の識別子、
稼働中の目印、レーンの映像の設定、周回数と開始位置。それぞれの理由は file-format.md
§10-3 にある。

映像ソースの名前は捨てない(file-format.md §10-3)。捨てて「装置に
設定している映像」に戻すと、決まった画面を見ていた条件が警告なしに別の映像を
見ることになる。
"""
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import __version__
from .conditions import DOC_NAME, IMAGES, ConditionStore
from .files import write_file
from .names import name_key, validate_name
from .procrefs import refs_of
from .project import Project

# 構成情報のバージョン。手順や条件とは別に数える(file-format.md §10-1)
PACK_FORMAT = 1
SUFFIX = ".padcue"

# padcue ファイルの中の場所。プロジェクトと同じ並びにする(file-format.md §10-1)
DIR_PROC = "procedures"
DIR_FRAMETABLE = "frametables"
DIR_COND = "conditions"
MANIFEST = "pack.json"

# zip のエントリ名を UTF-8 だと示すビット。名前が日本語のまま入るので、
# これを立てないと、ほかのツールで展開したときに名前が文字化けする(file-format.md §10-1)
UTF8_FLAG = 0x800


@dataclass
class Contents:
    """padcue ファイルに入れるものの一覧。"""
    procedures: list[str] = field(default_factory=list)
    frametables: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    # 参照されているのに見つからなかったもの [(種類, 名前)]
    missing: list[tuple[str, str]] = field(default_factory=list)
    # ファイルはあるのに読めず、入れなかった手順 [(種類, 名前)]。中の参照を
    # たどれないので入れない。一覧では「エラー」の行で、見つからない物とは違う
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    def entries(self) -> list[dict]:
        return ([{"kind": "procedure", "name": n} for n in self.procedures]
                + [{"kind": "frametable", "name": n} for n in self.frametables]
                + [{"kind": "condition", "name": n} for n in self.conditions])

    def total(self) -> int:
        return (len(self.procedures) + len(self.frametables)
                + len(self.conditions))


def as_tops(tops: list) -> list[tuple[str, str]]:
    """選んだものを (種類, 名前) の並びに変換する。名前だけなら手順とみなす。"""
    out = []
    for t in tops:
        if isinstance(t, str):
            out.append(("procedure", t))
        elif isinstance(t, dict):
            out.append((t.get("kind", "procedure"), t.get("name", "")))
        else:
            out.append((t[0], t[1]))
    return out


def existing_names(project: Project, store: ConditionStore) -> dict:
    """種類ごとに、元からある名前を {照合のキー: 綴り} で返す。"""
    return {
        "procedure": {name_key(n): n for n in project.procedure_names()},
        "frametable": {name_key(n): n for n in project.frametable_names()},
        "condition": {name_key(n): n for n in store.names()},
    }


def collect(project: Project, tops: list) -> Contents:
    """選んだものから、必要なものをすべてたどって集める(file-format.md §10-2)。

    選べるのは手順・フレーム表・条件である。フレーム表と条件は
    依存を持たないので、そのまま入るだけである。

    一度たどった手順は再びたどらない。同じ手順を 2 か所から呼ぶ形でも、
    呼び出しが循環している形でも、たどりはこれで終わる。

    足りないものがあっても出すのをやめない。構成情報から外し、ない物は `missing`、
    ファイルはあるのに読めない手順と条件は `unreadable` に残して、呼び出し元が知らせる
    (file-format.md §10-2)。

    名前は同一性のキー(name_key)で照合し、保存されている綴りで入れる。綴りの
    違う参照(大文字と小文字、NFC と NFD)を別々に入れると、同じ名前が 2 つある
    padcue ファイルになり、インポートが拒否する(file-format.md §2)。
    """
    store = ConditionStore(project.root)
    here = existing_names(project, store)
    got = Contents()
    seen: set[str] = set()
    picked = as_tops(tops)
    queue = [n for k, n in picked if k == "procedure"]
    frametables: set[str] = {n for k, n in picked if k == "frametable"}
    conds: set[str] = {n for k, n in picked if k == "condition"}
    while queue:
        asked = queue.pop(0)
        if name_key(asked) in seen:
            continue
        seen.add(name_key(asked))
        name = here["procedure"].get(name_key(asked))
        if name is None:
            got.missing.append(("procedure", asked))
            continue
        try:
            doc = project.load_procedure_doc(name)
        except FileNotFoundError:
            got.missing.append(("procedure", asked))     # 一覧を作った後に消えた
            continue
        except (OSError, ValueError):
            got.unreadable.append(("procedure", name))
            continue
        got.procedures.append(name)
        more, used_frametables, used_conds = refs_of(doc)
        frametables |= used_frametables
        conds |= used_conds
        queue.extend(sorted(more))
    for kind, asked, out in (("frametable", frametables, got.frametables),
                             ("condition", conds, got.conditions)):
        firsts: dict[str, str] = {}
        for n in sorted(asked):
            firsts.setdefault(name_key(n), n)
        for key, n in sorted(firsts.items(), key=lambda kv: kv[1]):
            if key not in here[kind]:
                got.missing.append((kind, n))
            elif kind == "condition" and not _condition_readable(store,
                                                                 here[kind][key]):
                got.unreadable.append((kind, here[kind][key]))
            else:
                out.append(here[kind][key])
    return got


def _condition_readable(store: ConditionStore, name: str) -> bool:
    """条件を書き出せるか。condition.json と、それが指す画像のすべてを読めるか。

    書き出しの途中で読めないと、選んだ手順のエクスポートがまるごと失敗する。
    手順と同じく、読めない件として外す(file-format.md §10-2)。画像は中身を
    読まず、あるかだけを見る(書き出しで読む)。
    """
    try:
        return all(store.image_path(name, rel).is_file() for rel in store.files(name))
    except (OSError, ValueError):
        return False


KIND_JA = {"procedure": "手順", "frametable": "フレーム表", "condition": "条件"}


def lost_tops(tops: list, got: Contents) -> list[tuple[str, str, bool]]:
    """選んだもの自体のうち、入らなかったもの [(種類, 名前, 見つからないか)]。

    3 つ目が偽なら、ファイルはあるのに読めない。選んだもの自体が 1 つでも
    入らなければ、画面もコマンドも書かずに拒否する。書くと、中身の欠けた padcue
    ファイルを「保存しました」と表示して渡すことになり、受け取った人は何が
    欠けたかを知らない。
    """
    missing = {(k, name_key(n)) for k, n in got.missing}
    unreadable = {(k, name_key(n)) for k, n in got.unreadable}
    return [(kind, name, (kind, name_key(name)) in missing)
            for kind, name in as_tops(tops)
            if (kind, name_key(name)) in missing | unreadable]


def lost_text(kind: str, name: str, missing: bool) -> str:
    """入らなかったもの 1 件の文(lost_tops)。"""
    what = f"{KIND_JA.get(kind, kind)}「{name}」"
    return f"{what}が見つかりません" if missing else f"{what}を読み込めません"


def _manifest(tops: list[str], got: Contents, note: str) -> dict:
    return {
        "kind": "pack",
        "format": PACK_FORMAT,
        "created": date.today().isoformat(),
        "tool": f"padcue {__version__}",
        "note": note,
        "top": [{"kind": k, "name": n} for k, n in as_tops(tops)],
        "entries": got.entries(),
    }


def _add(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    """エントリを 1 つ書く。名前が UTF-8 であることを必ず示す。"""
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.flag_bits |= UTF8_FLAG
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def single_file(project: Project, got: Contents) -> tuple[str, bytes] | None:
    """1 件で 1 つのファイルなら、その名前と中身を返す(file-format.md §10-1)。

    条件は画像を伴うフォルダーなので、1 件でも padcue ファイルになる。
    """
    if got.total() != 1:
        return None
    if len(got.frametables) == 1:
        name = got.frametables[0]
        return f"{name}.csv", project.frametable_path(name).read_bytes()
    if len(got.procedures) == 1:
        name = got.procedures[0]
        return f"{name}.json", project.procedure_path(name).read_bytes()
    return None


def write(project: Project, tops: list, dest: Path, *,
          note: str = "") -> Contents:
    """padcue ファイルを書く。dest は拡張子まで含めた保存先である。

    中身をすべて作ってから、一時ファイルに書いて置き換える(files.write_file)。
    既にある同じ名前のファイルは、書き終えるまで元のまま残る。
    """
    for _kind, name in as_tops(tops):
        validate_name(name)
    got = collect(project, tops)
    store = ConditionStore(project.root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    lone = single_file(project, got)
    if lone is not None and dest.suffix != SUFFIX:
        write_file(dest, lone[1])
        return got
    # zip はメモリで作ってから書く。読み込める padcue ファイルは展開後 256MiB まで
    # (unpack.MAX_TOTAL_BYTES)なので、メモリに収まらない大きさを作る使い方はない
    buf = io.BytesIO()
    _write_zip(project, store, tops, got, note, buf)
    write_file(dest, buf.getvalue())
    return got


def _write_zip(project: Project, store: ConditionStore, tops: list,
               got: Contents, note: str, out: io.BytesIO) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        _add(zf, MANIFEST,
             (json.dumps(_manifest(tops, got, note),
                         ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        for name in got.procedures:
            _add(zf, f"{DIR_PROC}/{name}.json",
                 project.procedure_path(name).read_bytes())
        for name in got.frametables:
            _add(zf, f"{DIR_FRAMETABLE}/{name}.csv",
                 project.frametable_path(name).read_bytes())
        for name in got.conditions:
            base = f"{DIR_COND}/{name}"
            _add(zf, f"{base}/{DOC_NAME}", store.doc_path(name).read_bytes())
            for rel in store.files(name):
                _add(zf, f"{base}/{IMAGES}/{Path(rel).name}",
                     store.image_bytes(name, rel))


def dest_for(project: Project, tops: list, got: Contents,
             folder: Path) -> Path:
    """出す先の位置を決める。拡張子は、どの形で出すかで決まる(file-format.md §10-1)。

    1 件で 1 つのファイルになるものだけが裸のファイルで、それ以外は
    `.padcue` である。条件は 1 件でもフォルダーなので `.padcue` になる。
    """
    lone = single_file(project, got)
    suffix = Path(lone[0]).suffix if lone else SUFFIX
    return folder / (suggest_name(tops) + suffix)


def suggest_name(tops: list) -> str:
    """出すファイルの名前。

    数えるのは利用者が選んだものであって、たどって追加したものではない。
    「親」を出したら名前は「親」である。呼んでいる手順が 8 件あっても、
    渡す相手にとってそれは「親」の一式だからである。
    """
    picked = as_tops(tops)
    if not picked:
        return "padcue ファイル"
    head = picked[0][1]
    if len(picked) == 1:
        return head
    return f"{head} ほか {len(picked) - 1} 件"
