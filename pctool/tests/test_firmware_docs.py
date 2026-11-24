"""手順書が書いているファームウェアの挙動が、実装と一致していることを確認する。

文書の決まり(桁揃え・折り返し・リンク・図の文字幅)は test_docs.py が見る。
ここでは、文書が実装の挙動をどう説明しているかを見る。例えば、LED の色が何で
決まるかを文書と実装で違えると、利用者は実機の前で見比べて迷う。

利用者が実機の前で見比べるもの(LED の色・装置のボタン・ピン番号)は、
ずれると、装置が故障していると誤解させる。読むだけで確認できる形にしておく。
"""
from __future__ import annotations

import re

from padcue import binfmt, mockdevice, proto
from padcue.mockdevice import MockDevice, _Conn
from padcue.statustext import status_line
from tests.conftest import REPO

RUNBOOK = REPO / "docs" / "runbook.md"
GETTING_STARTED = REPO / "docs" / "getting-started.md"
COMM = REPO / "docs" / "specs" / "comm-protocol.md"
APP_STATE = REPO / "firmware" / "main" / "app_state.c"
APP_BUTTON = REPO / "firmware" / "main" / "app_button.c"
APP_CONFIG = REPO / "firmware" / "main" / "app_config.h"
APP_CTRL = REPO / "firmware" / "main" / "app_ctrl.c"
APP_ENGINE = REPO / "firmware" / "main" / "app_engine.c"
APP_USB = REPO / "firmware" / "main" / "app_usb.c"
APP_WIFI = REPO / "firmware" / "main" / "app_wifi.c"
MAIN_C = REPO / "firmware" / "main" / "main.c"


def says(doc, phrase: str) -> bool:
    """文書が語句を含むか。段落の中の改行と字下げと空白を除いて比べるので、
    語句が行をまたいでいても見つかる。"""
    flat = re.sub(r"\s+", "", doc.read_text(encoding="utf-8"))
    return re.sub(r"\s+", "", phrase) in flat


def section(title: str) -> str:
    """導入と使い方の文書の、見出し 1 つぶんの本文。"""
    src = (GETTING_STARTED.read_text(encoding="utf-8")
           + RUNBOOK.read_text(encoding="utf-8"))
    i = src.index(f"### {title}")
    j = src.find("\n### ", i + 1)
    return src[i:j if j > 0 else len(src)]


def led_colors_in_firmware() -> list[str]:
    """apply_led() が状態ごとに点灯させる色(C 側のコメントから取る)。"""
    src = APP_STATE.read_text(encoding="utf-8")
    body = src[src.index("static void apply_led"):src.index("static void led_task")]
    return re.findall(r"case APP_STATE_\w+:.*?//\s*(\S+?)(?:\(|\s|$)", body)


def test_led_table_lists_every_state_the_firmware_shows():
    """LED の色表に、実装が点灯させるすべての色があること(過不足なし)。"""
    colors = led_colors_in_firmware()
    assert len(colors) >= 7, f"apply_led から色を取れていない: {colors}"
    table = section("LED の意味")
    missing = [c for c in colors if c not in table]
    assert not missing, (
        f"実装が点けるのに手順書の LED 表にない色: {missing}。"
        "app_state.c の apply_led と付録「LED の意味」を照合すること")
    # 状態の色のほかに、装置を判別するための白の速い点滅(IDENTIFY)と、診断モードの
    # 白のゆっくりした点滅(app_state_led_step)の行がある
    assert "白(速く点滅)" in table, "LED 表に、判別するための点滅の行がない"
    assert "白(ゆっくり点滅)" in table, "LED 表に、診断モードの点滅の行がない"
    # 表の行数(見出しと区切りを除く)が状態の数と、点滅の 2 行と合っていること
    rows = [ln for ln in table.split("\n")
            if ln.startswith("|") and not re.fullmatch(r"\|[-|]+\|", ln.strip())]
    assert len(rows) - 1 == len(colors) + 2, (
        f"LED 表の行数 {len(rows) - 1} と実装の状態数 {len(colors)} が合わない")


def test_the_status_quoted_in_the_runbook_is_this_firmware():
    """手順書に転記した padcue status の出力は、このツリーのファームウェアのバージョンで
    あること。

    最初の書き込み(1 つにまとめたイメージ)は ota_0 から起動する。
    """
    text = (GETTING_STARTED.read_text(encoding="utf-8")
            + RUNBOOK.read_text(encoding="utf-8"))
    config_h = (REPO / "firmware/main/app_config.h").read_text(encoding="utf-8")
    ver = re.search(r'#define PADEMU_FW_VERSION "([^"]+)"', config_h).group(1)
    assert status_line("ファームウェア", f"{ver} (ota_0)") in text
    assert f"padcue-firmware-{ver}.bin" in text
    assert f"padcue-firmware-{ver}-ota.bin" in text


def test_long_press_seconds_match_the_firmware():
    """装置のボタンの長押し秒数が実装と一致すること。"""
    m = re.search(r"#define LONG_PRESS_MS\s+(\d+)",
                  APP_BUTTON.read_text(encoding="utf-8"))
    assert m, "app_button.c に LONG_PRESS_MS がない"
    want = f"{int(m.group(1)) / 1000:g} 秒長押し"
    table = section("装置のボタン")
    assert want in table, f"手順書のボタン表に「{want}」がない(実装は {m.group(1)}ms)"


def test_diagnostic_mode_is_documented():
    """起動時長押しの診断モードが手順書にあること。

    無線もシリアルも使えないときに、装置を復旧する手段はこの診断モードだけである。
    手順書に書いていないと、その状態の人は入り方を知れない。
    """
    assert "app_button_held_at_boot()" in MAIN_C.read_text(encoding="utf-8"), \
        "main.c の診断モードがなくなっている(このテストを見直すこと)"
    table = section("装置のボタン")
    assert "診断モード" in table, "付録のボタン表に診断モードがない"


def test_the_diagnostic_mode_is_decided_before_waiting_for_wifi():
    """診断モードの判定(ボタン)を、Wi-Fi の接続を待つより前に読むこと。

    手順書は「LED が白く光ったらボタンを離す」と案内しており、白は電源を入れて
    すぐに光る。Wi-Fi の接続を待った後で読むと、資格情報を保存した装置では、
    案内どおりの人がもう離していて診断モードに入れない。保存された設定は診断
    モードでも読む。診断モードでも制御サーバーは動くので、読まないと HELLO と実行が
    既定の周期になる。
    """
    body = c_function(MAIN_C, "app_main")
    pressed = body.index("app_button_held_at_boot()")
    assert body.index("app_button_init(") < pressed < body.index("app_wifi_start()")
    assert body.index("load_settings()") < body.index("if (diag)")


def test_the_runbook_says_when_to_release_the_button():
    """診断モードに入るときにボタンを離す合図が、手順書にあり、装置の動きと合うこと。

    合図は「LED が白く光ったら離す」である。白は BOOT の色で、app_state_init が
    付ける(LED のタスクは main より優先度が高いので、作った時点で光る)。判定は
    その後で、20 ミリ秒ずつ 2 回待って読み終える。人が白を見て離すまでには、
    それより長くかかる。合図がないと、挿してすぐ離す人は判定の前に離してしまう。
    """
    body = c_function(MAIN_C, "app_main")
    assert (body.index("app_led_init()") < body.index("app_state_init()")
            < body.index("app_button_held_at_boot()"))
    boot = re.search(r"case APP_STATE_BOOT:.*?//\s*(\S+)",
                     APP_STATE.read_text(encoding="utf-8")).group(1)
    assert boot == "白"
    held = c_function(APP_BUTTON, "app_button_held_at_boot")
    assert held.count("vTaskDelay(pdMS_TO_TICKS(POLL_MS))") == 2
    assert re.search(r"#define POLL_MS 20\b", APP_BUTTON.read_text(encoding="utf-8"))
    assert "3. LED が白く光ったら、ボタンを離す" in section("1-2. コマンドで書き込む")
    assert "LED が白く光ったら離す" in section("装置のボタン")
    tool = (REPO / "pctool/tools/set_wifi_serial.py").read_text(encoding="utf-8")
    assert "LED が白く光ったら、ボタンを離す" in tool


def test_starting_wifi_does_not_stop_the_boot():
    """Wi-Fi を開始できなくても、起動を中断しないこと。

    保存した資格情報や無線の状態で ESP_ERROR_CHECK が中断すると、起動のたびに同じ所で
    異常終了し、診断モードの設定コンソールにも入れなくなる(資格情報を修正する手段が
    なくなる)。
    Wi-Fi の機能はホストでは動かせないので、ソースで確認する。
    """
    body = c_function(APP_WIFI, "app_wifi_start")
    for call in ("esp_wifi_set_mode", "esp_wifi_set_config", "esp_wifi_start",
                 "esp_wifi_set_ps"):
        assert call + "(" in body, f"app_wifi_start が {call} を呼んでいない"
        assert f"ESP_ERROR_CHECK({call}(" not in body, \
            f"{call} の失敗で起動が中断する"
    # 開始できなかったときは USB だけで動き、制御サーバーと探索を起こさない
    main = c_function(MAIN_C, "app_main")
    assert "bool net_up = (err == ESP_OK);" in main


def test_led_pin_matches_the_firmware():
    """手順書が案内する LED のピン番号が実装と一致すること。"""
    m = re.search(r"#define PADEMU_PIN_LED\s+(\d+)",
                  APP_CONFIG.read_text(encoding="utf-8"))
    assert m, "app_config.h に PADEMU_PIN_LED がない"
    src = GETTING_STARTED.read_text(encoding="utf-8")
    assert f"GPIO{m.group(1)}" in src, \
        f"手順書の LED のピン番号が実装(GPIO{m.group(1)})と違う"


# ---- 制御のやり取り(comm-protocol.md と app_ctrl.c) ----

def c_function(path, name: str) -> str:
    """C の関数 1 つぶんの本文(定義の行から、桁 0 の閉じ括弧まで)。

    宣言(末尾が `;`)ではなく定義を取り出し、次の関数まで含めない。含めると、
    「その関数にないはずのもの」を隣の関数の中身で見つけてしまう。
    """
    src = path.read_text(encoding="utf-8")
    m = re.search(rf"^[^\n]*\b{re.escape(name)}\([^;]*?\)\s*\{{$", src, re.M)
    assert m, f"{path.name} に {name} の定義がない"
    end = src.index("\n}", m.end())
    return src[m.start():end + 2]


def request_fields(func: str) -> set:
    """その要求が読む欄の名前。"""
    return set(re.findall(r'cJSON_GetObjectItem\(req, "(\w+)"\)',
                          c_function(APP_CTRL, func)))


def doc_command_fields(name: str) -> set:
    """コマンド一覧の表が、その要求に書いている欄の名前。"""
    src = COMM.read_text(encoding="utf-8")
    for ln in src.split("\n"):
        cells = [c.strip() for c in ln.split("|")[1:-1]]
        if len(cells) >= 2 and cells[0] == name:
            return set() if cells[1] == "なし" else {
                c.strip() for c in cells[1].split(",")}
    raise AssertionError(f"コマンド一覧に {name} がない")


def test_command_numbers_match_the_pc_side():
    """コマンドの種別の番号が、装置と PC で同じであること。

    番号がずれると、要求が別のコマンドとして実行される。片方だけ修正しても
    どちらのソースも正しく見えるので、照合でしか気づけない。
    """
    found = dict(re.findall(r"^#define (T_\w+)\s+(0x[0-9a-fA-F]+)",
                            APP_CTRL.read_text(encoding="utf-8"), re.M))
    assert "T_STOP_AT_MARK" in found, "app_ctrl.c に T_STOP_AT_MARK がない"
    bad = []
    for name, val in found.items():
        want = getattr(proto, name, None)
        if want != int(val, 16):
            bad.append(f"{name}: 装置 {val} / PC {want}")
    assert not bad, f"コマンドの番号が一致していない: {bad}"


def test_the_command_table_lists_the_numbers_of_the_device():
    """コマンド一覧の表の種別の番号が、装置のソースと同じであること。

    padcue とは別に装置と通信するツールを作る人は、この表の番号でパケットを組む。
    表だけが古いと、別のコマンドとして実行される。
    """
    found = {k[2:]: int(v, 16) for k, v in re.findall(
        r"^#define (T_\w+)\s+(0x[0-9a-fA-F]+)",
        APP_CTRL.read_text(encoding="utf-8"), re.M)
        if k not in ("T_RESP", "T_ERROR")}
    rows = [[c.strip() for c in ln.split("|")[1:-1]]
            for ln in COMM.read_text(encoding="utf-8").split("\n")]
    head = next(r for r in rows if r[:1] == ["コマンド"])
    col = head.index("種別")
    doc = {r[0]: int(r[col], 16) for r in rows
           if len(r) == len(head) and r[0] in found}
    assert doc == found, f"文書 {doc} / 装置 {found}"


def test_a_transfer_is_refused_while_the_engine_runs():
    """PUT は、状態だけでなくエンジンが動いているかも見て拒否すること。

    実行中の手順は転送のバッファーの中を指している。状態は別のタスクが少し遅れて
    書き換えるので、状態だけを見ると、動いている手順の中身を書き換えうる。
    """
    body = c_function(APP_CTRL, "cmd_put")
    assert body.index("app_engine_is_running()") < body.index(
        "memcpy(app_store_buffer()")


def test_lost_replies_are_counted_the_same_way_on_both_sides():
    """失った応答の数え方が、装置の TX_LOST の記録と padcue status で同じであること。

    failed_replies は失敗した試行の回数で、後の再送で送れた応答も含む。それを
    数えると、回復した一時的な失敗まで「送信できなかった」と出て、件数も膨らむ。
    """
    lost = re.search(r"uint32_t lost = ([^;]+);",
                     c_function(MAIN_C, "supervisor_task")).group(1)
    assert lost == "tx.dropped_replies + tx.bad_reports"
    text = (REPO / "pctool/padcue/statustext.py").read_text(encoding="utf-8")
    assert ('lost_reply = d.get("dropped_replies", 0) + d.get("bad_reports", 0)'
            in text)


def test_unsigned_request_numbers_are_read_through_json_uint():
    """要求の符号なしの数の欄は、json_uint を通して読むこと。

    範囲の外の double を符号なし整数へ変換すると未定義動作になり、整数でない値は
    警告なしに切り捨てられる。生の valuedouble を読んでよいのは、json_uint 自身と、
    負の値をとる手動操作の軸(cmd_passthru が範囲を確認する)だけである。
    """
    src = APP_CTRL.read_text(encoding="utf-8")
    names = re.findall(r"^static \w+ \**(\w+)\(", src, re.M)
    raw = {n for n in names if "valuedouble" in c_function(APP_CTRL, n)}
    assert raw == {"json_uint", "cmd_passthru"}


def test_the_run_takes_only_the_procedure_id():
    """RUN が周回数も再開点も受け取らないこと。

    くり返しは手順の中の SETCNT と DJNZ が回し、途中から始めるときは続きの
    手順を変換器が合成する。装置が周回数を持つと、同じことを 2 か所が数える。
    """
    assert request_fields("cmd_run") == doc_command_fields("RUN") == {"id"}


def test_a_run_is_refused_while_the_switch_is_away_or_asleep():
    """Switch と USB でつながっていないときと、スリープしているときは、RUN を拒否する
    こと。

    入力が本体に送られないまま手順の終わりまで進み、何もしなかった実行が完了として
    残る。サスペンドの間も USB はつながったままに見える(usb_mounted は真)ので、
    サスペンドは別に見て、STATUS にも別の欄で出す。
    """
    body = c_function(APP_CTRL, "start_run")
    start = body.index("app_engine_start(")
    assert body.index("app_usb_is_mounted()") < start
    assert body.index("app_usb_is_suspended()") < start
    run = c_function(APP_CTRL, "cmd_run")
    for result in ("RUN_NO_SWITCH", "RUN_SWITCH_ASLEEP"):
        assert re.search(rf'case {result}:\s*return send_error\(sock, "BAD_STATE"',
                         run), result
    assert "usb_suspended" in status_fields_in_firmware()
    assert says(COMM, "RUN は BAD_STATE で拒否する")
    arch = REPO / "docs/design/firmware-architecture.md"
    assert says(arch,
                "サスペンドしているとき(Switch がスリープしている)は、RUN を拒否する")


def usb_event_case(event: str) -> str:
    """app_usb.c の on_usb_event の、その通知の case の本文。"""
    body = c_function(APP_USB, "on_usb_event")
    start = body.index(f"case {event}:")
    return body[start:body.index("break;", start)]


def test_a_reenumeration_while_running_stops_the_run():
    """実行中に本体が装置を列挙し直したら(給電は続いたまま)、切断と同じく実行を停止し、
    中断を記録して ERROR にすること(firmware-architecture.md §6)。

    TinyUSB はバスのリセットでは切断を知らせず(usbd.c の DCD_EVENT_BUS_RESET)、設定し
    直すと ATTACHED だけを出す。列挙し直しの間の入力は本体に送られないのに、停止し
    なければ実行は進み続け、完了として残る。USB の列挙はホストで起こせないので、
    ソースで確かめる。
    """
    for func, kind in (("on_usb_mount", "APP_LOG_USB_MOUNT"),
                       ("on_usb_umount", "APP_LOG_USB_UMOUNT"),
                       ("on_usb_suspend", "APP_LOG_USB_SUSPEND")):
        assert f"stop_run_for_usb({kind});" in c_function(MAIN_C, func), func
    stop = c_function(MAIN_C, "stop_run_for_usb")
    order = [stop.index(s) for s in ("app_engine_is_running()",
                                     "app_engine_stop(APP_STOP_ERROR)",
                                     "app_run_abort_log()", "app_state_fault(why)")]
    assert order == sorted(order), stop


def test_a_run_starts_only_after_the_switch_finished_setting_up_the_usb():
    """実行を始めるのは、本体が設定し終えた通知(ATTACHED)を受けた後だけであること。

    TinyUSB の tud_mounted() は、設定の要求を受けた時点で真になり、ATTACHED はその後に
    出る(usbd.c の SET_CONFIGURATION は cfg_num を書いてから tud_mount_cb を呼ぶ)。
    その間に始めた実行は、続いて来る ATTACHED を列挙し直しと混同して停止する。
    ATTACHED を受けたら、実行中かを見てから(on_mount)接続したことにする。逆の順だと、
    その間に始まった実行を停止する。切断の通知と再接続(app_usb_reconnect)は、
    接続していないことにしてから後始末をする。
    """
    src = APP_USB.read_text(encoding="utf-8")
    assert "static _Atomic bool s_attached;" in src       # 起動の直後は接続していない
    assert "return tud_mounted() && atomic_load(&s_attached);" in src
    attached = usb_event_case("TINYUSB_EVENT_ATTACHED")
    assert (attached.index("s_cb.on_mount()")
            < attached.index("atomic_store(&s_attached, true)")), attached
    detached = usb_event_case("TINYUSB_EVENT_DETACHED")
    assert (detached.index("atomic_store(&s_attached, false)")
            < detached.index("s_cb.on_umount()")), detached
    reconnect = c_function(APP_USB, "app_usb_reconnect")
    assert (reconnect.index("atomic_store(&s_attached, false)")
            < reconnect.index("s_cb.on_umount()"))
    assert "s_detached" not in src


def test_a_started_run_resets_the_send_counts_and_ends_manual_control():
    """始めた RUN だけが送信の計測値を 0 に戻し、手動操作を終えること。

    始められなかった RUN で計測値を戻すと、前の実行の停止した理由と区切りは残るのに、
    送信の計測値だけが消える。手動操作を残すと、実行が終わった後に、実行の前の
    手動操作の入力がまた送られる。
    """
    body = c_function(APP_CTRL, "start_run")
    start = body.index("app_engine_start(")
    assert start < body.index("app_usb_reset_tx_stats()")
    assert start < body.index("app_usb_set_manual(NULL, false)")
    assert says(COMM, "RUN で実行を始めたときと、物理ボタンを長押ししたときも、"
                      "手動操作は終わる")


def test_the_logs_are_kept_until_the_pc_acknowledges_them():
    """LOGS が boot と ack を受け、boot と from と entries を返すこと。

    返した時点で消すと、応答を PC が受け取る前に接続が切れたときに、その記録が
    どこにも残らない。読み出しと消去の規則そのものは test_firmware_main.py が
    ホストで動かして確認する。
    """
    assert request_fields("cmd_logs") == doc_command_fields("LOGS") == {"boot", "ack"}
    body = c_function(APP_CTRL, "cmd_logs")
    assert set(re.findall(r'cJSON_Add\w+ToObject\(j, "(\w+)"', body)) == {
        "boot", "from", "entries"}
    assert says(COMM, "応答は `boot`、返した最初の記録の番号 `from`、記録の並び "
                      "`entries` である")


def test_the_stop_commands_match_the_document():
    """STOP は即時だけ、STOP_AT_MARK が予約と解除を受けること。"""
    assert request_fields("cmd_stop") == doc_command_fields("STOP") == set()
    body = c_function(APP_CTRL, "cmd_stop_at_mark")
    assert request_fields("cmd_stop_at_mark") == \
        doc_command_fields("STOP_AT_MARK") == {"mask", "release"}
    # 欄はどちらも必須で、欠けていれば拒否する。既定の値で予約すると、PC が
    # 書いたつもりの種類と違う区切りで実行が停止する
    assert re.search(r'if \(!cJSON_IsBool\(rel\)\) return send_error\(sock, "BAD_ARG"',
                     body), "release を省いた要求を拒否していない"
    assert re.search(r'if \(!cJSON_IsNumber\(m\)\) return send_error\(sock, "BAD_ARG"',
                     body), "mask を省いた要求を拒否していない"


def test_identify_is_the_same_everywhere():
    """IDENTIFY の欄と点滅の長さが、装置と模擬装置と画面と文書で同じであること。

    画面は、押した装置の丸を装置の LED と同じ長さだけ点滅させる。長さがずれると、
    画面の丸が消えても装置が光っている、またはその逆になり、どれを押したかの
    照合が崩れる。
    """
    assert request_fields("cmd_identify") == doc_command_fields("IDENTIFY") == set()
    hdr = (REPO / "firmware" / "main" / "app_state.h").read_text(encoding="utf-8")
    m = re.search(r"#define APP_IDENTIFY_US \((\d+) \* 1000 \* 1000LL\)", hdr)
    assert m, "app_state.h に APP_IDENTIFY_US がない"
    sec = int(m.group(1))
    assert mockdevice.IDENTIFY_S == sec, mockdevice.IDENTIFY_S
    js = (REPO / "pctool/padcue/web/devices.js").read_text(encoding="utf-8")
    assert f"const IDENTIFY_MS = {sec * 1000};" in js, "画面の丸の長さが装置と違う"
    assert says(COMM, f"LED を {sec} 秒間、白く速く点滅させる")
    assert f"{sec} 秒たつと状態の色に戻ります" in section("LED の意味")


def test_usb_reconnect_is_the_same_everywhere():
    """USB_RECONNECT の欄、切断時間の範囲、拒否の文が、装置と PC と文書で同じで
    あること。切断と接続は、usb タスクだけが行うこと。

    範囲がずれると、PC のコマンドが通した切断時間を装置が拒否するか、その逆に
    なる。USB のレジスターを操作するのを TinyUSB と同じコア 1 に合わせるので、
    ほかのタスクからは切断も接続もしない(firmware-architecture.md の USB の節)。
    """
    assert (request_fields("cmd_usb_reconnect")
            == doc_command_fields("USB_RECONNECT") == {"hold_ms"})
    usb_h = REPO / "firmware/main/app_usb.h"
    lo, hi = (c_define(usb_h, f"APP_USB_RECONNECT_HOLD_{n}_MS") for n in ("MIN", "MAX"))
    assert (lo, hi) == (proto.USB_RECONNECT_HOLD_MS_MIN,
                        proto.USB_RECONNECT_HOLD_MS_MAX)
    body = c_function(APP_CTRL, "cmd_usb_reconnect")
    call = body[body.index("snprintf("):]
    call = call[:call.index(";")]
    assert "APP_USB_RECONNECT_HOLD_MIN_MS, APP_USB_RECONNECT_HOLD_MAX_MS" in call
    # 拒否の文は装置が範囲を埋めて作る。PC 側は、埋めた後の文をそのまま持つ
    fmt = "".join(re.findall(r'"([^"]*)"', call))
    assert fmt % (lo, hi) == proto.USB_RECONNECT_HOLD_BAD
    assert says(COMM, f"hold_ms は {lo} から {hi} の整数である")
    usb_c = REPO / "firmware/main/app_usb.c"
    src, task = usb_c.read_text(encoding="utf-8"), c_function(usb_c, "usb_task")
    for name in ("tud_disconnect()", "tud_connect()"):
        assert src.count(name) == task.count(name) == 1, name


def test_the_stop_reasons_match_the_pc_side():
    """停止した理由の文字列が、装置と PC で同じであること。"""
    names = set(re.findall(r'return "(\w*)";',
                           c_function(APP_CTRL, "stop_reason_name")))
    assert names == set(proto.STOP_REASONS), \
        f"停止の理由の名前が PC 側と一致していない: 装置 {sorted(names)}"


def status_fields_in_firmware() -> set:
    """STATUS が組み立てる欄の名前(入れ子の中身は数えない)。"""
    body = c_function(APP_CTRL, "cmd_status")
    return set(re.findall(r'cJSON_Add\w+ToObject\(j, "(\w+)"', body))


def status_fields_in_doc() -> set:
    """comm-protocol.md が STATUS の欄として挙げている名前。"""
    src = COMM.read_text(encoding="utf-8")
    sec = src[src.index("## STATUS の欄"):src.index("## エラー応答")]
    table = set(re.findall(r"^\| `(\w+)`", sec, re.M))
    tail = sec[sec.index("装置の様子を測った値も返す。"):]
    return table | set(re.findall(r"`(\w+)`", tail))


def test_the_status_fields_match_the_document():
    """STATUS が返す欄が、文書の一覧とちょうど一致すること。

    画面はこの欄だけを読む。文書にない欄が増えても、欄が消えても、どちらの
    ソースを読んでも気づけない。
    """
    doc, dev = status_fields_in_doc(), status_fields_in_firmware()
    assert dev - doc == set(), f"文書にない欄を装置が返している: {sorted(dev - doc)}"
    assert doc - dev == set(), f"文書にあるのに装置が返さない欄: {sorted(doc - dev)}"


def test_the_status_fields_match_the_mock_device():
    """模擬装置が返す欄も、同じ一覧とちょうど一致すること。

    受け入れテストの期待値は模擬装置が持つので、こちらがずれると装置の側だけ
    直っていてもテストが通ってしまう。
    """
    got = set(MockDevice()._cmd_status().obj)
    doc = status_fields_in_doc()
    assert got == doc, (
        f"模擬装置にだけある欄: {sorted(got - doc)} / "
        f"文書にだけある欄: {sorted(doc - got)}")


def hello_fields_in_doc() -> set:
    """comm-protocol.md が HELLO の応答の欄として表に挙げている名前。"""
    src = COMM.read_text(encoding="utf-8")
    start = src.index("**HELLO** は、送り手が自分の識別子を送る要求である。")
    sec = src[start:src.index("`crash` は、")]
    return set(re.findall(r"^\| `(\w+)`", sec, re.M))


def test_the_hello_fields_match_the_document():
    """HELLO が返す欄が、装置と模擬装置と文書の表でちょうど一致すること。

    PC は state・frame_period_ns・rolled_back なども必須として読む。文書の表に
    欄が欠けていると、文書を見て作った模擬装置やツールに PC がつながらない。
    """
    doc = hello_fields_in_doc()
    dev = set(re.findall(r'cJSON_Add\w+ToObject\(j, "(\w+)"',
                         c_function(APP_CTRL, "cmd_hello")))
    hello = proto.Message(proto.T_HELLO, {})
    mock = set(MockDevice()._dispatch(_Conn(None), hello).obj)
    assert dev == doc, (f"装置にだけ: {sorted(dev - doc)} / "
                        f"文書にだけ: {sorted(doc - dev)}")
    assert mock == doc, (f"模擬装置にだけ: {sorted(mock - doc)} / "
                         f"文書にだけ: {sorted(doc - mock)}")


def test_the_procedure_in_the_status_is_the_one_in_the_buffer():
    """STATUS の proc は、装置のソースでは、バッファーに格納されている手順の id で
    あること。

    PUT を最後まで受けたときと RUN で読み込んだときに替わり、実行が終わっても
    消えない。模擬装置が同じ値を返すことは、test_client.py の
    test_the_status_names_the_procedure_in_the_buffer が見る。
    """
    assert 'cJSON_AddStringToObject(j, "proc", app_store_staged_id())' in \
        c_function(APP_CTRL, "cmd_status")
    store = REPO / "firmware" / "main" / "app_store.c"
    for func in ("app_store_stage_buffered", "app_store_load"):
        assert "s_staged_id" in c_function(store, func), func


def c_define(path, name: str) -> int:
    m = re.search(rf"^#define {name}\s+(\d+)", path.read_text(encoding="utf-8"),
                  re.M)
    assert m, f"{path.name} に {name} がない"
    return int(m.group(1))


def test_the_packet_limit_is_the_same_everywhere():
    """本体部の上限 8192 が、装置と PC と文書で同じであること。

    装置が受け取れる大きさより PC が大きく送ると、装置は読む前に接続を切る。
    どちらのソースを読んでも、片方だけ見ているかぎり正しく見える。
    """
    dev = c_define(APP_CTRL, "MAX_PACKET")
    assert dev == proto.MAX_BODY, f"装置 {dev} / PC {proto.MAX_BODY}"
    assert says(COMM, f"上限は {dev} バイトである")
    # 超えたときは、黙って切らずに理由を返す
    assert '"TOO_LARGE"' in APP_CTRL.read_text(encoding="utf-8")


def c_string(path, name: str) -> str:
    """#define で置いた C の文字列。行をまたいで並べた文字列はつないで返す。"""
    m = re.search(rf"^#define {name}\b((?:[^\n]*\\\n)*[^\n]*)",
                  path.read_text(encoding="utf-8"), re.M)
    assert m, f"{path.name} に {name} がない"
    return "".join(re.findall(r'"([^"]*)"', m.group(1)))


def test_the_config_limits_are_the_same_everywhere():
    """CONFIG が受け付ける値の範囲が、装置と PC と模擬装置と文書で同じであること。

    装置が拒否する値を PC のツールや模擬装置が受け付けると、PC 側のテストは装置が
    拒否する設定でも通る。Wi-Fi の値の範囲は app_wifi.h の関数で決め、保存する 2 つの
    経路(設定コンソールと CONFIG)が同じ関数を使う。
    別々に書くと、片方の経路だけが通す値が生まれる。範囲の中身が装置と PC で
    同じことは、test_firmware_main.py が装置の関数を動かして確認する。
    """
    wifi_h = REPO / "firmware/main/app_wifi.h"
    limits = [c_define(wifi_h, f"APP_WIFI_{n}")
              for n in ("SSID_MAX", "PASS_MIN", "PASS_MAX")]
    assert limits == [proto.WIFI_SSID_MAX, proto.WIFI_PASS_MIN, proto.WIFI_PASS_MAX]
    # 拒否する文も同じにする。PC は装置の文をそのまま見せる
    assert c_string(wifi_h, "APP_WIFI_SSID_BAD") == proto.WIFI_SSID_BAD
    assert c_string(wifi_h, "APP_WIFI_PASS_BAD") == proto.WIFI_PASS_BAD
    for name, func in (("app_provision.c", "do_wifi_set"),
                       ("app_ctrl.c", "cmd_config")):
        body = c_function(REPO / "firmware/main" / name, func)
        assert "app_wifi_ssid_ok(" in body and "app_wifi_pass_ok(" in body, name
    engine_h = REPO / "firmware/main/app_engine.h"
    lo, hi = (c_define(engine_h, f"APP_ENGINE_PERIOD_{n}_NS") for n in ("MIN", "MAX"))
    ok = c_function(APP_ENGINE, "app_engine_frame_period_ok")
    assert "APP_ENGINE_PERIOD_MIN_NS" in ok and "APP_ENGINE_PERIOD_MAX_NS" in ok
    # 拒否する文に受け付ける範囲を入れる。範囲がないと、何を入れればよいか分からない
    config = c_function(APP_CTRL, "cmd_config")
    assert "APP_ENGINE_PERIOD_MIN_NS, (unsigned)APP_ENGINE_PERIOD_MAX_NS" in config
    assert (lo, hi) == (proto.FRAME_PERIOD_NS_MIN, proto.FRAME_PERIOD_NS_MAX)
    assert says(COMM, f"frame_period_ns が {lo} から {hi} の整数")
    usb_h = REPO / "firmware/main/app_usb.h"
    lo, hi = (c_define(usb_h, f"APP_USB_BINTERVAL_{n}") for n in ("MIN", "MAX"))
    assert (lo, hi) == (proto.USB_BINTERVAL_MIN, proto.USB_BINTERVAL_MAX)
    assert "APP_USB_BINTERVAL_MIN, APP_USB_BINTERVAL_MAX" in config
    assert says(COMM, f"usb_binterval が {lo} から {hi} の整数")
    assert says(COMM, f"wifi_ssid が 1 から {proto.WIFI_SSID_MAX} バイト")
    assert says(COMM, f"wifi_pass が空か、{proto.WIFI_PASS_MIN} から "
                      f"{proto.WIFI_PASS_MAX - 1} バイトか、16 進数 "
                      f"{proto.WIFI_PASS_MAX} 桁である")


def test_both_ways_of_saving_wifi_mark_the_values_new():
    """Wi-Fi の値を保存する 2 つの経路(CONFIG と設定コンソール)が、どちらも
    app_wifi_save で保存すること。起動でつながったら印をクリアすること。

    app_wifi_save は値と一緒に「新しく保存した値」の印を立てる(印を先に書くことは
    test_firmware_main.py が装置の関数を動かして確かめる)。経路が自分で値を書くと、
    その経路で保存した値が誤っていても設定待ちに戻らず、装置は Wi-Fi につながらない
    まま USB だけで起動し、PC から何もできなくなる(app_boot.h)。
    """
    for name, func in (("app_ctrl.c", "cmd_config"),
                       ("app_provision.c", "do_wifi_set")):
        body = c_function(REPO / "firmware/main" / name, func)
        assert "nvs_set_str(" not in body, f"{name} が値を自分で書いている"
        assert "nvs_commit(h)" in body[body.index("app_wifi_save(h, "):], name
    main = (REPO / "firmware/main/main.c").read_text(encoding="utf-8")
    assert "if (is_new) app_wifi_mark_verified();" in main


def test_the_number_of_connections_is_the_same_everywhere():
    """制御の接続の数 4 が、装置と模擬装置と文書で同じであること。"""
    dev = c_define(APP_CTRL, "MAX_CLIENTS")
    assert dev == mockdevice.MAX_CONNS, \
        f"装置 {dev} / 模擬装置 {mockdevice.MAX_CONNS}"
    assert says(COMM, f"{dev} 本まで同時に受ける")


def test_a_fifth_connection_is_closed_and_the_others_keep_going():
    """5 番目の接続は受け取ってすぐ閉じ、今の 4 本はそのまま動き続けること。

    あとから来た接続のために今の接続を手放すと、サーバーが動いている間に CLI が
    つないだだけで、実行中のレーンの状態取得ができなくなる。
    """
    body = c_function(APP_CTRL, "accept_client")
    assert re.search(r"if \(!slot \|\| !rx\) \{.*?close\(sock\);\s*return;", body,
                     re.S), "空きがないときに、新しい接続を閉じていない"
    assert "close_client(" not in body, "新しい接続のために今の接続を閉じている"
    assert says(COMM, "既にある 4 本はそのまま動き続ける")


def test_a_vanished_peer_is_closed_by_keepalive():
    """相手が消えた接続を、TCP の keepalive で無通信の打ち切りより先に閉じること。

    lwIP の既定の待ち時間は 2 時間で、無通信の 180 秒より先に閉じない。消えた
    接続が 4 本の枠を占めると、戻った PC がつなげない。
    """
    body = c_function(APP_CTRL, "accept_client")
    vals = {k: int(v) for k, v in re.findall(r"int (keep_\w+) = (\d+);", body)}
    assert vals == {"keep_idle_s": 10, "keep_intvl_s": 5, "keep_cnt": 3}, vals
    for opt in ("TCP_KEEPIDLE", "TCP_KEEPINTVL", "TCP_KEEPCNT"):
        assert f"setsockopt(sock, IPPROTO_TCP, {opt}," in body, opt
    idle = int(re.search(r"#define IDLE_TIMEOUT_US \((\d+) \* 1000000ULL\)",
                         APP_CTRL.read_text(encoding="utf-8")).group(1))
    assert vals["keep_idle_s"] + vals["keep_intvl_s"] * vals["keep_cnt"] < idle
    arch = REPO / "docs/design/firmware-architecture.md"
    assert says(arch, "3 回応答がなければ閉じるので、約 25 秒で閉じる")


def test_a_reply_waits_at_least_one_millisecond_per_send():
    """応答を送る待ち時間を、ミリ秒に切り上げて最短 1 ミリ秒で渡していること。

    lwIP は待ち時間をミリ秒に切り捨て、0 ミリ秒を「上限なし」とみなす。残りが 1 ミリ秒を
    切ったまま渡すと、読まない相手への送信が終わらず、制御のタスクが進まなくなる。
    """
    body = c_function(APP_CTRL, "send_all")
    assert "(left + 999) / 1000" in body, "残りの時間をミリ秒に切り上げていない"
    assert re.search(r"\.tv_usec = .*\(ms % 1000\) \* 1000", body), \
        "マイクロ秒の端数を lwIP へ渡している"
    assert says(COMM, "読まなくなってから閉じるまでは最長で約 2 秒かかる")


def test_the_update_and_manual_control_belong_to_their_connection():
    """更新は始めた接続に、手動操作は最後に入力を送った接続に結び付いていること。

    接続ごとに持たないと、別の端末が接続を閉じただけで、進行中の更新が
    中断し、手動操作が解除される。
    """
    body = c_function(APP_CTRL, "cleanup_client")
    assert re.search(r"if \(c->has_ota &&", body), \
        "更新の後始末が、その接続が始めたものかを見ていない"
    assert re.search(r"if \(c->has_manual &&", body), \
        "手動操作の後始末が、その接続が入力を送っていたかを見ていない"
    assert says(COMM, "更新は始めた接続の、手動操作は最後に入力を送った接続の"
                      "ものである")


# ---- 操作権 ----

APP_OWNER = REPO / "firmware" / "main" / "app_owner.c"


def doc_owner_column(value: str) -> set:
    """コマンド一覧の表の「操作権」の列が value のコマンドの種別の名前。"""
    out = set()
    for ln in COMM.read_text(encoding="utf-8").split("\n"):
        cells = [c.strip() for c in ln.split("|")[1:-1]]
        if len(cells) >= 3 and cells[2] == value:
            out.add("T_" + cells[0])
    return out


def test_the_commands_that_need_ownership_are_the_same_everywhere():
    """操作権が必要なコマンドと、解除だけ必要なコマンドが、装置と PC と文書で同じで
    あること。

    片方だけ増やすと、装置を動かす要求が誰からでも通るか、所有者からも
    通らなくなる。どちらもソースを別々に読んでいるかぎり正しく見える。
    「区切りで停止」(STOP_AT_MARK)は、予約は誰でも送れ、解除(release が真)には
    操作権が必要である。所有者でない接続が解除できると、所有者が停止したかった
    区切りを越えて実行が続く。
    """
    body = c_function(APP_CTRL, "owner_required")
    always = body[:body.index("return true;")]
    dev = set(re.findall(r"case (T_\w+):", always))
    pc = {n for n in dir(proto)
          if n.startswith("T_") and getattr(proto, n) in proto.OWNER_REQUIRED}
    assert dev == pc, f"装置だけ {sorted(dev - pc)} / PC だけ {sorted(pc - dev)}"
    doc = doc_owner_column("必要")
    assert dev == doc, f"装置だけ {sorted(dev - doc)} / 文書だけ {sorted(doc - dev)}"
    # 解除だけ必要なもの。装置は release を見て決める
    to_release = re.findall(
        r'case (T_\w+):\s*(?://[^\n]*\n\s*)*'
        r'return cJSON_IsTrue\(cJSON_GetObjectItem\(req, "release"\)\);', body)
    pc_release = {n for n in dir(proto)
                  if n.startswith("T_") and getattr(proto, n) in proto.OWNER_TO_RELEASE}
    assert set(to_release) == pc_release == doc_owner_column("解除だけ必要") == {
        "T_STOP_AT_MARK"}
    assert says(COMM, "予約は誰でも送れるが、解除には操作権が必要である")


def test_the_ownership_timeout_is_the_same_everywhere():
    """失効までの 30 秒が、装置と模擬装置と文書で同じであること。"""
    m = re.search(r"#define OWNER_TIMEOUT_US \((\d+) \* 1000000ULL\)",
                  APP_OWNER.read_text(encoding="utf-8"))
    assert m, "app_owner.c に OWNER_TIMEOUT_US がない"
    sec = int(m.group(1))
    assert sec == mockdevice.OWNER_TIMEOUT_S, \
        f"装置 {sec} 秒 / 模擬装置 {mockdevice.OWNER_TIMEOUT_S} 秒"
    assert says(COMM, f"無通信が {sec} 秒続くと操作権は")


def test_the_heartbeat_is_the_owning_connection_only():
    """生存の確認に数えるのは、操作権を取った接続だけであること。

    同じ識別子を送る別の接続(同じ padcue の CLI)で数えると、サーバーの
    接続が切れていても CLI が通信している間は失効しなくなり、「スリープから
    戻って同じ識別子で取得し直す」という通常の手順が通らなくなる。
    """
    body = c_function(APP_OWNER, "app_owner_touch")
    assert "s_conn == conn" in body, \
        "生存の確認が、操作権を取った接続かどうかを見ていない"
    # 失効を先に見ること。生存の確認が先だと、31 秒通信がなかった接続の読み出しが
    # 時計を戻してしまい、いつまでも失効しない
    d = c_function(APP_CTRL, "dispatch")
    assert d.index("app_owner_expire()") < d.index("app_owner_touch("), \
        "生存の確認を失効より先に記録している"


def test_a_request_moves_a_reconnecting_device_back_to_idle():
    """要求を受け取ったら、Wi-Fi の接続待ち(WIFI_CONNECTING)を待機中に戻すこと。

    要求を受け取ったのは、つながっている証拠である。supervisor は 100 ミリ秒ごとに
    しかつながったかを見ないので、つなぎ直した直後に受け取った要求(切れている間に PC が
    送り、TCP が送り直したもの)は、待機中にだけ受ける要求(COMMIT、RUN、DELETE、
    CONFIG、OTA、USB_RECONNECT)なら「待機中にのみ」で拒否される。制御のサーバーは
    lwIP と cJSON を使うのでホストで動かせず、ソースで確かめる。
    """
    d = c_function(APP_CTRL, "dispatch")
    m = re.search(r"app_state_transition\(\s*"
                  r"APP_STATE_BIT\(APP_STATE_WIFI_CONNECTING\),\s*APP_STATE_IDLE\)", d)
    assert m, "要求を受け取っても接続待ちのまま"
    assert m.start() < d.index("switch (type)")


def test_closing_a_connection_does_not_end_the_ownership():
    """接続が切れても操作権は消えないこと。

    切断は失効に数えない。数えると、PC がスリープに入るたびに操作権が
    消え、実行中の装置を別の padcue が奪えてしまう。
    """
    body = c_function(APP_CTRL, "close_client")
    assert "app_owner_forget_conn" in body, \
        "接続を閉じるときに、指していた接続を忘れていない"
    assert "app_owner_release" not in body, \
        "接続が切れただけで操作権を手放している"


def test_a_long_press_releases_the_ownership_and_manual_control():
    """装置のボタンの長押しが、停止に加えて、操作権と手動操作を解除すること。

    手動操作の入力を送っていた PC が消えると(ふたを閉じたノート PC など)、装置は
    その接続が閉じるまで最後の入力を送り続ける。停止する手段はボタンしかない。
    """
    body = c_function(MAIN_C, "on_button_long")
    assert "APP_STOP_BUTTON" in body and "app_owner_release_any()" in body
    assert "app_usb_set_manual(NULL, false)" in body
    assert says(COMM, "長押しが停止と、操作権と手動操作の解除")
    assert says(REPO / "docs/concepts.md", "長押しは「停止」と、操作権と手動操作の解除")
    # 利用者が参照する付録の表と、README・SECURITY の止め方にも、手動操作が終わる
    # ことを書く
    assert "手動操作も終える" in section("装置のボタン")
    for doc in ("README.md", "SECURITY.md"):
        assert says(REPO / doc, "全入力を離して実行と手動操作を停止し"), doc


# ---- 保存済みの手順の一覧 ----

APP_STORE_H = REPO / "firmware" / "main" / "app_store.h"


def test_the_procedure_list_limit_is_the_same_everywhere():
    """一覧に含まれる手順の数 64 が、装置と模擬装置と文書で同じであること。

    含まれなかった手順は実行も削除もできない。ファイルを読む順番は決まっていない
    ので、上限を超えると、どれが見えるかも決まらない。
    """
    m = re.search(r"#define APP_STORE_LIST_MAX (\d+)",
                  APP_STORE_H.read_text(encoding="utf-8"))
    assert m, "app_store.h に APP_STORE_LIST_MAX がない"
    n = int(m.group(1))
    assert n == mockdevice.LIST_LIMIT, \
        f"装置 {n} 件 / 模擬装置 {mockdevice.LIST_LIMIT} 件"
    assert says(COMM, f"LIST が返す手順は {n} 件までである")
    # 切り詰めたことを応答に含める。黙って短い一覧を返すと原因が分からない
    assert '"truncated"' in c_function(APP_CTRL, "cmd_list")
    # 上限は手順の形のファイルだけで数える。形の違うファイルが混ざると、ちょうど
    # 上限の件数のときに、含まれない手順がないのに切り詰めたと示す
    scan = c_function(REPO / "firmware/main/app_store.c", "scan_flash")
    assert scan.index("stem != APP_STORE_ID_HEX") < scan.index("if (n >= max)")


def test_the_procedure_buffer_is_the_same_size_everywhere():
    """手順を受け取るバッファーの大きさが、装置と模擬装置と変換の上限で同じであること。

    模擬装置だけ大きいと、模擬装置で通った手順が装置では「手順が大きすぎます」で
    拒否される。変換が入れるレコードの数の上限も、この大きさから決まる。
    """
    m = re.search(r"#define APP_STORE_MAX_PROC_SIZE \((\d+) \* (\d+)\)",
                  APP_STORE_H.read_text(encoding="utf-8"))
    assert m, "app_store.h に APP_STORE_MAX_PROC_SIZE がない"
    size = int(m.group(1)) * int(m.group(2))
    assert size == mockdevice.PROC_BUFFER_SIZE, \
        f"装置 {size} / 模擬装置 {mockdevice.PROC_BUFFER_SIZE}"
    assert binfmt.HEADER_SIZE + binfmt.RECORD_CAPACITY * binfmt.RECORD_SIZE <= size
    assert binfmt.HEADER_SIZE + (binfmt.RECORD_CAPACITY + 1) * binfmt.RECORD_SIZE > size


# ---- 記録のリングと空きヒープ ----

APP_LOG = REPO / "firmware" / "main" / "app_log.c"


def test_the_log_ring_is_one_ring_within_the_budget():
    """記録のリングがロックをかけた 1 本で、予算に収まること。

    書き手はどれもタスクで、割り込みからは書かない。書き手のいないリングを
    持つと、予算をそこに配ることになる。app_log_entry_t の大きさ(欄から数える)
    で 640 件が 16 キロバイトに収まることを、ここで数える。
    """
    src = APP_LOG.read_text(encoding="utf-8")
    sizes = re.findall(r"#define RING_SIZE\w* (\d+)", src)
    assert sizes == ["640"], sizes
    header = (REPO / "firmware" / "main" / "app_log.h").read_text(encoding="utf-8")
    struct_body = re.search(
        r"typedef struct \{(.*?)\} app_log_entry_t;", header, re.S).group(1)
    words = struct_body.count("uint32_t")
    entry_size = (words * 4 + struct_body.count("uint8_t") + 3) // 4 * 4  # 4 バイト境界
    assert int(sizes[0]) * entry_size <= 16 * 1024, entry_size
    # 書き込みはロックの中で行う。割り込みから呼ばないので IRAM には置かない
    put = c_function(APP_LOG, "app_log_put3")
    assert "xSemaphoreTake(s_lock" in put and "xSemaphoreGive(s_lock)" in put
    assert "IRAM_ATTR" not in src


def test_the_arm_limit_of_the_mock_is_the_one_of_the_device():
    """選択待ちの選択肢の上限が、装置のソースと PC の binfmt.py で同じであること。

    模擬装置のテスト(test_mock_limits.py)は binfmt.MAX_ARMS の数を試す。
    """
    src = (REPO / "firmware" / "components" / "pademu_core" / "include"
           / "pademu_core.h").read_text(encoding="utf-8")
    assert re.findall(r"#define PADEMU_MAX_ARMS (\d+)", src) == [str(binfmt.MAX_ARMS)]


def test_one_logs_response_carries_48_entries_at_most():
    """LOGS の 1 応答に含める件数の上限が 48 であること。

    ホストで C を実行するテスト(test_firmware_main.py)と模擬装置は、この値を
    コピーして使う。装置の値を変えたら、コピーも揃えて変える。
    """
    src = APP_CTRL.read_text(encoding="utf-8")
    assert re.findall(r"#define MAX_LOG_ENTRIES_PER_RESP (\d+)", src) == ["48"]
    assert mockdevice.LOGS_LIMIT == 48


def test_the_log_ring_is_the_same_size_on_the_mock_device():
    """模擬装置も、装置と同じ件数で記録を打ち切り、その後の記録を捨てて数えること。

    記録は PC が受け取りを知らせるまで消えない。PC が書けない間に装置だけが
    記録を捨てると、その振る舞いを模擬装置のテストで確認できない。
    """
    assert c_define(APP_LOG, "RING_SIZE") == mockdevice.LOG_RING
    # 装置は、埋まったバッファーに書かずに捨てた数を数える
    assert "s_dropped" in c_function(APP_LOG, "app_log_put3")


def test_the_free_heap_is_reported():
    """STATUS が空きヒープを返すこと。

    手順のバッファー 96 KiB を上げるかそのままにするかを決める材料にする。
    """
    assert "free_heap" in status_fields_in_firmware()
    assert says(COMM, "`free_heap` は空きヒープ")


def test_the_owner_lock_exists_before_the_button_works():
    """操作権のロックは、ボタンが働き始める前に作ること。

    長押しは操作権を手放す。ロックがまだないうちに押されると装置が異常終了する。
    ボタンは「PC がいなくなったときの最後の手段」である。ロックを制御サーバーの
    開始のところで作ると、ネットワークが立ち上がらない装置では一度も作られず、
    その装置でこそ長押しが働かない。
    """
    src = MAIN_C.read_text(encoding="utf-8")
    assert "app_owner_init()" in src, "main.c が操作権のロックを作っていない"
    assert src.index("app_owner_init()") < src.index("app_button_init("), \
        "app_owner_init が app_button_init より後にある"
    ctrl = APP_CTRL.read_text(encoding="utf-8")
    assert "app_owner_init()" not in ctrl, (
        "制御サーバーの開始がロックを作っている。そこはネットワークが"
        "立ち上がったときしか通らない")


def test_the_last_crash_is_reported():
    """前回異常終了した場所を HELLO で返すこと。

    装置は普通に動いている間 USB をコントローラーとして使うので、シリアルの
    ログが出ない。線をつながずに異常終了した場所を知る手段はここだけである。
    """
    src = APP_STATE.read_text(encoding="utf-8")
    assert "esp_core_dump_get_summary" in src, \
        "app_state.c がクラッシュの記録を読んでいない"
    assert "esp_core_dump_image_check" in src, \
        "記録があるかどうかを確認せずに読んでいる"
    assert "ESP_RST_PANIC" not in src, (
        "再起動の理由で判別している。無線で更新した後の再起動は理由が SW に"
        "なるので、直前に異常終了していても読み落とす")
    for field in ("exc_task", "exc_pc", "exc_cause", "bt",
                  "app_elf_sha256"):
        assert field in src, f"要約に {field} が入っていない"
    hello = c_function(APP_CTRL, "cmd_hello")
    assert 'cJSON_AddStringToObject(j, "crash", app_state_last_crash())' in hello, \
        "HELLO が crash を返していない"
    assert says(COMM, "`crash` は、装置に異常終了の記録が残っているときだけ"), \
        "comm-protocol.md が crash を説明していない"


def test_the_button_task_has_room_for_what_it_does():
    """ボタンのタスクのスタックが、そこでやることに足りていること。

    2048 バイトで実機が異常終了した。長押しは記録を出しながら実行を停止して操作権を
    手放す。記録の出力だけで `_vfprintf_r` が 800 バイト使う(実物の ELF を
    逆アセンブルして測った。経路ぜんぶで約 1100 バイト)。残りは 900 ほどで、
    そこにタスクの初期文脈と、割り込みが入ったときの文脈が積まれる。割り込みは、
    いったん動いているタスクのスタックに文脈を積んでから割り込み用のスタックへ
    移る(FreeRTOS の portasm.S)。
    """
    m = re.search(r'xTaskCreate\(btn_task, "btn", (\d+)',
                  APP_BUTTON.read_text(encoding="utf-8"))
    assert m, "app_button.c に btn タスクがない(このテストを見直すこと)"
    assert int(m.group(1)) >= 4096, (
        f"ボタンのタスクのスタックが小さい: {m.group(1)} バイト")


def test_the_error_codes_in_the_document_are_the_ones_in_the_code():
    """エラーの一覧が、装置が返すコードと PC 側が付けるコードに一致すること。

    一覧にないコードが返ると、読む人はその意味を調べられない。返らないコードが
    一覧にあると、起きないことへの備えを書かせてしまう。
    """
    doc = COMM.read_text(encoding="utf-8")
    sect = doc[doc.index("## エラー応答"):doc.index("## 装置の状態と LED")]
    table = set(re.findall(r"^\| `([A-Z_]+)`", sect, re.M))
    inner = sect[sect.index("装置の中の失敗は"):sect.index("PC 側が付ける code")]
    pc_side = sect[sect.index("PC 側が付ける code"):]
    in_fw = set(re.findall(r'send_(?:error|failure|report)\([^,]+, "([A-Z_]+)"',
                           APP_CTRL.read_text(encoding="utf-8")))
    in_doc = table | set(re.findall(r"`([A-Z_]+)`", inner))
    assert in_doc == in_fw, (sorted(in_doc - in_fw), sorted(in_fw - in_doc))
    client = (REPO / "pctool/padcue/client.py").read_text(encoding="utf-8")
    in_pc = set(re.findall(r'DeviceError\(\s*"([A-Z_]+)"', client))
    assert set(re.findall(r"`([A-Z_]+)`", pc_side)) == in_pc, in_pc


def test_the_task_table_lists_the_tasks_the_firmware_creates():
    """設計の文書のタスクの表は、ファームウェアが作るタスクと一致すること。"""
    arch = (REPO / "docs/design/firmware-architecture.md").read_text(
        encoding="utf-8")
    sect = arch[arch.index("## 2. タスクとコア"):arch.index("## 3. 実行エンジン")]
    created = set()
    for path in (REPO / "firmware/main").glob("*.c"):
        created |= set(re.findall(
            r'xTaskCreate(?:PinnedToCore)?\(\s*\w+,\s*"(\w+)"',
            path.read_text(encoding="utf-8")))
    named = set(re.findall(r"^\| (\w+)\(`", sect, re.M))
    assert "engine_init" in sect, "起動のときだけのタスクに触れていない"
    assert named | {"engine_init"} == created, (sorted(named), sorted(created))


def test_the_pc_side_log_kinds_in_the_document_have_a_sentence_on_screen():
    """グループの仕様が挙げる PC 側のログの種類は、どれも画面の文を持つこと。"""
    doc = (REPO / "docs/specs/coupling.md").read_text(encoding="utf-8")
    sect = doc[doc.index("**PC 側が書くログ**"):doc.index("**周の記録**")]
    kinds = set(re.findall(r"`(PC_[A-Z_]+)`", sect))
    lists_js = (REPO / "pctool/padcue/web/lists.js").read_text(encoding="utf-8")
    on_screen = set(re.findall(r"^\s*(PC_[A-Z_]+):", lists_js, re.M))
    # 取りこぼしの表示だけは文の表を通らず、ログの先頭の行として出す
    assert "'PC_LOG_DROPPED'" in lists_js
    on_screen.add("PC_LOG_DROPPED")
    assert kinds == on_screen, (sorted(kinds - on_screen),
                                sorted(on_screen - kinds))


def test_a_stop_reservation_while_idle_is_dropped():
    """実行していないときの「区切りで停止」の予約を、装置が捨てること。

    残すと、待機中の STATUS に予約が出たままになる。文書と模擬装置は、受け付けるが
    何も起きない、と定めている。
    """
    body = c_function(APP_ENGINE, "app_engine_stop_at_mark")
    guard = body.index("!app_engine_is_running() && !app_engine_is_awaiting()")
    store = body.index("atomic_store(&s_stop_mask, mask)")
    assert guard < store, "予約を保存してから確認している"
    assert says(COMM, "実行していないときの予約と解除は、受け付けて成功にするが")


def test_a_stop_reservation_racing_the_end_of_the_run_is_dropped():
    """確かめてから予約を書くまでの間に実行が終わっても、待機中に予約が残らないこと。

    実行を終える側(割り込みと STOP)が予約を消した後で予約を書くと、次の RUN まで
    STATUS の stop_at_mark が 0 でないまま残り、画面は予約が有効なように描く。予約の
    側は書いた後に実行中かを見直して、終わっていれば自分の予約を戻す。終える側は
    実行中の印をクリアした後で予約を消す。両方の読み書きを順序を全体で揃えて
    (seq_cst)行えば、どちらかが必ず消す。2 つのタスクの読み書きの間に割り込む順は
    ホストのハーネスで作れないので、ソースで確認する。
    """
    body = c_function(APP_ENGINE, "app_engine_stop_at_mark")
    store = body.index("atomic_store(&s_stop_mask, mask)")
    recheck = body.index("!atomic_load(&s_running)")
    undo = body.index("atomic_compare_exchange_strong(&s_stop_mask")
    assert store < recheck < undo, "書いた後に実行中かを見直していない"
    for func in ("halt_engine", "app_engine_stop"):
        body = c_function(APP_ENGINE, func)
        assert (body.index("atomic_store(&s_running, false)")
                < body.index("atomic_store(&s_stop_mask, 0)")), func
    # 予約を消すのは、終える 2 か所と、実行を始めるとき(実行中の印を立てる前)だけ
    src = APP_ENGINE.read_text(encoding="utf-8")
    assert src.count("&s_stop_mask, 0") == 3, \
        "実行中の印をクリアする前に予約を消す所がある"
    start = c_function(APP_ENGINE, "app_engine_start")
    assert (start.index("atomic_store(&s_stop_mask, 0)")
            < start.index("atomic_store_explicit(&s_running, true"))


def test_the_last_mark_carries_the_frame_it_was_passed_at():
    """区切りを通った時点のフレーム数を、装置も模擬装置も last_mark に含めること。

    PC はこれと frames_elapsed の差から「通ってからの経過」を出す。状態を読んだ
    時刻をそのまま使うと、状態を読む間隔(devicepool.DeviceLink.POLL_S の 1 秒)の
    ぶん遅れ、図の線が区切りのたびに最大 1 秒戻って見える。
    """
    ctrl = c_function(APP_CTRL, "cmd_status")
    assert 'cJSON_AddNumberToObject(lm, "frame"' in ctrl
    engine = c_function(APP_ENGINE, "store_last_mark")
    assert "s_last_mark.frame = s_pending_abs_frame" in engine
    mock = (REPO / "pctool/padcue/mockdevice.py").read_text(encoding="utf-8")
    assert '"frame": point.frame' in mock
    assert says(COMM, "`frame` がその区切りを通った時点の経過フレーム数")


def test_the_state_is_shown_by_its_name():
    """装置の状態を、STATUS も設定コンソールもシリアルのログも、名前で出すこと。

    番号だけでは、app_state.h の列挙を読まないと何の状態か分からない。診断モードの
    設定コンソールを使う利用者は、ソースを持っていない。
    """
    header = (REPO / "firmware/main/app_state.h").read_text(encoding="utf-8")
    enum = re.search(r"typedef enum \{(.*?)\} app_state_t;", header, re.S).group(1)
    states = re.findall(r"^\s*APP_STATE_(\w+)", enum, re.M)
    names = dict(re.findall(r'case APP_STATE_(\w+): return "(\w+)";',
                            c_function(APP_STATE, "app_state_name")))
    assert names == {s: s for s in states}, names
    provision = REPO / "firmware/main/app_provision.c"
    assert "app_state_name(app_state_get())" in c_function(provision, "do_status")
    assert "状態番号" not in provision.read_text(encoding="utf-8")
    assert "app_state_name(" in c_function(APP_STATE, "note_transition")
    assert "app_log_kind_name(" in c_function(APP_STATE, "app_state_fault")


def test_the_gptimer_setting_has_the_name_of_this_esp_idf():
    """GPTimer の割り込みをフラッシュの操作中も止めない設定を、今の ESP-IDF の
    名前で書くこと。

    古い名前も ESP-IDF の読み替えで有効になるが、ESP-IDF の文書で検索しても
    見つからない。
    """
    defaults = (REPO / "firmware/sdkconfig.defaults").read_text(encoding="utf-8")
    assert "CONFIG_GPTIMER_ISR_CACHE_SAFE=y" in defaults
    for path in (REPO / "firmware/sdkconfig.defaults", APP_ENGINE,
                 REPO / "docs/design/firmware-architecture.md"):
        assert "ISR_IRAM_SAFE" not in path.read_text(encoding="utf-8"), path.name
