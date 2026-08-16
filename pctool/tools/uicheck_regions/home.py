"""実行・監視の画面(1台のときのレーン)。"""
from __future__ import annotations

from ._harness import Checker, chip_text, lane, wait_state


def run_home(c: Checker, page):
    """実行・監視の画面(1台のときのレーン)。"""
    # ================= 実行・監視 =================
    # 1台構成の検査。装置台数に関わらず常にレーン
    # (原則 §1 系「1台と2台は同型」)なので、ここは #lanes .lane が1本だけの
    # 状態を前提に、run_multi/run_coupling と同じ流儀(lane()・has_text)で
    # レーンを操作する。接続・診断は装置カードの開閉式詳細(dev_row)側。
    print("[実行・監視]", flush=True)

    def t_lane_smoke():
        """1台構成でもレーンが1本出て、実行・停止・開始ラベルが働く。

        原則 §1 系「1台と2台は同型」の1台側の土台。台数で構造を変えない、
        という前提がまず崩れていないかをここで確かめ、以降の検査はその上に
        乗る。
        """
        assert page.locator("#lanes .lane").count() == 1, "レーンが1本出ていない"
        ln = lane(page)
        assert "1P" in ln.locator("h2").inner_text(), "レーンの見出しに装置名が無い"
        assert chip_text(page) == "待機中", chip_text(page)
        names = ln.locator(".lproc option").all_inner_texts()
        assert names == ["周回で変える", "素材周回", "選んで進む"], names
        assert ln.locator(".lloops").input_value() == "0", \
            f"周回の既定が 0 でない: {ln.locator('.lloops').input_value()!r}"
        assert ln.locator("button", has_text="今の周で止める").is_disabled(), \
            "停止中に区切り停止が押せる"
        assert ln.locator("button", has_text="今すぐ止める").is_disabled(), \
            "停止中に即時停止が押せる"
        assert ln.locator("button", has_text="周回実行").is_enabled(), \
            "待機中に実行が押せない"
        # 実行・停止も働くこと(開始ラベルは後続の検査で個別に見る)
        ln.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
    c.check("1台構成でもレーンが1本出て、実行・停止・開始ラベルが働く",
            t_lane_smoke)
