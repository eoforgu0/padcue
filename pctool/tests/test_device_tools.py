"""画面の API と、同じ操作をするコマンド。

通常の想定内の操作は画面でできるようにし、同じことをコマンドでもできるようにする
(principles.md P27)。装置の行のメニュー(状態、更新、設定、USB の再接続)、
選択、周回の終え方、グループの開始、手順の取り込み、時間軸の図の下書きを見る。
"""
import base64
import json
import struct
import unicodedata
import urllib.request

import pytest

from padcue import cli, firmware, gui, proto
from padcue.mockdevice import MockDevice
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from padcue.statustext import connection_summary, status_lines, status_rows
from tests.helpers import get, lane, post, wait_until

# 無線の更新に使う形のイメージ(アプリの頭と esp_app_desc_t)
_DESC = struct.pack("<I", 0xABCD5432) + b"\0" * 12 + b"0.1.0".ljust(32, b"\0")
APP = bytes([0xE9]) + b"\0" * 31 + _DESC + b"\0" * 8192


@pytest.fixture
def env(start_gui):
    proj, (dev,), base = start_gui("aaaa00000001")
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    return proj, dev, base


def test_the_status_on_the_screen_has_the_same_items_as_the_command(env):
    """「状態を表示」は、`padcue status` と同じ項目を同じ順で出すこと。"""
    _proj, _dev, base = env
    r = get(base, "/api/device_status?dev=1P")
    assert r["summary"] == "Switch に接続しています"
    labels = [row[0] for row in r["rows"]]
    assert labels[:7] == ["padcue", "ファームウェア", "USB の問い合わせの間隔", "状態",
                          "USB", "到達段階", "ジャイロ"]
    # 問い合わせの間隔は、設定の欄と同じ名前で呼び、値は数と単位だけにする
    assert r["rows"][2] == ["USB の問い合わせの間隔", "1ms"], r["rows"]
    lines = status_lines(lane(base))
    # コマンドの出力も同じ文で、コロンの位置(端末のセルの数)はどの行も揃う
    assert "USB の問い合わせの間隔: 1ms" in lines, lines

    def cells(s):
        return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)
    assert len({cells(ln[:ln.index(":")]) for ln in lines
                if not ln.startswith(" ")}) == 1, lines
    assert [ln.split(":")[0].strip() for ln in lines if not ln.startswith(" ")] \
        == [x for x in labels if x], lines


def test_the_status_of_an_unknown_device_says_why(env):
    _proj, _dev, base = env
    assert "登録されていません" in get(base, "/api/device_status?dev=9P")["error"]


@pytest.mark.parametrize("fields, want", [
    ({"usb_mounted": False}, "USB で接続されていません"),
    ({"usb_mounted": True, "usb_suspended": True}, "スリープ中"),
    ({"usb_mounted": True, "pair_step": 1}, "登録が完了していません"),
    ({"usb_mounted": True, "breadcrumb": 0x400}, "初期化が完了していません"),
    ({"usb_mounted": True, "breadcrumb": 0x7fb}, "接続しています"),
])
def test_the_connection_summary_names_the_state(fields, want):
    """接続の概要が、場面ごとの状態の語を示すこと。"""
    assert want in connection_summary(fields)


def test_a_requested_usb_reconnect_is_not_logged_as_a_fault(env):
    """画面から指示した再接続の切断には印を付けること(ログを赤くしない)。"""
    proj, dev, base = env
    assert post(base, "/api/device_reconnect_usb", {"dev": "1P"}).get("ok")

    def umounts():
        return [e for e in proj.logs.read(100) if e.get("kind") == "USB_UMOUNT"]
    assert wait_until(umounts, timeout=15), proj.logs.read(100)
    assert umounts()[-1].get("requested") is True, umounts()
    dev.usb_mounted = False     # 指示していない切断には付けない
    dev._log("USB_UMOUNT")
    assert wait_until(lambda: len(umounts()) == 2, timeout=15), umounts()
    assert not umounts()[-1].get("requested"), umounts()
    dev.usb_mounted = True


def test_a_refused_usb_reconnect_does_not_mark_the_next_unplug(env):
    """USB の再接続を装置が拒否したら、その後の本物の切断に印を付けないこと。

    印を付けると、指示していない切断がログで異常の色にならない。
    """
    proj, dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "サンプル", "laps": 100000}]}).get("ok")
    try:
        assert wait_until(lambda: lane(base).get("running")), lane(base)
        r = post(base, "/api/device_reconnect_usb", {"dev": "1P"})
        assert r.get("error") == "USB の再接続は待機中にのみ行えます", r
        # 切断の後に書かれた行を全部読む(早送りの実行は周の行を周期ごとに何百行も
        # 書くので、新しい方から決まった行数だけ読むと、切断の行が押し出される)
        since = proj.logs.mark()
        dev.usb_mounted = False     # 本物の切断
        dev._log("USB_UMOUNT")

        def umounts():
            got = proj.logs.read_page(1, count=1, mark=since).entries
            return [e for e in got if e.get("kind") == "USB_UMOUNT"]
        assert wait_until(umounts, timeout=15), proj.logs.read(100)
        assert not umounts()[-1].get("requested"), umounts()
    finally:
        dev.usb_mounted = True
        post(base, "/api/stop", {"mode": "immediate"})


def test_a_firmware_update_runs_and_the_device_comes_back(env):
    """画面からファイルで更新すると、進み具合が状態に出て、別のパーティションで戻ること。"""
    _proj, _dev, base = env
    before = lane(base)["partition"]
    data = base64.b64encode(APP).decode()
    assert post(base, "/api/firmware/ota/check", {"data": data})["version"] == "0.1.0"
    r = post(base, "/api/device_ota", {"dev": "1P", "data": data})
    assert r.get("ok") and lane(base)["ota"]["id"] == r["job"]
    assert wait_until(lambda: lane(base).get("ota", {}).get("state") == "sent"), \
        lane(base)
    assert wait_until(lambda: lane(base).get("partition") not in (None, before)
                      and not lane(base).get("error"), timeout=20), lane(base)
    assert lane(base)["ota"]["rebooted"], lane(base)["ota"]


def test_a_reconnect_while_sending_is_not_taken_for_the_restart(env, monkeypatch):
    """送っている間に収集がつなぎ直しても、送り終えた後の再起動とは数えないこと。

    収集は、応答の時間切れや改名でもつなぎ直す。更新を始めた時点の回数と比べると、
    装置が再起動する前に「再起動した」とみなし、前に更新前へ戻った装置では、更新の
    ダイアログが再起動を待たずに「更新前のファームウェアに戻りました」で終わる。
    """
    _proj, dev, base = env
    dev.ota_reboot_s = 2.0      # 送り終えてから再起動するまでの間を見る
    link = gui._Handler._pool().get("1P")

    class Reconnected(gui.DeviceClient):
        def ota(self, image, chunk=4096, progress=None):
            link.connects += 1   # 送っている間に収集がつなぎ直した
            return super().ota(image, chunk=chunk, progress=progress)

    monkeypatch.setattr(gui, "DeviceClient", Reconnected)
    data = base64.b64encode(APP).decode()
    assert post(base, "/api/device_ota", {"dev": "1P", "data": data}).get("ok")
    got = wait_until(lambda: (lambda o: o if o.get("state") == "sent" else None)(
        lane(base).get("ota") or {}))
    assert got and not got["rebooted"], got
    assert wait_until(lambda: lane(base)["ota"]["rebooted"], timeout=20), \
        lane(base)["ota"]


def test_an_update_that_fails_unexpectedly_ends_as_failed(env, monkeypatch):
    """更新が想定外の例外で終わっても、送っている途中のまま残さないこと。

    残ると、ダイアログを閉じられず、次の更新も「ファームウェアを更新している途中です」で
    拒否され続ける(操作画面のサーバーを起動し直すまで戻らない)。壊れた応答のパケットは
    ProtoError になり、通信の失敗(OSError)でも装置の拒否でもない。
    """
    _proj, _dev, base = env

    class Broken(gui.DeviceClient):
        def ota(self, image, chunk=4096, progress=None):
            raise proto.ProtoError("CRC 不一致")

    monkeypatch.setattr(gui, "DeviceClient", Broken)
    data = base64.b64encode(APP).decode()
    assert post(base, "/api/device_ota", {"dev": "1P", "data": data}).get("ok")
    got = wait_until(lambda: (lambda o: o if o.get("state") != "sending" else None)(
        lane(base).get("ota") or {}), timeout=10)
    assert got and got.get("state") == "failed", lane(base).get("ota")
    assert "ProtoError" in got.get("error", ""), got
    assert post(base, "/api/device_ota", {"dev": "1P", "data": data}).get("ok"), \
        "失敗した更新が、送っている途中として次の更新を拒否させる"


def test_an_update_of_an_unknown_device_says_why(env):
    _proj, _dev, base = env
    data = base64.b64encode(APP).decode()
    r = post(base, "/api/device_ota", {"dev": "9P", "data": data})
    assert "登録されていません" in r["error"], r


def test_a_cable_image_is_not_used_for_a_wireless_update():
    """ケーブルで書き込むイメージを無線の更新に渡すと、理由を示して拒否すること。"""
    entry = struct.pack("<HBBI", firmware.PARTITION_MAGIC, 0, 0, 0x10000)
    image = (bytes([0xE9]) + b"\0" * (firmware.PARTITION_TABLE - 1)
             + entry.ljust(32, b"\0"))
    with pytest.raises(firmware.FirmwareError, match=r"-ota\.bin"):
        firmware.ota_version(image)
    with pytest.raises(firmware.FirmwareError):
        firmware.ota_version(b"not a firmware")
    with pytest.raises(firmware.FirmwareError, match="大きい"):
        firmware.ota_version(b"\xe9" * (firmware.OTA_MAX + 1))
    assert firmware.ota_version(APP) == "0.1.0"
    # 画面で選んだファイルは、名前で先に示し、どのファイルを選べばよいかを続ける
    with pytest.raises(firmware.FirmwareError) as e:
        firmware.ota_version(image, "cable.bin")
    assert str(e.value) == ("「cable.bin」はケーブルで書き込むファイルです。"
                            "名前が -ota.bin で終わるファイルを選択してください。")


def test_the_settings_are_saved_on_the_device(env):
    """設定は装置へ送り、フレーム周期は画面の値も変わること。

    USB の問い合わせの間隔は次の起動から使われる。それまでは保存した値も画面へ
    渡す(今の値だけを見せると、保存できなかったように見える)。
    """
    _proj, dev, base = env
    assert post(base, "/api/device_settings", {
        "dev": "1P", "frame_period_ns": 16683333, "usb_binterval": 8,
        "wifi_ssid": "example", "wifi_pass": "password1"}).get("ok")
    d = lane(base)
    assert d["frame_period_ns"] == 16683333
    assert dev.frame_period_ns == 16683333
    assert d["binterval"] == 1 and d["binterval_saved"] == 8, d
    # 保存した値は、問い合わせの間隔の行の続きの行に出る
    rows = status_rows(d)
    at = [label for label, _ in rows].index("USB の問い合わせの間隔")
    assert rows[at + 1] == ("", "次の起動から 8ms"), rows
    assert post(base, "/api/device_settings", {"dev": "1P",
                                               "usb_binterval": 1}).get("ok")
    assert "binterval_saved" not in lane(base), "今の値へ戻したのに残っている"


def test_a_save_cut_off_midway_says_what_was_saved(env, monkeypatch):
    """設定の保存が途中で失敗したら、保存できた項目とできなかった項目を示すこと。

    装置は項目ごとに、受け取った時点で保存する。Wi-Fi の SSID の後で接続が切れると、
    装置には新しい SSID と古いパスワードが残り、次の起動で Wi-Fi につながらない。
    「接続できません」とだけ表示すると、何も保存されていないと読まれる。保存できた項目の
    画面の値(保存した USB の問い合わせの間隔)も合わせる。
    """
    _proj, dev, base = env
    real = gui.DeviceClient.config

    def cut_before_the_password(self, key, value):
        if key == "wifi_pass":
            raise ConnectionResetError("接続が切れた")
        return real(self, key, value)
    monkeypatch.setattr(gui.DeviceClient, "config", cut_before_the_password)
    r = post(base, "/api/device_settings", {
        "dev": "1P", "usb_binterval": 8,
        "wifi_ssid": "example", "wifi_pass": "password1"})
    assert r.get("error") == (
        "Wi-Fi のパスワードを保存できません。"
        "127.0.0.1 との接続が切断されました。電源とネットワークを確認してください。"
        "USB の問い合わせの間隔、Wi-Fi の SSID は保存しました。"), r
    assert dev.wifi_config == {"wifi_ssid": "example"}
    assert lane(base).get("binterval_saved") == 8, lane(base)


@pytest.mark.parametrize("body, want", [
    ({"frame_period_ns": 1}, "フレーム周期"),
    ({"usb_binterval": 99}, "USB の問い合わせの間隔"),
    ({"wifi_ssid": "x" * 40}, "SSID"),
    ({"wifi_ssid": "ok", "wifi_pass": "short"}, "パスワード"),
])
def test_wrong_settings_are_refused_and_not_stored(env, body, want):
    """正しくない設定は拒否し、装置には何も保存しないこと。"""
    _proj, dev, base = env
    before = (dev.frame_period_ns, dev.saved_binterval, dict(dev.wifi_config))
    assert want in post(base, "/api/device_settings", {"dev": "1P", **body})["error"]
    assert (dev.frame_period_ns, dev.saved_binterval, dev.wifi_config) == before


def test_a_preview_draws_the_unsaved_content(env):
    """保存していない中身で図を描き、ファイルは変えないこと。"""
    proj, _dev, base = env
    doc = proj.load_procedure_doc("サンプル")
    saved = get(base, "/api/timeline?name=" + urllib.request.quote("サンプル")
                + "&choose=")["total_frames"]
    doc["body"].append({"type": "wait", "frames": 600})
    r = post(base, "/api/timeline/preview", {"name": "サンプル", "doc": doc})
    assert r["total_frames"] == saved + 600
    assert proj.load_procedure_doc("サンプル") != doc
    assert post(base, "/api/procedure/check", {"name": "サンプル", "doc": doc})[
        "frames"] == saved + 600


def test_a_preview_of_a_broken_doc_says_why(env):
    _proj, _dev, base = env
    r = post(base, "/api/timeline/preview", {"name": "サンプル", "doc": {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "frametable", "ref": "ないフレーム表"}]}})
    assert r.get("error") and r.get("error_at")


def test_the_scan_state_is_on_the_screen(env):
    """探索の失敗と最後に探した時刻を、画面へ渡すこと(デモは探さないので渡さない)。"""
    _proj, _dev, base = env
    assert "scan" not in get(base, "/api/state")

    class Scan:
        candidates = ()
        last_at = 12.0
        error = "探索に失敗"
    gui._Handler.scan = Scan()
    try:
        assert get(base, "/api/state")["scan"] == {"at": 12.0, "error": "探索に失敗"}
    finally:
        gui._Handler.scan = None


def test_select_works_from_the_command(env, capsys, monkeypatch):
    """選択待ちの選択肢を、コマンドからも選べること。"""
    proj, _dev, base = env
    monkeypatch.setattr(cli, "_gui_base", lambda _p, _a=None: base)
    proj.save_procedure_doc("選ぶ", {"kind": "procedure", "format": FORMAT_VERSION,
                                   "body": [
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "一", "what": {"kind": "manual"}},
            {"name": "二", "what": {"kind": "manual"}}]}]})
    root = ["--project", str(proj.root)]
    assert cli.main([*root, "select", "1"]) == 1
    assert "選択待ちではありません" in capsys.readouterr().err
    assert post(base, "/api/run", {"plan": [
        {"name": "選ぶ", "laps": 1, "dev": "1P"}]}).get("ok")
    assert wait_until(lambda: lane(base).get("awaiting")), lane(base)
    assert cli.main([*root, "select", "2"]) == 0
    assert wait_until(lambda: not lane(base).get("awaiting")), lane(base)


def test_lap_end_works_from_the_command(env, capsys, monkeypatch):
    """周回の終え方を、コマンドからも変えられること。実行中でなければ拒否すること。"""
    proj, _dev, base = env
    monkeypatch.setattr(cli, "_gui_base", lambda _p, _a=None: base)
    proj.save_procedure_doc("周回", {"kind": "procedure", "format": FORMAT_VERSION,
                                   "body": [
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "進む", "what": {"kind": "manual"}}]}]}]})
    root = ["--project", str(proj.root)]
    assert cli.main([*root, "lap-end", "end"]) == 1
    assert "実行中ではありません" in capsys.readouterr().err
    assert post(base, "/api/run", {"plan": [
        {"name": "周回", "laps": 5, "dev": "1P"}]}).get("ok")
    assert wait_until(lambda: lane(base).get("awaiting")), lane(base)
    # 手順に「最後の回」がないので、選べるのは end だけである
    assert wait_until(lambda: (get(base, "/api/state")["groups"][-1].get("lap_end")
                               or {}).get("options"))
    assert cli.main([*root, "lap-end", "next"]) == 1
    assert "選択できる終え方: end" in capsys.readouterr().err
    assert cli.main([*root, "lap-end", "end"]) == 0
    assert "変更しました" in capsys.readouterr().out
    assert wait_until(lambda: lane(base).get("laps_total") == 1
                      and not lane(base).get("lap_end_pending")), lane(base)
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")


def test_status_and_watch_say_the_choices(env, capsys, monkeypatch):
    """選択待ちなら、`select` に渡す番号と選択肢を status と run -w が表示すること。"""
    proj, _dev, base = env
    monkeypatch.setattr(cli, "_gui_base", lambda _p, _a=None: base)
    proj.save_procedure_doc("選ぶ", {"kind": "procedure", "format": FORMAT_VERSION,
                                   "body": [
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "一", "what": {"kind": "manual"}},
            {"name": "二", "what": {"kind": "manual"}}]}]})
    root = ["--project", str(proj.root)]
    assert post(base, "/api/run", {"plan": [
        {"name": "選ぶ", "laps": 1, "dev": "1P"}]}).get("ok")
    assert wait_until(lambda: lane(base).get("awaiting")), lane(base)
    assert cli.main([*root, "status"]) == 0
    assert "1 一 / 2 二" in capsys.readouterr().out
    states = iter([lane(base), {"state": "IDLE"}])
    assert cli._watch_progress(lambda: next(states), lambda: ("", "")) == 0
    assert "選択待ち: 1 一 / 2 二" in capsys.readouterr().err


def test_logs_show_what_was_kept_when_the_device_has_nothing_new(tmp_path, capsys):
    """操作画面が受け取った記録を、サーバーなしの logs でも出すこと。

    装置の中は受け取り済みの分が消えるので、装置だけを読むと「ログはありません」に
    なり、記録が残っていないように見える。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev:
        cfg = proj.load_config()
        cfg["devices"] = [{"id": dev.device_id, "name": "1P",
                           "host": "127.0.0.1", "port": dev.port}]
        proj.save_config(cfg)
        root = ["--project", str(tmp_path)]
        assert cli.main([*root, "logs"]) == 0       # 装置の中を受け取る
        first = capsys.readouterr().out
        assert "BOOT" in first, first
        assert cli.main([*root, "logs"]) == 0
        again = capsys.readouterr().out
        assert "BOOT" in again and "ログはありません" not in again, again


def test_list_shows_the_names_of_what_was_sent(tmp_path, capsys):
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev:
        cfg = proj.load_config()
        cfg["devices"] = [{"id": dev.device_id, "name": "1P",
                           "host": "127.0.0.1", "port": dev.port}]
        proj.save_config(cfg)
        root = ["--project", str(tmp_path)]
        assert cli.main([*root, "push", "サンプル"]) == 0
        capsys.readouterr()
        assert cli.main([*root, "list"]) == 0
        assert "サンプル" in capsys.readouterr().out


def test_a_label_before_a_wait_is_drawn_before_the_cut(env):
    """同じ時刻の待ちより前に置いたラベルは、切れ目の手前に描くこと。"""
    proj, _dev, base = env
    arm = {"name": "一", "what": {"kind": "manual"}}
    proj.save_procedure_doc("順", {"kind": "procedure", "format": FORMAT_VERSION,
                                  "body": [
        {"type": "label", "id": "lb-1", "name": "前"},
        {"type": "branch", "id": "br-1", "arms": [arm]},
        {"type": "label", "id": "lb-2", "name": "後"},
        {"type": "wait", "frames": 10}]})
    tl = get(base, "/api/timeline?name=" + urllib.request.quote("順") + "&choose=")
    got = {lb["text"]: lb.get("before_cut") for lb in tl["labels"]}
    assert got == {"前": True, "後": False}, tl["labels"]


def test_lap_end_needs_the_server(tmp_path, capsys):
    proj = Project(tmp_path)
    proj.init_sample()
    assert cli.main(["--project", str(tmp_path), "lap-end", "end"]) == 1
    assert "gui --no-open" in capsys.readouterr().err
    assert cli.main(["--project", str(tmp_path), "run", "サンプル",
                     "--group", "1P", "2P"]) == 1
    assert "gui --no-open" in capsys.readouterr().err


def test_a_group_starts_from_the_command(tmp_path, monkeypatch, start_gui):
    _proj, _devices, base = start_gui("aaaa00000001", "bbbb00000002", speed=20.0)
    assert wait_until(lambda: lane(base, "2P").get("visible")), lane(base, "2P")
    monkeypatch.setattr(cli, "_gui_base", lambda _p, _a=None: base)
    assert cli.main(["--project", str(tmp_path), "run", "サンプル",
                     "--group", "1P", "2P"]) == 0

    def in_one_group():
        group = lane(base).get("group")
        return group and group == lane(base, "2P").get("group")
    assert wait_until(in_one_group), get(base, "/api/state")
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")


def test_import_says_what_it_did_not_take(tmp_path, capsys):
    """見つからないファイルと、同じ内容で入れなかったものを示すこと。"""
    proj = Project(tmp_path)
    proj.init_sample()
    root = ["--project", str(tmp_path)]
    assert cli.main([*root, "import", str(tmp_path / "ない.json")]) == 1
    assert "」が見つかりません" in capsys.readouterr().err
    (tmp_path / "keep").mkdir()
    same = tmp_path / "keep" / "サンプル.json"
    same.write_text(json.dumps(proj.load_procedure_doc("サンプル"), ensure_ascii=False),
                    encoding="utf-8")
    assert cli.main([*root, "import", str(same)]) == 0
    assert "同じ内容のため" in capsys.readouterr().out


def test_import_from_the_command_keeps_what_is_there(tmp_path, capsys):
    """同じ名前で中身の違う手順は別の名前で入れ、元の手順を置き換えないこと。"""
    proj = Project(tmp_path)
    proj.init_sample()
    other = {"kind": "procedure", "format": FORMAT_VERSION,
             "body": [{"type": "wait", "frames": 7}]}
    keep = (tmp_path / "keep")
    keep.mkdir()
    named = keep / "サンプル.json"
    named.write_text(json.dumps(other), encoding="utf-8")
    before = proj.load_procedure_doc("サンプル")
    assert cli.main(["--project", str(tmp_path), "import", str(named)]) == 0
    assert proj.load_procedure_doc("サンプル") == before
    assert "インポートしました" in capsys.readouterr().out
    assert proj.load_procedure_doc("サンプル (2)")["body"] == other["body"]


def test_import_shows_the_commands_before_taking_them(tmp_path, capsys):
    """コマンドを含むファイルは、コマンドを見せて、--yes があるときだけ入れること。"""
    proj = Project(tmp_path)
    proj.init_sample()
    src = tmp_path / "コマンド.json"
    src.write_text(json.dumps({"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "script", "id": "sc-1", "cmd": "echo hello"}]}), encoding="utf-8")
    assert cli.main(["--project", str(tmp_path), "import", str(src)]) == 1
    err = capsys.readouterr().err
    assert "echo hello" in err and "--yes" in err
    assert not (tmp_path / "procedures" / "コマンド.json").exists()
    assert cli.main(["--project", str(tmp_path), "import", str(src), "--yes"]) == 0
    assert proj.load_procedure_doc("コマンド")["body"][0]["cmd"] == "echo hello"


def test_import_says_what_it_could_not_read(tmp_path, capsys):
    """padcue ファイルの中の読めなかった件と理由を示し、終了コード 1 で知らせること。

    残りはインポートする(file-format.md §10-1)。読めなかった件を使う手順の参照が
    どれを指すかも、画面と同じ文で示す(file-format.md §10-5)。
    """
    import zipfile

    from padcue import pack
    proj = Project(tmp_path)
    proj.init_sample()
    newer = {"kind": "procedure", "format": FORMAT_VERSION + 1, "body": []}
    caller = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 1}, {"type": "call", "id": "cl-1", "ref": "新しい"}]}
    src = tmp_path / "渡された.padcue"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(pack.MANIFEST, json.dumps(
            {"kind": "pack", "format": pack.PACK_FORMAT,
             "entries": [{"kind": "procedure", "name": "新しい"},
                         {"kind": "procedure", "name": "呼ぶ"}]}))
        zf.writestr("procedures/新しい.json", json.dumps(newer))
        zf.writestr("procedures/呼ぶ.json", json.dumps(caller))
    assert cli.main(["--project", str(tmp_path), "import", str(src)]) == 1
    out = capsys.readouterr()
    assert "手順「呼ぶ」をインポートしました" in out.out
    assert ("手順「新しい」はインポートできません。新しいバージョンの padcue で"
            "作成されています。padcue を更新してください。") in out.err, out.err
    assert "「呼ぶ」が使用する「新しい」が見つかりません" in out.err, out.err


def test_ota_from_the_command_checks_the_image_first(tmp_path, capsys):
    """コマンドの ota も、画面と同じ確認をして、送る前に理由を示すこと。

    リリースの 2 つのファイルを間違えて、ケーブルで書き込むイメージを渡すと、
    装置に全体を送ってしまう。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    dev = MockDevice(speed=2000.0)
    dev.start()
    try:
        proj.save_config({"devices": [{"id": dev.device_id, "name": "1P",
                                       "host": "127.0.0.1", "port": dev.port}]})
        entry = struct.pack("<HBBI", firmware.PARTITION_MAGIC, 0, 0, 0x10000)
        cable = (bytes([0xE9]) + b"\0" * (firmware.PARTITION_TABLE - 1)
                 + entry.ljust(32, b"\0"))
        for image, said in (
                (cable, "ケーブルで書き込むファイルです"),
                (b"not a firmware", "ファームウェアのファイルではありません")):
            path = tmp_path / "選んだ.bin"
            path.write_bytes(image)
            assert cli.main(["--project", str(tmp_path), "ota", str(path)]) == 1
            captured = capsys.readouterr()
            assert said in captured.err, captured.err
            assert "更新前" not in captured.out
    finally:
        dev.stop()


@pytest.fixture
def two(start_gui, monkeypatch):
    """2 台を登録した操作画面のサーバー。コマンドはこのサーバーを経由する。"""
    proj, _devices, base = start_gui("aaaa00000001", "bbbb00000002")
    assert wait_until(lambda: lane(base, "2P").get("visible")), lane(base, "2P")
    monkeypatch.setattr(cli, "_gui_base", lambda _p, _a=None: base)
    return proj, base


@pytest.mark.parametrize("argv, what", [
    (["select", "1"], "選択する装置が指定されていません(登録済み: 1P・2P)"),
    (["lap-end", "end"], "周回数を変える装置が指定されていません(登録済み: 1P・2P)"),
])
def test_two_devices_need_the_device_named(two, capsys, argv, what):
    """装置が 2 台以上なら、select と lap-end は --device を省略できないこと
    (coupling.md D4)。

    1 台目を自分で選んで送ると、2P だけが選択待ちのときに「選択待ちではありません」と
    答え、両方が待っていれば選んでいない 1P が進む。
    """
    proj, _base = two
    assert cli.main(["--project", str(proj.root), *argv]) == 1
    err = capsys.readouterr().err
    assert what in err, err
    assert f"指定するには: python -m padcue --device <名前> {' '.join(argv)}" in err


def test_two_devices_need_the_device_named_without_the_server(tmp_path, capsys):
    """サーバーがなくても(直結の select)、2 台以上なら --device を省略できないこと。"""
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_config({"devices": [
        {"id": "aaaa00000001", "name": "1P", "host": "192.0.2.1", "port": 5555},
        {"id": "bbbb00000002", "name": "2P", "host": "192.0.2.2", "port": 5555}]})
    assert cli.main(["--project", str(tmp_path), "select", "1"]) == 1
    assert "選択する装置が指定されていません" in capsys.readouterr().err
