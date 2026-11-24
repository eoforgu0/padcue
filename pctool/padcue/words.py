"""利用者が読む文のつなぎ方(gui-principles.md §7-12)と、値の見せ方。

句点は、1 文なら付けず、2 文以上なら各文に付ける。理由の文を別の文に埋め込むと、
「。。」や、最後の文だけ句点のない形になりやすい。文をつなぐときはこれを通す。
"""
from __future__ import annotations

import errno
import json
import os
import re


def sentences(*parts: str) -> str:
    """空でない文を順につなぐ。2 文以上になったら、各文の末尾に句点を付ける。

    部分は 1 文でも、句点で区切った複数の文でもよい。末尾の句点の有無は問わない。
    """
    texts = [p.strip().rstrip("。") for p in parts if p and p.strip()]
    joined = "。".join(texts)
    return joined + "。" if "。" in joined else joined


def as_json(v) -> str:
    """ファイルに書いた値を、ファイルに書く形(JSON)で見せる。

    Python の表記(True・None・'x')は、手順や条件のファイルを書く人が見る形と違う。
    """
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def error_text(e: Exception) -> str:
    """保存・読み込みの失敗を画面の文にする。

    ファイルの読み書きの失敗は、errno・ファイルの場所・英語の理由を出さずに、
    次にできることを書く。それ以外(名前が不正など)は、利用者に見せるために
    書いた文ならそのまま画面の文である。Python が書いた英語の文は、想定して
    いない失敗の文にする(unexpected_text)。
    """
    if isinstance(e, OSError) \
            and not isinstance(e, (ConnectionError, TimeoutError)):
        if isinstance(e, FileNotFoundError) and _too_long(e):
            return ("ファイルのパスが長すぎます。名前を短くするか、プロジェクトの"
                    "フォルダーを浅い場所に移してください。")
        if isinstance(e, FileNotFoundError):
            return ("ファイルが見つかりません。プロジェクトのフォルダーを移動または"
                    "削除していないか確認してください。")
        if isinstance(e, PermissionError):
            return ("ファイルにアクセスできません。ほかのアプリで開いていないか、"
                    "読み取り専用になっていないかを確認してください。")
        if e.errno == errno.ENOSPC:
            return ("ディスクの空き容量が足りません。空き容量を確保してから、"
                    "もう一度実行してください。")
        return ("ファイルを読み書きできません。"
                "ほかのアプリで開いていないか確認してください。")
    return str(e) if for_user(e) else unexpected_text(e)


# Windows のパスの長さの上限(終端の文字を含めて 260。file-format.md §2)。名前が
# 長いか、プロジェクトのフォルダーが深いと、新しく作る物でも上限を超え、
# FileNotFoundError(WinError 3 か 206)になる
_PATH_MAX = 259
_FILENAME_EXCED_RANGE = 206


def _too_long(e: OSError) -> bool:
    """パスが長すぎて開けなかったか。

    Windows はパスの長さを UTF-16 の単位で数える(絵文字は 1 文字が 2 単位)。
    置き換え(一時ファイルから正式な名前へ)は 2 つ目の名前も見る。
    """
    if getattr(e, "winerror", None) == _FILENAME_EXCED_RANGE:
        return True
    for name in (e.filename, e.filename2):
        if isinstance(name, (str, os.PathLike)):
            full = os.path.abspath(name)
            if len(str(full).encode("utf-16-le", "surrogatepass")) // 2 > _PATH_MAX:
                return True
    return False


# かなと漢字。利用者に見せる文は日本語で書くので、これを含まない例外の文は、
# Python や OS が書いた英語である
_JA = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
# 例外の文が引用した値('…' と "…")。Python の英語の文も、引用した値には日本語が
# 入りうる(int("あ") の文など)
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def for_user(e: Exception) -> bool:
    """例外の文が、利用者に見せるために書いた文か(引用の外に日本語がある)。"""
    return bool(_JA.search(_QUOTED.sub("", str(e))))


def unexpected_text(e: Exception) -> str:
    """想定していない例外の文。コマンドと操作画面のサーバーの処理(gui.py)が使う。

    英語の文は原因を調べる手がかりなので、括弧に入れて付ける(gui-decisions.md)。
    """
    return f"予期しないエラーが発生しました({type(e).__name__}: {e})"


# 装置の状態の名前。画面(web/core.js の STATES)と同じ言い方にする。装置が返す
# 符号は内部の名前なので、利用者に見せる所ではこれに替える
STATE_JA = {"BOOT": "起動中", "WIFI_CONNECTING": "Wi-Fi 接続中", "IDLE": "待機中",
            "RUNNING": "実行中", "AWAITING": "選択待ち", "ERROR": "異常停止",
            "OTA": "更新中"}


def state_ja(code) -> str:
    """装置の状態の符号を、画面と同じ名前にする(知らない符号はそのまま)。"""
    return STATE_JA.get(code, str(code))


def busy_text(name: str, lanes: list[str]) -> str:
    """実行中のレーンが使っているために消せないことを伝える文。"""
    return f"「{name}」は {'・'.join(lanes)} の実行で使用しているため削除できません"
