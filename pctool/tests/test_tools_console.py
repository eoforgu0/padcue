"""pctool/tools/ のツールが、日本語を表せないコンソールでも異常終了しないこと。

ツールはどれも日本語を出す。英語の Windows のコンソール(cp1252)のように、その字を
表せない文字コードだと、素の print は UnicodeEncodeError で異常終了する。日本語の
環境では起きないので、テストにしておかないと CI で初めて気づくことになる。
"""
import os
import subprocess
import sys

import pytest

from tests.conftest import REPO

TOOLS = REPO / "pctool" / "tools"
# 引数なしで実行すると、使い方を出すか、入力がないと表示して、すぐ終わるツール
NAMES = ["accept", "accept_device", "runbook_walk",
         "set_wifi_serial", "shoot", "uicheck"]
# set_wifi_serial はポートを省くと装置の COM ポートを探して開き、改行を送る。
# 診断モードの装置がこの PC につながっていると、テストがその装置と通信してしまう。
# ないポートを渡して、開けずに終わる経路を通す
ARGS = {"set_wifi_serial": ["NO-SUCH-PORT"]}


def test_every_tool_is_listed():
    """ツールを追加したら、このテストの一覧にも追加すること。"""
    found = sorted(p.stem for p in TOOLS.glob("*.py") if not p.stem.startswith("_"))
    assert found == sorted(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_the_tool_survives_a_console_that_cannot_print_japanese(name):
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    got = subprocess.run([sys.executable, str(TOOLS / f"{name}.py"),
                          *ARGS.get(name, [])],
                         input=b"\n", capture_output=True, env=env,
                         cwd=REPO, timeout=120)
    err = got.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in err, err[-400:]
    assert "Traceback" not in err, err[-400:]
