"""装置の状態の項目。`padcue status` と操作画面の「状態を表示」が同じ項目を出す。

経路ごとに書き分けると、片方にだけ出る行が生まれる。行と順はここで決める。
"""
from __future__ import annotations

import unicodedata

from . import __version__
from .client import firmware_problem
from .words import state_ja

# 直前の再起動が異常終了によるものだったことを示す、装置の再起動の理由
# (reset_reason)。このとき装置は、異常終了した場所の記録をフラッシュに書いてから
# 再起動している
_CRASH_RESETS = ("PANIC", "INT_WDT", "TASK_WDT")
# Switch との初期化が終わったときの到達段階(procon-protocol.md)
_BREADCRUMB_READY = 0x7fb
# ラベルの幅(セル)。コロンの前を、最も長い項目名(USB の問い合わせの間隔)の
# 幅に合わせる
LABEL_CELLS = 22


def _pairing_rows(d: dict) -> list[tuple[str, str]]:
    """ペアリングの観測値。

    本体側にこの装置の登録記録がないと、本体は新規ペアリング(フェーズ 0x01)を
    再要求し続け、完了するまですべての入力を無視する。それが起きていることを、
    この行がないと外から切り分けられない。
    """
    step = int(d.get("pair_step") or 0)
    reqs = int(d.get("pair_reqs") or 0)
    # 値が 2 つ以上あるので、続きの行に名前と値の対で並べる(status_rows と同じ)
    seen = [("", f"直近のフェーズ 0x{step:02x}"), ("", f"受けた要求 {reqs} 回")]
    if step in (0x01, 0x02):
        return [("Switch 登録", "⚠ 未完了"), *seen,
                ("", "Switch への登録が完了していないため、入力が無視されることが"
                     "あります")]
    if step:
        return [("Switch 登録", "受理済み"), *seen]
    return [("Switch 登録", "要求なし")]


def arm_list(names: list) -> str:
    """選択肢を、`padcue select` に渡す番号つきで書く。"""
    return " / ".join(f"{i} {n}" for i, n in enumerate(names, 1))


def status_rows(d: dict) -> list[tuple[str, str]]:
    """状態の項目を、(ラベル, 値) の並びで返す。ラベルが空の行は前の行の続き。

    d は /api/state の装置の項目と同じ形である。
    """
    why = firmware_problem(d.get("fw") or "")
    # スリープ中も USB の接続は続いている(装置は Switch が起きるのを待つ)
    usb = ("未接続" if not d.get("usb_mounted")
           else "スリープ中" if d.get("usb_suspended") else "接続")
    rows = [
        ("padcue", __version__),
        ("ファームウェア", f"{d.get('fw')} ({d.get('partition')})"
         + (f"  ⚠ {why}" if why else "")),
        # 項目名は設定の欄と同じにする。値は数と単位だけ(値の中に項目名を書かない)
        ("USB の問い合わせの間隔", f"{d.get('binterval')}ms"),
        *([("", f"次の起動から {d['binterval_saved']}ms")]
          if d.get("binterval_saved") else []),
        ("状態", state_ja(d.get("state"))),
        ("USB", usb),
        ("到達段階", f"0x{d.get('breadcrumb', 0):03x}"),
        ("ジャイロ", "本体で有効化済み" if d.get("imu_enabled")
         else "本体からの有効化なし。送信した値は使用されません。"),
        *_pairing_rows(d),
    ]
    if d.get("awaiting") and d.get("arm_names"):
        rows.append(("選択肢", arm_list(d["arm_names"])))
    if d.get("running"):
        rows.append(("実行中",
                     f"{d.get('proc', '')} {d.get('frames_elapsed')} フレーム"))
    # ずれの測定値は 0 でも出す。出ていないのが「遅れていない」のか
    # 「測っていない」のか分からないと、計測値として意味を成さない。値が 2 つある
    # 項目は、1 つの値の中に詰めず、続きの行に名前と値の対で並べる
    late = f"フレーム処理 {d.get('max_late_us', 0)}µs"
    over = d.get("late_events", 0) + d.get("deliver_late", 0)
    if over:
        late += f"  ⚠ 超過 {over} 回"
    rows += [("最大遅延", late),
             ("", f"読み取り待ち {d.get('deliver_max_us', 0)}µs")]
    if d.get("log_dropped"):
        rows.append(("記録落ち",
                     f"⚠ {d['log_dropped']} 件。この間の記録は残っていません。"))
    # 失った応答は、再送しても送れずに捨てたものと、壊れていて捨てたものである。
    # failed_replies は失敗した試行の回数で、後の再送で送れた応答も含むので数えない
    # (装置の TX_LOST の記録と同じ数え方)
    lost_reply = d.get("dropped_replies", 0) + d.get("bad_reports", 0)
    if lost_reply or d.get("dropped_inputs"):
        rows += [("送信失敗", f"⚠ 応答 {lost_reply} 件"),
                 ("", f"通常入力 {d.get('dropped_inputs', 0)} 件")]
    if d.get("rolled_back"):
        rows.append(("更新", "⚠ 更新前のファームウェアに戻りました"))
    if d.get("crash"):
        # 装置は動いている間シリアルのログを出さないので、異常終了の場所を知る手段は
        # ここだけである。装置が返した要約をそのまま出す。装置は
        # 記録を消さないので、要約は以前の起動のものでありうる。直前の再起動が
        # 異常終了によるものだったときだけ「前回」と書く
        rows.append(("前回の異常終了", f"⚠ {d['crash']}")
                    if d.get("reset_reason") in _CRASH_RESETS
                    else ("過去の異常終了", d["crash"]))
    return rows


def _cells(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def status_line(label: str, value: str) -> str:
    """コマンドの出力の 1 行。コロンの位置を揃える。ラベルが空なら前の行の続きで、
    値の桁に合わせる。"""
    if not label:
        return f"{' ' * (LABEL_CELLS + 2)}{value}"
    return f"{label}{' ' * (LABEL_CELLS - _cells(label))}: {value}"


def status_lines(d: dict) -> list[str]:
    """コマンドの出力の行。端末やフォントによって幅が変わる文字(⚠)を、桁を担う
    位置に置かないよう、記号は値の側に置く。"""
    return [status_line(label, value) for label, value in status_rows(d)]


def connection_summary(d: dict) -> str:
    """Switch との接続の状態と、次にすること。"""
    if not d.get("usb_mounted"):
        return ("Switch に USB で接続されていません。ドックとケーブルを確認するか、"
                "「USB を再接続」を実行してください。")
    if d.get("usb_suspended"):
        return "Switch がスリープ中です"
    if int(d.get("pair_step") or 0) in (0x01, 0x02):
        return ("Switch への登録が完了していません。本体の「持ちかた/順番を変える」"
                "画面を開いたまま、「USB を再接続」を実行してください。")
    if (d.get("breadcrumb", 0) & _BREADCRUMB_READY) != _BREADCRUMB_READY:
        return ("Switch との初期化が完了していません。「USB を再接続」を実行して"
                "ください。")
    return "Switch に接続しています"
