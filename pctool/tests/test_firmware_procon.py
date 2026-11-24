"""Pro コントローラー互換プロトコル(C 実装)を模擬 Switch から駆動する検証。

実機キャプチャーで観測された初期化シーケンスを再生し、応答を仕様に照合する。
C のビルドと gcc がないときの扱いは conftest.py の procon_host_exe / gcc fixture。
"""

import re
from itertools import pairwise

import pytest

from padcue import binfmt
from tests.conftest import CORE
from tests.helpers import MAC, Device, run_handshake
from tests.switchsim import (
    PAIRING_PAYLOAD,
    ProtocolViolation,
    handshake_sequence,
    parse_input_report,
    unpack_stick_calibration,
    verify_reply,
)


@pytest.fixture
def device(procon_host_exe):
    d = Device(procon_host_exe)
    yield d
    d.close()


# pademu_procon.h の pademu_procon_bc_t のビット番号と同じ
_BC = {
    "HS_STATUS": 0, "HS_SHAKE": 1, "HS_BAUD": 2, "HS_HIDONLY": 3,
    "SUB_DEVINFO": 4, "SUB_MODE": 5, "SUB_SPI": 6, "SUB_LED": 7,
    "SUB_IMU": 8, "SUB_RUMBLE": 9, "INPUT_SENT": 10,
}


def _bit(name: str) -> int:
    return 1 << _BC[name]


def test_full_handshake_sequence(device):
    """実機順序の初期化シーケンスを完了し、全応答が仕様に一致すること。"""
    run_handshake(device)
    bc = device.breadcrumb()
    required = (_bit("HS_STATUS") | _bit("HS_SHAKE")
                | _bit("HS_HIDONLY") | _bit("SUB_DEVINFO")
                | _bit("SUB_MODE") | _bit("SUB_SPI")
                | _bit("SUB_LED") | _bit("SUB_IMU")
                | _bit("SUB_RUMBLE"))
    assert bc & required == required, f"到達段階が不足: {bc:#x}"


def test_player_lights_callback(device):
    """プレイヤー LED の通知を受け取れること(1P/2P の判別に使う)。"""
    run_handshake(device)
    val, calls = device.led()
    assert (val, calls) == (0x01, 1)


# 実測: 本体側にこの装置の登録記録がないと、本体は新規ペアリング
# (arg[0]=0x01 + 本体 BT MAC(LE))を送ってくる。全フェーズに固定 0x03 を
# 返すとこれが完了せず、本体はコントローラー登録を行わないまま
# すべての入力を無視する(自動・手動とも Switch が無反応になる)。
# 応答形式は dekuNukem の BT 資料に従う(実物の Pro コントローラーでの実測は未採取)。
# 実測時の Switch 2 の要求の形(引数先頭 8 バイト)。本体 BT MAC は公開できないので、
# 文書用に予約された値(00-00-5E-00-53-02)に置き換えてある(ここを本物にしても
# 動きは変わらない)
PAIR_PHASE1_SWITCH2 = bytes([0x01, 0x02, 0x53, 0x00, 0x5E, 0x00, 0x00, 0x3C])


def test_pairing_phase1_returns_own_mac(device):
    """新規ペアリング開始(arg 0x01)に、自機 MAC(LE)を返すこと。

    固定 0x03 応答では本体が同じ要求を 100〜400ms 間隔で再送し続け、
    登録未完のまま入力が無視される(実測)。"""
    run_handshake(device)
    reply = device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + PAIR_PHASE1_SWITCH2)
    assert reply[13] == 0x81                      # ACK(データ付き)
    assert reply[14] == 0x01                      # サブコマンドのエコー
    assert reply[15] == 0x01                      # フェーズのエコー
    assert reply[16:22] == bytes(reversed(MAC))   # 自機 MAC(LE)


def test_pairing_phase2_ltk_is_stable_and_xored(device):
    """LTK 要求(arg 0x02)に、毎回同じ 16 バイトを 0xAA XOR で返すこと。

    値が呼ぶたびに変わると、本体側の保存と一致せず、再ペアリングが
    終わらなくなる。"""
    run_handshake(device)
    out = bytes([0x01, 0x00]) + bytes(8) + bytes([0x01, 0x02])
    r1 = device.send_output(out)
    r2 = device.send_output(out)
    assert r1[13] == 0x81 and r1[15] == 0x02
    ltk = r1[16:32]
    assert ltk == r2[16:32]                       # 何度でも同じ
    expected = bytes(((MAC[i % 6] + i) & 0xFF) ^ 0xAA for i in range(16))
    assert ltk == expected                        # 装置(MAC)から決まる


def test_pairing_phase3_and_known_host_keep_capture_reply(device):
    """保存(arg 0x03)と登録済みの本体の情報を渡す(arg 0x04)には 0x03 を返すこと。

    0x04 への 0x03 応答は、実物の Pro コントローラーの通信の記録
    (bypass_procon_log.txt)と全バイト一致で裏付けのある唯一の経路。フェーズ別の応答を追加しても
    この 2 つは実測どおりに保つ。"""
    run_handshake(device)
    for phase_args in (bytes([0x03]), PAIRING_PAYLOAD):
        reply = device.send_output(
            bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + phase_args)
        assert reply[13] == 0x81
        assert reply[15] == 0x03
        assert reply[16] == 0x00


def test_pairing_phase23_do_not_clobber_host_identity(device):
    """フェーズ 0x02/0x03 は本体識別子の記録を上書きしないこと。

    実機の出力レポートはゼロ埋めで送られてくるため、0x02/0x03 まで記録すると
    識別子が「02+ゼロ」になり、どの本体でも同じ値になって、PC が本体を区別でき
    なくなる。"""
    run_handshake(device)                          # 0x04 で記録が入る
    # フェーズ 02(引数の残りはゼロ埋め=実機と同じ形)を受けても記録は不変
    device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x01, 0x02]) + bytes(36))
    device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x01, 0x03]) + bytes(36))
    seen = device.host_info()
    assert seen is not None
    assert seen[:8] == PAIRING_PAYLOAD[:8]         # 0x04 の中身のまま
    # 取り出した後にフェーズ 01 が来れば、新しい識別子として記録し直す
    assert device.host_info() is None
    device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + PAIR_PHASE1_SWITCH2)
    assert device.host_info() == PAIR_PHASE1_SWITCH2


def test_the_same_host_identity_is_recorded_once(device):
    """同じ本体の識別子をくり返し受けても、記録のフラグは 1 回しか立たないこと。

    本体にこの装置の登録がないと、登録が済むまで本体はフェーズ 01 を 100〜400ms
    ごとに送り続ける。そのたびに記録すると、装置の記録のリングが数分で埋まり、
    その後の USB の切断や中断の記録が捨てられる。内容が変われば記録する。
    """
    run_handshake(device)                          # 0x04 で記録が入る
    assert device.host_info() is not None
    for _ in range(3):
        device.send_output(
            bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + PAIRING_PAYLOAD)
    assert device.host_info() is None, "同じ内容でもう一度記録した"
    for _ in range(3):
        device.send_output(
            bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + PAIR_PHASE1_SWITCH2)
    assert device.host_info() == PAIR_PHASE1_SWITCH2
    assert device.host_info() is None, "同じ内容でもう一度記録した"


def test_pairing_counters_track_requests(device):
    """ペアリングの回数と直近のフェーズを、PC から読めること(登録未完の検知用)。

    pair_step が 1 のまま回数だけ増えているときは、本体が新規ペアリングを
    受け付けず、同じ要求を再送している。"""
    run_handshake(device)                          # キャプチャー経路で 1 回(0x04)
    reqs0, step0 = device.pair()
    assert step0 == 0x04
    for _ in range(3):
        device.send_output(
            bytes([0x01, 0x00]) + bytes(8) + bytes([0x01]) + PAIR_PHASE1_SWITCH2)
    reqs, step = device.pair()
    assert reqs == reqs0 + 3
    assert step == 0x01


def test_stick_calibration_is_linear(device):
    """返す較正値が中心 2048・全域直線であること(生値の分解能を失わない)。"""
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

    一致しないと「エコーされた長さぶんデータがある」前提のホストが末尾を誤読する。
    """
    run_handshake(device)
    for requested, expected in ((0x1D, 0x1D), (0x1E, 0x1D), (0xFF, 0x1D)):
        reply = device.send_output(
            bytes([0x01, 0x00]) + bytes(8) + bytes([0x10])
            + bytes([0x3D, 0x60, 0x00, 0x00, requested]))
        assert reply[15:19] == bytes([0x3D, 0x60, 0x00, 0x00])  # アドレスはそのまま
        assert reply[19] == expected


def test_spi_unknown_region_is_ff(device):
    """未定義領域は 0xFF(未書き込み)であること。"""
    run_handshake(device)
    reply = device.send_output(
        bytes([0x01, 0x00]) + bytes(8) + bytes([0x10])
        + bytes([0x00, 0x70, 0x00, 0x00, 0x18]))   # 0x7000: どの領域でもない
    assert reply[20:20 + 0x18] == b"\xff" * 0x18


def test_spi_user_calib_region_matches_capture(device):
    """ユーザー較正領域(0x8010〜)が実機の実測ログと同じ形であること。

    スティック部分(0x8010〜0x8025)は未設定(0xFF)、0x8026 にユーザー IMU
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
    # 共有バイトは HOME(0x10)に加えて有線給電ビット(0x80。実測で常時 1)
    assert rep["buttons"] == (0x08 | 0x80, 0x10 | 0x80, 0x08)  # A|ZR, HOME, DL
    assert rep["left"] == (0, 4095)      # -2048/+2047 → 0/4095(+2048 の 1:1)
    assert rep["right"] == (2049, 2047)
    for s in rep["imu"]:
        assert s["accel"] == (-1, 0, 4096)
        assert s["gyro"] == (300, -300, 1)


def test_stick_raw_value_resolution(device):
    """+1 の入力差がワイヤ上でも +1 になること(分解能を下げない)。"""
    run_handshake(device)
    seen = []
    for v in (-2048, -2047, 0, 1, 2046, 2047):
        device.set_state(lx=v)
        seen.append(parse_input_report(device.input_report())["left"][0])
    assert seen == [0, 1, 2048, 2049, 4094, 4095]


def test_timer_increments_and_wraps(device):
    """timer バイトが毎レポート増加し 0xFF で巻き戻ること。

    256 より多く読むので、開始の値によらず 0xFF から 0x00 への巻き戻りを通る。
    """
    run_handshake(device)
    timers = [parse_input_report(device.input_report())["timer"]
              for _ in range(300)]
    assert all(b == (a + 1) % 256 for a, b in pairwise(timers)), timers
    assert 0xFF in timers[:-1], timers


def test_unknown_subcommand_is_acked(device):
    """未知のサブコマンドにも ACK を返し、接続を切らないこと。"""
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


# ---- USB 送信の仲裁 ----

def test_replies_are_not_dropped_one_after_another(device):
    """サブコマンドを 20 回続けて送っても、応答が 1 つずつ出て、欠けないこと。

    TinyUSB は送信中のレポートを黙って捨てるため、応答を優先キューに入れ
    「空いたら応答、なければ定期入力」の順に送る仲裁が必要である
    (docs/design/firmware-architecture.md の「IN のエンドポイントの取り合い」)。
    """
    run_handshake(device)
    sent = 0
    for i in range(20):
        # ホストがサブコマンドを送る(応答はキューへ)
        assert device.tx_out(bytes([0x01, i & 0x0F]) + bytes(8) + bytes([0x02])) is True
        sent += 1
        # IN が空くたびに 1 つ送信(応答が優先される)
        rep = device.tx_next()
        assert rep is not None and rep[0] == 0x21, "応答より先に定期入力が出た"
        assert rep[14] == 0x02
    stats = device.tx_stats()
    assert stats["dropped"] == 0
    assert stats["replies"] == sent


def test_periodic_input_is_filler_when_no_reply(device):
    """応答がないときは定期入力レポートが送られること。"""
    run_handshake(device)
    for _ in range(3):
        rep = device.tx_next()
        assert rep is not None and rep[0] == 0x30
    assert device.tx_stats()["replies"] == 0


def test_reply_takes_priority_over_periodic_input(device):
    """応答がキューに入っていれば、定期入力より先に応答が出ること。"""
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
    for i in range(6):  # 深さ 4 を超えて入れる
        results.append(device.tx_out(bytes([0x01, i]) + bytes(8) + bytes([0x02])))
    assert results[:4] == [True] * 4
    assert results[4:] == [False, False]
    assert device.tx_stats()["dropped"] == 2


def test_failed_reply_is_resent_not_lost(device):
    """送信に失敗した応答は消えず、次の送信でもう一度出ること。

    pademu_tx_next() の時点でキューから外して「送った」と数えると、
    tud_hid_report() が false を返したときに応答はどこにも残らず、数にも
    入らないまま消える(Switch 側は応答を待ち続ける)。
    """
    run_handshake(device)
    device.tx_out(bytes([0x01, 0x00]) + bytes(8) + bytes([0x04]))
    before = device.tx_stats()["replies"]
    assert device.tx_fail() > 0                    # 送ろうとしたが失敗
    assert device.tx_stats()["replies"] == before  # 送った数は増えない
    assert device.tx_stats()["pending"] == 1       # キューに残っている
    assert device.tx_stats2()["failed_replies"] == 1
    rep = device.tx_next()                         # 次の送信で再送
    assert rep[0] == 0x21 and rep[13] == 0x83
    assert device.tx_stats()["pending"] == 0
    assert device.tx_stats2()["retry"] == 0        # 成功したら再送回数は戻る


def test_reply_that_never_sends_is_dropped_and_counted(device):
    """送れない応答がキューに残り続けず、捨てた数が残ること。"""
    run_handshake(device)
    device.tx_out(bytes([0x01, 0x00]) + bytes(8) + bytes([0x04]))
    for _ in range(8):     # PADEMU_TX_MAX_RETRY 回
        device.tx_fail()
    assert device.tx_stats()["pending"] == 0       # キューに残さない
    assert device.tx_stats()["dropped"] == 1       # 捨てたことは残る
    assert device.tx_stats2()["failed_replies"] == 8


def test_failed_periodic_input_is_counted_not_silent(device):
    """定期の入力レポートの送信に失敗したことも数えること。"""
    run_handshake(device)
    assert device.tx_fail() > 0        # キューは空なので定期入力が対象
    assert device.tx_stats2()["dropped_inputs"] == 1
    assert device.tx_stats()["inputs"] == 0


def test_discarded_bad_report_is_counted_and_removed(device):
    """レポート ID が壊れた応答は捨てるが、捨てた事実は数に残ること。"""
    run_handshake(device)
    device.tx_out(bytes([0x01, 0x00]) + bytes(8) + bytes([0x04]))
    device.tx_bad()
    assert device.tx_stats()["pending"] == 0
    assert device.tx_stats2()["bad_reports"] == 1
    assert device.tx_stats()["replies"] == 0


def test_hid_descriptor_has_the_recorded_length_and_ends(device):
    """HID レポートディスクリプターが実測と同じ 203 バイトで、先頭が Usage Page、
    末尾が End Collection であること。中身の全バイトは見ない。"""
    procon_len, first, last = device.descriptor_info()
    assert procon_len == 203
    assert (first, last) == (0x05, 0xC0)  # Usage Page で始まり End Collection で終わる


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
        accel[G]  = raw × 4.0   ÷ (accel 係数 - 原点)
        gyro[dps] = raw × 936.0 ÷ (gyro 係数  - 原点)
    で、分母はコントローラーが自己申告した値。つまり係数はこちらの選択で決まる。
    角度指定は作らないので換算そのものは運用に影響しないが、この値は
    出せる回転速度の上限を決める(係数を小さくすると上限が上がる)。
    意図せず変わると挙動が変わるので固定して監視する。
    """
    src = (CORE / "pademu_procon.c").read_text(encoding="utf-8")
    acc = int(re.search(r"#define IMU_ACC_SENS\s+(\d+)", src).group(1))
    gyro = int(re.search(r"#define IMU_GYRO_SENS\s+(\d+)", src).group(1))
    # 換算すると 1 生値は約 0.000244 G と約 0.0700 dps になる
    assert acc == 16384 and gyro == 13371, (acc, gyro)


def test_imu_is_zero_until_host_enables(device):
    """IMU の 36 バイトは、本体がサブコマンド 0x40 で有効化するまでゼロであること
    (実物の Pro コントローラーと同じ)。
    """
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
