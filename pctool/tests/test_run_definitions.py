"""実行は、開始した時点の手順、フレーム表、条件で最後まで動く。

決まりは specs/coupling.md D12 にある。
"""
import errno
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from padcue.conditions import COMPARE_H, COMPARE_W, ConditionStore
from padcue.condwatch import CondWatcher
from padcue.coupler import Coupler
from padcue.frames import encode_png
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from tests.fakedev import FakeLink, FakePool
from tests.helpers import add_images, manual_arm

DEV = "aaaa00000001"


def solid(value: int) -> bytes:
    return encode_png(COMPARE_W, COMPARE_H, bytes((value, value, value))
                      * (COMPARE_W * COMPARE_H))


@pytest.fixture
def proj(tmp_path):
    """呼び出し先、フレーム表、条件を使う手順のあるプロジェクト。"""
    p = Project(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(solid(200))
    store = ConditionStore(tmp_path)
    store.save("画面", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[solid(10)])
    p.save_frametable_grid("表", ["F", "A"], [["1", "0"]])
    fmt = {"kind": "procedure", "format": FORMAT_VERSION}
    p.save_procedure_doc("呼ばれる", {**fmt, "body": [
        {"type": "frametable", "ref": "表"}]})
    p.save_procedure_doc("手順", {**fmt, "body": [
        {"type": "call", "id": "cl-1", "ref": "呼ばれる"},
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "出た", "what": {"kind": "condition", "ref": "画面"}, "body": []},
            manual_arm("出ない")]}]})
    return p


def test_the_saved_definitions_hold_everything_the_run_reads(proj):
    """保存物に、手順、呼び出し先、フレーム表、条件と基準画像が入ること。"""
    root = proj.sent.frozen_root(proj.sent.freeze("手順"))
    assert (root / "procedures" / "手順.json").is_file()
    assert (root / "procedures" / "呼ばれる.json").is_file()
    assert (root / "frametables" / "表.csv").is_file()
    assert ConditionStore(root).load("画面").refs
    assert proj.sent.frozen(root.name).build_safe("手順", write=False)[0] is not None
    assert not (root / "build").exists(), "保存物に変換の結果を書かない"


def test_the_same_contents_share_one_saved_copy(proj):
    """中身が同じなら同じ保存物を使い、中身が変われば別の保存物になること。"""
    first = proj.sent.freeze("手順")
    assert proj.sent.freeze("手順") == first
    assert proj.sent.sources_id("手順") == first
    ConditionStore(proj.root).save(
        "画面", {"kind": "file", "path": "x.png"}, [(0, 0, 1, 1)], 70)
    assert proj.sent.freeze("手順") != first


@pytest.mark.parametrize("change", ["procedure", "callee", "frametable", "condition",
                                    "image", "video"])
def test_any_edit_after_the_start_is_noticed(proj, change):
    """開始の後の編集と装置の映像の変更を、見つけること。"""
    sid = proj.sent.freeze("手順")
    r = proj.sent.frozen(sid).build("手順", write=False)
    video = {"source": {"kind": "file", "path": "a.png"}, "crop": None}
    proj.sent.remember(r, None, DEV, sources=sid, video=video)
    art = proj.sent.built(r.hash, DEV)
    assert not proj.sent.edited_since(art, video)
    fmt = {"kind": "procedure", "format": FORMAT_VERSION}
    store = ConditionStore(proj.root)
    if change == "procedure":
        proj.save_procedure_doc("手順", {**fmt, "body": []})
    elif change == "callee":
        proj.save_procedure_doc("呼ばれる", {**fmt, "body": []})
    elif change == "frametable":
        proj.save_frametable_grid("表", ["F", "A"], [["1", "1"]])
    elif change == "condition":
        store.save("画面", {"kind": "file", "path": "x.png"}, [(0, 0, 1, 1)], 70)
    elif change == "image":
        path = store.image_path("画面", store.load("画面").ref_files[0])
        path.write_bytes(solid(11))
    else:
        video = None
    assert proj.sent.edited_since(art, video), change


def test_renaming_the_procedure_alone_is_not_an_edit(proj):
    """手順の名前を変えただけでは、実行の内容の変更と扱わないこと。"""
    sid = proj.sent.freeze("手順")
    r = proj.sent.frozen(sid).build("手順", write=False)
    proj.sent.remember(r, None, DEV, sources=sid)
    proj.rename_procedure("手順", "新しい名前")
    art = proj.sent.built(r.hash, DEV)
    assert art.name == "新しい名前"
    assert art.source_name == "手順"
    assert not proj.sent.edited_since(art, None)


def test_the_judgement_reads_the_condition_as_started(proj):
    """実行中に条件を保存しても、その実行の判定は開始した時点の条件で行うこと。

    条件で選ばれる選択肢が変われば、その後の入力も変わる。
    """
    sid = proj.sent.freeze("手順")
    r = proj.sent.frozen(sid).build("手順", write=False)
    proj.sent.remember(r, None, DEV, sources=sid, video=None)
    cw = CondWatcher(proj, pool=None, autostart=False)
    link = SimpleNamespace(cfg={"name": "1P"}, registered_id=DEV,
                           status={"proc": r.hash})
    root, _video = cw._started_with(link, link.status)
    assert cw.pick_index(["画面"], root=root) is None, "開始した時点は一致しない"
    add_images(ConditionStore(proj.root), "画面", refs=[solid(200)])
    assert cw.pick_index(["画面"]) == 0, "今の条件は一致する(このテストの前提)"
    assert cw.pick_index(["画面"], root=root) is None
    assert cw._started_with(link, link.status)[0] == proj.sent.frozen_root(sid)


def test_the_video_of_the_device_is_the_one_at_the_start(proj):
    """判定に使う装置の映像は、開始した時点のものであること。"""
    sid = proj.sent.freeze("手順")
    r = proj.sent.frozen(sid).build("手順", write=False)
    started = {"source": {"kind": "file", "path": "a.png"}, "crop": None}
    proj.sent.remember(r, None, DEV, sources=sid, video=started)
    cw = CondWatcher(proj, pool=None, autostart=False)
    link = SimpleNamespace(cfg={"name": "1P", "video": {"source": {"kind": "file",
                                                                   "path": "b.png"}}},
                           registered_id=DEV, status={"proc": r.hash})
    assert cw._started_with(link, link.status)[1] == started


def test_a_transfer_without_saved_definitions_reads_the_project(proj):
    """保存物のない転送(コマンドの push)は、今のフォルダーと今の設定を読むこと。"""
    r = proj.build("手順")
    proj.sent.remember(r, r, DEV)
    cw = CondWatcher(proj, pool=None, autostart=False)
    link = SimpleNamespace(cfg={"name": "1P", "video": "project-video"},
                           registered_id=DEV, status={"proc": r.hash})
    assert cw._started_with(link, link.status) == (proj.root, "project-video")
    assert not proj.sent.edited_since(proj.sent.built(r.hash, DEV), None)


def test_saved_definitions_a_run_does_not_use_are_removed(proj):
    """動いている実行が使わない古い保存物を消し、新しい保存物は残すこと。

    保存してから実行の記録に含まれるまでの間に消さないよう、新しい保存物は残す。
    """
    used = proj.sent.freeze("手順")
    proj.save_frametable_grid("表", ["F", "A"], [["1", "1"]])
    old = proj.sent.freeze("手順")
    proj.save_frametable_grid("表", ["F", "A", "B"], [["1", "1", "1"]])
    new = proj.sent.freeze("手順")
    long_ago = time.time() - 7200
    for sid in (used, old):
        os.utime(proj.sent.frozen_root(sid), (long_ago, long_ago))
    proj.sent.drop_unused({used})
    assert proj.sent.frozen_root(used).is_dir()
    assert not proj.sent.frozen_root(old).exists()
    assert proj.sent.frozen_root(new).is_dir()


def test_a_cleanup_at_the_same_time_does_not_remove_a_reused_copy(proj, monkeypatch):
    """古い保存物を使い回す開始と、別の開始の片付けが重なっても、使い回す保存物が
    消えないこと。

    片付けは 1 時間より古い保存物を消し、使い回す開始は保存物の時刻を新しくする。
    片付けが古い時刻を読んだ後、消す前に、開始が使い回すと決めると、開始は消えた
    保存物から変換することになる。ここでは、その順番を作る。片付けは、別の開始が
    使う保存物を知らないので、残す物を渡さない。
    """
    sid = proj.sent.freeze("手順")
    root = proj.sent.frozen_root(sid)
    long_ago = time.time() - 7200
    os.utime(root, (long_ago, long_ago))
    real = type(root).stat
    deleting = threading.Event()
    # 開始が使い回すかを決め終えたか、片付けが持つロックを待ち始めた
    decided = threading.Event()
    cleaner = threading.Thread(target=proj.sent.drop_unused, args=(set(),))
    sources_lock = proj.sent._sources_lock

    class _Watched:
        """保存物のロック。片付けでないスレッドが取得しに来たら decided を立てる。"""

        def __enter__(self):
            if threading.current_thread() is not cleaner:
                decided.set()
            return sources_lock.__enter__()

        def __exit__(self, *exc):
            return sources_lock.__exit__(*exc)
    monkeypatch.setattr(proj.sent, "_sources_lock", _Watched())

    def stat_then_wait_for_the_reuse(self, *args, **kwargs):
        got = real(self, *args, **kwargs)
        if self == root and threading.current_thread() is cleaner:
            deleting.set()
            # 片付けを古い時刻を読んだ所で止め、開始が使い回すかを決め終えるか、
            # ロックを待ち始めるまで待つ(5 秒は、どちらも起きないときにテストを
            # 打ち切る上限)
            decided.wait(5)
        return got
    monkeypatch.setattr(type(root), "stat", stat_then_wait_for_the_reuse)
    cleaner.start()
    try:
        assert deleting.wait(5), "片付けが古い保存物を消しに来ない(このテストの前提)"
        got = proj.sent.freeze("手順")
    finally:
        decided.set()
        cleaner.join()
    assert got == sid
    assert (proj.sent.frozen_root(got) / "procedures" / "手順.json").is_file(), \
        "使い回した保存物が消えた"


def test_a_cleanup_cut_short_leaves_no_partial_copy_to_reuse(proj, monkeypatch):
    """片付けが保存物を消す途中で中断しても、開始が中身の欠けた保存物を使い回さない
    こと。消し残りは、次の片付けで消すこと。

    中のファイルを誰かが開いていると、消せるファイルだけが消えてフォルダーが残る。
    それを使い回すと、開始した時点の定義の一部がないまま変換する。ここでは、消す
    途中で中のファイルが 1 つ開かれている形を作る。
    """
    sid = proj.sent.freeze("手順")
    root = proj.sent.frozen_root(sid)

    def files_in(d):
        return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
    whole = files_in(root)
    long_ago = time.time() - 7200
    os.utime(root, (long_ago, long_ago))
    real = shutil.rmtree
    held = [True]

    def one_file_held_open(path, *args, **kwargs):
        stuck = list(Path(path).rglob("呼ばれる.json"))
        if not (held[0] and stuck):
            return real(path, *args, **kwargs)
        # 開かれている 1 つのほかは消え、そのファイルと、それを含むフォルダーが残る
        for p in Path(path).rglob("*"):
            if p.is_file() and p not in stuck:
                p.unlink()
        if not kwargs.get("ignore_errors"):
            raise PermissionError(errno.EACCES, "アクセスが拒否されました。",
                                  str(stuck[0]))
        return None
    monkeypatch.setattr(shutil, "rmtree", one_file_held_open)
    proj.sent.drop_unused(set())
    got = proj.sent.freeze("手順")
    assert got == sid
    assert files_in(proj.sent.frozen_root(got)) == whole, \
        "中身の欠けた保存物を使い回した"
    held[0] = False
    proj.sent.drop_unused({got})
    assert [p.name for p in proj.sent.sources_dir().iterdir()] == [got], \
        "消し残りを次の片付けで消さない"


def test_a_brief_hold_on_the_new_files_does_not_stop_the_save(proj, monkeypatch):
    """書いた直後のファイルをほかのプロセスが一瞬開いていても、保存できること。

    保存は、一時のフォルダーに書いてから名前を変える。Windows は、ほかのプロセスが
    中のファイルを開いている間、フォルダーの名前の変更を PermissionError
    (WinError 5)で拒否する。書いた直後のフォルダーは、たまにこれで拒否され、
    すぐに変えられるようになる。ここでは、その拒否を 1 度だけ起こす。
    """
    real = os.replace
    refused = []

    def held_once(src, dst):
        if not refused and Path(src).is_dir():
            refused.append(src)
            raise PermissionError(errno.EACCES, "アクセスが拒否されました。",
                                  str(src), 5, str(dst))
        real(src, dst)
    monkeypatch.setattr(os, "replace", held_once)
    sid = proj.sent.freeze("手順")
    assert refused, "名前の変更を拒否できていない(このテストの前提)"
    assert (proj.sent.frozen_root(sid) / "procedures" / "手順.json").is_file()
    assert not [p.name for p in proj.sent.sources_dir().iterdir()
                if p.suffix == ".tmp"], "一時のフォルダーが残った"


def test_the_reason_a_save_failed_is_left_in_the_output(tmp_path, monkeypatch, capsys):
    """開始の前の保存ができないとき、画面の文は変えず、OS の理由を操作画面のサーバーの
    出力に残すこと。

    画面には OS の理由を出さない。出力にも残さないと、たまにしか起きない失敗の
    原因を後から追えない。
    """
    proj = Project(tmp_path)
    proj.init_sample()

    def no_space(name):
        raise OSError(errno.ENOSPC, "ディスクに十分な空き領域がありません。",
                      str(tmp_path / "sent"))
    monkeypatch.setattr(proj.sent, "freeze", no_space)
    coupler = Coupler(proj, FakePool(FakeLink("1P")))
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": 1}])
    finally:
        coupler.close()
    assert r["error"] == "1P の手順「サンプル」を保存できないため、開始できません", r
    err = capsys.readouterr().err
    assert "ディスクに十分な空き領域がありません。" in err, err
