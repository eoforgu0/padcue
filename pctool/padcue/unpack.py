"""padcue ファイルをインポートする(file-format.md §10-1・§10-4・§10-5)。

ほかの人から受け取るものなので、名前が信用できない padcue ファイルは、構成情報を読むより
先に拒否する。チェックの順は決まっている(file-format.md §10-1。read のとおり)。

1. 中のファイルの数と、展開したときの大きさ
2. パスワードと圧縮方式
3. エントリ名。保存先フォルダーの外を指す名前、知らない先頭、`..`、リンクを拒否する
4. 構成情報のバージョン。読めるバージョンでなければ開かず、中の 1 件もインポートしない
5. 中の 1 件ごとの読み込み

1 件でも合わなければ padcue ファイル全体を拒否する。名前の細工が書き間違いだとは
考えないからである。

インポートする前に扱いを 1 件ずつ決める(file-format.md §10-4)。照合は
名前で行い、Unicode の NFC に正規化したうえで英字の大文字と小文字を区別しない
(names.name_key。プロジェクトの中で名前の重なりを見るときと同じ比べ方である)。
"""
from __future__ import annotations

import copy
import json
import lzma
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .conditions import (
    AFTER_COUNT,
    DOC_NAME,
    IMAGES,
    ConditionStore,
    after_rel,
    check_doc,
    fresh_image_names,
    image_name,
)
from .conditions import FORMAT_VERSION as COND_FORMAT
from .files import read_text, write_file
from .frametable import FrametableError, load_frametable
from .names import NAME_MAX, name_key, refuse_taken, validate_name
from .pack import (
    DIR_COND,
    DIR_FRAMETABLE,
    DIR_PROC,
    KIND_JA,
    MANIFEST,
    PACK_FORMAT,
    existing_names,
)
from .procfmt import FORMAT_VERSION, NEST_TEXT, is_int, nest_too_deep
from .procrefs import arms_of, blocks, condition_whats, refs_of
from .project import Project
from .words import as_json, error_text, sentences

# インポートできるファイルの種類。拒否するときに示す
_IMPORTABLE = ("インポートできるのは padcue ファイル、手順の .json、フレーム表の .csv "
               "です")

# 扱い(file-format.md §10-4)
SKIP = "skip"          # インポートしない
TAKE = "take"          # インポートする
REPLACE = "replace"    # 置き換える
RENAME = "rename"      # 別の名前でインポートする

KINDS = ("procedure", "frametable", "condition")
ACTION_JA = {SKIP: "インポートしない", TAKE: "インポートする",
             REPLACE: "置き換える", RENAME: "別の名前でインポートする"}
# 状態ごとに選べる扱い(file-format.md §10-4 の表)。同じ名前で内容も同じ件は、
# インポートしても何も変わらないので「インポートしない」だけである
CHOICES = {"new": (TAKE, SKIP), "same": (SKIP,),
           "differs": (RENAME, REPLACE, SKIP)}

# 構成情報の欄(file-format.md §10-1)。バージョンごとに欄の種類が決まっているので、ほかの
# 欄は拒否する(file-format.md §3・§8-1)
_MANIFEST_KEYS = {"kind", "format", "created", "tool", "note", "top", "entries"}
_BROKEN_LIST = "padcue ファイルが壊れています。中の一覧を読み込めません。"


# 文は理由だけを書く。画面はどれも「「<ファイルの名前>」をインポートできません。
# <理由>」の形で出す(gui の /api/pack/inspect が頭を付ける)
class PackError(Exception):
    """padcue ファイル全体を拒否する理由。1 件でも合わなければここへ来る。"""


class ChoiceError(ValueError):
    """選んだ扱いに合わないものがある。何もインポートしていない。

    problems は [(種類, 名前, 理由)] で、画面はその件の行の下に理由を出す。
    """

    def __init__(self, problems: list):
        super().__init__(problems[0][2])
        self.problems = problems


class ApplyError(ValueError):
    """インポートの書き込みが途中で失敗した。文はそのまま画面に出す。

    wrote は失敗より前に書き終えた件 [(種類, 名前)] で、書き戻さない。文は、
    どれを書けなかったか、その理由、既にインポートした物を書く。何も書かないと、
    利用者は何が入ったかを確認しようがない。
    """

    def __init__(self, wrote: list, kind: str, name: str, cause: Exception):
        self.wrote = list(wrote)
        # ValueError は文そのものである(条件の置き換えの失敗は、元の条件を
        # 残した場所まで書く。conditions.put_folder)。OS の失敗は errno と英語を
        # 出さずに、次にできることを書く
        parts = ([str(cause)] if isinstance(cause, ValueError)
                 else [f"{KIND_JA[kind]}「{name}」をインポートできません",
                       error_text(cause)])
        if wrote:
            done = "、".join(f"{KIND_JA[k]}「{n}」" for k, n in wrote)
            parts += [f"{done}はインポート済みです",
                      "ファイルを選択し直して、残りをインポートしてください"]
        super().__init__(sentences(*parts))


def _segment(name: str, where: str) -> str:
    """名前にあたる区切りを、file-format.md §2 の名前の規則でチェックする。"""
    try:
        return validate_name(name)
    except ValueError as e:
        raise PackError(f"{where}: {e}") from None


# padcue ファイルの中身の上限(file-format.md §10-1)。中身はメモリに読んで確認する
# ので、上限がないと、小さく圧縮したファイルで大きなメモリを確保させられる。手順と
# フレーム表は大きくても数 MiB、条件の画像は 1 枚数 MiB なので、普通の使い方では
# 達しない値にする
MAX_ENTRIES = 10000
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def check_sizes(zf: zipfile.ZipFile) -> None:
    """中のファイルの数と、展開したときの大きさを、読む前に確認する。"""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise PackError("padcue ファイルの中のファイルが多すぎます"
                        f"(上限 {MAX_ENTRIES} 個)")
    total = 0
    for info in infos:
        if info.file_size > MAX_ENTRY_BYTES:
            mb = MAX_ENTRY_BYTES // (1024 * 1024)
            raise PackError(sentences(
                f"大きすぎるファイル「{info.filename}」が含まれています",
                f"中のファイルは 1 つにつき {mb}MB までです"))
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise PackError(sentences(
            "padcue ファイルの中身が大きすぎます",
            f"展開した後の合計は {MAX_TOTAL_BYTES // (1024 * 1024)}MB までです"))


# 読める圧縮方式。padcue のエクスポートは Deflate で書く(pack._add)。zip を作る
# ほかのツールが選べる方式のうち、Python の zipfile が読めないもの(Deflate64 など)は
# 中身を読むところで初めて失敗するので、読む前に拒否する
_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2,
            zipfile.ZIP_LZMA}
_USE_EXPORTED = "padcue でエクスポートしたファイルを使用してください"


def check_methods(zf: zipfile.ZipFile) -> None:
    """パスワードと圧縮方式を、読む前に確認する。

    padcue のエクスポートは、どちらも使わない。受け取った人が別のツールで
    作り直したファイルである。
    """
    for info in zf.infolist():
        if info.flag_bits & 0x1:          # 暗号化のフラグ(zip の汎用フラグの 0 ビット)
            raise PackError(sentences("ファイルにパスワードが設定されています",
                                      _USE_EXPORTED))
        if info.compress_type not in _METHODS:
            raise PackError(sentences("padcue が読めない方式で圧縮されています",
                                      _USE_EXPORTED))


def check_names(zf: zipfile.ZipFile) -> None:
    """エントリ名をすべてチェックする(file-format.md §10-1)。

    1 件でも合わなければ拒否する。
    """
    seen: dict[tuple[str, str], str] = {}
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue                      # フォルダーそのものは中身を持たない
        parts = name.replace("\\", "/").split("/")
        if (name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":")
                or any(p in ("", ".", "..") for p in parts)):
            raise PackError(f"保存先フォルダーの外を指すパスは使用できません: {name}")
        # 0xA000 はリンクの種別。展開すると保存先フォルダーの外のファイルを指しうる
        if (info.external_attr >> 16) & 0xF000 == 0xA000:
            raise PackError(f"リンクが含まれています: {name}")
        if name == MANIFEST:
            continue
        kind, item = _item_of(parts, name)
        key = (kind, name_key(item))
        if key in seen and seen[key] != item:
            raise PackError(f"同じ名前が 2 つあります: {seen[key]} と {item}")
        seen[key] = item


def _item_of(parts: list, name: str) -> tuple[str, str]:
    """エントリが属する 1 件の種類と名前を、場所から決める。知らない場所は拒否する。

    手順とフレーム表は拡張子を外した名前、条件はフォルダー名そのものである。条件の
    名前の点は拡張子ではない(「v1.2」と「v1.3」は別の名前)。名前は
    file-format.md §2 の名前の規則で確認してから返す。
    """
    head = parts[0]
    if head == DIR_PROC and len(parts) == 2 and parts[1].endswith(".json"):
        return "procedure", _segment(parts[1][: -len(".json")], name)
    if head == DIR_FRAMETABLE and len(parts) == 2 and parts[1].endswith(".csv"):
        return "frametable", _segment(parts[1][: -len(".csv")], name)
    if head == DIR_COND and len(parts) >= 3:
        item = _segment(parts[1], name)
        if len(parts) == 3 and parts[2] == DOC_NAME:
            return "condition", item
        if len(parts) == 4 and parts[2] == IMAGES:
            # 条件のフォルダーに書くので、条件の画像の名前の綴りのほかは受けない
            try:
                image_name(f"{IMAGES}/{parts[3]}")
            except ValueError:
                raise PackError(
                    f"画像ではないファイルが含まれています: {name}") from None
            return "condition", item
    raise PackError(f"padcue ファイルに入れられないファイルが含まれています: {name}")


@dataclass
class PackFile:
    """読み終えた padcue ファイル。"""
    manifest: dict = field(default_factory=dict)
    procedures: dict = field(default_factory=dict)    # 名前 -> 文書
    frametables: dict = field(default_factory=dict)   # 名前 -> 本文
    conditions: dict = field(default_factory=dict)    # 名前 -> {doc, images}
    # 構成情報にないのに入っていたもの [(種類, 名前)]
    extra: list = field(default_factory=list)
    # 読めなくて外したもの [(種類, 名前, 理由)]
    unreadable: list = field(default_factory=list)

    def names(self, kind: str) -> list[str]:
        return sorted(getattr(self, kind + "s"))


def read(path: Path) -> PackFile:
    """padcue ファイルを読む。チェックの順は file-format.md §10-1 のとおりである。

    単体のファイル(手順の `.json`、フレーム表の `.csv`)も同じ関数で読む。
    エクスポートは 1 件で 1 つのファイルになるものを裸で出すので
    (file-format.md §10-1)、ここで受けないと、自分でエクスポートしたものを自分で
    インポートできない。
    """
    if not zipfile.is_zipfile(path):
        return _read_single(path)
    try:
        with zipfile.ZipFile(path) as zf:
            check_sizes(zf)                               # 1. 数と大きさ
            check_methods(zf)                             # 2. パスワードと圧縮方式
            check_names(zf)                               # 3. エントリ名
            # 構成情報は中の仕組みの名前なので、利用者には「中の一覧」と書く
            if MANIFEST not in zf.namelist():
                raise PackError("padcue ファイルが壊れています。中の一覧がありません。")
            try:
                manifest = json.loads(zf.read(MANIFEST))
            except (ValueError, RecursionError):
                raise PackError(_BROKEN_LIST) from None
            _check_manifest(manifest)                     # 4. 構成情報のバージョン
            return _read_items(zf, manifest)              # 5. 1 件ごと
    except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError):
        # 圧縮した中身が途中で切れているか、壊れている。zip の頭と一覧は読めた
        raise PackError("padcue ファイルが壊れています") from None


def _procedure_json(raw: bytes):
    """手順のファイルの中身を読む。単体のファイルも padcue ファイルの中も、これで読む。

    UTF-8 として読み、先頭の BOM は外す(フレーム表の読み込みと同じ)。bytes の
    まま json.loads に渡すと、BOM と UTF-16 を判別して読むので、文字列で読む単体の
    ファイルと結果が一致しない。UTF-8 でなければ UnicodeDecodeError を上げる
    (file-format.md §8-1)。
    """
    return json.loads(raw.decode("utf-8").lstrip("\ufeff"))


def _read_single(path: Path) -> PackFile:
    """1 つのファイルを、中身 1 件の padcue ファイルとして読む
    (file-format.md §10-1・§10-5)。

    名前はファイル名そのものなので、名前の規則でチェックする。構成情報はないので、
    種類とバージョンだけを見て組み立てる。
    """
    # 拡張子は大文字と小文字を区別しない。Windows のエクスプローラーは既定で
    # 拡張子を隠すので、「手順.JSON」も利用者には手順の JSON ファイルに見える
    suffix = path.suffix.lower()
    # padcue ファイルは zip である。zip として読めないなら、形式ではなく中身が
    # 壊れている(「対応していない形式」と書くと事実と違う)
    if suffix == ".padcue":
        raise PackError("padcue ファイルが壊れています")
    name = _segment(path.stem, path.name)
    if suffix == ".json":
        try:
            doc = _procedure_json(path.read_bytes())
        except (OSError, ValueError, RecursionError):
            raise PackError("手順のファイル(.json)として読み込めません") from None
        _check_procedure(doc)
        pf = PackFile(manifest={"kind": "pack", "format": PACK_FORMAT,
                                "entries": [{"kind": "procedure",
                                             "name": name}]})
        pf.procedures[name] = doc
        return pf
    if suffix == ".csv":
        try:
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            load_frametable(name, text)
        except FrametableError as e:
            raise PackError(str(e)) from None
        except (OSError, UnicodeDecodeError):
            raise PackError("ファイルを読み込めないか、文字コードが"
                            " UTF-8 ではありません") from None
        pf = PackFile(manifest={"kind": "pack", "format": PACK_FORMAT,
                                "entries": [{"kind": "frametable",
                                             "name": name}]})
        pf.frametables[name] = text
        return pf
    raise PackError(_IMPORTABLE)


def _check_manifest(doc) -> None:
    """構成情報のバージョンと形を見る(file-format.md §3・§10-1)。

    読めるバージョンより新しいものは、padcue を更新すれば開ける。それ以外でバージョンが
    今のバージョンでないものは、壊れたものとして扱う。
    """
    if not isinstance(doc, dict) or doc.get("kind") != "pack":
        raise PackError(_BROKEN_LIST)
    got = doc.get("format")
    if is_int(got) and got > PACK_FORMAT:
        raise PackError("この padcue ファイルは新しいバージョンの padcue で"
                        "作成されています。padcue を更新してください。")
    if not is_int(got) or got != PACK_FORMAT:
        raise PackError(_BROKEN_LIST)
    extra = sorted(set(doc) - _MANIFEST_KEYS)
    if extra:
        raise PackError("padcue ファイルが壊れています。中の一覧に使用できない項目が"
                        f"あります: {'、'.join(extra)}。")
    entries = doc.get("entries", [])
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and isinstance(e.get("kind"), str)
            and isinstance(e.get("name"), str) for e in entries):
        raise PackError(_BROKEN_LIST)
    if not isinstance(doc.get("note", ""), str):
        raise PackError(_BROKEN_LIST)


def _read_items(zf: zipfile.ZipFile, manifest: dict) -> PackFile:
    pf = PackFile(manifest=manifest)
    listed = {(e["kind"], e["name"]) for e in manifest.get("entries", [])}
    for name in zf.namelist():
        if name == MANIFEST or name.endswith("/"):
            continue
        # 区切りは check_names と同じく、どちらの流儀でも受ける
        parts = name.replace("\\", "/").split("/")
        if parts[0] == DIR_PROC:
            nm = parts[1][: -len(".json")]
            try:
                doc = _procedure_json(zf.read(name))
                _check_procedure(doc)
                pf.procedures[nm] = doc
            except UnicodeDecodeError:
                # フレーム表と同じ文にする
                pf.unreadable.append(("procedure", nm,
                                      "文字コードが UTF-8 ではありません"))
            except (ValueError, RecursionError):
                pf.unreadable.append(("procedure", nm, "読み込めません"))
            except PackError as e:
                pf.unreadable.append(("procedure", nm, str(e)))
        elif parts[0] == DIR_FRAMETABLE:
            nm = parts[1][: -len(".csv")]
            try:
                # 改行はどちらの流儀でも受ける。書き戻すときは、このツールが
                # 自分で保存するときと同じ形になる
                text = zf.read(name).decode("utf-8").replace("\r\n", "\n")
                load_frametable(nm, text)
                pf.frametables[nm] = text
            except UnicodeDecodeError:
                pf.unreadable.append(("frametable", nm,
                                      "文字コードが UTF-8 ではありません"))
            except FrametableError as e:
                pf.unreadable.append(("frametable", nm, str(e)))
        elif parts[0] == DIR_COND:
            got = pf.conditions.setdefault(parts[1], {"doc": None, "images": {}})
            if parts[2] == DOC_NAME:
                got["doc"] = zf.read(name)
            else:
                got["images"][f"{IMAGES}/{parts[3]}"] = zf.read(name)
    for nm in sorted(pf.conditions):
        got = pf.conditions[nm]
        try:
            got["doc"] = _read_condition(got["doc"], got["images"])
        except PackError as e:
            del pf.conditions[nm]
            pf.unreadable.append(("condition", nm, str(e)))
    for kind in KINDS:
        for nm in sorted(getattr(pf, kind + "s")):
            if (kind, nm) not in listed:
                pf.extra.append((kind, nm))
    # 構成情報に書いてあるのに入っていないものは、インポートできない件として出す
    # (file-format.md §10-1)。黙って捨てると、それを参照する手順が、
    # インポートした後で参照先を失った理由が分からない
    for kind, nm in sorted(listed):
        if kind not in KINDS:
            continue
        if nm in getattr(pf, kind + "s") or any(
                u[0] == kind and u[1] == nm for u in pf.unreadable):
            continue
        pf.unreadable.append((kind, nm, "padcue ファイルに含まれていません"))
    return pf


def _check_format(doc: dict, want: int) -> None:
    """1 件の文書のバージョンを見る(file-format.md §3)。

    プロジェクトの手順と条件を読むときと同じく、新しいバージョンには次にできることを
    書く。主語は付けない。画面はその件の種類と名前を先に表示する。
    """
    got = doc.get("format")
    if is_int(got) and got > want:
        raise PackError(sentences("新しいバージョンの padcue で作成されています",
                                  "padcue を更新してください"))
    if not is_int(got) or got != want:
        raise PackError(f"format は {want} である必要があります: {as_json(got)}")


def _check_procedure(doc) -> None:
    """手順の種類とバージョンを見る。入れ子が深すぎる手順も拒否する
    (file-format.md §10-1)。

    深すぎる手順は、インポートの参照の書き換え(rewrite のコピー)が途中で
    異常終了する。プロジェクトに置いても読み込みが拒否する(procfmt.NEST_MAX)。
    """
    if not isinstance(doc, dict) or doc.get("kind") != "procedure":
        raise PackError(sentences("手順のファイルではありません", _IMPORTABLE))
    _check_format(doc, FORMAT_VERSION)
    if nest_too_deep(doc):
        raise PackError(NEST_TEXT)


def _read_condition(raw: bytes | None, images: dict) -> dict:
    """条件の condition.json を、プロジェクトの条件を読むときと同じチェックにかける
    (file-format.md §9-1)。

    チェックせずにインポートすると、その条件を使う手順を開始するたびに、条件が
    読めないというメッセージが出る。参照している画像が入っているかも見る。ないまま
    インポートすると、判定できない条件になる。
    """
    if raw is None:
        raise PackError("条件の設定が含まれていません")
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        raise PackError("読み込めません") from None
    if not isinstance(doc, dict) or doc.get("kind") != "condition":
        raise PackError("条件のファイルではありません")
    _check_format(doc, COND_FORMAT)
    try:
        check_doc(doc, "条件の設定")
    except ValueError as e:
        raise PackError(str(e)) from None
    for rel in [*doc["refs"], *doc["negatives"]]:
        if rel not in images:
            raise PackError(f"画像「{rel}」が含まれていません")
    return doc


# ---- 名前の衝突(file-format.md §10-4) ----

@dataclass
class Item:
    """インポートする 1 件と、その扱い。"""
    kind: str
    name: str
    state: str                 # "new" / "same" / "differs"
    action: str                # SKIP / TAKE / REPLACE / RENAME
    as_name: str = ""          # 別の名前でインポートするときの名前
    # 元からある同じ名前の物の綴り(なければ空)。「置き換える」と「インポート
    # しない」は、この綴りの物を指す
    here_name: str = ""
    # 元からある物の、確認したときのリビジョン(Project.file_rev)。選んだ後に元からある
    # 物が編集されたら、確認し直させる。new・same・differs は選べる扱いを決める欄で、
    # 中身が変わっても同じ値のままのことがある(differs のまま別の中身になる)
    here_rev: str = ""
    # 「置き換える」で消える画像の枚数(条件だけ。基準画像・一致しないはずの画面)
    losing_images: int = 0
    # 「置き換える」で意味が変わる、元からある手順の名前
    used_by: list = field(default_factory=list)
    # padcue ファイルの中で、これを使用している手順の名前。「インポートしない」
    # を選んだときに参照がどれを指すかを、インポートの前に示すのに使う
    # (file-format.md §10-5)
    pack_users: list = field(default_factory=list)
    # padcue ファイルの中の一覧にないのに入っていた(file-format.md §10-1)
    extra: bool = False


def _users_in_pack(pf: PackFile) -> dict:
    """padcue ファイルの中の手順が使用するものを {(種類, キー): [手順]} で返す。"""
    out: dict = {}
    for proc in sorted(pf.procedures):
        for kind, pool in zip(KINDS, refs_of(pf.procedures[proc]), strict=True):
            for key in sorted({name_key(r) for r in pool}):
                out.setdefault((kind, key), []).append(proc)
    return out


def _same_content(project: Project, store: ConditionStore, kind: str,
                  name: str, mine: str, pf: PackFile) -> bool:
    """padcue ファイルの中の name と、元からある mine の中身が同じか。

    元からあるファイルは、元からある綴り mine で開く。NFC と NFD だけが違う名前は
    同じ名前だが(file-format.md §2)、ファイル名としては別の綴りなので、中の
    綴りでは開けない。
    """
    if kind == "procedure":
        try:
            return project.load_procedure_doc(mine) == pf.procedures[name]
        except (OSError, ValueError):
            return False
    if kind == "frametable":
        try:
            got = project.frametable_path(mine).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 元からある表が読めない(表計算ソフトで保存した Shift_JIS など)。同じとは
            # 言えないので、内容が違うと扱う(手順と条件と同じ)
            return False
        return got == pf.frametables[name]
    try:
        doc = json.loads(read_text(store.doc_path(mine)))
        check_doc(doc, "")
    except (OSError, ValueError):
        return False                    # 元からある条件が読めない。同じとは言えない
    theirs = pf.conditions[name]

    def here(rel):
        path = store.image_path(mine, rel)
        return path.read_bytes() if path.is_file() else None

    try:
        mine_images = _image_contents(doc, here)
    except OSError:
        return False
    return (_settings_of(doc) == _settings_of(theirs["doc"])
            and mine_images == _image_contents(theirs["doc"], theirs["images"].get))


def _settings_of(doc: dict) -> dict:
    """condition.json の、画像の並びを除いた欄。"""
    return {k: v for k, v in doc.items() if k not in ("refs", "negatives")}


def _image_contents(doc: dict, read) -> tuple:
    """条件の画像の中身を、並びの順に並べる。read は位置から中身(なければ None)。

    画像の名前は比べない。インポートした画像には新しい名前を付けるので
    (conditions.fresh_image_names)、名前で比べると、同じ物を入れ直しても
    「内容が違う」になる。基準画像ごとに、直後の画像の中身も並べる。
    """
    refs = tuple((read(rel), tuple(read(after_rel(rel, k))
                                   for k in range(1, AFTER_COUNT + 1)))
                 for rel in doc["refs"])
    return refs, tuple(read(rel) for rel in doc["negatives"])


def _losing_images(store: ConditionStore, name: str) -> int:
    """「置き換える」で消える、元からある条件の画像の枚数。

    数えるのは画面に出る画像(基準画像と一致しないはずの画面)である。元からある
    条件が読めない(壊れている)ときは、どれが何の画像か分からない
    ので、フォルダーにある画像をすべて数える。読めないと拒否すると、ファイル全体を
    インポートできず、「置き換える」で修正することもできない。
    """
    try:
        return len(store.refs(name)) + len(store.negatives(name))
    except ValueError:
        return len(list((store.cond_dir(name) / IMAGES).glob("*.png")))


def free_name(taken: set, name: str) -> str:
    """空いている「名前 (2)」を作る(file-format.md §10-4)。

    taken は照合のキーの集まり。番号を追加すると名前の長さの上限
    (names.NAME_MAX)を超えるときは、元の名前の末尾を切ってから追加する。ダイアログを
    開いた直後から、利用者が何も入力していない行を「長すぎます」で拒否しない。
    """
    n = 2
    while True:
        tail = f" ({n})"
        # 切った末尾の空白は除く(空白が 2 つ続くと、見た目で 1 つと区別できない)
        got = name[:NAME_MAX - len(tail)].rstrip() + tail
        if name_key(got) not in taken:
            return got
        n += 1


def plan(project: Project, pf: PackFile) -> list[Item]:
    """1 件ごとの扱いを決める(file-format.md §10-4 の表)。既定を入れて返す。"""
    store = ConditionStore(project.root)
    here = existing_names(project, store)
    users = _users_in_pack(pf)
    extra = set(pf.extra)
    out = []
    for kind in KINDS:
        # 自動で付ける番号は、元からある名前に加えて padcue ファイルの中の名前も
        # 避ける。中に「表」と「表 (2)」があるとき、「表」を「表 (2)」にすると、
        # 2 件が同じ名前に書かれて片方が消える。読めなかった件の名前も避ける
        # (check はそれも「ほかの行の名前」と数えて拒否する)
        used = ({*here[kind], *(name_key(n) for n in pf.names(kind))}
                | {name_key(n) for k, n, _why in pf.unreadable if k == kind})
        for name in pf.names(kind):
            mine = here[kind].get(name_key(name), "")
            if not mine:
                item = Item(kind, name, "new", TAKE)
            elif _same_content(project, store, kind, name, mine, pf):
                item = Item(kind, name, "same", SKIP)
            else:
                item = Item(kind, name, "differs", RENAME,
                            as_name=free_name(used, name))
                used.add(name_key(item.as_name))
                if kind == "condition":
                    item.losing_images = _losing_images(store, mine)
                item.used_by = _users_of(project, kind, mine)
            item.here_name = mine
            item.here_rev = project.file_rev(kind, mine) if mine else ""
            item.pack_users = users.get((kind, name_key(name)), [])
            item.extra = (kind, name) in extra
            out.append(item)
    return out


def left_out(project: Project, pf: PackFile) -> list[dict]:
    """読めなくて外した件に、それを使用している手順と、元からある同じ名前の物の
    綴りを付ける(file-format.md §10-1・§10-5)。

    読めなかった件は「インポートしない」と同じ扱いなので、参照は元からある物を
    指すか、見つからない。どちらになるかを、インポートの前にその件のそばで示す。
    """
    here = existing_names(project, ConditionStore(project.root))
    users = _users_in_pack(pf)
    return [{"kind": kind, "name": name, "why": why,
             "pack_users": users.get((kind, name_key(name)), []),
             "here_name": here[kind].get(name_key(name), "")}
            for kind, name, why in pf.unreadable]


def _users_of(project: Project, kind: str, name: str) -> list[str]:
    """元からある手順のうち、その名前を参照しているもの(file-format.md §10-5)。"""
    out = []
    key = name_key(name)
    for other in project.procedure_names():
        try:
            doc = project.load_procedure_doc(other)
        except (OSError, ValueError):
            continue
        pool = dict(zip(KINDS, refs_of(doc), strict=True))[kind]
        if any(name_key(r) == key for r in pool):
            out.append(other)
    return out


def check(project: Project, pf: PackFile, choices: list) -> tuple[list, list]:
    """画面が選んだ扱いを、書く前に全件チェックする(file-format.md §10-4)。

    返すのは、扱いを当てた 1 件ごとと、合わない件 [(種類, 名前, 理由)] である。
    choices は Item か、同じ欄を持つ辞書(画面の JSON)の並びで、見るのは kind・
    name・state・here_rev・action・as_name だけである。

    状態は画面から受け取らず、その時点のインポート先で作り直す。確認した後に同じ名前の
    物がインポート先にできていると、「インポートする」はそれを上書きする。画面が見ていた
    状態と違えば、その件を合わない件にする。

    別の名前は、名前の規則、元からある名前、ほかの行の名前(padcue ファイルの中の
    名前と、ほかの行が付ける別の名前)と比べる。元からある名前に書くと元からある
    物を黙って上書きし、条件ならフォルダーごと消える。ほかの行と同じ名前にすると、
    一緒に入れる手順がどちらを指すのかが入れ替わる。読めなかった件の名前も
    ほかの行の名前に含める。その名前を付けると、読めなかった件を指していた参照が、
    何のメッセージもないまま別の中身を指す。
    """
    items = plan(project, pf)
    here = existing_names(project, ConditionStore(project.root))
    chosen = {}
    for c in choices or []:
        c = vars(c) if isinstance(c, Item) else c
        if isinstance(c, dict):
            chosen[(str(c.get("kind")), str(c.get("name")))] = c
    bad: dict = {}
    for it in items:
        c = chosen.get((it.kind, it.name))
        if c is None:                   # 選んでいない件は書かない
            it.action = SKIP
            continue
        it.action = str(c.get("action"))
        it.as_name = str(c.get("as_name") or "")
        if (c.get("state", it.state) != it.state
                or c.get("here_rev", it.here_rev) != it.here_rev):
            bad[(it.kind, it.name)] = (f"同じ名前の{KIND_JA[it.kind]}が変更されました。"
                                       "ファイルを選択し直してください。")
        elif it.action not in CHOICES[it.state]:
            bad[(it.kind, it.name)] = (
                f"「{ACTION_JA.get(it.action, it.action)}」は選択できません")
    for it in items:
        if it.action != RENAME or (it.kind, it.name) in bad:
            continue
        try:
            new = validate_name(it.as_name)
            refuse_taken(here[it.kind].values(), new)
        except ValueError as e:
            bad[(it.kind, it.name)] = str(e)
            continue
        others = {name_key(n) for kind, n, _why in pf.unreadable
                  if kind == it.kind}
        for o in items:
            if o is not it and o.kind == it.kind:
                others.add(name_key(o.name))
                if o.action == RENAME:
                    others.add(name_key(o.as_name.strip()))
        if name_key(new) in others:
            bad[(it.kind, it.name)] = f"「{new}」はほかの行の名前と重複しています"
            continue
        it.as_name = new
    problems = [(it.kind, it.name, bad[(it.kind, it.name)])
                for it in items if (it.kind, it.name) in bad]
    return items, problems


def script_commands(pf: PackFile) -> list[tuple[str, str]]:
    """padcue ファイルの中の手順に入っているコマンドの本文をすべて返す
    (file-format.md §10-3・§10-5)。

    構成情報ではなく実際のファイルから集める。画面は、そのうちインポートする手順の
    ものだけを出す。インポートでは何も実行しない。コマンドを持つのは、手順の
    ブロック「スクリプト」と、選択肢の「選択条件」のスクリプトである。
    """
    out = []
    for name in sorted(pf.procedures):
        for nd in blocks(pf.procedures[name].get("body")):
            if nd.get("type") == "script" and nd.get("cmd"):
                out.append((name, nd["cmd"]))
            for arm in arms_of(nd):
                what = arm.get("what")
                if isinstance(what, dict) and what.get("kind") == "script" \
                        and what.get("cmd"):
                    out.append((name, what["cmd"]))
    return out

# ---- インポートする(file-format.md §10-4 の参照の書き換えを含む) ----


def final_names(items: list) -> dict:
    """種類ごとに「padcue ファイルの中の名前のキー -> 実際に付ける名前」を作る。

    キーで探すのは、手順の参照の綴りが中の名前と大文字と小文字で違っても、同じ物を
    指すからである(file-format.md §2)。綴りで探すと書き換えが漏れ、参照が
    元からある別の中身を指す。

    「置き換える」は元からあるファイル名の綴りを残す(file-format.md §10-4)。
    「インポートしない」を選んだものは元からある同じ名前の物を指し、綴りもそれに
    合わせる。元にもなければ、その名前のままにしておく(参照の先がないことは、
    インポートの前に示している)。
    """
    out: dict = {k: {} for k in KINDS}
    for it in items:
        if it.action == RENAME:
            got = it.as_name
        elif it.action == TAKE:
            got = it.name
        else:
            got = it.here_name or it.name
        out[it.kind][name_key(it.name)] = got
    return out


def rewrite(doc: dict, names: dict) -> dict:
    """手順の中の参照を、実際に付けた名前へ書き換える(file-format.md §10-4)。

    書き換えるのは padcue ファイルの中だけでよい。参照は
    インポートするものからインポートするものを指すだけなので、元からある手順の
    参照は元の名前のままで正しい。識別子は書き換えない(file-format.md §6-1)。
    受け取った文書は書き換えず、コピーを書き換えて返す。
    """
    doc = copy.deepcopy(doc)
    for nd in blocks(doc.get("body")):
        kind = {"call": "procedure", "frametable": "frametable"}.get(nd.get("type"))
        if kind and isinstance(nd.get("ref"), str):
            nd["ref"] = names[kind].get(name_key(nd["ref"]), nd["ref"])
        for what in condition_whats(nd):
            if isinstance(what.get("ref"), str):
                what["ref"] = names["condition"].get(
                    name_key(what["ref"]), what["ref"])
    return doc


def dangling(project: Project, pf: PackFile, items: list,
             names: dict) -> list:
    """インポートした後に参照の先がないもの [(手順, 種類, 名前)](file-format.md §10-4)。

    padcue ファイルの中の件(一覧の行と、読めなかった件)を指す参照は除く。
    それらがどこを指すかは、インポートの前にその件のそばで示している
    (file-format.md §10-5)。ここに残るのは、エクスポートしたときに見つからず、
    padcue ファイルに入らなかった物である。
    """
    have = {k: set(v) for k, v in
            existing_names(project, ConditionStore(project.root)).items()}
    for it in items:
        if it.action != SKIP:
            have[it.kind].add(name_key(names[it.kind][name_key(it.name)]))
    told = {(it.kind, name_key(it.name)) for it in items}
    told |= {(kind, name_key(name)) for kind, name, _why in pf.unreadable}
    out = []
    for it in items:
        if it.kind != "procedure" or it.action == SKIP:
            continue
        for kind, pool in zip(KINDS, refs_of(pf.procedures[it.name]),
                              strict=True):
            for ref in sorted(pool):
                key = name_key(ref)
                if (kind, key) not in told and key not in have[kind]:
                    out.append((names["procedure"][name_key(it.name)], kind, ref))
    return out


def lost_starts(project: Project, pf: PackFile, items: list, problems: list,
                lanes: list = ()) -> list:
    """「置き換える」手順ごとに、置き換えると見つからなくなるプリセットとレーンの
    開始位置 [(padcue ファイルの中の名前, [開始位置])](presets.lost_starts)。

    手順の編集で保存するときと同じく、置き換える手順 1 件ずつ、その中身で書いた後の
    手順と照合する。置き換える手順は、一緒にインポートする新しい手順(インポートする・
    別の名前でインポートする)を呼びうるので、それらの中身も一緒に渡す。どれも、
    参照を実際に付ける名前へ書き換えた中身(apply が書くもの)である。items と
    problems は check の結果で、合わない件は書かないので数えない。lanes は実行・
    監視のレーンが選んでいる開始位置である。
    """
    bad = {(kind, name) for kind, name, _why in problems}
    ok = [it for it in items if (it.kind, it.name) not in bad]
    names = final_names(ok)
    new = {names["procedure"][name_key(it.name)]:
           rewrite(pf.procedures[it.name], names)
           for it in ok if it.kind == "procedure" and it.action in (TAKE, RENAME)}
    out = []
    for it in ok:
        if it.kind != "procedure" or it.action != REPLACE:
            continue
        doc = rewrite(pf.procedures[it.name], names)
        lost = project.presets.lost_starts_of({**new, it.here_name: doc}, lanes)
        if lost:
            out.append((it.name, lost))
    return out


def apply(project: Project, pf: PackFile, choices: list) -> dict:
    """選んだ扱いのとおりにインポートする。承認済みのものだけを渡すこと。

    書く前に全件をチェックし(check)、1 件でも合わなければ何も書かずに ChoiceError を
    上げる。1 件ずつ確認しながら書くと、途中の件で拒否したときに、前の件だけが
    書かれて残る。

    「インポートする」と「別の名前でインポートする」は、書き込む先のファイルが
    既にあれば拒否する。名前の照合で別の名前と判断しても、ファイルの場所では
    同じ場所になることがある(Windows の短い名前など)。そのまま書くと、元から
    ある物を上書きし、条件ならフォルダーごと消える。

    チェックを通った後の書き込みそのもの(ほかのアプリが開いているファイルなど)が
    途中で失敗したら、ApplyError を上げる。書き終えた件は書き戻さない。
    """
    items, problems = check(project, pf, choices)
    if problems:
        raise ChoiceError(problems)
    store = ConditionStore(project.root)
    names = final_names(items)
    there = {"procedure": lambda n: project.procedure_path(n).exists(),
             "frametable": lambda n: project.frametable_path(n).exists(),
             "condition": store.exists}
    for it in items:
        dest = names[it.kind][name_key(it.name)]
        if it.action in (TAKE, RENAME) and there[it.kind](dest):
            problems.append((it.kind, it.name, f"「{dest}」は既にあります"))
    if problems:
        raise ChoiceError(problems)
    wrote = []
    # 参照される側から書く(条件、フレーム表、手順の順)。書き込みが途中で
    # 失敗しても、新しい手順が古い参照先を指す組み合わせは残らない
    for it in sorted(items, key=lambda it: -KINDS.index(it.kind)):
        if it.action == SKIP:
            continue
        name = names[it.kind][name_key(it.name)]
        try:
            _write_item(project, store, pf, it, name, names)
        except (OSError, ValueError) as e:
            raise ApplyError(wrote, it.kind, name, e) from e
        wrote.append((it.kind, name))
    return {"wrote": wrote,
            "dangling": dangling(project, pf, items, names)}


def _write_item(project: Project, store: ConditionStore, pf: PackFile,
                it: Item, name: str, names: dict) -> None:
    """1 件を name の名前で書く(apply)。"""
    if it.kind == "procedure":
        project.save_procedure_doc(name, rewrite(pf.procedures[it.name], names))
    elif it.kind == "frametable":
        project.frametable_path(name).parent.mkdir(parents=True, exist_ok=True)
        write_file(project.frametable_path(name), pf.frametables[it.name])
    else:
        got = pf.conditions[it.name]
        # 画像には、この PC で新しい名前を付ける(conditions.fresh_image_names)
        doc, images = fresh_image_names(got["doc"], got["images"])
        # 置き換えはフォルダーごと(file-format.md §10-4)。condition.json
        # だけ差し替えると、元からある画像と新しい条件が一致しない
        store.put_folder(
            name, json.dumps(doc, ensure_ascii=False, indent=2) + "\n", images)
