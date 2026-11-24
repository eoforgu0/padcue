"""道具が出力フォルダーの中に作る作業フォルダーを、実行のたびに作り直す部品。

作り直すのは、道具が作った目印のあるフォルダーだけである。目印のない空でない
フォルダーは、利用者が置いた物かもしれないので、消さずに作り直しを拒否する。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# 道具が作ったフォルダーに置く目印
MARK = ".padcue-workdir"


def rebuild_workdir(work: Path) -> bool:
    """work を空の作業フォルダーとして作り直す。作れなければ文を出して False を返す。

    前回の道具が作ったフォルダーは、中身ごと消して作り直す。空でないフォルダーに
    目印がなければ、何も消さずに False を返す。
    """
    if work.exists():
        if any(work.iterdir()) and not (work / MARK).is_file():
            print(f"{work} は、このツールが作ったフォルダーではないため作り直せません。"
                  "別の出力フォルダーを指定してください。", file=sys.stderr)
            return False
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / MARK).write_text(
        "道具が作ったフォルダー。次の実行で作り直す。\n", encoding="utf-8")
    return True
