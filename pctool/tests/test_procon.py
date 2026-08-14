"""Pro コン互換プロトコル(C 実装)を模擬 Switch から駆動する検証。

実機キャプチャで観測された初期化シーケンスを再生し、応答を仕様に照合する。
gcc が無い環境では skip。
"""
import subprocess
from pathlib import Path

import pytest

from switchctl import binfmt, switchsim
from switchctl.switchsim import (
    PAIRING_PAYLOAD, ProtocolViolation, handshake_sequence,
    parse_input_report, unpack_stick_calibration, verify_reply,
)
from tests.test_hostc import CORE, find_gcc

MAC = bytes([0x04, 0x03, 0xD6, 0x00, 0x00, 0x01])


@pytest.fixture(scope="session")
def procon_exe(tmp_path_factory):
    gcc = find_gcc()
    if gcc is None:
        pytest.skip("gcc が見つかりません")
    del tmp_path_factory
    # リポジトリ内に置く理由は test_hostc.host_exe と同じ(誤検知対策)
    out = CORE.parents[2] / "build" / "hosttest" / "procon_host.exe"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [gcc, "-O2", "-std=c11", "-Wall", "-Werror",
           "-I", str(CORE / "include"),
           str(CORE / "pademu_procon.c"), str(CORE / "pademu_hidpad.c"),
           str(CORE / "pademu_tx.c"), str(CORE / "pademu_usb_desc.c"),
           str(CORE / "pademu_usb_desc_hidpad.c"),
           str(CORE / "host" / "procon_host.c"),
           "-o", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"C ビルド失敗:\n{res.stderr}"
    return out


class Device:
    """C 実装のプロセスを「デバイス」として扱うラッパ。"""

    def __init__(self, exe: Path):
        self.proc = subprocess.Popen(
            [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)

    def _cmd(self, line: str) -> str:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline().strip()

    def send_output(self, data: bytes) -> bytes | None:
        r = self._cmd("out " + data.hex())
        if r == "none":
            return None
        assert r.startswith("in "), r
        return bytes.fromhex(r[3:])

    def set_state(self, buttons=0, lx=0, ly=0, rx=0, ry=0,
                  gx=0, gy=0, gz=0, ax=0, ay=0, az=0) -> None:
        r = self._cmd(f"state {buttons} {lx} {ly} {rx} {ry} "
                      f"{gx} {gy} {gz} {ax} {ay} {az}")
        assert r == "ok", r

    def input_report(self) -> bytes:
        r = self._cmd("input")
        assert r.startswith("in "), r
        return bytes.fromhex(r[3:])

    def hidpad_report(self) -> bytes:
        r = self._cmd("hidpad")
        assert r.startswith("in "), r
        return bytes.fromhex(r[3:])

    def breadcrumb(self) -> int:
        r = self._cmd("bc")
        return int(r.split()[1], 16)

    def tx_out(self, data: bytes) -> bool | None:
        """出力レポートを処理し、応答は送出キューへ積む(USB 統合を模した経路)。"""
        r = self._cmd("txout " + data.hex())
        if r == "none":
            return None
        assert r.startswith("queued "), r
        return r.split()[1] == "1"

    def tx_next(self) -> bytes | None:
        r = self._cmd("txnext")
        if r == "empty":
            return None
        assert r.startswith("in "), r
        return bytes.fromhex(r[3:])

    def tx_fail(self) -> int:
        """送出しようとしたが失敗した場合(エンドポイントが空かない等)。"""
        r = self._cmd("txfail")
        assert r.startswith("fail "), r
        return int(r.split()[1])

    def tx_bad(self) -> None:
        """レポートが壊れていて送らずに捨てた場合。"""
        assert self._cmd("txbad") == "discarded"

    def tx_stats(self) -> dict:
        r = self._cmd("txstats").split()
        return {"replies": int(r[1]), "inputs": int(r[2]),
                "dropped": int(r[3]), "pending": int(r[4])}

    def tx_stats2(self) -> dict:
        r = self._cmd("txstats2").split()
        return {"failed_replies": int(r[1]), "dropped_inputs": int(r[2]),
                "bad_reports": int(r[3]), "retry": int(r[4])}

    def descriptor_info(self) -> tuple[int, int, int, int]:
        r = self._cmd("desc").split()
        return int(r[1]), int(r[2]), int(r[3], 16), int(r[4], 16)

    def led(self) -> tuple[int, int]:
        r = self._cmd("led")
        _, val, calls = r.split()
        return int(val, 16), int(calls)

    def pair(self) -> tuple[int, int]:
        """ペアリングの観測値 (受けた回数, 直近フェーズ)。"""
        r = self._cmd("pair")
        _, reqs, step = r.split()
        return int(reqs), int(step, 16)

    def host_info(self) -> bytes | None:
        """本体識別子の控え(取り出すと消える)。無ければ None。"""
        r = self._cmd("hostinfo")
        if r == "none":
            return None
        _, _n, hexs = r.split()
        return bytes.fromhex(hexs)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def device(procon_exe):
    d = Device(procon_exe)
    yield d
    d.close()


def run_handshake(device: Device) -> None:
    for exp in handshake_sequence(MAC):
        reply = device.send_output(exp.out)
        verify_reply(exp, reply)


def test_full_handshake_sequence(device):
    """実機順序の初期化シーケンスを完走し、全応答が仕様に一致すること。"""
    run_handshake(device)
    bc = device.breadcrumb()
    required = (switchsim_bc("HS_STATUS") | switchsim_bc("HS_SHAKE")
                | switchsim_bc("HS_HIDONLY") | switchsim_bc("SUB_DEVINFO")
                | switchsim_bc("SUB_MODE") | switchsim_bc("SUB_SPI")
                | switchsim_bc("SUB_LED") | switchsim_bc("SUB_IMU")
                | switchsim_bc("SUB_RUMBLE"))
    assert bc & required == required, f"到達段階が不足: {bc:#x}"


# pademu_procon.h の pademu_procon_bc_t と対応(順序が正)
_BC = {
    "HS_STATUS": 0, "HS_SHAKE": 1, "HS_BAUD": 2, "HS_HIDONLY": 3,
    "SUB_DEVINFO": 4, "SUB_MODE": 5, "SUB_SPI": 6, "SUB_LED": 7,
    "SUB_IMU": 8, "SUB_RUMBLE": 9, "INPUT_SENT": 10,
}


def switchsim_bc(name: str) -> int:
    return 1 << _BC[name]


def test_player_lights_callback(device):
    """プレイヤーLED 通知が届くこと(1P/2P 判別の土台)。"""
    run_handshake(device)
    val, calls = device.led()
    assert (val, calls) == (0x01, 1)


# 2026-08-06 の実測: 本体側にこの個体の登録記録が無いと、本体は新規ペアリング
# (arg[0]=0x01 + 本体 BT MAC(LE))を送ってくる。旧実装は全フェーズに固定
# 0x03 を返しており、これが完了しないと本体はコントローラー登録を行わず
# **全ての入力を無視する**(自動・手動とも Switch 無反応の根因)。
# 応答形式は dekuNukem の BT 資料に従う(実機プロコンでの実測は未採取)。
# 実測時の Switch 2 の要求そのもの(引数先頭 8 バイト)
PAIR_PHASE1_SWITCH2 = bytes([0x01, 0x9D, 0xF7, 0x6A, 0x6A, 0x30, 0x4C, 0x3C])


def test_pairing_phase1_returns_own_mac(device):
    """新規ペアリング開始(arg 0x01)に、自機 MAC(LE)を名乗り返すこと。

    固定 0x03 応答では本体が同じ要求を 100〜400ms 間隔で再送し続け、
    登録未完のまま入力が無視される(2026-08-06 の実測)。"""
    run_handshake(device)
    reply = device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + PAIR_PHASE1_SWITCH2)
    assert reply[13] == 0x81                      # ACK(データ付き)
    assert reply[14] == 0x01                      # サブコマンドのエコー
    assert reply[15] == 0x01                      # フェーズのエコー
    assert reply[16:22] == bytes(reversed(MAC))   # 自機 MAC(LE)


def test_pairing_phase2_ltk_is_stable_and_xored(device):
    """LTK 要求(arg 0x02)に、毎回同じ 16 バイトを 0xAA XOR で返すこと。

    値が呼ぶたびに変わると、本体側の保存と食い違って再ペアリングが
    終わらなくなる。"""
    run_handshake(device)
    out = bytes([0x01, 0x00]) + bytes(8) + bytes([0x01, 0x02])
    r1 = device.send_output(out)
    r2 = device.send_output(out)
    assert r1[13] == 0x81 and r1[15] == 0x02
    ltk = r1[16:32]
    assert ltk == r2[16:32]                       # 何度でも同じ
    expected = bytes(((MAC[i % 6] + i) & 0xFF) ^ 0xAA for i in range(16))
    assert ltk == expected                        # 個体(MAC)から決まる


def test_pairing_phase3_and_known_host_keep_capture_reply(device):
    """保存(arg 0x03)と既知本体の記録手渡し(arg 0x04)は従来どおり 0x03。

    0x04 への 0x03 応答は実機プロコンのキャプチャ
    (bypass_procon_log.txt)と全バイト一致で検証済みの唯一の経路。
    フェーズ対応の追加で壊してはならない。"""
    run_handshake(device)
    for phase_args in (bytes([0x03]), PAIRING_PAYLOAD):
        reply = device.send_output(
            bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + phase_args)
        assert reply[13] == 0x81
        assert reply[15] == 0x03
        assert reply[16] == 0x00


def test_pairing_phase23_do_not_clobber_host_identity(device):
    """フェーズ 0x02/0x03 は本体識別子の控えを上書きしないこと。

    実機の出力レポートはゼロ埋めで届くため、0x02/0x03 まで控えると
    識別子が「02+ゼロ」になり、どの本体でも同じ値になって命名が壊れる。"""
    run_handshake(device)                          # 0x04 で控えが入る
    # フェーズ 02(引数の残りはゼロ埋め=実機と同じ形)を受けても控えは不変
    device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x01, 0x02]) + bytes(36))
    device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x01, 0x03]) + bytes(36))
    seen = device.host_info()
    assert seen is not None
    assert seen[:8] == PAIRING_PAYLOAD[:8]         # 0x04 の中身のまま
    # 取り出した後にフェーズ 01 が来れば、新しい識別子として控え直す
    assert device.host_info() is None
    device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + PAIR_PHASE1_SWITCH2)
    assert device.host_info() == PAIR_PHASE1_SWITCH2


def test_pairing_counters_track_requests(device):
    """ペアリングの回数と直近フェーズが観測できること(登録未完の検知用)。

    「pair_step が 1 のまま回数だけ増える」= 本体が新規ペアリングを
    受理せず再送している、を PC 側から切り分けられるようにする。"""
    run_handshake(device)                          # キャプチャ経路で 1 回(0x04)
    reqs0, step0 = device.pair()
    assert step0 == 0x04
    for _ in range(3):
        device.send_output(
            bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + PAIR_PHASE1_SWITCH2)
    reqs, step = device.pair()
    assert reqs == reqs0 + 3
    assert step == 0x01


def test_stick_calibration_is_linear(device):
    """返す較正値が中心 2048・全域直線であること(生値の刻みを潰さない)。"""
    run_handshake(device)
    reply = device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x10])
        + bytes([0x3D, 0x60, 0x00, 0x00, 0x19]))
    assert reply[13] == 0x90
    data = reply[20:20 + 0x19]
    left = unpack_stick_calibration(data[0:9], left=True)
    right = unpack_stick_calibration(data[9:18], left=False)
    for cal in (left, right):
        assert cal["center"] == (2048, 2048)
        assert cal["min"] == (0, 0)
        assert cal["max"] == (4095, 4095)
    assert data[19:22] == bytes([0x32, 0x32, 0x32])  # 本体色


def test_spi_size_echo_matches_actual_data(device):
    """要求サイズが上限(0x1D)を超えても、エコーする長さと実データ長が一致すること。

    食い違うと「エコーされた長さぶんデータがある」前提のホストが末尾を誤読する。
    """
    run_handshake(device)
    for requested, expected in ((0x1D, 0x1D), (0x1E, 0x1D), (0xFF, 0x1D)):
        reply = device.send_output(
            bytes([0x01, 0x00]) + bytes(8) + bytes([0x10])
            + bytes([0x3D, 0x60, 0x00, 0x00, requested]))
        assert reply[15:19] == bytes([0x3D, 0x60, 0x00, 0x00])  # アドレスはそのまま
        assert reply[19] == expected


def test_spi_unknown_region_is_ff(device):
    """未定義領域は 0xFF(未書き込み)。"""
    run_handshake(device)
    reply = device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x10])
        + bytes([0x00, 0x70, 0x00, 0x00, 0x18]))   # 0x7000: どの領域でもない
    assert reply[20:20 + 0x18] == b"\xff" * 0x18


def test_spi_user_calib_region_matches_capture(device):
    """ユーザー較正領域(0x8010〜)が実機の実測ログと同じ形であること。

    スティック部分(0x8010〜0x8025)は未設定(0xFF)、0x8026 にユーザーIMU
    較正の目印 b2 a1、0x8028 に較正 24B。本体はこの目印を見て 0x8028 の
    ユーザー較正を使う(実測ログで確認済みの経路)。値は工場較正 0x6020 と
    同一なので、どちらを使われても換算は同じ直線になる(生値原則)。"""
    run_handshake(device)
    reply = device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x10])
        + bytes([0x10, 0x80, 0x00, 0x00, 0x18]))
    assert reply[20:20 + 22] == b"\xff" * 22
    assert reply[42:44] == bytes([0xB2, 0xA1])
    r_user = device.send_output(
        bytes([0x01, 0x01]) + bytes(8) + bytes([0x10])
        + bytes([0x28, 0x80, 0x00, 0x00, 0x18]))
    r_factory = device.send_output(
        bytes([0x01, 0x02]) + bytes(8) + bytes([0x10])
        + bytes([0x20, 0x60, 0x00, 0x00, 0x18]))
    assert r_user[20:20 + 24] == r_factory[20:20 + 24]  # ユーザー較正 = 工場較正


def test_input_report_roundtrip(device):
    """入力レポートの各フィールドが送った生値と 1:1 で一致すること。"""
    run_handshake(device)
    buttons = ((1 << binfmt.BUTTONS["A"]) | (1 << binfmt.BUTTONS["ZR"])
               | (1 << binfmt.BUTTONS["HOME"]) | (1 << binfmt.BUTTONS["DL"]))
    device.set_state(buttons=buttons, lx=-2048, ly=2047, rx=1, ry=-1,
                     gx=300, gy=-300, gz=1, ax=-1, ay=0, az=4096)
    rep = parse_input_report(device.input_report())
    # 共有バイトは HOME(0x10)に加えて有線給電ビット(0x80。実測で常時1)
    assert rep["buttons"] == (0x08 | 0x80, 0x10 | 0x80, 0x08)  # A|ZR, HOME, DL
    assert rep["left"] == (0, 4095)      # -2048/+2047 → 0/4095(+2048 の 1:1)
    assert rep["right"] == (2049, 2047)
    for s in rep["imu"]:
        assert s["accel"] == (-1, 0, 4096)
        assert s["gyro"] == (300, -300, 1)


def test_stick_raw_value_resolution(device):
    """+1 の入力差がワイヤ上でも +1 になること(分解能を落とさない)。"""
    run_handshake(device)
    seen = []
    for v in (-2048, -2047, 0, 1, 2046, 2047):
        device.set_state(lx=v)
        seen.append(parse_input_report(device.input_report())["left"][0])
    assert seen == [0, 1, 2048, 2049, 4094, 4095]


def test_timer_increments_and_wraps(device):
    """timer バイトが毎レポート増加し 0xFF で巻き戻ること。"""
    run_handshake(device)
    first = parse_input_report(device.input_report())["timer"]
    second = parse_input_report(device.input_report())["timer"]
    assert second == (first + 1) % 256


def test_unknown_subcommand_is_acked(device):
    """未知のサブコマンドにも ACK を返し、接続を落とさないこと。"""
    run_handshake(device)
    reply = device.send_output(bytes([0x01, 0x00]) + bytes(8) + bytes([0x7E]))
    assert reply is not None
    assert reply[0] == 0x21 and reply[13] & 0x80 and reply[14] == 0x7E


def test_rumble_only_report_has_no_reply(device):
    """0x10(rumble のみ)には応答しないこと。"""
    run_handshake(device)
    assert device.send_output(bytes([0x10]) + bytes(9)) is None


def test_verify_reply_detects_violation():
    """検証器自体が違反を検出できること(テストの妥当性確認)。"""
    exp = handshake_sequence(MAC)[1]
    with pytest.raises(ProtocolViolation):
        verify_reply(exp, bytes(64))
    with pytest.raises(ProtocolViolation):
        verify_reply(exp, None)


# ---- USB 送出の仲裁(レビュー指摘 B)----

def test_replies_are_never_dropped_under_load(device):
    """定期入力レポートを送り続けながらサブコマンドを連投しても応答が落ちないこと。

    TinyUSB は送信中のレポートを黙って捨てるため、応答を優先キューに積み
    「空いたら応答→無ければ定期入力」の順に送る仲裁が要る(設計 pademu_tx)。
    """
    run_handshake(device)
    sent = 0
    for i in range(20):
        # ホストがサブコマンドを送る(応答はキューへ)
        assert device.tx_out(bytes([0x01, i & 0x0F]) + bytes(8) + bytes([0x02])) is True
        sent += 1
        # IN が空くたびに1つ送出(応答が優先される)
        rep = device.tx_next()
        assert rep is not None and rep[0] == 0x21, "応答より先に定期入力が出た"
        assert rep[14] == 0x02
    stats = device.tx_stats()
    assert stats["dropped"] == 0
    assert stats["replies"] == sent


def test_periodic_input_is_filler_when_no_reply(device):
    """応答が無いときは定期入力レポートが送られること。"""
    run_handshake(device)
    for _ in range(3):
        rep = device.tx_next()
        assert rep is not None and rep[0] == 0x30
    assert device.tx_stats()["replies"] == 0


def test_reply_takes_priority_over_periodic_input(device):
    """応答が積まれていれば、定期入力より先に応答が出ること。"""
    run_handshake(device)
    device.tx_next()  # 定期入力が出る状態から開始
    device.tx_out(bytes([0x01, 0x00]) + bytes(8) + bytes([0x04]))
    rep = device.tx_next()
    assert rep[0] == 0x21 and rep[13] == 0x83  # 0x04 の ACK
    assert device.tx_next()[0] == 0x30         # 応答を出し切ったら定期入力へ


def test_reply_queue_overflow_is_counted(device):
    """キューが溢れた場合は黙って捨てず、数えられること(異常ログの対象)。"""
    run_handshake(device)
    results = []
    for i in range(6):  # 深さ 4 を超えて積む
        results.append(device.tx_out(bytes([0x01, i]) + bytes(8) + bytes([0x02])))
    assert results[:4] == [True] * 4
    assert results[4:] == [False, False]
    assert device.tx_stats()["dropped"] == 2


def test_failed_reply_is_resent_not_lost(device):
    """送出に失敗した応答は消えず、次の周期でもう一度出ること。

    以前は pademu_tx_next() の時点でキューから外して「送った」と数えていたため、
    tud_hid_report() が false を返すと応答はどこにも残らず、数にも入らないまま
    消えていた(Switch 側は応答を待ち続ける)。
    """
    run_handshake(device)
    device.tx_out(bytes([0x01, 0x00]) + bytes(8) + bytes([0x04]))
    before = device.tx_stats()["replies"]
    assert device.tx_fail() > 0                    # 送ろうとしたが失敗
    assert device.tx_stats()["replies"] == before  # 送った数は増えない
    assert device.tx_stats()["pending"] == 1       # キューに残っている
    assert device.tx_stats2()["failed_replies"] == 1
    rep = device.tx_next()                         # 次の周期で再送
    assert rep[0] == 0x21 and rep[13] == 0x83
    assert device.tx_stats()["pending"] == 0
    assert device.tx_stats2()["retry"] == 0        # 成功したら再送回数は戻る


def test_reply_that_never_sends_is_dropped_and_counted(device):
    """送れない応答でキューが詰まったままにならず、捨てた数が残ること。"""
    run_handshake(device)
    device.tx_out(bytes([0x01, 0x00]) + bytes(8) + bytes([0x04]))
    for _ in range(8):     # PADEMU_TX_MAX_RETRY 回
        device.tx_fail()
    assert device.tx_stats()["pending"] == 0       # 詰まらせない
    assert device.tx_stats()["dropped"] == 1       # 捨てたことは残る
    assert device.tx_stats2()["failed_replies"] == 8


def test_failed_periodic_input_is_counted_not_silent(device):
    """定期入力の送出失敗も数えること(1フレーム落ちたという事実は残す)。"""
    run_handshake(device)
    assert device.tx_fail() > 0        # キューは空なので定期入力が対象
    assert device.tx_stats2()["dropped_inputs"] == 1
    assert device.tx_stats()["inputs"] == 0


def test_discarded_bad_report_is_counted_and_removed(device):
    """レポートIDが壊れた応答は捨てるが、捨てた事実は数に残ること。"""
    run_handshake(device)
    device.tx_out(bytes([0x01, 0x00]) + bytes(8) + bytes([0x04]))
    device.tx_bad()
    assert device.tx_stats()["pending"] == 0
    assert device.tx_stats2()["bad_reports"] == 1
    assert device.tx_stats()["replies"] == 0


def test_hid_descriptor_is_preserved_in_repo(device):
    """実測の HID レポートディスクリプタ 203 バイトがコードに保全されていること。"""
    procon_len, hidpad_len, first, last = device.descriptor_info()
    assert procon_len == 203
    assert (first, last) == (0x05, 0xC0)  # Usage Page で始まり End Collection で終わる
    assert hidpad_len == 86


# ---- 保険モード(HID ゲームパッド方式)----

HIDPAD = {"Y": 1 << 0, "B": 1 << 1, "A": 1 << 2, "X": 1 << 3,
          "L": 1 << 4, "R": 1 << 5, "ZL": 1 << 6, "ZR": 1 << 7,
          "MINUS": 1 << 8, "PLUS": 1 << 9, "LCLICK": 1 << 10,
          "RCLICK": 1 << 11, "HOME": 1 << 12, "CAPTURE": 1 << 13}


def parse_hidpad(r: bytes) -> dict:
    assert len(r) == 8
    return {"buttons": r[0] | (r[1] << 8), "hat": r[2],
            "lx": r[3], "ly": r[4], "rx": r[5], "ry": r[6]}


def test_hidpad_button_mapping(device):
    """論理ボタンが HORIPAD 系の割り当てへ正しく変換されること。"""
    device.set_state(buttons=(1 << binfmt.BUTTONS["A"]) | (1 << binfmt.BUTTONS["ZR"])
                     | (1 << binfmt.BUTTONS["CAPTURE"]))
    rep = parse_hidpad(device.hidpad_report())
    assert rep["buttons"] == HIDPAD["A"] | HIDPAD["ZR"] | HIDPAD["CAPTURE"]
    assert rep["hat"] == 8  # 中立


def test_hidpad_hat_directions(device):
    """十字キーが HAT の 8 方向へ変換され、相反する同時押しは中立になること。"""
    cases = {
        ("DU",): 0, ("DU", "DR"): 1, ("DR",): 2, ("DD", "DR"): 3,
        ("DD",): 4, ("DD", "DL"): 5, ("DL",): 6, ("DU", "DL"): 7,
        (): 8, ("DU", "DD"): 8, ("DL", "DR"): 8,
    }
    for names, expected in cases.items():
        mask = 0
        for n in names:
            mask |= 1 << binfmt.BUTTONS[n]
        device.set_state(buttons=mask)
        assert parse_hidpad(device.hidpad_report())["hat"] == expected, names


def test_hidpad_stick_precision_loss_is_documented(device):
    """スティックは 8bit へ丸められる(保険モードの構造的制約の実証)。"""
    device.set_state(lx=0, ly=0, rx=0, ry=0)
    rep = parse_hidpad(device.hidpad_report())
    assert (rep["lx"], rep["ly"], rep["rx"], rep["ry"]) == (128, 128, 128, 128)

    device.set_state(lx=-2048, ly=2047, rx=2047, ry=-2048)
    rep = parse_hidpad(device.hidpad_report())
    assert (rep["lx"], rep["ly"]) == (0, 0)      # 左端 / 上端
    assert (rep["rx"], rep["ry"]) == (255, 255)  # 右端 / 下端

    # 同じ 16 刻みの区間に入る生値は 8bit では区別できない(分解能 1/16)
    # (lx+2048) を 16 で割った商が値になるため 100..111 は同一、112 で 1 増える
    seen = []
    for v in (100, 111, 112):
        device.set_state(lx=v)
        seen.append(parse_hidpad(device.hidpad_report())["lx"])
    assert seen == [134, 134, 135]


def test_short_and_malformed_output_reports(device):
    """短い・壊れた出力レポートでクラッシュしないこと。"""
    run_handshake(device)
    assert device.send_output(b"") is None
    assert device.send_output(bytes([0x80])) is None
    assert device.send_output(bytes([0xFF, 0x00])) is None
    reply = device.send_output(bytes([0x01, 0x00]) + bytes(8) + bytes([0x10, 0x00]))
    assert reply is not None and reply[13] == 0x00  # 引数不足は NACK


def test_imu_calibration_defines_the_scale():
    """IMU 較正値が、生値→物理量の換算をこちらで決めていること。

    公開されている変換式(hid-nintendo 等)は
        accel[G]  = raw × 4.0   ÷ (accel係数 - 原点)
        gyro[dps] = raw × 936.0 ÷ (gyro係数  - 原点)
    で、分母はコントローラーが自己申告した値。つまり係数はこちらの選択で決まる。
    角度指定は作らないので換算そのものは運用に影響しないが、この値は
    **出せる回転速度の上限**を決める(係数を小さくすると上限が上がる)。
    意図せず変わると挙動が変わるので固定して見張る。
    """
    import re
    src = (CORE / "pademu_procon.c").read_text(encoding="utf-8")
    acc = int(re.search(r"#define IMU_ACC_SENS\s+(\d+)", src).group(1))
    gyro = int(re.search(r"#define IMU_GYRO_SENS\s+(\d+)", src).group(1))
    assert acc == 16384 and gyro == 13371, (acc, gyro)
    assert abs(4.0 / acc - 0.000244140625) < 1e-9
    assert abs(936.0 / gyro - 0.0700022) < 1e-6, "1 生値 ≒ 0.0700 dps のはず"


def test_imu_is_zero_until_host_enables(device):
    """IMU 36B は本体がサブコマンド 0x40 で有効化するまでゼロ埋め(実在品と同じ)。"""
    run_handshake(device)
    device.set_state(gx=300, gy=-300, az=4096)
    # いったん無効化(0x40 arg=0)
    device.send_output(bytes([0x01, 0x00]) + bytes(8) + bytes([0x40, 0x00]))
    rep = parse_input_report(device.input_report())
    for s in rep["imu"]:
        assert s["accel"] == (0, 0, 0) and s["gyro"] == (0, 0, 0), rep["imu"]
    # 有効化(0x40 arg=1)すると値が現れる
    device.send_output(bytes([0x01, 0x01]) + bytes(8) + bytes([0x40, 0x01]))
    rep = parse_input_report(device.input_report())
    assert rep["imu"][0]["gyro"] == (300, -300, 0)
    assert rep["imu"][0]["accel"] == (0, 0, 4096)
