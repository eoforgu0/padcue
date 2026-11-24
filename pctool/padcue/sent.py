"""装置へ送った手順のコピー(sent/)と、実行を開始した時点の定義(sent/sources/)。

装置は名前を持たず、内容の id で手順を報告する。周回数と開始位置とグループのレーンの
数ごとに別の id になるので、id から手順名や対応表へ戻す出所が要る。手順のファイルは
編集で変わり、変換し直しても同じ id は作れない(そのときの手順の中身が要る)ので、装置へ
送った成果物をここへ残す。1 件を 1 ファイル(<id>.json)にするのは、id で見つけられて、
古い分を 1 ファイルずつ消せるからである。

実行は、開始した時点の手順、呼び出し先、フレーム表、条件で最後まで動く
(coupling.md D12)。開始のときに定義を先に保存し、変換と判定は保存物だけを読む。
保存物はプロジェクトと同じ形なので、そのまま Project と ConditionStore で読める。
変換の入力が保存物だけなので、編集が重なっても、開始した内容と、周回の終え方を
変えたときの続きの内容は同じになる。
"""
from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import binfmt
from .conditions import DOC_NAME, IMAGES, ConditionStore, image_name
from .files import remove_file, retry_while_in_use, write_file
from .procmodel import StartPos, start_pos
from .procrefs import refs_of
from .proto import is_proc_id
from .words import as_json

if TYPE_CHECKING:
    from .project import BuildResult, Project

# 片付けが消す途中の保存物の名前の終わり(SentStore.drop_unused)。freeze は id の
# 名前のフォルダーだけを使うので、この名前のものを保存物と混同しない
_DROPPING = ".del"


def _digest(raw: bytes) -> bytes:
    return hashlib.blake2b(raw, digest_size=16).digest()


def _digests_id(digests: dict) -> str:
    """{相対の位置: 中身の指紋} から、開始した時点の定義の id を作る。"""
    h = hashlib.blake2b(digest_size=16)
    for rel in sorted(digests):
        h.update(rel.encode("utf-8") + b"\0" + digests[rel])
    return h.hexdigest()


def _move_into_place(tmp: Path, dst: Path) -> None:
    """書き終えたフォルダー tmp の名前を dst に変える。

    Windows は、ほかのプロセスが中のファイルを開いている間、フォルダーの名前の
    変更を PermissionError(WinError 5)で拒否する。書いた直後のフォルダーは、
    たまにこれで拒否され、すぐに変えられるようになるので、少し待ってやり直す
    (retry_while_in_use)。

    同じ中身を同時に保存して、dst が先にできたなら、そちらを使う(dst があると、
    名前の変更は拒否される)。
    """
    def move() -> None:
        try:
            os.replace(tmp, dst)
        except OSError:
            if not dst.is_dir():
                raise
    retry_while_in_use(move)


@dataclass
class BuiltInfo:
    """送った手順のコピーから読み戻した成果物。

    BuildResult と違って中身(blob)を持たない。送った手順のコピーにあるのは、記録と
    実行中の装置を読むのに必要な欄だけである。中身が必要なのは転送のときで、そのときは
    その場で変換したばかりの BuildResult がある。
    """
    id: str
    name: str
    laps: int | str = 1
    lanes: int = 1
    start: StartPos | None = None
    lap_map: list = field(default_factory=list)
    awaits: list = field(default_factory=list)
    marks: list = field(default_factory=list)
    loop_map: list = field(default_factory=list)
    total_frames: int = 0
    events: int = 0
    at: float = 0.0
    # 開始した時点の定義(BuildResult の同じ名前の欄)
    sources: str = ""
    source_name: str = ""
    video: dict | None = None
    # 実行中に周回数を変えるときに使う(BuildResult の同じ名前の欄)
    finals: bool = True
    final_whens: list = field(default_factory=list)
    final_branches: list = field(default_factory=list)

    @property
    def hash(self) -> str:
        return self.id


@dataclass
class SentView:
    """装置へ送った手順を、全体を描く形(周回数のくり返しを 1 周だけ・先頭から・送る
    手順と同じレーンの数)で変換したもの。その 1 周は、その実行のある周として
    「対象の回」を解決する(dsl.compile_procs の drawn_laps と drawn_lap)。

    実行中のレーンの図と、その上の現在位置・止まる先・待ちの位置は、これから
    描く。手順のファイルは開始した後も編集されうるので、開始のときに 1 周目として
    変換したものを、送った記録(sent/<id>.json)に一緒に残しておく。ほかの周の分は、
    開始した時点の定義(保存物)から作る(positions.lap_view)。図を描くのに必要な欄
    だけを持つ。
    """
    blob: bytes
    total_frames: int
    labels: list
    precondition: str
    resume_points: list
    awaits: list
    marks: list
    warnings: list
    # くり返しの回番号の対応表。くり返しの 2 回目より後の位置を、図の上に置くのに
    # 使う(区切りの frame は最初の回の位置である)
    loop_map: list = field(default_factory=list)
    # 図のリビジョン(中身から作る)。同じ装置で同じ id を始め直すと、開始位置より前や
    # ラベル名だけが違うものに差し替わりうる。画面はこれで図を取得し直す
    rev: str = field(init=False, default="")

    def __post_init__(self):
        self.rev = hashlib.blake2b(
            json.dumps(self.to_doc(), ensure_ascii=False,
                       sort_keys=True).encode("utf-8"),
            digest_size=8).hexdigest()

    @property
    def hash(self) -> str:
        return binfmt.proc_hash(self.blob)

    @classmethod
    def of(cls, r: BuildResult) -> SentView:
        return cls(blob=r.blob, total_frames=r.total_frames, labels=r.labels,
                   precondition=r.precondition, resume_points=r.resume_points,
                   awaits=r.awaits, marks=r.marks, warnings=r.warnings,
                   loop_map=r.loop_map)

    def to_doc(self) -> dict:
        return {"blob": base64.b64encode(self.blob).decode("ascii"),
                "total_frames": self.total_frames, "labels": self.labels,
                "precondition": self.precondition, "resume_points": self.resume_points,
                "awaits": self.awaits, "marks": self.marks,
                "warnings": self.warnings, "loop_map": self.loop_map}

    @classmethod
    def from_doc(cls, v: dict) -> SentView:
        return cls(blob=base64.b64decode(v.get("blob") or ""),
                   total_frames=int(v.get("total_frames", 0)),
                   labels=v.get("labels") or [],
                   precondition=v.get("precondition") or "",
                   resume_points=v.get("resume_points") or [],
                   awaits=v.get("awaits") or [], marks=v.get("marks") or [],
                   warnings=v.get("warnings") or [],
                   loop_map=v.get("loop_map") or [])


def _latest_meta(doc) -> dict:
    """送った手順のコピー(sent/<id>.json)の、装置ごとの情報のうち最も新しいもの。

    名前の表と、古い送った手順のコピーの片付けに使う。どちらも装置を問わない。
    """
    metas = doc.get("by_dev") if isinstance(doc, dict) else None
    metas = [m for m in (metas or {}).values() if isinstance(m, dict)]
    return max(metas, key=lambda m: float(m.get("at", 0.0)), default={})


class SentStore:
    """プロジェクトの送った手順のコピーと、開始した時点の定義。"""

    def __init__(self, project):
        self.project = project
        self.root = project.root
        self._lock = project._build_cache_lock
        # 装置へ送った成果物(id で探す)。正式な出所は sent/ で、これはそのキャッシュ
        # である(サーバーを起動し直すと空になり、sent/ から読み直す)
        self._built_by_id: dict = {}
        # 送った手順のレコードの並び(id で探す)。「止まる先」を毎周期読み直さない
        self._sent_records: dict = {}
        # 送った手順の、全体を描く変換結果((装置 ID, 送った中身の id) で探す。SentView)
        # 開始位置より前やラベル名だけを修正して別の装置で開始すると、送る中身は同じ
        # id のまま描画用の変換結果だけが違うので、装置ごとに持つ
        self._sent_views: dict = {}
        # 開始した時点の定義を読むプロジェクト(id で探す)と、画像の指紋のキャッシュ
        self._frozen: dict = {}
        self._image_digests: dict = {}
        # 開始が保存物を使い回すと決めて時刻を新しくするのと、片付けが時刻を見て
        # 消すのを、重ねないためのロック(freeze と drop_unused)。重なると、片付けが
        # 古い時刻を読んだ後に開始が使い回すと決め、開始は消えた保存物から変換する
        self._sources_lock = threading.Lock()

    def sources_dir(self) -> Path:
        return self.dir() / "sources"

    def _source_parts(self, name: str, as_name: str = "") -> tuple[dict, dict]:
        """手順 name の実行に使うファイルを読む。

        返すのは {相対の位置: 中身} と {相対の位置: 基準画像などの場所} である。
        読めないファイルは含めない(変換と判定がその理由を出す)。as_name は、手順
        name の保存物の中の名前である(開始の後に改名された手順を比べるときに使う)。
        """
        texts: dict[str, bytes] = {}
        images: dict[str, Path] = {}
        seen: set[str] = set()
        frametables: set[str] = set()
        conds: set[str] = set()
        todo = [name]
        while todo:
            pname = todo.pop()
            if pname in seen:
                continue
            seen.add(pname)
            try:
                raw = self.project.procedure_path(pname).read_bytes()
            except (OSError, ValueError):
                continue
            saved = as_name if as_name and pname == name else pname
            texts[f"procedures/{saved}.json"] = raw
            try:
                doc = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                continue
            more_procs, more_frametables, more_conds = refs_of(doc)
            todo.extend(more_procs)
            frametables |= more_frametables
            conds |= more_conds
        for ref in frametables:
            try:
                raw = self.project.frametable_path(ref).read_bytes()
                texts[f"frametables/{ref}.csv"] = raw
            except (OSError, ValueError):
                pass
        store = ConditionStore(self.root)
        for cond in conds:
            try:
                raw = store.doc_path(cond).read_bytes()
            except (OSError, ValueError):
                continue
            # 中身の壊れた条件も保存する。判定が同じ理由を出せるようにする
            texts[f"conditions/{cond}/{DOC_NAME}"] = raw
            try:
                doc = json.loads(raw.decode("utf-8"))
                rels = [*doc.get("refs", []), *doc.get("negatives", [])]
            except (ValueError, TypeError, AttributeError, RecursionError):
                continue
            for rel in rels:
                try:
                    images[f"conditions/{cond}/{IMAGES}/{image_name(rel)}"] = \
                        store.image_path(cond, rel)
                except (ValueError, TypeError):
                    pass
        return texts, images

    def _image_digest(self, path: Path) -> bytes | None:
        """画像の指紋。毎秒比べるので、大きさと更新時刻が同じ間は読み直さない。"""
        try:
            st = path.stat()
            key = (st.st_size, st.st_mtime_ns)
            got = self._image_digests.get(path)
            if got is None or got[0] != key:
                got = (key, _digest(path.read_bytes()))
                self._image_digests[path] = got
            return got[1]
        except OSError:
            return None

    def sources_id(self, name: str, as_name: str = "") -> str:
        """手順 name の実行に使うファイルの今の中身の id(freeze が返す id と比べる)。"""
        texts, images = self._source_parts(name, as_name)
        digests = {rel: _digest(raw) for rel, raw in texts.items()}
        for rel, path in images.items():
            got = self._image_digest(path)
            if got is not None:
                digests[rel] = got
        return _digests_id(digests)

    def freeze(self, name: str) -> str:
        """手順 name の実行に使うファイルを保存し、id を返す。保存できなければ OSError。

        中身が同じなら同じ id になり、前の保存物を使い回す。
        """
        texts, images = self._source_parts(name)
        parts = dict(texts)
        for rel, path in images.items():
            try:
                parts[rel] = path.read_bytes()
            except OSError:
                pass
        sid = _digests_id({rel: _digest(raw) for rel, raw in parts.items()})
        dst = self.sources_dir() / sid
        with self._sources_lock:
            if dst.is_dir():
                os.utime(dst)       # 古い保存物を片付ける処理に消されないようにする
                return sid
        tmp =self.sources_dir() / f"{sid}.{threading.get_ident()}.tmp"
        try:
            for rel, raw in parts.items():
                p = tmp / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)
            tmp.mkdir(parents=True, exist_ok=True)     # 中身がないときも保存物を作る
            _move_into_place(tmp, dst)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return sid

    def frozen(self, sid: str) -> Project:
        """保存物 sid を読むプロジェクト。"""
        with self._lock:
            got = self._frozen.get(sid)
            if got is None:
                got = self._frozen[sid] = type(self.project)(self.frozen_root(sid))
        return got

    def frozen_root(self, sid: str) -> Path:
        """保存物 sid の場所。sid は送った記録から読む値なので、形を確認する。"""
        if not is_proc_id(sid):
            raise ValueError(f"定義の id が正しくありません: {as_json(sid)}")
        return self.sources_dir() / sid

    def edited_since(self, art, video) -> bool:
        """実行を開始した後に、その実行の定義か装置に設定した映像が変わったか。"""
        if not art.sources:
            return False
        return (self.sources_id(art.name, art.source_name) != art.sources
                or video != art.video)

    def drop_unused(self, keep: set, older_than: float = 3600.0) -> None:
        """動いている実行が使わない保存物を消す。

        続きを始めるのに必要な間だけ置く。保存してから実行の記録に書かれるまでの間に
        消さないよう、新しい保存物は残す。

        消すときは、先に freeze が使わない名前(<元の名前>.<乱数>.del)へ変えてから
        消す。中のファイルを誰かが開いていると、消せるファイルだけが消えてフォルダーが
        残る。元の名前のまま消すと、freeze は中身の欠けた保存物を使い回す。名前を
        変えられなければ、今回は消さずに残す(欠けずに残る)。消し残りは、次の
        片付けで消す。
        """
        d = self.sources_dir()
        if not d.is_dir():
            return
        limit = time.time() - older_than
        for p in d.iterdir():
            if p.name.endswith(_DROPPING):
                shutil.rmtree(p, ignore_errors=True)    # 前の片付けの消し残り
                continue
            if p.name in keep:
                continue
            gone = p.with_name(f"{p.name}.{secrets.token_hex(4)}{_DROPPING}")
            # 時刻を見てから名前を変え終えるまでの間に、開始が使い回すと決めない
            # ようにする
            with self._sources_lock:
                try:
                    if p.stat().st_mtime >= limit:
                        continue
                    retry_while_in_use(functools.partial(p.rename, gone))
                except OSError:
                    continue
                with self._lock:
                    self._frozen.pop(p.name, None)
            shutil.rmtree(gone, ignore_errors=True)

    def dir(self):
        return self.root / "sent"

    def name_of(self, sid: str) -> str:
        """装置へ送った手順の名前。送った手順のコピーがなければ空である。"""
        try:
            doc = json.loads((self.dir() / f"{sid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(_latest_meta(doc).get("name") or "")

    def remember(self, r: BuildResult,
                       view: BuildResult | None = None, dev: str = "",
                       sources: str = "", source_name: str = "",
                       video: dict | None = None) -> None:
        """装置へ送る成果物を送った手順のコピーに残す。

        呼ぶのは装置へ転送するところである。画面は毎秒すべての手順を変換する
        ので、変換のたびに書くと送った手順のコピーが下書きで埋まる。装置が報告する id は
        転送したものだけなので、それだけを残せば足りる。

        view は、同じ手順を全体を描く形(周回数のくり返しを 1 周だけ・先頭から・送る
        手順と同じレーンの数。その 1 周は送る手順の周回数の 1 周目として解決する)で、
        送る手順と続けて変換したものである。実行中のレーンの図はこれから描く(SentView)。
        dev は送る先の装置 ID で、描画用の変換結果も PC 側の情報(名前・選択待ち・区切り)
        も装置ごとに残す(分からなければ空)。sources と source_name は開始した時点の
        定義の id とその中の手順の名前、video は装置に設定した映像である。

        id は装置へ送るレコードから作るので、スクリプトのコマンドや選択肢の
        「選択条件」だけが違う 2 つの手順は同じ id になる。同じ id の情報を 1 つに
        まとめると、片方の装置へ送ったときに、もう片方の装置で実行中の判定まで
        入れ替わる。
        """
        at = r.start
        meta = {
            "name": r.name, "laps": r.laps, "lanes": r.lanes,
            "start": (None if at is None else
                      {"path": list(at.path), "block": at.block,
                       "turns": list(at.turns), "after": at.after,
                       "waited": at.waited}),
            "lap_map": r.lap_map, "awaits": r.awaits, "marks": r.marks,
            "loop_map": r.loop_map,
            "total_frames": r.total_frames, "events": r.events,
            "at": time.time(),
            "finals": r.finals,
            "final_whens": [list(w) for w in r.final_whens],
            "final_branches": r.final_branches,
            "sources": sources, "source_name": source_name or r.name, "video": video,
        }
        r = replace(r, sources=sources, source_name=source_name or r.name, video=video)
        sent_view = SentView.of(view) if view is not None else None
        d = self.dir()
        path = d / f"{r.hash}.json"
        # 装置ごとの情報と描画用の変換結果を持つ。同じ id で別の装置へ送った分は残す
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        old = old if isinstance(old, dict) else {}
        views = old.get("views") if isinstance(old.get("views"), dict) else {}
        by_dev = old.get("by_dev") if isinstance(old.get("by_dev"), dict) else {}
        if sent_view is not None:
            views[dev] = sent_view.to_doc()
        else:
            views.pop(dev, None)
        by_dev[dev] = meta
        doc = {"id": r.hash, "by_dev": by_dev, "views": views}
        with self._lock:
            # 転送したものをキャッシュとして覚える。ここで上書きしないと、実行中に
            # 編集した後の情報で判定してしまう
            self._built_by_id[(dev, r.hash)] = r
            if sent_view is not None:
                self._sent_views[(dev, r.hash)] = sent_view
            else:
                self._sent_views.pop((dev, r.hash), None)
        try:
            d.mkdir(parents=True, exist_ok=True)
            write_file(path, json.dumps(doc, ensure_ascii=False))
            # 送ったバイト列そのものも置く。「止まる先」を求めるには、装置に
            # 入っているレコードをたどる必要がある
            write_file(d / f"{r.hash}.bin", r.blob)
        except OSError:
            # 送った手順のコピーに残せなくても実行は続ける。困るのは、あとで記録を
            # 読むときに手順名が出ないことだけである
            pass

    def built(self, proc_id: str, dev: str):
        """装置 dev へ転送した成果物を id で探す。知らない id なら None。

        キャッシュにあるのも送った手順のコピーにあるのも、装置へ転送したものだけである
        (キャッシュは remember が入れる)。変換しただけのものは入らない。同じ id でも、
        装置ごとに PC 側の情報が違いうる(remember)ので、装置 ID と
        一緒に探す。その装置の分がなければ、送る先の分からない転送の分(空の
        装置 ID。接続先を直接指定したコマンド)を使う。

        メモリ上のキャッシュを先に見て、なければ送った手順のコピーから読む。サーバーを起動し直した
        あとや、実行中に手順のファイルを編集したあとでも、装置が実行している
        手順そのものが見つかる。
        """
        # id は送った手順のコピーのファイル名になる(sent/<id>.json)。id の形でなければ
        # 探さない。装置の状態と画面の要求から来る値で、細工した値でプロジェクトの外を
        # 読ませない
        if not is_proc_id(proc_id):
            return None
        # 送る先の分からない分は、その装置の分がキャッシュにも送った手順の
        # コピーにもないと確認してから使う。キャッシュにある分を先に返すと、送った手順の
        # コピーにあるその装置の分を読まない
        got = self._built_for(proc_id, dev)
        if got is None and dev:
            got = self._built_for(proc_id, "")
        return got

    def _built_for(self, proc_id: str, dev: str):
        """装置 dev の分だけを、キャッシュ、送った手順のコピーの順に探す(built)。
        なければ None。
        """
        with self._lock:
            hit = self._built_by_id.get((dev, proc_id))
        if hit is not None:
            return hit
        try:
            doc = json.loads(
                (self.dir() / f"{proc_id}.json").read_text(
                    encoding="utf-8"))["by_dev"][dev]
            if not isinstance(doc, dict):
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        at = doc.get("start")
        art = BuiltInfo(
            id=proc_id, name=doc.get("name", ""),
            laps=doc.get("laps", 1), lanes=int(doc.get("lanes", 1)),
            start=(None if not at else
                   start_pos(at.get("path"), at.get("block", ""),
                             at.get("turns"), at.get("after"), at.get("waited"))),
            lap_map=doc.get("lap_map") or [],
            awaits=doc.get("awaits") or [],
            marks=doc.get("marks") or [],
            loop_map=doc.get("loop_map") or [],
            total_frames=int(doc.get("total_frames", 0)),
            events=int(doc.get("events", 0)),
            at=float(doc.get("at", 0.0)),
            finals=bool(doc.get("finals", True)),
            final_whens=[tuple(w) for w in doc.get("final_whens") or []],
            final_branches=list(doc.get("final_branches") or []),
            sources=str(doc.get("sources") or ""),
            source_name=str(doc.get("source_name") or doc.get("name", "")),
            video=doc.get("video"))
        # 読んだものは覚えておく。実行中は画面(毎秒)と監視(0.5 秒)が
        # レーンごとに探すので、そのたびにファイルを読むことになる
        with self._lock:
            self._built_by_id.setdefault((dev, proc_id), art)
        return art

    def view(self, proc_id: str, dev: str = "") -> SentView | None:
        """その装置へ送った手順の、全体を描く変換結果を探す。なければ None。

        メモリ上のキャッシュを先に見て、なければ送った手順のコピーから読む(サーバーを起動し
        直した後も、開始のときの図が見つかる)。装置の分がなく、送る先の分からない
        変換結果(空の装置 ID)があれば、それを使う。
        """
        if not is_proc_id(proc_id):          # built と同じ
            return None
        with self._lock:
            hit = self._sent_views.get((dev, proc_id))
        if hit is not None:
            return hit
        try:
            doc = json.loads(
                (self.dir() / f"{proc_id}.json").read_text(
                    encoding="utf-8"))
            views = doc.get("views") or {}
            v = views.get(dev) or views.get("")
            if not isinstance(v, dict):
                return None
            got = SentView.from_doc(v)
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        with self._lock:
            self._sent_views.setdefault((dev, proc_id), got)
        return got

    def keep_view(self, proc_id: str, dev: str, r: BuildResult) -> SentView:
        """送った記録に図の変換結果がない実行の、代わりの変換結果を覚える(覚えるだけ)。

        図と、その上の位置の対応付けは同じ変換結果から作る。今の中身を毎回変換し直すと、
        実行中に手順を修正したとき、図は前の中身のまま位置だけが修正した中身に
        対応付けられる。最初に作った変換結果を、その実行の図として使い続ける。
        """
        v = SentView.of(r)
        with self._lock:
            return self._sent_views.setdefault((dev, proc_id), v)

    def records(self, proc_id: str):
        """装置へ送ったレコードを読み戻す。なければ None。

        返すのは (レコードの並び, 総フレーム数) である。「止まる先」を求める
        ときにたどる。読み直しは高くつくので覚えておく。
        """
        if not is_proc_id(proc_id):          # built と同じ
            return None
        with self._lock:
            hit = self._sent_records.get(proc_id)
        if hit is not None:
            return hit
        try:
            data = (self.dir() / f"{proc_id}.bin").read_bytes()
            got = binfmt.decode(data)
        except (OSError, binfmt.DecodeError):
            return None
        with self._lock:
            self._sent_records[proc_id] = got
        return got

    def names(self) -> dict[str, str]:
        """送った手順のコピーにある id から手順名への対応(記録の復元に使う)。"""
        out: dict[str, str] = {}
        d = self.dir()
        if d.is_dir():
            for f in d.glob("*.json"):
                try:
                    doc = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                meta = _latest_meta(doc)
                if meta.get("name"):
                    out[doc.get("id", f.stem)] = meta["name"]
        with self._lock:
            for h, name in self.project._built_ids.items():
                out.setdefault(h, name)
        return out

    def drop_old(self, oldest: float) -> None:
        """記録から参照されえなくなった成果物を sent/ から消す。

        残っている記録より古い成果物は、もうどの行からも参照されない。件数では
        切らない。記録が 1000 時間ぶん残るのに送った手順のコピーが先に切れると、古い行の
        手順名が出なくなる。

        ただし、動いているグループのレーンが指す成果物(runstate.json の proc_id)は、
        記録より古くても消さない。成果物の時刻は転送した時刻なので、長い実行の
        途中で古い記録の世代を消すと、その実行の成果物も記録より古くなる。停止した
        位置、実行中のレーンの周の数え、選択待ちの判定、周回を終えた続きの変換は、
        この成果物から読む。runstate.json が読めなければ、今回は何も消さない。
        """
        d = self.dir()
        if not d.is_dir() or oldest <= 0:
            return
        keep = self._on_record()
        if keep is None:
            return
        for f in d.glob("*.json"):
            if f.stem in keep:
                continue
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
                if float(_latest_meta(doc).get("at", 0.0)) < oldest:
                    # .bin を先に消す。.json が先だと、.bin を消せなかったときに
                    # 探す手がかり(.json)がなくなり、.bin がずっと残る
                    remove_file(f.with_suffix(".bin"))
                    remove_file(f)
            except (OSError, ValueError):
                continue

    def _on_record(self) -> set[str] | None:
        """動いているグループのレーンが指す成果物の id(runstate.json の proc_id)。

        runstate.json の読み書きは coupler.py が行う。ファイルがなければ、指している
        成果物はない。読めなければ None を返す(消してよいかが分からない)。
        """
        try:
            data = json.loads((self.root / "runstate.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError):
            return None
        groups = data.get("groups") if isinstance(data, dict) else None
        return {x["proc_id"] for g in (groups if isinstance(groups, list) else [])
                if isinstance(g, dict)
                for x in (g.get("lanes") if isinstance(g.get("lanes"), list) else [])
                if isinstance(x, dict) and isinstance(x.get("proc_id"), str)}

    def rename(self, old: str, new: str) -> None:
        """装置へ送った成果物に記録した名前を付け替える。

        実行中の装置は成果物の id しか報告せず、レーンはその名前を示す。停止した
        後の図と「停止した位置」も、その名前で探す。付け替えないと、改名した瞬間
        から実行中のレーンが古い名前を示し、停止した後の図も「停止した位置」も
        出なくなる(実行中の図は、送った記録から id で探すので変わらない)。
        """
        with self._lock:
            for r in self._built_by_id.values():
                if r.name == old:
                    r.name = new
        d = self.dir()
        if not d.is_dir():
            return
        for path in d.glob("*.json"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            metas = doc.get("by_dev")
            if not isinstance(metas, dict):
                continue
            hit = [m for m in metas.values()
                   if isinstance(m, dict) and m.get("name") == old]
            if not hit:
                continue
            for m in hit:
                m["name"] = new
            try:
                write_file(path, json.dumps(doc, ensure_ascii=False))
            except OSError:
                pass
