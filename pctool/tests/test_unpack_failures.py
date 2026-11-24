"""インポートが途中で失敗するとき、受け取ったファイルを読めないときの扱い
(unpack.py。file-format.md §10-1・§10-4)。

次の 3 つが成立すること。
 - 書き込みが途中で失敗しても、新しい手順が古い参照先を指す組み合わせは残らない
   (参照される側から書く)。何が入ったかと、書けなかった件を示す
 - 受け取ったファイルの形の問題を、padcue の不具合(予期しないエラー)にしない
 - ダイアログを開いた直後の既定の扱いが、そのまま通る(利用者が何も入力して
   いない行を拒否しない)
"""
import errno
import json
import zipfile

import pytest

from padcue import pack, unpack
from padcue.conditions import ConditionStore
from padcue.names import NAME_MAX, name_key, validate_name
from padcue.procfmt import NEST_TEXT
from tests.helpers import condition_doc as cond_doc
from tests.helpers import export_pack as export
from tests.helpers import pack_manifest as manifest
from tests.helpers import pack_project as make
from tests.helpers import procedure_doc as proc
from tests.helpers import zip_pack as bad_pack


def test_writing_starts_from_what_is_referred_to(tmp_path):
    """書く並びは、条件、フレーム表、手順の順であること。

    途中で失敗しても、新しい手順が古い参照先で動く組み合わせが残らない。
    """
    pf = export(tmp_path, ["周回"], procs={"周回": proc([
        {"type": "frametable", "ref": "連続入力"},
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "出た", "what": {"kind": "condition", "ref": "画面"},
             "body": []}]}])},
        frametables={"連続入力": "F,A\n1,1\n"}, conds=["画面"])
    here = make(tmp_path, sub="here")
    got = unpack.apply(here, pf, unpack.plan(here, pf))
    assert [k for k, _n in got["wrote"]] == ["condition", "frametable", "procedure"]


def test_a_write_that_fails_halfway_says_what_got_in(tmp_path, monkeypatch):
    """書き込みが途中で失敗したら、書けなかった件と、既に入った物を示すこと。

    表計算ソフトがフレーム表を開いていると、置き換えが拒否される。何も示さないと、
    利用者には何が入ったのかが分からない。手順はまだ書かれていない。
    """
    # 手順から参照されない表も 1 件、選んで入れる
    pf = export(tmp_path, ["周回", ("frametable", "別の表")],
                procs={"周回": proc([
                    {"type": "frametable", "ref": "連続入力"},
                    {"type": "wait", "frames": 10}])},
                frametables={"連続入力": "F,A\n1,1\n", "別の表": "F,B\n1,1\n"})
    here = make(tmp_path, procs={"周回": proc([{"type": "wait", "frames": 20}])},
                frametables={"連続入力": "F,B\n1,1\n"}, sub="here")
    items = unpack.plan(here, pf)
    for it in items:
        it.action = unpack.TAKE if it.state == "new" else unpack.REPLACE
    real = unpack.write_file

    def refuse(dst, data):
        if dst.name == "連続入力.csv":
            raise PermissionError(13, "Permission denied", str(dst))
        real(dst, data)
    monkeypatch.setattr(unpack, "write_file", refuse)
    with pytest.raises(unpack.ApplyError) as e:
        unpack.apply(here, pf, items)
    text = str(e.value)
    assert "フレーム表「連続入力」をインポートできません" in text, text
    assert "ほかのアプリで開いていないか" in text, text
    assert "フレーム表「別の表」はインポート済みです" in text, text
    assert "Permission" not in text and "Errno" not in text, text
    assert e.value.wrote == [("frametable", "別の表")]
    # 手順は参照先より後に書くので、まだ古いまま
    assert here.load_procedure_doc("周回")["body"] == [{"type": "wait", "frames": 20}]


def test_a_condition_kept_aside_is_told_where(tmp_path, monkeypatch):
    """条件の置き換えも元に戻すことも失敗したら、元の条件の場所を画面に示すこと。

    場所を示さないと、画像を含む元の条件がどこにあるのかを知る手段がない。
    """
    store = ConditionStore(tmp_path)
    store.save("画面", {"kind": "file", "path": "x.png"}, [(0.0, 0.0, 1.0, 1.0)], 88)
    real = type(tmp_path).replace

    def flaky(self, target):
        if self.name in ("new", "old"):
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, target)
    monkeypatch.setattr(type(tmp_path), "replace", flaky)
    with pytest.raises(ValueError) as e:
        store.put_folder("画面", json.dumps(cond_doc()), {})
    text = str(e.value)
    assert "条件「画面」を置き換えられず" in text, text
    assert ".padcue-" in text and "old" in text, text
    assert "Permission" not in text, text


def test_a_brief_hold_on_a_new_condition_folder_does_not_stop_the_import(
        tmp_path, monkeypatch):
    """書いた直後の条件のフォルダーをほかのプロセスが一瞬開いていても、条件を
    インポートできること。

    条件は、別の場所に書き終えてから、フォルダーの名前を変えて入れる。Windows は、
    ほかのプロセスが中のファイルを開いている間、フォルダーの名前の変更を
    PermissionError(WinError 5)で拒否する。書いた直後のフォルダーは、たまにこれで
    拒否され、すぐに変えられるようになる。ここでは、その拒否を 1 度だけ起こす。
    """
    pf = export(tmp_path, [("condition", "画面")], conds=["画面"])
    here = make(tmp_path, sub="here")
    real = type(tmp_path).replace
    refused = []

    def held_once(self, target):
        if not refused and self.name == "new":
            refused.append(self)
            raise PermissionError(errno.EACCES, "アクセスが拒否されました。",
                                  str(self), 5, str(target))
        return real(self, target)
    monkeypatch.setattr(type(tmp_path), "replace", held_once)
    got = unpack.apply(here, pf, unpack.plan(here, pf))
    assert refused, "名前の変更を拒否できていない(このテストの前提)"
    assert got["wrote"] == [("condition", "画面")]
    assert ConditionStore(here.root).load("画面").refs


@pytest.mark.parametrize("flag, ctype, words", [
    (0x1, zipfile.ZIP_DEFLATED, "パスワードが設定されています"),
    (0x0, 9, "padcue が読めない方式で圧縮されています"),
])
def test_a_zip_padcue_cannot_read_is_refused_for_what_it_is(tmp_path, flag, ctype,
                                                          words):
    """パスワード付きの zip と、読めない方式の zip は、その理由で拒否すること。

    padcue の不具合と混同させない(どちらも別のツールで作り直したファイルなので、
    padcue でエクスポートし直すよう求める)。
    """
    dest = tmp_path / "受け取った.padcue"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("pack.json", manifest())
    raw = bytearray(dest.read_bytes())
    # 中央ディレクトリの汎用フラグ(+8)と圧縮方式(+10)を書き換える
    at = raw.rfind(b"PK\x01\x02")
    raw[at + 8] |= flag
    raw[at + 10] = ctype
    dest.write_bytes(bytes(raw))
    with pytest.raises(unpack.PackError) as e:
        unpack.read(dest)
    assert words in str(e.value)
    assert "padcue でエクスポートしたファイルを使用してください" in str(e.value)


def test_a_padcue_with_a_broken_body_is_called_broken(tmp_path):
    """圧縮した中身が壊れた padcue ファイルは、壊れていると示すこと。

    手順 1 件だけでは裸の .json になるので、同じ名前を 2 度渡して padcue ファイルに
    する。
    """
    src = make(tmp_path, procs={"周回": proc([{"type": "wait", "frames": 1}] * 50)},
               sub="src")
    dest = tmp_path / "x.padcue"
    pack.write(src, ["周回", "周回"], dest)
    raw = bytearray(dest.read_bytes())
    at = raw.find(b"procedures/") + 40
    raw[at:at + 20] = b"\xff" * 20                  # 圧縮した中身を壊す
    dest.write_bytes(bytes(raw))
    with pytest.raises(unpack.PackError, match="壊れています"):
        unpack.read(dest)


def test_an_upper_case_extension_is_read(tmp_path):
    """単体のファイルは、拡張子の大文字と小文字を区別しないこと。

    Windows のエクスプローラーは拡張子を隠すので、「手順.JSON」も利用者には
    手順の JSON ファイルに見える。名前は拡張子を除いた部分のまま。
    """
    path = tmp_path / "周回.JSON"
    path.write_text(json.dumps(proc([])), encoding="utf-8")
    assert list(unpack.read(path).procedures) == ["周回"]
    path = tmp_path / "表.CSV"
    path.write_text("F,A\n1,1\n", encoding="utf-8")
    assert list(unpack.read(path).frametables) == ["表"]


def test_a_procedure_nested_too_deep_is_left_out(tmp_path):
    """入れ子が深すぎる手順は、読めない件として外し、ほかはインポートできること。

    インポートは参照を書き換えるために手順をコピーする。深すぎると、そのコピーが途中で
    異常終了し、「予期しないエラー」になる。
    """
    body = [{"type": "wait", "frames": 1}]
    for i in range(80):
        body = [{"type": "branch", "id": f"br-{i}", "otherwise": [],
                 "arms": [{"name": "a", "body": body}]}]
    p = bad_pack(tmp_path, {
        "pack.json": manifest(("procedure", "深い"), ("procedure", "普通")),
        "procedures/深い.json": json.dumps(proc(body)),
        "procedures/普通.json": json.dumps(proc([]))})
    pf = unpack.read(p)
    assert pf.unreadable == [("procedure", "深い", NEST_TEXT)]
    here = make(tmp_path, sub="here")
    got = unpack.apply(here, pf, unpack.plan(here, pf))
    assert got["wrote"] == [("procedure", "普通")]


def test_the_default_new_name_fits_the_name_limit(tmp_path):
    """既定の別の名前が、名前の長さの上限に収まること。

    番号を追加して上限を超えると、ダイアログを開いた直後から、利用者が何も入力して
    いない行が「長すぎます」で拒否され、インポートを押せない。
    """
    long = "表" * (NAME_MAX - 2)
    for taken, want in (({long}, " (2)"), ({long, long[:-2] + " (2)"}, " (3)")):
        got = unpack.free_name({name_key(n) for n in taken}, long)
        assert len(got) == NAME_MAX and got.endswith(want), got
        assert validate_name(got) == got


def test_the_default_new_name_avoids_a_left_out_item(tmp_path):
    """既定の別の名前が、読めなかった件の名前を避けること(check は拒否する)。"""
    p = bad_pack(tmp_path, {
        "pack.json": manifest(("frametable", "表"), ("frametable", "表 (2)")),
        "frametables/表.csv": "F,A\n1,1\n",
        "frametables/表 (2).csv": "F,知らない列\n1,1\n"})
    pf = unpack.read(p)
    assert [n for _k, n, _w in pf.unreadable] == ["表 (2)"]
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    items = unpack.plan(here, pf)
    assert items[0].as_name == "表 (3)"
    assert unpack.check(here, pf, items)[1] == []
