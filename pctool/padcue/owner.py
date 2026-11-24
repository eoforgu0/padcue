"""操作権の識別子(この PC のこの padcue ぶん)。

装置の操作権は 1 つで、装置を動かす、装置に入っているものや設定を変える、USB を
再接続する、異常を解除する、「区切りで停止」の予約を取り消すのに必要である
(comm-protocol.md の操作権)。
誰が持っているかは、この識別子で区別する。

識別子は padcue ごと、つまりこのコードのツリーごとに 1 つである。同じ padcue の
画面もコマンドもテストのツールも同じ値を送るので、互いに操作権を奪い合わない。
別のツリー(開発用の別の作業場)は別の値になるので、そちらから実行しようと
すると拒否される。これが目的である。待機中の装置へ、別のツールがうっかり実行を
送る事故を防ぐ。

値は初回に作る乱数で、ツリーの直下(project.padcue_root)の `.padcue-client-id` に
置く。git では追跡しない(複製したツリーが同じ識別子を送ると、区別できなく
なる)。
書けない場所にツリーがあるときは、その実行の間だけ有効な値を使う。
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

from .project import padcue_root

_FILE = ".padcue-client-id"
_cached = ""
# 最初の呼び出しが重なっても、同じ値を返す(装置ごとの収集のスレッドが同時に
# 送る)。別々の値を送ると、先に送った方の値で取った操作権が残る
_lock = threading.Lock()


def client_id() -> str:
    """この padcue の識別子。なければ作る。

    形は 32 文字の 16 進で、通信プロトコルが許す文字(英数字と - と _)に
    収まる。呼ぶたびに読み書きしないよう、最初の 1 回だけ憶える。
    """
    global _cached
    with _lock:
        if not _cached:
            _cached = _read_or_make(padcue_root() / _FILE)
        return _cached


def _read_or_make(path: Path) -> str:
    """ファイルの識別子を読む。なければ作って書く。

    作るときは、ないときだけ作る形で開く。同じツリーの別のプロセス(コマンドと
    操作画面)が同時に作っていたら、そちらが書いた値を読み直して使う。
    """
    got = _read(path)
    if got:
        return got
    text = secrets.token_hex(16)
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(text + "\n")
        return text
    except FileExistsError:
        # 別のプロセスが同時に作った。書き終わるのを少し待って読み直す。空の
        # ままなら(中身のないファイルが残っていた)、作った値で書き直す
        time.sleep(0.1)
        got = _read(path)
        if got:
            return got
        try:
            path.write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass
        return text
    except OSError:
        return text     # 書けなくても、その実行の間は使える


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
