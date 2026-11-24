"""ファームウェアの状態機械と操作権と起動のときのボタンの判定と記録のリングと手順の
保存(firmware/main の C)を、ホストで実行して確認する。

app_state.c、app_owner.c、app_button.c、app_wifi_creds.c、app_engine.c、app_log.c、
app_store.c と、終了処理の app_runend.h と起動の判定の app_boot.h を、
ロックと時刻とボタンの線とファイルの操作を差し替えて PC の gcc でビルドする
(firmware/main/host)。文書と実装の文字列を照合するテスト(test_firmware_docs.py)
では、振る舞いの不一致は見つからない。
模擬装置(mockdevice.py)は同じ規則を Python で別に書いたものなので、ファームウェアと
ずれても模擬装置のテストは通る。C のビルドと gcc がないときの扱いは conftest.py の
app_host_exe と gcc の fixture にある。
"""
from __future__ import annotations

import re
import subprocess

import pytest

from padcue import binfmt, proto
from tests.conftest import MAIN


def _enum(header: str, type_name: str, prefix: str) -> dict[str, int]:
    """C の enum の名前と値。値を書いていない行は、前の値に 1 を足した値である。"""
    src = (MAIN / header).read_text(encoding="utf-8")
    body = re.search(rf"typedef enum \{{(.*?)\}} {type_name};", src, re.S).group(1)
    out: dict[str, int] = {}
    n = 0
    for name, val in re.findall(rf"^\s*{prefix}(\w+)(?:\s*=\s*(\d+))?,", body, re.M):
        n = int(val) if val else n
        out[name] = n
        n += 1
    return out


STATE = _enum("app_state.h", "app_state_t", "APP_STATE_")
STATE_NAME = {v: k for k, v in STATE.items()}
LOG_NAME = {v: k for k, v in _enum("app_log.h", "app_log_kind_t", "APP_LOG_").items()}
ENGINE = _enum("app_engine.h", "app_engine_status_t", "APP_ENGINE_")
ENGINE_NAME = {v: k for k, v in ENGINE.items()}
STOP = _enum("app_engine.h", "app_stop_reason_t", "APP_STOP_")
STOP_NAME = {v: k for k, v in STOP.items()}
# 停止した後に押されたボタン。名前は STATUS の button_after_stop と同じ(app_ctrl.c)
PRESS = _enum("app_engine.h", "app_press_t", "APP_PRESS_")
PRESS_NAME = {v: "" if k == "NONE" else k.lower() for k, v in PRESS.items()}
PRESS_SHORT, PRESS_LONG = PRESS["SHORT"], PRESS["LONG"]

# ハーネスが終了処理に渡すエンジンの値(host/app_host.c の ENGINE_*)
FRAMES, LATE, INDEX, MARK = 120, 2, 7, 3

OWNER_TIMEOUT_US = 30 * 1_000_000


class Host:
    """ハーネスを 1 つ起動し、コマンドを 1 行ずつ送って答えを受け取る。"""

    def __init__(self, exe, *args: str, cwd=None):
        self.proc = subprocess.Popen(
            [str(exe), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=cwd)

    def __call__(self, line: str) -> str:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        reply = self.proc.stdout.readline().strip()
        assert reply and not reply.startswith("ERR"), (line, reply)
        return reply

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass                       # ハーネスが先に終わっていた(電源を切った)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # 終了しないハーネスを残さない
            self.proc.kill()
            self.proc.wait()

    # ---- 状態機械 ----

    def state(self) -> str:
        return STATE_NAME[int(self("state").split()[1])]

    def move(self, frm: list[str], to: str) -> bool:
        mask = sum(1 << STATE[s] for s in frm)
        return self(f"move {mask} {STATE[to]}") == "yes"

    def logs(self) -> list[tuple]:
        """溜まった記録(種別の名前, a, b, c)。STATE の a と b は状態の名前にする。"""
        out = []
        for item in self("logs").split()[1:]:
            kind, a, b, c = item.split(":")
            name = LOG_NAME[int(kind)]
            if name == "STATE":
                out.append((name, STATE_NAME[int(a)], STATE_NAME[int(b)]))
            else:
                out.append((name, int(a), int(b), int(c)))
        return out

    def go(self, *path: str) -> None:
        """起動から、path の状態を順にたどる(記録は捨てる)。"""
        prev = "BOOT"
        for s in path:
            assert self.move([prev], s), (prev, s)
            prev = s
        self.logs()

    # ---- 操作権 ----

    def owner(self) -> tuple[str, int] | None:
        parts = self("owner").split()
        return None if parts[1] == "-" else (parts[1], int(parts[2]))

    # ---- 起動のときのボタン ----

    def boot(self, *levels: int) -> tuple[bool, list[str]]:
        """ボタンの線が levels を順に返すときの判定と、待ちと読みの順。"""
        assert self("pins " + " ".join(map(str, levels))) == "ok"
        parts = self("boot").split()
        return parts[1] == "yes", parts[2:]

    # ---- Wi-Fi の資格情報 ----

    def creds(self) -> tuple[str, str] | None:
        """起動のときに使う SSID とパスワード。使えるものがなければ None。"""
        parts = self("creds").split()[1:]
        return None if parts == ["-"] else tuple(unhx(p) for p in parts)


def hx(s: str) -> str:
    """ハーネスに渡す Wi-Fi の値の書き方(x の後に UTF-8 の 16 進)。"""
    return "x" + s.encode("utf-8").hex()


def unhx(s: str) -> str:
    return bytes.fromhex(s[1:]).decode("utf-8")


@pytest.fixture
def host(app_host_exe):
    h = Host(app_host_exe)
    yield h
    h.close()


@pytest.fixture
def engine(engine_host_exe):
    h = Host(engine_host_exe)
    yield h
    h.close()


@pytest.fixture
def ring(log_host_exe):
    h = Host(log_host_exe)
    yield h
    h.close()


def engine_status(h: Host) -> dict:
    """実行エンジンの公開している値(host/engine_host.c の status)。"""
    (st, why, err, running, mark, index, stop_mask, frames,
     stopped_mask, pressed) = h("status").split()[1:]
    return {"status": ENGINE_NAME[int(st)], "stop_reason": STOP_NAME[int(why)],
            "engine_err": int(err), "running": running == "1",
            "last_mark": int(index) if mark == "1" else None,
            "stop_mask": int(stop_mask), "frames_elapsed": int(frames),
            "stopped_mask": int(stopped_mask),
            "pressed": PRESS_NAME[int(pressed)]}


# ---- 状態機械 ----


def test_the_error_is_left_only_by_clearing_it(host):
    """ERROR から出るのは CLEAR_ERROR(app_state_clear_error)だけであること。

    どの状態からでも移せる条件を付けても、ERROR からは移らない。そうでないと、
    IDLE を書く誰かが異常のフラグをクリアしてしまい、PC からは異常が見えなくなる。
    """
    host.go("WIFI_CONNECTING", "IDLE", "RUNNING")
    assert host("fault") == "ok"
    assert host.state() == "ERROR"
    for to in ("IDLE", "RUNNING", "WIFI_CONNECTING", "OTA"):
        assert not host.move(list(STATE), to)
    assert host.state() == "ERROR"
    assert host("clear") == "yes"
    assert host.state() == "IDLE"
    # ERROR でないときの解除は何もしない
    assert host("clear") == "no"
    assert host.state() == "IDLE"
    assert host.logs() == [("STATE", "RUNNING", "ERROR"), ("STATE", "ERROR", "IDLE")]


@pytest.mark.parametrize("ended", ["STOPPED", "FINISHED"])
def test_a_run_end_after_the_usb_was_unplugged_keeps_the_error(host, ended):
    """USB の切断で ERROR にした後に終了処理が来ても、ERROR が残ること。

    実機では、USB タスクが実行を停止して ERROR にする間に、supervisor が古い状態
    (RUNNING)を読んで終了処理(app_run_end_land)を呼ぶことがある。終了処理が
    IDLE を書くと、PC からは USB の切断が見えず、待機中に見える。
    """
    host.go("WIFI_CONNECTING", "IDLE", "RUNNING")
    host(f"engine {ENGINE[ended]} 1")
    host("fault")
    host("land")
    assert host.state() == "ERROR"
    assert [e for e in host.logs() if e[0] == "STATE"] == [
        ("STATE", "RUNNING", "ERROR")]


@pytest.mark.parametrize("ended, state, log", [
    ("FINISHED", "IDLE", ("RUN_DONE", FRAMES, LATE, MARK)),
    ("STOPPED", "IDLE", ("RUN_ABORT", FRAMES, LATE, MARK)),
    ("FAULT", "ERROR", ("ENGINE_FAULT", INDEX, LATE, MARK)),
])
@pytest.mark.parametrize("running", ["RUNNING", "AWAITING"])
def test_the_run_end_is_classified_by_how_it_ended(host, running, ended, state, log):
    """終了処理が、終わり方に応じて記録を 1 行残し、状態を移すこと。"""
    host.go("WIFI_CONNECTING", "IDLE", "RUNNING",
            *(["AWAITING"] if running == "AWAITING" else []))
    host(f"engine {ENGINE[ended]} 1")
    host("land")
    assert host.state() == state
    assert host.logs() == [log, ("STATE", running, state)]
    # 終了の報告を行えないとき(STOP と supervisor の両方が来たときの
    # 2 回目)は、記録を書かない。状態の終了処理は何度呼んでも同じになる
    host("land")
    assert host.state() == state
    assert host.logs() == []


def test_a_late_run_end_leaves_the_next_run_alone(host):
    """終了処理が来たときにエンジンが動いていれば、何もしないこと。

    supervisor は状態を読んでから終了処理を呼ぶ。その間に制御タスクが STOP(前の
    実行の終了処理)と RUN(次の実行)を続けて処理すると、終了処理は次の実行に
    ついて中断の記録を書き、RUNNING を IDLE に戻してしまう。
    """
    host.go("WIFI_CONNECTING", "IDLE", "RUNNING")
    host(f"engine {ENGINE['RUNNING']} 1")
    host("running 1")
    host("land")
    assert host.state() == "RUNNING"
    assert host.logs() == []


@pytest.mark.parametrize("then", [[], ["OTA"]])
def test_a_late_fault_end_does_not_latch_the_error_again(host, then):
    """異常の終了処理は、今の状態が実行系のときだけ ERROR にすること。

    STOP の終了処理が ERROR にした後、CLEAR_ERROR で IDLE に戻し(さらに更新を
    始め)てから、古い状態を読んだ supervisor の終了処理が来ることがある。そこで
    ERROR にすると、解除したはずの異常が戻り、更新中の状態を上書きする。
    """
    host.go("WIFI_CONNECTING", "IDLE", "RUNNING")
    host(f"engine {ENGINE['FAULT']} 1")
    host("land")
    assert host("clear") == "yes"
    for s in then:
        assert host.move(["IDLE"], s)
    host.logs()
    host("land")
    assert host.state() == (then[-1] if then else "IDLE")
    assert host.logs() == []


def test_a_change_needs_the_state_it_was_meant_for(host):
    """状態の書き換えは、今の状態が条件に合うときだけ行われること。

    supervisor は Wi-Fi が切れていると IDLE を WIFI_CONNECTING にする。読んでから
    書くまでの間に RUN が RUNNING にしていたら、それを上書きしてはいけない。
    上書きすると、実行中なのに転送(PUT)を受け付けてしまう。
    """
    host.go("WIFI_CONNECTING", "IDLE", "RUNNING")
    assert not host.move(["IDLE"], "WIFI_CONNECTING")
    assert host.state() == "RUNNING"
    assert host.logs() == []
    # RUN は、Wi-Fi が切れて WIFI_CONNECTING になった後でも実行系へ移せる
    assert host.move(["RUNNING"], "IDLE")
    assert host.move(["IDLE"], "WIFI_CONNECTING")
    assert host.move(["IDLE", "WIFI_CONNECTING"], "RUNNING")
    assert host.state() == "RUNNING"
    # 既にその状態なら成功として扱い、記録は増やさない
    assert host.move(["RUNNING", "AWAITING"], "RUNNING")
    assert host.logs() == [("STATE", "RUNNING", "IDLE"),
                           ("STATE", "IDLE", "WIFI_CONNECTING"),
                           ("STATE", "WIFI_CONNECTING", "RUNNING")]


def test_a_second_fault_writes_no_record(host):
    """ERROR のときにもう一度 ERROR にしても、STATE の記録は増えないこと。

    画面に「異常 から 異常」という行が出ても、読む人の役に立たない。
    """
    host.go("WIFI_CONNECTING", "IDLE", "RUNNING")
    host("fault")
    host("fault")
    assert host.logs() == [("STATE", "RUNNING", "ERROR")]


# ---- LED の表示 ----

WHITE, DARK = (24, 24, 24), (0, 0, 0)
CYAN = (0, 12, 12)                # 待機中(app_state.c の apply_led)
IDENTIFY_US = 5_000_000           # 判別のための点滅の長さ
HALF_US = 150_000                 # その点灯と消灯のそれぞれの長さ


def led(host) -> tuple[tuple[int, int, int], int]:
    """LED の表示を 1 回ぶん決めさせ、点灯させた色と、次に改めて決めるまでのミリ秒を
    返す。"""
    parts = host("led").split()
    return tuple(int(x) for x in parts[1:4]), int(parts[4])


def test_identify_blinks_white_for_five_seconds(host):
    """判別の要求を受けると、0.15 秒ごとに白の点灯と消灯をくり返し、5 秒たつと
    状態の表示に戻ること。

    点滅の切り替えは受けた時刻から数える。締め切りの直前は締め切りまでしか待たず、
    5 秒を超えて白が残らない。
    """
    host.go("WIFI_CONNECTING", "IDLE")
    host("time 0")
    assert led(host) == (CYAN, 500)
    assert host("identify") == "ok"
    assert led(host) == (WHITE, 150)
    host(f"time {HALF_US}")
    assert led(host) == (DARK, 150)
    host(f"time {2 * HALF_US + 40_000}")
    assert led(host) == (WHITE, 110)
    host(f"time {IDENTIFY_US - 1000}")
    assert led(host) == (DARK, 1)
    host(f"time {IDENTIFY_US}")
    assert led(host) == (CYAN, 500)
    assert host.state() == "IDLE"
    assert host.logs() == [], "判別の点滅で状態が変わった"


def test_identify_again_restarts_the_five_seconds(host):
    """点滅の途中でもう一度受けたら、その時点から 5 秒になること。"""
    host.go("WIFI_CONNECTING", "IDLE")
    host("time 0")
    host("identify")
    assert led(host) == (WHITE, 150)
    host("time 3000000")
    host("identify")
    assert led(host) == (WHITE, 150)
    host(f"time {3_000_000 + IDENTIFY_US - 100_000}")
    assert led(host) == (WHITE, 50)
    host(f"time {3_000_000 + IDENTIFY_US}")
    assert led(host) == (CYAN, 500)


@pytest.mark.parametrize("path, fault, after", [
    (("WIFI_CONNECTING", "IDLE", "RUNNING"), False, ((0, 24, 0), 800)),
    (("WIFI_CONNECTING", "IDLE", "RUNNING", "AWAITING"), False, ((24, 16, 0), 500)),
    (("WIFI_CONNECTING", "IDLE", "RUNNING"), True, ((32, 0, 0), 400)),
], ids=["running", "awaiting", "error"])
def test_identify_is_shown_in_any_state_and_leaves_it_alone(host, path, fault, after):
    """実行中、選択待ち、異常の間も判別の点滅を出し、終わるとその状態の表示に
    戻ること。点滅は LED の表示だけで、状態は変えない。
    """
    host.go(*path)
    if fault:
        host("fault")
        host.logs()
    want = host.state()
    host("time 0")
    host("identify")
    assert led(host) == (WHITE, 150)
    host(f"time {IDENTIFY_US}")
    assert led(host) == after
    assert host.state() == want
    assert host.logs() == [], "判別の点滅で状態が変わった"


DIAG_ON = (12, 12, 12)            # 診断モードの点滅の点灯(起動中の白と同じ明るさ)


@pytest.mark.parametrize("path", [("WIFI_CONNECTING",), ("WIFI_CONNECTING", "IDLE")],
                         ids=["connecting", "idle"])
def test_diagnostic_mode_blinks_white_slowly(host, path):
    """診断モードの間は、Wi-Fi の接続中でも待機でも、白の点灯と消灯を 1 秒ごとに
    くり返すこと。

    USB を始めないので本体からは見えず、待機のシアンのままでは、普通に動いて
    いるのに本体が反応しないように見える。
    """
    host.go(*path)
    assert host("diag") == "ok"
    assert [led(host) for _ in range(3)] == [(DIAG_ON, 1000), (DARK, 1000),
                                             (DIAG_ON, 1000)]


def test_identify_and_error_are_shown_over_the_diagnostic_mode(host):
    """診断モードでも、判別の点滅と異常の表示は先に出すこと。"""
    host.go("WIFI_CONNECTING", "IDLE")
    host("diag")
    host("time 0")
    host("identify")
    assert led(host) == (WHITE, 150)
    host(f"time {IDENTIFY_US}")
    assert led(host)[1] == 1000
    host.go()            # 記録を捨てる
    host.move(["IDLE"], "RUNNING")
    host("fault")
    assert led(host)[1] == 400


# ---- OTA で入れたファームウェアの確定 ----

IMG = _enum("host/include/esp_ota_ops.h", "esp_ota_img_states_t", "ESP_OTA_IMG_")


@pytest.mark.parametrize("pending", [0, 1])
@pytest.mark.parametrize("diag", [0, 1])
@pytest.mark.parametrize("connected", [0, 1])
def test_an_updated_image_that_cannot_reach_wifi_goes_back(host, pending, diag,
                                                           connected):
    """前のファームウェアへ戻すのは、OTA の後の最初の起動で、診断モードでなく、
    Wi-Fi につながらないときだけであること(app_boot.h)。

    つながらないまま確定すると、次の OTA も PC からの操作もできなくなる。診断モードは
    シリアルで資格情報を修正するための起動なので、戻すと修正している途中でバージョンが
    変わる。
    """
    want = "rollback" if (pending, diag, connected) == (1, 0, 0) else "confirm"
    assert host(f"verdict {pending} {diag} {connected}") == want


@pytest.mark.parametrize("state, rolled", [
    # 確定しないまま再起動して、ブートローダーが戻した(確定する前に異常終了したときと、
    # Wi-Fi につながらずに戻したとき)
    ("ABORTED", True),
    ("VALID", False), ("NEW", False), ("PENDING_VERIFY", False), ("INVALID", False),
])
def test_going_back_is_reported(app_host_exe, state, rolled):
    """更新先の区画の状態から、前のファームウェアへ戻ったことを STATUS に出すこと。"""
    h = Host(app_host_exe, str(IMG[state]))
    try:
        assert h("rolled") == ("yes" if rolled else "no")
    finally:
        h.close()


def test_the_verdict_comes_before_the_image_is_confirmed():
    """main.c は、確定する前に判定すること。後で判定しても、確定した後では戻せない。"""
    src = (MAIN / "main.c").read_text(encoding="utf-8")
    verdict = src.index("app_boot_verdict(pending, diag, connected)")
    confirm = src.index("esp_ota_mark_app_valid_cancel_rollback()")
    assert verdict < confirm
    assert verdict < src.index("app_usb_start(")
    # 戻すときは確定しないまま再起動する。そうすればブートローダーが ABORTED にし、
    # 上の rolled_back の判定にそのまま出る
    assert "esp_restart();" in src[verdict:confirm]


# ---- 設定待ちで起動するか ----

@pytest.mark.parametrize("button", [0, 1])
@pytest.mark.parametrize("has_creds", [0, 1])
@pytest.mark.parametrize("is_new", [0, 1])
@pytest.mark.parametrize("connected", [0, 1])
def test_the_device_waits_for_wifi_settings_when_it_cannot_reach_wifi(
        host, button, has_creds, is_new, connected):
    """設定待ちで起動するのは、ボタンを押したまま起動したとき、Wi-Fi の値がないとき、
    新しく保存した値でつながらなかったときであること(app_boot.h)。

    値がない装置は PC から何もできないので、書き込んだばかりの装置はボタンを押さずに
    設定できる。新しい値でつながらなければ、値が誤っている可能性が高いので、設定待ちに
    戻して理由を出す。一度つながった値でつながらないときは、ルーターの不調のような
    一時的な理由もあるので、普通に起動してつなぎ直し続ける。
    """
    setup = button or not has_creds or (is_new and not connected)
    got = host(f"setup {button} {has_creds} {is_new} {connected}")
    assert got == ("setup" if setup else "normal")


def test_the_setup_mode_is_decided_after_the_verdict_and_before_the_usb():
    """main.c は、OTA の確定の判断の後で、USB を始める前に設定待ちかを決めること。

    前のファームウェアへ戻すときは、設定待ちにせずに戻す。USB を始めた後では、
    シリアルの設定コンソールを出せない。
    """
    src = (MAIN / "main.c").read_text(encoding="utf-8")
    verdict = src.index("app_boot_verdict(pending, diag, connected)")
    setup = src.index("app_boot_setup_mode(")
    assert verdict < setup < src.index("app_usb_start(")


def test_the_usb_lock_is_made_on_every_boot():
    """USB を始めない起動(診断モードと Wi-Fi の設定待ち)でも、USB の排他があること。

    監視のタスクと STATUS は、USB を始めたかどうかにかかわらず送信の計測値を読む。
    排他がなければ起動の直後に異常終了し、再起動をくり返して設定コンソールが使えない。
    FreeRTOS はホストで動かせないので、起動の順序をソースで見る。
    """
    src = (MAIN / "main.c").read_text(encoding="utf-8")
    made = src.index("app_usb_init();")
    assert made < src.index("app_button_init(")
    assert made < src.index('"supervisor"')
    assert made < src.index("app_usb_start(")
    usb = (MAIN / "app_usb.c").read_text(encoding="utf-8")
    init = usb[usb.index("void app_usb_init"):usb.index("esp_err_t app_usb_start")]
    assert "s_lock = xSemaphoreCreateMutexStatic" in init
    assert usb.count("s_lock = ") == 1
    # USB を始めていなければ、再接続を起こす相手(usb タスク)もいない
    reconnect = usb[usb.index("esp_err_t app_usb_reconnect"):]
    guard = reconnect.index("s_sender == NULL")
    assert guard < reconnect.index("xTaskNotifyGive(s_sender)")


# ---- 実行エンジン ----


def test_a_run_that_cannot_start_keeps_how_the_last_one_ended(engine):
    """最初のレコードを取れずに始められなかった RUN は、前の実行の値を残すこと。

    始まっていない実行について「異常で停止した」と返すと、PC は待機中の装置を
    異常として扱う。前の実行が停止した理由と最後に通った区切りも消える。
    """
    good = binfmt.encode([binfmt.State(0, 1, mark=binfmt.MARK_LABEL),
                          binfmt.State(5, 0), binfmt.End()], 5)
    assert engine(f"start {good.hex()}") == "start 0"
    engine("alarm 0")          # 区切りの付いた最初のレコードを出す
    engine(f"stop {STOP['STOPPED']}")
    before = engine_status(engine)
    assert before == {"status": "STOPPED", "stop_reason": "STOPPED",
                      "engine_err": 0, "running": False, "last_mark": 0,
                      "stop_mask": 0, "frames_elapsed": 0, "stopped_mask": 0,
                      "pressed": ""}
    # 未初期化のカウンターを最初に読む手順。読み込みは通り、最初の 1 歩で停止する
    bad = binfmt.encode([binfmt.Djnz(0, 1, 0), binfmt.End()], 0)
    assert engine(f"start {bad.hex()}") != "start 0"
    assert engine_status(engine) == before


@pytest.mark.parametrize("busy, started", [(1, True), (3, False)])
def test_a_timer_busy_with_another_task_retries_then_fails_the_start(
        engine, busy, started):
    """タイマーが別のタスクの操作の途中でも、RUN の開始で装置が異常終了しないこと。

    選択の側は、停止に気づくとタイマーを後から止める(app_engine_select)。その途中に
    RUN が重なると、gptimer は ESP_ERR_INVALID_STATE を返す。開始は相手に譲って
    やり直し、それでも用意できなければ失敗を返す(呼び出し元は START_FAILED にする)。
    始められなかったときは、前の実行の値を残す。
    """
    good = binfmt.encode([binfmt.State(0, 1, mark=binfmt.MARK_LABEL),
                          binfmt.State(5, 0), binfmt.End()], 5)
    assert engine(f"start {good.hex()}") == "start 0"
    engine("alarm 0")
    engine(f"stop {STOP['STOPPED']}")
    before = engine_status(engine)
    engine(f"busy {busy}")
    if started:
        assert engine(f"start {good.hex()}") == "start 0"
        assert engine_status(engine)["running"] is True
        assert engine("timer") == "timer 1"
    else:
        # ESP_ERR_INVALID_STATE(0x103)
        assert engine(f"start {good.hex()}") == f"start {0x103}"
        assert engine_status(engine) == before
        assert engine("timer") == "timer 0"


def test_the_run_start_and_the_run_end_are_placed_inside_the_same_lock():
    """実行の開始(RUN)と終了処理が、同じロックの取得と解放の間に置かれていること。

    終了処理が「実行が停止している」と確認してから状態を戻すまでの間に次の実行が
    始まると、終わった実行の報告と状態の書き換えを、次の実行に適用してしまう。
    タスクどうしの競合はホストのテストでは作れないので、ここではコードの
    配置を確認する。停止しているかはロックの中で確認し、どの戻り方でもロックを解放する。
    """
    ctrl = (MAIN / "app_ctrl.c").read_text(encoding="utf-8")
    run = ctrl[ctrl.index("static int cmd_run("):]
    run = run[:run.index("\n}\n")]
    assert (run.index("app_state_run_lock()") < run.index("start_run(")
            < run.index("app_state_run_unlock()")), run
    end = (MAIN / "app_runend.h").read_text(encoding="utf-8")
    land = end[end.index("static inline void app_run_end_land(void) {"):]
    land = land[:land.index("\n}\n")]
    assert land.index("app_state_run_lock()") < land.index("app_engine_is_running()")
    # 途中の return の前と、最後とで、ロックを解放する
    assert land.count("app_state_run_unlock()") == land.count("return;") + 1, land
    for part in land.split("return;")[:-1]:
        assert part.rstrip().endswith("app_state_run_unlock();"), part[-80:]


def test_each_run_gets_the_next_run_number(engine):
    """実行の通し番号(STATUS の run_gen)は、まだ実行していなければ 0 で、実行を
    始めるたびに 1 進み、停止した後も次の実行まで同じであること。PC は起動の印と
    この番号で実行を見分ける(同じ手順をすぐ始め直しても別の実行と分かる)。"""
    blob = binfmt.encode([binfmt.State(5, 0), binfmt.End()], 5)
    assert engine("rungen") == "rungen 0"
    assert engine(f"start {blob.hex()}") == "start 0"
    assert engine("rungen") == "rungen 1"
    engine("stop 0")
    assert not engine_status(engine)["running"], engine_status(engine)
    assert engine("rungen") == "rungen 1"
    assert engine(f"start {blob.hex()}") == "start 0"
    assert engine("rungen") == "rungen 2"


def test_a_select_for_an_earlier_wait_does_not_move_the_next_one(engine):
    """前の選択待ちに宛てた選択は、次の選択待ちを進めないこと。

    時間切れの判定は、通し番号を読んでから選ぶまでの間に、PC の選択でその選択待ちが
    進み、エンジンが次の選択待ちに入っていることがある。選ぶ関数の中で、権利を
    取った後に通し番号を照合するので、古い宛先の選択は拒否される。
    """
    blob = binfmt.encode([binfmt.Await(0, (1, 1)), binfmt.Await(0, (2, 2)),
                          binfmt.State(5, 0), binfmt.End()], 5)
    assert engine(f"start {blob.hex()}") == "start 0"
    engine("alarm 0")                           # 1 つ目の選択待ちに入る
    awaiting, first = engine("await").split()[1:]
    assert awaiting == "1"
    assert engine(f"select 0 {first}") == "select 0"
    engine("alarm 0")                           # 2 つ目の選択待ちに入る
    awaiting, second = engine("await").split()[1:]
    assert awaiting == "1" and int(second) == int(first) + 1, (awaiting, second)
    # ESP_ERR_INVALID_VERSION(0x10A)。制御のサーバーはこれを STALE_SELECT にする
    assert engine(f"select 0 {first}") == f"select {0x10A}", \
        "前の選択待ち宛ての選択が通った"
    assert engine("await").split()[1:] == ["1", second], "古い宛先の選択で進んだ"
    assert engine(f"select 0 {second}") == "select 0"
    assert engine("await").split()[1] == "0"
    # 選択待ちでないときは ESP_ERR_INVALID_STATE(0x103)。サーバーは BAD_STATE にする
    assert engine(f"select 0 {second}") == f"select {0x103}", "選択待ちでないのに選べた"


# 60Hz の周期(app_engine.c の既定)で、フレームの予定時刻をタイマーのカウントにする
def due_us(frame: int) -> int:
    return frame * 16666667 // 1000


def test_a_fault_drops_the_stop_reservation(engine):
    """手順データの異常で停止したら、「区切りで停止」の予約も消えること。

    予約は、停止したとき、STOP、実行の終わり、次の RUN で消える(comm-protocol.md の
    stop_at_mark)。異常で停止した後に残ると、画面は停止している装置の予約が有効な
    ように表示し続ける。異常の解除の後も、次の操作まで残る。
    """
    # 600 フレーム目の次に 500 フレーム目が来る(復号は時刻の逆行を見ない)
    blob = binfmt.encode([binfmt.State(0, 1), binfmt.State(600, 0),
                          binfmt.State(500, 0), binfmt.End()], 600)
    assert engine(f"start {blob.hex()}") == "start 0"
    engine("alarm 0")
    engine(f"mark {binfmt.MARK_LAP}")
    assert engine_status(engine)["stop_mask"] == binfmt.MARK_LAP
    engine(f"alarm {due_us(600)}")              # 次を読んで時刻の逆行に当たる
    st = engine_status(engine)
    assert (st["status"], st["stop_reason"], st["running"]) == ("FAULT", "ERROR", False)
    assert st["stop_mask"] == 0, "異常で停止した後も予約が残っている"


@pytest.mark.parametrize("mask", [binfmt.MARK_LAP, binfmt.MARK_ALL], ids=["lap", "all"])
def test_a_stop_at_a_mark_tells_which_reservation_stopped_it(engine, mask):
    """「区切りで停止」の予約により区切りで停止したら、停止させた予約の mask を返すこと
    (STATUS の stopped_mask)。予約(stop_mask)は停止すると消えるが、停止させた予約は
    次の RUN まで残る。次の RUN で 0 に戻り、予約のほかの止まり方では 0 のままである。

    PC は、完了の連動停止の予約(周の境だけ)で停止したのか、人が重ねた予約(装置の
    上面のボタンの短押しは 3 種すべて)で停止したのかを、これで区別する。どちらも同じ
    周の境で停止するので、停止した区切りでは区別できない。
    """
    blob = binfmt.encode([binfmt.State(0, 1), binfmt.State(5, 0, mark=binfmt.MARK_LAP),
                          binfmt.State(10, 1), binfmt.End()], 10)
    assert engine(f"start {blob.hex()}") == "start 0"
    engine("alarm 0")
    engine(f"mark {mask}")
    engine(f"alarm {due_us(5)}")                 # 周の境のレコードを出さずに停止する
    st = engine_status(engine)
    assert (st["running"], st["stop_reason"], st["stop_mask"], st["stopped_mask"]) \
        == (False, "AT_MARK", 0, mask)
    assert engine(f"start {blob.hex()}") == "start 0"
    assert engine_status(engine)["stopped_mask"] == 0, "次の RUN で消えない"
    engine("alarm 0")
    engine(f"stop {STOP['STOPPED']}")
    assert engine_status(engine)["stopped_mask"] == 0


def test_a_button_pressed_after_a_stop_is_kept_until_the_next_run(engine):
    """実行していない間に押された装置のボタンを残し(STATUS の button_after_stop)、
    長押しが短押しより優先され、次の RUN で消すこと。実行している間に押されたボタンは、
    停止と予約として反映されるので残さないこと。

    周回を終えるために予約で停止し、ほかの装置を待つ間は、PC から見ると実行の途中で
    ある。装置は停止していて、ボタンで停止させるものがない。残さないと、PC は人が
    ボタンで停止させたことを知らずに続きを始め、装置は手順の最後まで動く。
    """
    blob = binfmt.encode([binfmt.State(0, 1), binfmt.State(5, 0, mark=binfmt.MARK_LAP),
                          binfmt.State(10, 1), binfmt.End()], 10)
    assert engine(f"start {blob.hex()}") == "start 0"
    engine("alarm 0")
    engine(f"press {PRESS_SHORT}")               # 動いている間は残さない
    assert engine_status(engine)["pressed"] == ""
    engine(f"mark {binfmt.MARK_LAP}")
    engine(f"alarm {due_us(5)}")                 # 周の境で停止する
    assert engine_status(engine)["running"] is False
    engine(f"press {PRESS_SHORT}")
    assert engine_status(engine)["pressed"] == "short"
    engine(f"press {PRESS_LONG}")
    engine(f"press {PRESS_SHORT}")
    assert engine_status(engine)["pressed"] == "long", "短押しが長押しを消した"
    assert engine(f"start {blob.hex()}") == "start 0"
    assert engine_status(engine)["pressed"] == "", "次の RUN で消えない"


def test_a_stop_on_a_wait_tells_which_reservation_stopped_it(engine):
    """選択待ちで止まっている間に受けた予約で、その場で停止したときも、停止させた予約の
    mask を返すこと。

    選択待ちは区切りなので、そのマークを含む予約を受けると、アラームを待たずに停止する
    (app_engine_stop_at_mark)。区切りの予定時刻に停止するときと、停止させる処理の経路が
    違う。
    """
    blob = binfmt.encode([binfmt.Await(0, (1,), mark=binfmt.MARK_AWAIT),
                          binfmt.State(5, 1), binfmt.State(10, 0), binfmt.End()], 10)
    assert engine(f"start {blob.hex()}") == "start 0"
    engine("alarm 0")                           # 選択待ちに入る
    assert engine("await").split()[1] == "1"
    engine(f"mark {binfmt.MARK_ALL}")
    st = engine_status(engine)
    assert (st["running"], st["stop_reason"], st["stopped_mask"]) == \
        (False, "AT_MARK", binfmt.MARK_ALL)


@pytest.mark.parametrize("where", ["alarm", "start"])
def test_a_stop_while_selecting_leaves_the_timer_stopped(engine, where):
    """選ぶ途中で別のタスクが停止させても、選んだ側がタイマーを動かしたままに
    しないこと。

    選択(PC の SELECT と時間切れ)はタイマーを動かして再開する。停止させる側は、まだ
    タイマーが止まっている間に停止の処理を済ませることがある。そのまま動かすと、
    出力は全入力を離したままでも、次のアラームまでカウンターが進み、STATUS の
    経過フレームが停止させた後も増え続ける。where は、停止の処理が割り込む場所である
    (アラームを設定するとき、タイマーを動かすとき)。
    """
    blob = binfmt.encode([binfmt.Await(0, (1,)), binfmt.State(5, 1),
                          binfmt.State(10, 0), binfmt.End()], 10)
    assert engine(f"start {blob.hex()}") == "start 0"
    engine("alarm 0")                           # 選択待ちに入り、タイマーを止める
    awaiting, gen = engine("await").split()[1:]
    assert awaiting == "1" and engine("timer") == "timer 0"
    engine(f"race {where}")
    assert engine(f"select 0 {gen}") == "select 0"
    st = engine_status(engine)
    assert (st["status"], st["stop_reason"], st["running"]) == \
        ("STOPPED", "STOPPED", False)
    assert engine("timer") == "timer 0", "停止させた後もタイマーが動いている"


def test_the_elapsed_frames_go_past_the_total_after_an_arm_with_a_length(engine):
    """長さのある選択肢を通った後も、経過フレームが総フレームで止まらないこと。

    総フレーム(ヘッダーの total_frames)は、選択肢の中身を長さ 0 として数える。
    通った選択肢の長さは、後ろの時刻に足される(ARMJOIN の delta)。経過フレームを
    総フレームで抑えると、選択肢を通った後の実行の後半で経過が止まり、最後に通った
    区切りのフレームより小さくなる。PC はその差で図の現在位置を出すので、線が
    区切りに戻り続ける。終わりの記録(RUN_DONE)の値も総フレームで止まる。
    模擬装置は抑えない(mockdevice.py)。
    """
    blob = binfmt.encode([binfmt.State(0), binfmt.Await(5, (2,)),
                          binfmt.State(5, 1), binfmt.State(12, 0),   # 選択肢の中
                          binfmt.ArmJoin(5, 7), binfmt.State(10, 2), binfmt.End()],
                         10)
    assert engine(f"start {blob.hex()}") == "start 0"
    engine("alarm 0")
    engine(f"alarm {due_us(5)}")                 # 選択待ちに入る
    gen = engine("await").split()[2]
    assert engine(f"select 0 {gen}") == "select 0"
    engine(f"alarm {due_us(5)}")
    engine(f"alarm {due_us(12)}")                # 総フレームの 10 を超えた
    st = engine_status(engine)
    assert (st["running"], st["frames_elapsed"]) == (True, 12)
    engine(f"alarm {due_us(17)}")                # 続きの最後のレコード(10 + 7)
    st = engine_status(engine)
    assert (st["status"], st["frames_elapsed"]) == ("FINISHED", 17)


def test_a_new_run_does_not_send_the_input_the_last_one_was_stopped_with(engine):
    """STOP で停止させた実行で押していた入力を、次の実行の最初に送らないこと。

    STOP は公開している入力を書き換えず、停止させたことを示す印で全入力を離した入力を
    返させる(app_engine_stop)。次の RUN がその印をクリアしてから最初のアラームまでの
    間に usb タスクが入力を作ると、前の実行で押していたボタンを送る。
    """
    held = binfmt.encode([binfmt.State(0, 1), binfmt.State(100, 0), binfmt.End()], 100)
    assert engine(f"start {held.hex()}") == "start 0"
    engine("alarm 0")
    assert engine("snap") == "snap 1"
    engine(f"stop {STOP['STOPPED']}")
    assert engine(f"start {held.hex()}") == "start 0"
    assert engine("snap") == "snap 0", "前の実行で押していた入力を送る"


# ---- 手順の保存 ----
#
# ハーネス(host/store_host.c)は、起動したフォルダーの procs を装置の /procs として
# 扱う。起動し直すと、装置の電源を入れ直したことになる。

PROC = binfmt.encode([binfmt.State(i, i % 2) for i in range(40)] + [binfmt.End()], 39)
PROC_ID = binfmt.proc_hash(PROC)


@pytest.fixture
def store(store_host_exe, tmp_path):
    """呼ぶたびに、同じフォルダーで手順の保存のハーネスを起動し直す。"""
    hosts: list[Host] = []

    def boot() -> Host:
        hosts.append(Host(store_host_exe, cwd=tmp_path))
        return hosts[-1]

    yield boot
    for h in hosts:
        h.close()


def saved_files(tmp_path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "procs").iterdir())


@pytest.mark.parametrize("cut", ["write", "rename"])
def test_a_power_cut_while_saving_leaves_nothing_under_the_procedure_name(
        store, tmp_path, cut):
    """保存(COMMIT)の途中で電源が切れても、手順の名前で作りかけのファイルが残らないこと。

    残ると、起動し直した後の一覧(LIST)に含まれる。PC は一覧にある手順を送り直さずに
    実行するので、実行は「手順のデータが壊れている」か「手順が見つかりません」で
    失敗し続ける。送り直しても、中身が違うので「同じ id の別の手順」として拒否される。
    cut は電源が切れる時点で、書き込みの途中と、書き終えて名前を変える直前である。
    """
    h = store()
    assert h(f"put {PROC.hex()}") == f"put {PROC_ID}"
    h(f"cut {cut}")
    assert h(f"commit {PROC_ID}") == "cut"
    # 終わり切って書きかけのファイルを放すまで待つ。放す前に次を起動すると、Windows は
    # そのファイルを消せず、名前も変えられない
    h.proc.wait(timeout=10)
    h = store()
    assert h("list") == "list", "作りかけの手順が一覧に載る"
    h(f"put {PROC.hex()}")
    assert h(f"commit {PROC_ID}") == "commit 0"
    assert h(f"load {PROC_ID}") == f"load 0 {len(PROC)}"
    assert saved_files(tmp_path) == [f"{PROC_ID}.bin"], "書きかけが残っている"


@pytest.mark.parametrize("damage", ["half", "empty"])
def test_a_damaged_saved_procedure_is_dropped_when_it_is_read(store, tmp_path, damage):
    """保存した手順の中身が id と合わなければ、読むときに消して「ない」と返すこと。

    中身が合わないのは、書きかけか、ほかの理由で壊れたファイルである。残すと一覧に
    含まれ続け、PC はその id を送り直さないので、同じ手順を始められない。「ない」と
    返せば、PC は次の開始で送り直す。
    """
    (tmp_path / "procs").mkdir()
    (tmp_path / "procs" / f"{PROC_ID}.bin").write_bytes(
        PROC[:len(PROC) // 2] if damage == "half" else b"")
    h = store()
    assert h("list") == f"list {PROC_ID}"
    assert h(f"load {PROC_ID}") == f"load {0x105} 0"      # ESP_ERR_NOT_FOUND
    assert h("list") == "list"
    assert saved_files(tmp_path) == []


def test_a_damaged_saved_procedure_is_replaced_when_it_is_sent_again(store, tmp_path):
    """同じ名前のファイルが壊れていれば、送り直した手順で置き換えること。

    ファイルの中身から計算した id が要求の id と合わないので、別の手順(id の衝突)
    ではない。いつも送ってから実行する経路(画面の開始)は、保存で拒否されると実行に
    進まず、読むときの片付けに到達しない。
    """
    (tmp_path / "procs").mkdir()
    (tmp_path / "procs" / f"{PROC_ID}.bin").write_bytes(PROC[:len(PROC) // 2])
    h = store()
    h(f"put {PROC.hex()}")
    assert h(f"commit {PROC_ID}") == "commit 0"
    assert h(f"load {PROC_ID}") == f"load 0 {len(PROC)}"


@pytest.mark.parametrize("where", ["write", "close"])
def test_a_failed_save_leaves_no_file(store, tmp_path, where):
    """保存の書き込みが失敗したら、書きかけのファイルを残さないこと。

    書き込みは閉じるときにも失敗しうる(溜まっていた分をそこで書く)。残すと、同じ
    id の保存は中身の不一致で拒否され続け、再起動の後は一覧に出る。
    """
    h = store()
    h(f"put {PROC.hex()}")
    h(f"fail {where}")
    assert h(f"commit {PROC_ID}") == "commit -1"       # ESP_FAIL
    assert h("list") == "list"
    assert saved_files(tmp_path) == []
    assert h(f"commit {PROC_ID}") == "commit 0"


# ---- 記録のリング ----
#
# 起動すると番号 0 に BOOT の記録が入る(app_log_init)。ハーネスの put N は、
# a に N を入れた状態の記録を 1 件書く。read は、返した記録の a だけを並べる。
# BOOT の a は 0 である。


def ring_read(h: Host, *ack: int, limit: int = 48) -> tuple[int, list[int]]:
    """LOGS の 1 回ぶん。ack に (boot, ack) を渡すと、受け取った分を知らせる。

    limit の既定は app_ctrl.c の MAX_LOG_ENTRIES_PER_RESP と同じ値で、
    test_firmware_docs.py がその値を照合する。
    """
    cmd = f"ack {ack[0]} {ack[1]} {limit}" if ack else f"read {limit}"
    parts = h(cmd).split()[1:]
    return int(parts[0]), [int(a) for a in parts[1:]]


def ring_boot(h: Host) -> int:
    return int(h("boot").split()[1])


def test_reading_the_logs_leaves_them_until_acked(ring):
    """LOGS は記録を返すだけで消さず、受け取ったと知らせた分だけを消すこと。

    返した時点で消すと、PC が応答を受け取る前に接続が切れたときに、その記録が
    どこにも残らない。番号は起動からの通し番号で、from から 1 ずつ続く。
    """
    for a in (11, 12, 13):
        ring(f"put {a}")
    assert ring_read(ring) == (0, [0, 11, 12, 13])
    assert ring_read(ring) == (0, [0, 11, 12, 13]), "読んだだけで消えた"
    boot = ring_boot(ring)
    assert ring_read(ring, boot, 2) == (2, [12, 13])
    # 応答を受け取れずに送り直した ack と、前の ack は、何もしない
    assert ring_read(ring, boot, 2) == (2, [12, 13])
    assert ring_read(ring, boot, 1) == (2, [12, 13])
    assert ring_read(ring, boot, 4) == (4, [])
    ring("put 14")
    assert ring_read(ring, boot, 4) == (4, [14]), "次に記録する番号が続いていない"


def test_an_ack_from_another_boot_or_ahead_removes_nothing(ring):
    """違う起動の ack と、まだ記録していない番号の ack では、何も消さないこと。

    違う起動の ack は、装置が起動し直す前の番号である。今の起動の記録をその番号で
    消すと、PC が受け取っていない記録が消える。まだ記録していない番号の ack に
    従うと、残っている記録をすべて消す。
    """
    ring("random 7")
    ring("put 11")
    boot = ring_boot(ring)
    assert ring_read(ring, boot + 1, 2) == (0, [0, 11])
    assert ring_read(ring, boot, 3) == (0, [0, 11])
    assert ring_read(ring, boot, 2) == (2, [])


def test_the_boot_is_chosen_once_at_the_first_read(ring):
    """起動ごとの乱数は、最初に読み出すときに 1 回だけ決めること。

    制御のサーバーは Wi-Fi を始めた後に動く。そのときに決めれば、乱数に無線の
    雑音が入っている。決めた後は、同じ起動の間は変えない。
    """
    ring("random 7")
    ring_read(ring)
    ring("random 9")
    assert ring_boot(ring) == 7
    ring_read(ring)
    assert ring_boot(ring) == 7


def test_one_read_returns_at_most_the_limit(ring):
    """渡した上限の件数までを返し、残りは続きの番号から返ること。"""
    for a in range(1, 60):
        ring(f"put {a}")
    first, got = ring_read(ring)
    assert (first, got) == (0, list(range(0, 48)))
    assert ring_read(ring, ring_boot(ring), 48) == (48, list(range(48, 60)))


def test_a_full_ring_keeps_the_oldest_and_counts_the_rest(ring):
    """リングがあふれたら、新しい記録を捨てて、捨てた数を数えること。

    残っているのは PC がまだ受け取っていない記録で、古い方から上書きすると、
    それを失う。受け取ったと知らせた分を消すと、また書ける。
    """
    size = int(re.search(r"#define RING_SIZE (\d+)",
                         (MAIN / "app_log.c").read_text(encoding="utf-8")).group(1))
    for a in range(1, size + 1):                # BOOT と合わせて size + 1 件
        ring(f"put {a}")
    assert ring("dropped") == "dropped 1"
    first, got = ring_read(ring, limit=size + 8)
    assert (first, len(got), got[-1]) == (0, size, size - 1)
    boot = ring_boot(ring)
    assert ring_read(ring, boot, size) == (size, [])
    ring("put 999")
    assert ring_read(ring) == (size, [999])


# ---- Wi-Fi の資格情報 ----

PASSWORDS = ["", "a" * 7, "a" * 8, "a" * 63, "p" * 64,
             "0123456789abcdefABCDEF" * 2 + "0" * 20, "g" + "0" * 63, "0" * 65,
             "あ" * 21, "あ" * 22, "with space 8"]
SSIDS = ["", "a", "a" * 32, "a" * 33, "あ" * 10, "あ" * 11]


@pytest.mark.parametrize("pw", PASSWORDS)
def test_the_device_and_the_pc_take_the_same_passwords(host, pw):
    """パスワードの範囲が、装置(app_wifi.h)と PC(proto.py)で同じであること。

    範囲は空、8〜63 バイト、16 進数 64 桁である。PC だけが通す値は装置に拒否され、
    装置だけが通す値は PC のツールで入れられない。
    """
    assert (host(f"passok {hx(pw)}") == "yes") is (
        proto.wifi_value_problem("wifi_pass", pw) is None)


@pytest.mark.parametrize("ssid", SSIDS)
def test_the_device_and_the_pc_take_the_same_ssids(host, ssid):
    assert (host(f"ssidok {hx(ssid)}") == "yes") is (
        proto.wifi_value_problem("wifi_ssid", ssid) is None)


def test_the_password_rule_is_wpa():
    """空、8〜63 バイトのパスフレーズ、16 進数 64 桁の鍵だけを通すこと。"""
    ok = [pw for pw in PASSWORDS if proto.wifi_value_problem("wifi_pass", pw) is None]
    assert ok == ["", "a" * 8, "a" * 63, "0123456789abcdefABCDEF" * 2 + "0" * 20,
                  "あ" * 21, "with space 8"]


def test_a_saved_ssid_without_a_password_is_not_used(host):
    """NVS に SSID だけがあり、パスワードを保存していなければ、使わないこと。

    資格情報は SSID とパスワードの組で、揃っていなければ使わない(Wi-Fi が設定
    されていないのと同じで、USB だけで動く)。パスワードのない Wi-Fi は、空の
    パスワードを保存して表すので、保存していないのとは別である。
    """
    assert host.creds() is None
    host(f"nvs wifi_ssid {hx('cafe')}")
    assert host.creds() is None
    host(f"nvs wifi_pass {hx('')}")
    assert host.creds() == ("cafe", "")
    host(f"nvs wifi_pass {hx('saved-pass')}")
    assert host.creds() == ("cafe", "saved-pass")
    host("nvs wifi_pass -")
    assert host.creds() is None


@pytest.mark.parametrize("key", ["wifi_ssid", "wifi_pass"])
@pytest.mark.parametrize("writes", [0, 1, 2])
def test_a_power_cut_while_saving_wifi_never_leaves_a_new_value_unmarked(host, key,
                                                                         writes):
    """Wi-Fi の値を保存する途中で電源が切れても、新しい値が、新しく保存した値の印
    なしで残らないこと。

    印のない値は、一度つながった値として扱われる。値が誤っていても、つながらない
    まま普通に起動してつなぎ直し続け、設定待ちにならないので PC からも見えない
    (app_boot.h)。印だけが残るのはよい。古い値でつながれば印がクリアされ、
    つながらなければ設定待ちになるだけである。ESP-IDF の NVS は値を 1 つ書くたびに
    フラッシュへ書く。writes は、電源が切れるまでに書けた回数である。
    """
    host(f"nvs wifi_ssid {hx('home')}")
    host(f"nvs wifi_pass {hx('old-pass')}")
    assert host("isnew") == "no"
    host(f"power {writes}")
    assert host(f"save {key} {hx('new-value')}") == "save 0"
    host("power -1")
    changed = host.creds() != ("home", "old-pass")
    if changed:
        assert host("isnew") == "yes", "新しい値が、印なしで残った"
    if writes == 2:
        assert changed, "書き終えたのに値が変わっていない"


def test_a_saved_password_without_an_ssid_is_not_used(host):
    """NVS にパスワードだけがあれば、使わないこと。

    CONFIG は wifi_pass だけを保存することもできる。SSID がなければ、つなぐ先が
    決まらない。
    """
    host(f"nvs wifi_pass {hx('saved-pass')}")
    assert host.creds() is None


# ---- 操作権 ----


def test_the_owner_is_taken_away_only_by_force(host):
    """所有者がいるとき、別の識別子は force でだけ取れること。"""
    assert host("acquire pc-a 1 0") == "yes"
    assert host("acquire pc-b 2 0") == "no"
    assert host("is pc-a") == "yes"
    assert host("acquire pc-b 2 1") == "yes"
    assert host("is pc-a") == "no" and host("is pc-b") == "yes"
    assert host("acquire pc-a 1 0") == "no"


def test_the_same_id_takes_it_back_without_force(host):
    """同じ識別子なら、接続が変わっても force なしで取得し直せること。

    PC がスリープから戻ったときや、サーバーを起動し直したときの通常の流れである。
    """
    assert host("acquire pc-a 1 0") == "yes"
    host("forget 1")
    assert host("acquire pc-a 2 0") == "yes"
    assert host.owner() == ("pc-a", 0)


def test_the_owner_expires_after_30_seconds_of_silence(host):
    """通信のない状態が 30 秒を超えたときだけ失効すること。ちょうど 30 秒では
    失効しない。"""
    host("time 0")
    host("acquire pc-a 1 0")
    host(f"time {OWNER_TIMEOUT_US}")
    host("expire")
    assert host.owner() == ("pc-a", 30_000)
    host(f"time {OWNER_TIMEOUT_US + 1}")
    host("expire")
    assert host.owner() is None
    # 失効した後は、別の識別子が force なしで取れる
    assert host("acquire pc-b 2 0") == "yes"


def test_only_the_owning_connection_keeps_it_alive(host):
    """失効の時計を戻すのは、操作権を取った接続からの受信だけであること。

    同じ識別子を送る別の接続(同じ padcue の CLI)で戻すと、サーバーの接続が
    使えなくなっていても CLI が通信している間は失効しなくなる。
    """
    host("time 0")
    host("acquire pc-a 1 0")
    host("time 20000000")
    host("touch 2")
    host(f"time {OWNER_TIMEOUT_US + 1}")
    host("expire")
    assert host.owner() is None

    host("time 0")
    host("acquire pc-a 1 0")
    host("time 20000000")
    host("touch 1")
    host(f"time {OWNER_TIMEOUT_US + 1}")
    host("expire")
    assert host.owner() == ("pc-a", 10_000)


def test_a_closed_connection_no_longer_keeps_it_alive(host):
    """接続が閉じても操作権は残り、その接続を指さなくなること。

    閉じた接続を指したままだと、次の接続がメモリの同じアドレスに置かれたときに、別の
    送り手の通信で失効の時計が戻る。
    """
    host("time 0")
    host("acquire pc-a 1 0")
    host("forget 1")
    assert host.owner() == ("pc-a", 0)
    host("time 20000000")
    host("touch 1")
    host(f"time {OWNER_TIMEOUT_US + 1}")
    host("expire")
    assert host.owner() is None


def test_only_the_owner_id_can_release_but_any_release_ends_it(host):
    """解放できるのは所有者の識別子だけで、release_any(長押し)はだれの操作権でも
    手放すこと。"""
    host("acquire pc-a 1 0")
    host("release pc-b")
    assert host("is pc-a") == "yes"
    host("release pc-a")
    assert host.owner() is None
    host("acquire pc-b 2 0")
    host("release_any")
    assert host.owner() is None


@pytest.mark.parametrize("cid, ok", [
    ("", False), ("a", True), ("a" * 64, True), ("a" * 65, False),
    ("A_b-9", True), ("a.b", False), ("a/b", False),
])
def test_the_id_shape(host, cid, ok):
    """識別子は英数字と _ と - で、1 から 64 文字であること。"""
    assert host(f"valid {cid}".rstrip()) == ("yes" if ok else "no")
    if cid:
        # 形の違う識別子では、所有者がいなくても取れない
        assert host(f"acquire {cid} 1 0") == ("yes" if ok else "no")


# ---- 起動のときのボタン ----


def test_the_button_is_read_only_after_the_pull_up_settles(host):
    """起動のときのボタンは、待ってから読むこと。

    プルアップを有効にした直後は、線の容量を充電しきる前に 0 を読みうる。押して
    いないのに押したと読むと、診断モードに入ってコントローラーとして接続しない。
    """
    held, steps = host.boot(0, 0)
    assert held
    assert steps == ["d20", "r0", "d20", "r0"]


@pytest.mark.parametrize("levels, held", [
    ((1,), False),        # 押していない
    ((0, 1), False),      # 1 回だけ 0 を読んだ(線の揺れ)
    ((0, 0), True),       # 押したまま
])
def test_the_button_counts_as_held_only_when_read_twice(host, levels, held):
    """間を空けて 2 回とも押されているときだけ、押したとみなすこと。"""
    assert host.boot(*levels)[0] is held
