"""ファイルの置き換えと、フォルダーの消去。

書き込みの途中で失敗しても元の中身が残るよう、同じフォルダーの一時ファイルに
書いてから置き換える。別のスレッドやプロセスが一瞬開いているための拒否は、少し
待ってやり直す(retry_while_in_use)。
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def retry_while_in_use(op: Callable[[], T]) -> T:
    """op() を呼んで、その結果を返す。PermissionError なら、少し待って呼び直す。

    Windows は、別のスレッドやプロセスがファイルを開いている瞬間や置き換えている
    瞬間だと、置き換え・読み込み・消去・フォルダーの名前の変更を PermissionError
    (WinError 5 か 32)で拒否する。開いている時間は短いので、少し待ってやり直せば
    通る。5 ms から倍にして 200 ms までの間隔で 12 回待ち(合計 1.5 秒)、それでも
    駄目なら最後の例外のまま上げる(黙って諦めると「保存したつもり」の変更が
    消える)。PermissionError でない例外は、待たずにそのまま上げる。
    """
    delay = 0.005
    for _ in range(12):
        try:
            return op()
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
    return op()


def replace_file(tmp: Path, dst: Path) -> None:
    """一時ファイル tmp で dst を置き換える(書き込み途中で異常終了しても壊れた
    ファイルが残らない)。

    Windows は、別のスレッドやプロセスが dst を読んでいる瞬間だと置き換えを
    PermissionError(WinError 5)で拒否する。読む側は毎秒の状態取得のような短い
    ものなので、少し待ってやり直す(retry_while_in_use)。
    """
    retry_while_in_use(lambda: tmp.replace(dst))


def remove_tree(path: Path) -> None:
    """フォルダー path を中身ごと消す(もうなければ何もしない)。

    Windows は、別のスレッドやプロセスが中のファイルを開いている瞬間だと、消去を
    PermissionError(WinError 32)で拒否する。読む側は画面の取得のような短いもの
    なので、少し待って消し直す(retry_while_in_use)。途中まで消えた残りも、
    やり直しで消える。
    """
    def remove() -> None:
        if path.exists():
            shutil.rmtree(path)
    retry_while_in_use(remove)


def remove_file(path: Path) -> None:
    """ファイル path を消す(もうなければ何もしない)。

    Windows は、別のスレッドやプロセスが path を開いている瞬間だと、消去を
    PermissionError(WinError 32)で拒否する。少し待って消し直す
    (retry_while_in_use)。
    """
    retry_while_in_use(lambda: path.unlink(missing_ok=True))


def read_text(path: Path) -> str:
    """path を UTF-8 で読む。先頭の BOM は外す。

    利用者が手で編集するファイル(登録情報、手順、プリセット、条件、並び順)はこれで
    読む。エディターによっては BOM を付けて保存する。インポートとフレーム表と同じく
    外して読む(JSON の規格も、BOM を無視してよいとしている)。

    Windows は、別のスレッドが replace_file で置き換えている瞬間に開くと
    PermissionError(WinError 5)で拒否する。毎秒の状態の取得が登録情報を読むので、
    保存と重なると画面に「予期しないエラー」が出る。置き換えはすぐ済むので、
    少し待って読み直す(retry_while_in_use)。
    """
    return retry_while_in_use(
        lambda: path.read_text(encoding="utf-8").removeprefix("﻿"))


def write_file(dst: Path, data: str | bytes) -> None:
    """dst を、同じフォルダーの一時ファイルに書いてから置き換える。

    開いて書き直すと、書いている途中の失敗(空き容量の不足など)で元の中身まで
    失う。利用者が作った手順・フレーム表・プリセットと、登録情報、送った手順の
    コピー、動いているグループの記録は、この形で書く。

    置き換える前に中身をディスクへ書き出す(fsync)。電源が切れると、名前の
    付け替えが中身より先にディスクへ書き込まれることがあり、置き換えた後のファイルが
    0 バイトや 0 で埋まった中身になる(元の中身も一時ファイルも残らない)。

    一時ファイルの名前には、プロセスとスレッドの識別子を入れる。コマンド(別の
    プロセス)と操作画面のサーバーが同じファイルを同時に書くと、固定の名前では、片方が
    書いている途中の一時ファイルをもう片方が置き換える。
    """
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        # 文字列は Path.write_text と同じく文字のモードで書く(改行の扱いを変えない)
        with (open(tmp, "w", encoding="utf-8") if isinstance(data, str)
              else open(tmp, "wb")) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        replace_file(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# write_file の一時ファイルの名前(<置き換え先の名前>.<プロセス>.<スレッド>.tmp)
_UNFINISHED = re.compile(r".+\.\d+\.\d+\.tmp")
# 書いて置き換えるのは 1 秒もかからない。置いてからこれだけたった一時ファイルは、
# 終わらなかった書き込みの残りである
_UNFINISHED_AGE_S = 60.0


def sweep_unfinished_writes(folder: Path, below: bool = False) -> int:
    """folder に残った、write_file の書きかけの一時ファイルを消し、その数を返す。

    書いている途中にプロセスが強制終了されると(電源が切れた、タスクを終了させた)、
    一時ファイルが置き換えられずに残る。置き換え先は前の中身のまま無事だが、残った
    一時ファイルは誰も消さない。

    消すのは、一時ファイルの名前の形で、置いてから 1 分たったファイルだけである。
    置いたばかりの物は、別のプロセス(コマンド)がいま書いている途中かもしれない。
    below が真なら、下のフォルダーの中も見る。
    """
    limit = time.time() - _UNFINISHED_AGE_S
    n = 0
    for here, dirs, names in os.walk(folder):
        if not below:
            dirs.clear()
        for name in names:
            if not _UNFINISHED.fullmatch(name):
                continue
            path = Path(here, name)
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
                    n += 1
            except OSError:
                continue
    return n
