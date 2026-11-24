"""一時ファイルでの置き換えが、Windows の一時的な拒否で諦めないこと。

登録情報 padcue.json などは一時ファイルに書いて置き換える。Windows は別スレッドが
その瞬間に読んでいると PermissionError(WinError 5)で拒否する。読む側は毎秒の
状態取得のような短いものなので、少し待ってやり直せば通る。諦めると改名や
登録が「保存したつもり」のまま失敗する。
"""
import os
import shutil
import time
from pathlib import Path

import pytest

from padcue.files import remove_tree, replace_file, sweep_unfinished_writes


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """やり直しの待ちを省く。"""
    monkeypatch.setattr("padcue.files.time.sleep", lambda s: None)


@pytest.fixture
def replace_pair(tmp_path):
    """置き換える一時ファイルと置き換え先の 2 つ(tmp, dst)を作って返す。"""
    dst = tmp_path / "a.json"
    dst.write_text("old", encoding="utf-8")
    tmp = tmp_path / "a.json.tmp"
    tmp.write_text("new", encoding="utf-8")
    return tmp, dst


def test_retries_while_target_is_briefly_locked(replace_pair, monkeypatch):
    tmp, dst = replace_pair
    real = Path.replace
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(5, "アクセスが拒否されました")
        return real(self, target)
    monkeypatch.setattr(Path, "replace", flaky)
    replace_file(tmp, dst)
    assert dst.read_text(encoding="utf-8") == "new"
    assert calls["n"] == 4


def test_gives_up_and_keeps_both_files(replace_pair, monkeypatch):
    """ずっと拒否されるなら例外を送出し、元のファイルも一時ファイルも残すこと。"""
    tmp, dst = replace_pair

    def denied(self, target):
        raise PermissionError(5, "アクセスが拒否されました")
    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(PermissionError):
        replace_file(tmp, dst)
    assert dst.read_text(encoding="utf-8") == "old"
    assert tmp.read_text(encoding="utf-8") == "new"


def test_removing_a_folder_waits_while_a_file_is_briefly_open(tmp_path, monkeypatch):
    """フォルダーの消去も、Windows の一時的な拒否(中のファイルを読んでいる瞬間)で
    諦めないこと。条件を開いている画面が condition.json を読む瞬間に消すと起きる。"""
    d = tmp_path / "条件"
    (d / "images").mkdir(parents=True)
    (d / "condition.json").write_text("{}", encoding="utf-8")
    real = shutil.rmtree
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(32, "別のプロセスが使用中です")
        return real(path)
    monkeypatch.setattr("padcue.files.shutil.rmtree", flaky)
    remove_tree(d)
    assert not d.exists() and calls["n"] == 3
    remove_tree(d)                  # もうなければ何もしない
    assert calls["n"] == 3


def test_removing_a_folder_gives_up_with_the_error(tmp_path, monkeypatch):
    """ずっと拒否されるなら例外を送出すること(例外にしないと、消えていないのに
    消えたように見える)。"""
    d = tmp_path / "条件"
    d.mkdir()

    def denied(path):
        raise PermissionError(32, "別のプロセスが使用中です")
    monkeypatch.setattr("padcue.files.shutil.rmtree", denied)
    with pytest.raises(PermissionError):
        remove_tree(d)
    assert d.exists()


def test_a_write_reaches_the_disk_before_the_replace(tmp_path, monkeypatch):
    """置き換える前に、一時ファイルの中身をディスクへ書き出すこと(fsync)。

    電源が切れると、名前の付け替えが中身より先にディスクへ書かれることがあり、
    置き換えた後のファイルが 0 バイトや 0 で埋まった中身になる。元の中身も一時
    ファイルも残らない。電源断そのものは起こせないので、書き出しと置き換えの順を見る。
    """
    import os

    from padcue import files
    order = []
    real_fsync, real_replace = os.fsync, files.replace_file

    def fsync(fd):
        order.append("fsync")
        real_fsync(fd)

    def replace(tmp, dst):
        order.append("replace")
        real_replace(tmp, dst)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(files, "replace_file", replace)
    files.write_file(tmp_path / "a.json", "中身")
    files.write_file(tmp_path / "b.bin", b"\x00\x01")
    assert order == ["fsync", "replace", "fsync", "replace"]
    assert (tmp_path / "a.json").read_text(encoding="utf-8") == "中身"
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01"


def test_two_writers_do_not_share_a_temporary_file(tmp_path, monkeypatch):
    """同じファイルへの 2 つの書き手(別のスレッドかプロセス)は、別の一時ファイルに
    書くこと。

    固定の名前だと、片方が書いている途中の一時ファイルをもう片方が置き換え、途中
    までの中身が正式なファイルになる。後から置き換える側は FileNotFoundError に
    なる。同時の重なりは決まった順で起こせないので、使う一時ファイルの名前を見る。
    """
    import threading

    from padcue import files
    used = []
    real_replace = files.replace_file

    def replace(tmp, dst):
        used.append(tmp.name)
        real_replace(tmp, dst)
    monkeypatch.setattr(files, "replace_file", replace)
    dst = tmp_path / "padcue.json"
    files.write_file(dst, "1")
    t = threading.Thread(target=files.write_file, args=(dst, "2"))
    t.start()
    t.join()
    assert len(set(used)) == 2, used
    assert all(n.startswith("padcue.json.") and n.endswith(".tmp") for n in used)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["padcue.json"]


def _left_behind(folder: Path, name: str, age: float) -> Path:
    """age 秒前に置かれたままのファイルを作る。"""
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("half", encoding="utf-8")
    at = time.time() - age
    os.utime(p, (at, at))
    return p


def test_unfinished_writes_left_by_a_killed_process_are_swept(tmp_path):
    """書いている途中にプロセスが強制終了されて残った一時ファイルを消すこと。消すのは、
    write_file の一時ファイルの名前で、置いてから 1 分たったファイルだけであること。

    置いたばかりの物は、別のプロセスがいま書いている途中かもしれない。名前の形が
    違う物は、padcue の一時ファイルではない。下のフォルダーは、指定されたときだけ見る。
    """
    old = _left_behind(tmp_path, "runstate.json.46568.47684.tmp", 3600)
    below = _left_behind(tmp_path / "a" / "images", "ref.png.1.2.tmp", 3600)
    new = _left_behind(tmp_path, "padcue.json.10.20.tmp", 5)
    others = [_left_behind(tmp_path, n, 3600) for n in (
        "memo.tmp", "a.json", "a.json.tmp", "a.json.12.tmp", "a.json.x.1.tmp")]
    folder = tmp_path / "b.1.2.tmp"
    folder.mkdir()
    at = time.time() - 3600
    os.utime(folder, (at, at))

    assert sweep_unfinished_writes(tmp_path) == 1
    assert not old.exists()
    assert below.exists(), "指定されていない下のフォルダーの中を消した"
    assert sweep_unfinished_writes(tmp_path, below=True) == 1
    assert not below.exists()
    assert new.exists(), "置いたばかりの一時ファイルを消した"
    assert all(p.exists() for p in others), [p.name for p in others if not p.exists()]
    assert folder.is_dir(), "フォルダーを消した"


def test_sweeping_a_missing_folder_does_nothing(tmp_path):
    """ないフォルダーの片付けは、何もせずに 0 を返すこと。"""
    assert sweep_unfinished_writes(tmp_path / "none") == 0
    assert sweep_unfinished_writes(tmp_path / "none", below=True) == 0
