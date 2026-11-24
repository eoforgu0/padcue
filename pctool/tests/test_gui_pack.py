"""padcue ファイルのエクスポートとインポートの API(gui.py)。

画面は、エクスポートはファイルとしてダウンロードさせ、インポートは中身を確認してから
承認する。承認するまで 1 件もインポートしない(file-format.md §10-5)ので、
確認の API とインポートの API を分けてある。
"""
import base64
import json
import socket
import urllib.parse
import urllib.request

import pytest

from padcue import gui, unpack
from tests.helpers import gui_server, pack_project, post, procedure_doc


@pytest.fixture
def pack_server(tmp_path):
    """手順「親」(フレーム表「表」を使う)を持つプロジェクトと、そのサーバーの URL。"""
    proj = pack_project(
        tmp_path,
        procs={"親": procedure_doc([{"type": "frametable", "ref": "表"}])},
        frametables={"表": "F,A\n1,1\n"})
    with gui_server(proj) as base:
        yield proj, base


def export(base, kind="procedure", name="親"):
    url = (f"{base}/api/pack/export?kind={urllib.parse.quote(kind)}"
           f"&name={urllib.parse.quote(name)}")
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read(), r.headers.get("Content-Disposition", "")


def test_exporting_hands_back_a_file_named_in_japanese(pack_server):
    """出すときはファイルとしてダウンロードさせること。名前は日本語のまま渡す。"""
    _proj, base = pack_server
    body, disp = export(base)
    assert body[:2] == b"PK", body[:8]
    assert "filename*=UTF-8''" in disp
    assert urllib.parse.quote("親.padcue", safe="") in disp


def test_exporting_nothing_is_refused(pack_server):
    _proj, base = pack_server
    with urllib.request.urlopen(f"{base}/api/pack/export", timeout=10) as r:
        got = json.loads(r.read())
    assert "選択してください" in got["error"]


def test_inspecting_does_not_import_anything(pack_server):
    """確認の API は 1 件も書かないこと(file-format.md §10-5)。

    承認しなければ 1 件もインポートしない。確認した時点で書いてしまうと、
    その約束が成立しない。
    """
    proj, base = pack_server
    blob, _ = export(base)
    here = pack_project(proj.root.parent, sub="here")
    gui._Handler.project = here
    got = post(base, "/api/pack/inspect",
               {"id": "x", "data": base64.b64encode(blob).decode()})
    assert not got["error"], got
    assert {i["name"] for i in got["items"]} == {"親", "表"}
    assert here.procedure_names() == [], "確認しただけで書いている"


def test_importing_writes_what_was_inspected(pack_server):
    """確認した物がそのまま、インポート先に入ること(確認で書かないことは上のテスト)。"""
    proj, base = pack_server
    blob, _ = export(base)
    here = pack_project(proj.root.parent, sub="here2")
    gui._Handler.project = here
    got = post(base, "/api/pack/inspect",
               {"id": "y", "data": base64.b64encode(blob).decode()})
    r = post(base, "/api/pack/import", {"id": "y", "items": got["items"]})
    assert not r["error"], r
    assert here.procedure_names() == ["親"]
    assert here.frametable_names() == ["表"]


def test_importing_without_inspecting_is_refused(pack_server):
    """確認していないものはインポートしないこと。"""
    _proj, base = pack_server
    got = post(base, "/api/pack/import", {"id": "みしらぬ", "items": []})
    assert "選択し直してください" in got["error"]


def _inspect_into(base, proj, blob, pid, **here):
    """here の中身でインポート先を作り直してから、blob を確認する。"""
    gui._Handler.project = pack_project(proj.root.parent, sub=f"here-{pid}", **here)
    got = post(base, "/api/pack/inspect",
               {"id": pid, "data": base64.b64encode(blob).decode()})
    assert not got["error"], got
    return gui._Handler.project, got


def test_the_choices_are_checked_before_importing(pack_server):
    """選んだ扱いを、インポートする前に確認できること(file-format.md §10-4)。

    別の名前の欄に元からある名前を入れたら、その欄のすぐ下に理由を出し、
    「インポート」を押せなくする。そのための理由を、件ごとに返す。
    """
    proj, base = pack_server
    blob, _ = export(base)
    _here, got = _inspect_into(base, proj, blob, "c", frametables={"表": "F,B\n1,1\n"})
    items = got["items"]
    table = next(i for i in items if i["kind"] == "frametable")
    table["as_name"] = "表"
    r = post(base, "/api/pack/check", {"id": "c", "items": items})
    assert r == {"problems": [["frametable", "表", "「表」は既にあります"]],
                 "lost_starts": [], "error": ""}, r


def test_a_wrong_choice_imports_nothing(pack_server):
    """1 件でも合わない扱いがあれば、何もインポートせずに理由を返すこと。"""
    proj, base = pack_server
    blob, _ = export(base)
    here, got = _inspect_into(base, proj, blob, "w", frametables={"表": "F,B\n1,1\n"})
    items = got["items"]
    next(i for i in items if i["kind"] == "frametable")["as_name"] = ""
    r = post(base, "/api/pack/import", {"id": "w", "items": items})
    assert r["problems"] == [["frametable", "表", "名前が空です"]], r
    assert here.procedure_names() == [], "合わない扱いがあるのに書いている"
    # 理由を見て修正すれば、同じ確認した結果のままインポートできる
    next(i for i in items if i["kind"] == "frametable")["as_name"] = "表 (2)"
    r = post(base, "/api/pack/import", {"id": "w", "items": items})
    assert not r["error"] and not r.get("problems"), r
    assert here.procedure_names() == ["親"]


def test_what_uses_each_item_comes_with_the_inspection(pack_server):
    """1 件ごとに、padcue ファイルの中で使用している手順と、元からある同じ名前の
    物を返すこと(file-format.md §10-5)。

    「インポートしない」を選んだときに参照がどれを指すかを、画面がその場で
    示すのに使う。
    """
    proj, base = pack_server
    blob, _ = export(base)
    _here, got = _inspect_into(base, proj, blob, "u", frametables={"表": "F,B\n1,1\n"})
    table = next(i for i in got["items"] if i["kind"] == "frametable")
    assert table["pack_users"] == ["親"] and table["here_name"] == "表"
    assert got["unreadable"] == []


def test_cancelling_lets_go_of_the_pack(pack_server):
    """キャンセルしたら、読んだ中身をサーバーが持ち続けないこと。

    中身は画像を含むので、確認するたびに残すと、サーバーが終わるまで溜まる。
    """
    proj, base = pack_server
    blob, _ = export(base)
    _inspect_into(base, proj, blob, "x")
    assert "x" in gui._PACKS
    assert post(base, "/api/pack/discard", {"id": "x"}) == {"error": ""}
    assert "x" not in gui._PACKS
    got = post(base, "/api/pack/import", {"id": "x", "items": []})
    assert "選択し直してください" in got["error"]


def test_a_pack_that_cannot_be_read_says_why(pack_server):
    """読めないものは、何なら受け取れるのかを付けて拒否すること。"""
    _proj, base = pack_server
    got = post(base, "/api/pack/inspect",
               {"id": "z", "name": "なにか.bin",
                "data": base64.b64encode(b"not a zip").decode()})
    # 理由は「「<名前>」をインポートできません。<理由>」の形で書き、受け取れる物を
    # 付ける。名前がないと、次に選んだファイルが読めなかったように見える
    assert got["error"].startswith("「なにか.bin」をインポートできません。"), got
    assert "インポートできるのは padcue ファイル" in got["error"], got


def test_a_bare_file_is_read_by_its_own_name(pack_server):
    """単体のファイルは、受け取ったときの名前で読むこと(file-format.md §10-1)。

    名前がそのまま中身の名前なので、捨てると種類も名前も分からなくなる。
    フレーム表は依存を持たないので、必ず単体の CSV で出る。
    """
    _proj, base = pack_server
    with urllib.request.urlopen(
            f"{base}/api/pack/export?kind=frametable&name="
            f"{urllib.parse.quote('表')}", timeout=10) as r:
        blob = r.read()
    assert blob.startswith(b"F,A"), blob[:8]
    got = post(base, "/api/pack/inspect",
               {"id": "s", "name": "表.csv",
                "data": base64.b64encode(blob).decode()})
    assert not got["error"], got
    got_items = [(i["kind"], i["name"]) for i in got["items"]]
    assert got_items == [("frametable", "表")]


def _raw_post(base, path, obj) -> bytes:
    """要求を 1 つ送り、接続が閉じるまでに受け取ったすべてを返す。"""
    u = urllib.parse.urlparse(base)
    body = json.dumps(obj).encode()
    with socket.create_connection((u.hostname, u.port), timeout=10) as s:
        s.sendall(f"POST {path} HTTP/1.0\r\nHost: {u.netloc}\r\n"
                  f"Content-Type: application/json\r\n"
                  f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        got = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return got
            got += chunk


def test_a_body_over_the_limit_is_refused_without_reading_it(pack_server):
    """宣言した本文の長さが上限を超える要求は、読まずに拒否すること。

    読んでから拒否すると、宣言した長さのぶんのメモリを確保してしまう。本文は送らずに、
    長さだけを宣言して確認する。
    """
    u = urllib.parse.urlparse(pack_server[1])
    with socket.create_connection((u.hostname, u.port), timeout=10) as s:
        s.sendall(f"POST /api/pack/inspect HTTP/1.1\r\nHost: {u.netloc}\r\n"
                  f"Content-Type: application/json\r\n"
                  f"Content-Length: {gui.MAX_REQUEST_BYTES + 1}\r\n\r\n".encode())
        raw = b""
        while chunk := s.recv(65536):      # 拒否した後、サーバーは接続を閉じる
            raw += chunk
        got = raw.decode("utf-8", "replace")
    assert got.startswith("HTTP/1.0 413") or got.startswith("HTTP/1.1 413"), got
    assert "大きすぎます" in got, got


def test_a_body_that_is_not_utf8_is_a_bad_request(pack_server):
    """本文が UTF-8 でない要求は、大きすぎる要求ではなく不正な要求として拒否する
    こと。
    """
    u = urllib.parse.urlparse(pack_server[1])
    body = b'{"name": "\xff"}'
    with socket.create_connection((u.hostname, u.port), timeout=10) as s:
        s.sendall(f"POST /api/pack/inspect HTTP/1.1\r\nHost: {u.netloc}\r\n"
                  f"Content-Type: application/json\r\nConnection: close\r\n"
                  f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        raw = b""
        while chunk := s.recv(65536):
            raw += chunk
        got = raw.decode("utf-8", "replace")
    assert got.startswith("HTTP/1.0 400") or got.startswith("HTTP/1.1 400"), got
    assert "要求の形式が正しくありません" in got, got


@pytest.mark.parametrize("path, obj", [
    ("/api/pack/inspect", {"data": "!!", "name": "x.padcue"}),
    ("/api/pack/import", {"id": "ない"}),
])
def test_the_pack_requests_answer_once(pack_server, path, obj):
    """インポートの要求は、応答を 1 つだけ返すこと。

    応答を書いた後に続きの経路へ流れると、「API が見つかりません」の 2 つ目の応答が
    同じ接続に書かれる。今のブラウザーは最初の応答だけを読むが、接続を使い
    回すと、次の要求がその 2 つ目を受け取る。
    """
    _proj, base = pack_server
    got = _raw_post(base, path, obj)
    assert got.count(b"HTTP/1.") == 1, got
    assert "API が見つかりません" not in got.decode("utf-8", "replace"), got


def _missing_of(base, name):
    url = f"{base}/api/pack/export?kind=procedure&name={urllib.parse.quote(name)}"
    with urllib.request.urlopen(url, timeout=10) as r:
        r.read()
        raw = r.headers.get("X-Padcue-Missing")
    assert raw is not None, "見つからなかったものを応答に載せていない"
    return json.loads(urllib.parse.unquote(raw))


def test_exporting_names_what_it_could_not_find(pack_server):
    """見つからず入れられなかったものを、応答で知らせること(file-format.md §10-2)。

    エクスポートはやめない。黙って外すと、受け取った人の画面で初めて「フレーム表が
    ありません」と分かり、出した側はどれが足りなかったのかを知らない。
    """
    proj, base = pack_server
    assert _missing_of(base, "親") == []
    (proj.root / "frametables" / "表.csv").unlink()
    assert _missing_of(base, "親") == [["frametable", "表"]]


def test_exporting_what_does_not_exist_is_refused(pack_server):
    """選んだもの自体がなければ、エクスポートせずに理由を返すこと。

    空の padcue ファイルをダウンロードさせて「エクスポートしました」と表示すると、
    受け取った側で初めて中身がないと分かる。
    """
    _proj, base = pack_server
    url = f"{base}/api/pack/export?kind=procedure&name={urllib.parse.quote('ない')}"
    with urllib.request.urlopen(url, timeout=10) as r:
        got = json.loads(r.read())
    assert got["error"] == "手順「ない」が見つかりません", got


def test_exporting_names_what_it_could_not_read(pack_server):
    """読めない手順は、見つからない物と分けて知らせること(file-format.md §10-2)。

    一覧には「エラー」の行で出ているので、「見つからない」と表示すると矛盾する。
    選んだもの自体が読めなければ、エクスポートせずに「手順「子」を読み込めません」と返す。
    """
    proj, base = pack_server
    (proj.root / "procedures" / "親.json").write_text(json.dumps(procedure_doc([
        {"type": "frametable", "ref": "表"},
        {"type": "call", "id": "cl-1", "ref": "子"}])), encoding="utf-8")
    (proj.root / "procedures" / "子.json").write_text("{oops", encoding="utf-8")
    url = f"{base}/api/pack/export?kind=procedure&name={urllib.parse.quote('親')}"
    with urllib.request.urlopen(url, timeout=10) as r:
        r.read()
        unreadable = json.loads(urllib.parse.unquote(
            r.headers.get("X-Padcue-Unreadable")))
        missing = json.loads(urllib.parse.unquote(r.headers.get("X-Padcue-Missing")))
    assert unreadable == [["procedure", "子"]] and missing == []
    url = f"{base}/api/pack/export?kind=procedure&name={urllib.parse.quote('子')}"
    with urllib.request.urlopen(url, timeout=10) as r:
        assert json.loads(r.read())["error"] == "手順「子」を読み込めません"


def test_only_the_last_pack_is_kept(pack_server):
    """持っておく読んだ中身は、最後に確認した 1 つだけであること。

    確認するダイアログを開いたままタブを閉じると、捨てる要求が送られない。中身は
    画像を含むので、確認するたびに残すとサーバーのメモリに溜まる。
    """
    proj, base = pack_server
    blob, _ = export(base)
    _inspect_into(base, proj, blob, "前")
    _inspect_into(base, proj, blob, "後")
    assert list(gui._PACKS) == ["後"]


def test_an_import_that_stops_halfway_says_what_got_in(pack_server, monkeypatch):
    """インポートの書き込みが途中で失敗したら、入った物と次にできることを示すこと。

    書いた後は、確認したときの状態がもう今と合わないので、ファイルを選択し
    直させる(押し直すと、身に覚えのない「変更されました」で拒否される)。
    """
    proj, base = pack_server
    blob, _ = export(base)
    here, got = _inspect_into(base, proj, blob, "x")
    real = unpack.write_file

    def refuse(dst, data):
        raise PermissionError(13, "Permission denied", str(dst))
    monkeypatch.setattr(unpack, "write_file", refuse)
    r = post(base, "/api/pack/import", {"id": "x", "items": got["items"]})
    assert "フレーム表「表」をインポートできません" in r["error"], r
    assert "x" in gui._PACKS, "何も書いていないのに選択し直させる"
    calls = []

    def second(dst, data):
        calls.append(dst)
        if dst.suffix == ".json":
            raise PermissionError(13, "Permission denied", str(dst))
        real(dst, data)
    monkeypatch.setattr(unpack, "write_file", second)
    monkeypatch.setattr(here, "save_procedure_doc",
                        lambda name, doc: second(here.procedure_path(name), ""))
    r = post(base, "/api/pack/import", {"id": "x", "items": got["items"]})
    assert "フレーム表「表」はインポート済みです" in r["error"], r
    assert "ファイルを選択し直して" in r["error"], r
    assert "x" not in gui._PACKS
