"""条件とスクリプトで、選択待ちの選択肢を自動で選ばせる(coupler とは別のスレッド)。

装置が分岐の選択待ちで止まっている間、その分岐の選択肢に付いた「選択条件」を
上から順に確認し、最初に成立した選択肢を装置に選ばせる(P14、P16)。選択待ちは
手順の中に何か所でも置け、くり返しの中や入れ子の中にも置けるので、
``BuildResult.awaits`` は複数の項目を持つ。いまどれで止まっているかは、装置の状態の
``event_index``(選択待ちの間は、止まっている位置の番号)と、awaits の各項目の
``index`` を照合して決める。これを行うのは ``current_await`` だけである。

このモジュールが確認する「選択条件」は、次の 2 つである。
- 条件: 映像が基準画像に一致するか
- 選択肢のスクリプト: コマンドの終了コードが 0 か

選択肢のスクリプトは、読み取るだけのコマンドであることを前提にする。待っている
間は、判定の周期(``POLL_S``)ごとにくり返し実行する。条件と同じく毎回確認し直すのが
正しい動きなので、2 回目からの実行をやめる仕組みは持たない。副作用のあるコマンドを
書いたときの結果は、書いた人の責任である。副作用のある処理には、手順のブロックの
「スクリプト」を使う。起動できない、締め切り(``GUARD_BUDGET_S``)までに終わらない、
0 以外で終わる、のどれも「成立しない」として扱い、次の選択肢と次の周期へ進む。
例外で監視のスレッドを異常終了させない。出力はコンソールにも出さない(毎周期の出力が積もる)。
起動できない、締め切りまでに終わらない、0 以外で終わってエラー出力に何か書いた、の
ときは、理由を記録に 1 度だけ残す(``PC_GUARD_ERROR``)。

選択待ちが手順のブロックの「スクリプト」のとき(止まっている位置の項目が
``"script"`` を持つとき)は、条件を確認する代わりに、この PC でコマンドを実行する。
装置から見ると選択肢が 1 つだけの分岐で、装置は止まって PC を待つだけである。
capture に映像ソースの名前があれば、その映像を PNG で一時ファイルに書き、環境変数で
スクリプトに渡す。wait が真なら終わるのを待ってから、偽なら起動しただけで、選択肢 0 を
選ばせる。実行はレーンごとに別のスレッドで行うので、終わるのを待つ間も、ほかの
レーンの判定は待たされない。起動や実行に失敗しても、選択肢 0 は選ばせる(装置を
選択待ちのまま残さない)。

判定もレーンごとに別のスレッドで行う。1 本のレーンが持つ判定の仕事は 1 つまでで、
前の判定が終わっていないレーンは、その周期を飛ばす。条件だけの選択待ちでは、判定が
終わったら待たずに次の判定を始める(画面が変わってから選ぶまでを短くするため)。
ただし判定の始まりどうしは ``AGAIN_S`` 空ける。
選択肢のスクリプトを含む選択待ちと、選択待ちでないレーンは、前の判定を終えて
から ``POLL_S`` 空けて見る(スクリプトを間を空けずに起動し続けない)。あるレーンの
映像の取り込みが遅れても、画像の比較に時間がかかっても、ほかのレーンの判定の周期は
変わらない(要件 R13 の「1 台の異常や無応答がほかの台の表示と操作を妨げない」)。映像の
取り込みを待つ長さには上限があるので、固まった取り込みでそのレーンの判定が止まったままに
なることもない(``_SharedSource``)。操作画面のサーバーの取り込み(``take_frame``)も同じ
``_SharedSource`` を通る。

装置に送るのは選択の指示(SELECT)だけで、装置のロック(``DeviceLink.lock``)は
そのときだけ取る。映像の取得とスクリプトの実行は装置と関係がないので、ロックの
外で行い、coupler の装置の操作を長く待たせない。

coupler(グループと連動停止)とは別のスレッドで動く。分岐の選択肢を自動で
選ばせるのはこのモジュールだけである。合流点を進める SELECT は coupler が送る。
条件もスクリプトも持たない選択待ち(人が選ぶ選択肢しかない)は、人が選ぶか
時間切れで進むので、ここでは何もしない。
"""
from __future__ import annotations

import concurrent.futures
import json
import locale
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path

from .client import DeviceError
from .conditions import (
    ConditionStore,
    aspect_differs,
    crop_to,
    is_lane_source,
    lane_crop,
    lane_file_why,
    make_source,
    matches,
    resolve_lane_source,
    thin_to_view,
    validate_source,
    validate_video,
)
from .frames import encode_png_fast
from .project import ConfigError
from .sources import (
    LANE_VIDEO_NAME,
    DShowSource,
    dshow_unknown,
    find_source_by_name,
    normalize_source_name,
)
from .words import error_text


def is_lane_video_name(name: str) -> bool:
    """手順に書いた映像ソースの名前が「装置に設定している映像」か。"""
    return normalize_source_name(name) == normalize_source_name(LANE_VIDEO_NAME)


def _frame_png(frame) -> bytes:
    """Frame を PNG にする(スクリプトに渡す画像。圧縮より速さを取る)。"""
    return encode_png_fast(frame.width, frame.height, frame.rgb)


# 条件が判定する映像が「装置に設定している映像」なのに、開始した時点で装置に映像が
# 設定されていなかったときの理由。判定は開始した時点の映像で行う(coupling.md D12)
# ので、待っている間に映像を設定しても、この実行の判定には使われない。そのため設定を
# 勧めず、開始したときのこととして書く(実行の外の文は conditions.NO_LANE_VIDEO_WHY)
STARTED_WITHOUT_VIDEO_WHY = "開始したとき、この装置に映像が設定されていませんでした"

# 記録に残すコマンドの文字数。python のスクリプトを絶対パスで書くと 80 文字を
# 簡単に超え、どのスクリプトが動いたのか記録から分からなくなる。200 文字は
# Windows の絶対パスと引数 2 つが収まる長さである
LOG_CMD_CHARS = 200

# 記録に残す、選択肢のスクリプトのエラー出力の文字数。Python の例外の最後の
# 行(例外の種類と文)が収まる長さにする
LOG_WHY_CHARS = 200


# 終了の合図(threading.Event)を見る間隔(秒)。待っているものを、合図から
# この長さのうちに切り上げる
_STOP_POLL_S = 0.05


def _run_shell(cmd: str, env: dict, timeout: float,
               stop: threading.Event | None = None, *,
               cwd=None, stdout=None, stderr=None) -> int:
    """コマンドをシェルで実行し、終了コードを返す。

    時間内に終わらなければ、シェルが起動したプロセスまで一緒に終了させてから
    subprocess.TimeoutExpired を投げる。subprocess.run の timeout はシェルしか
    終了させないので、Windows では cmd.exe だけが終わり、cmd.exe が起動した
    コマンドは残る。遅いスクリプトでは、残ったプロセスが判定の周期ごとに増える。

    stop が立ったときも、同じく終了させてから subprocess.TimeoutExpired を投げる。
    cwd と stdout と stderr は Popen
    にそのまま渡す(出力の既定は操作画面のコンソール)。
    """
    # Windows 以外では新しいプロセスグループで起動し、グループごと終了させる
    extra = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, shell=True, env=env, cwd=cwd, stdout=stdout,
                            stderr=stderr, **extra)
    end = time.monotonic() + timeout
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0 or (stop is not None and stop.is_set()):
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                return proc.wait(
                    timeout=left if stop is None else min(left, _STOP_POLL_S))
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise


def _last_line(f) -> str:
    """エラー出力を書かせた一時ファイルから、空でない最後の行を返す。

    前の行が読点(「、」か「,」)で終わっていれば、文の続きとみてつなぐ。
    Windows のシェルは、コマンドがないことを「'x' は、内部コマンドまたは外部
    コマンド、」「操作可能なプログラムまたは…」の 2 行に分けて書くので、
    最後の行だけでは何がないのかが抜ける。Python の例外の最後の行
    (「ZeroDivisionError: …」)は、前の行とつながない。

    読むのは終わりの 4 KB だけである。子のプロセスの文字コードは決まって
    いない。padcue.bat は Python に UTF-8 を使わせるが、cmd.exe などは
    Windows の既定の文字コードで書く。そこで、UTF-8 で読めなければ既定の
    文字コードで読む。
    """
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(max(0, size - 4096))
    raw = f.read()
    lines = raw.splitlines()
    if size > 4096 and lines:
        lines = lines[1:]       # 途中から読んだ最初の行は欠けている
    lines = [ln.strip() for ln in lines if ln.strip()]
    if not lines:
        return ""
    last = [lines.pop()]
    while lines and lines[-1].endswith(("、".encode(), "、".encode("cp932"), b",")):
        last.insert(0, lines.pop())
    try:
        parts = [ln.decode("utf-8") for ln in last]
    except UnicodeDecodeError:
        enc = locale.getpreferredencoding(False)
        parts = [ln.decode(enc, "replace") for ln in last]
    line = parts[0]
    for part in parts[1:]:
        # 日本語の文の折り返しは語の区切りではないので、空白を挟まずにつなぐ
        both = line[-1:].isascii() and part[:1].isascii()
        line += (" " if both else "") + part
    return _short(line, LOG_WHY_CHARS)


def _short(text: str, limit: int) -> str:
    """記録に書く長い文を limit 文字に切り詰める。切ったら「…」で終える。"""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _kill_tree(proc: subprocess.Popen) -> None:
    """proc と、proc が起動したプロセスを一緒に終了させる。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass            # 一緒に終了させられなくても、少なくともシェルは下で終了させる
    try:
        proc.kill()
    except OSError:
        pass            # もう終わっている
    proc.wait()


def current_await(project, status: dict, dev: str) -> tuple[str, dict] | None:
    """いま止まっている選択待ちの項目(awaits の 1 つ)と、その手順の名前を
    (proc, awt) で返す。

    ないときは None を返す。実行中の手順の変換の結果が登録されていない、手順に
    選択待ちがない、止まっている位置に対応する項目がない、のどれかである。

    装置は手順を中身から作った id で区別するので、状態の "proc" は実行中の
    手順の id である。その id で送った手順のコピーを探す。手順の名前から変換し直さない
    ので、実行中に利用者が手順のファイルを編集しても判定は続く。装置に入って
    いるレコードは転送したときのままなので、送った手順のコピーにある変換の結果は装置の中身と
    必ず一致する。

    止まっている選択待ちを選ぶ手順は、この 1 か所だけに置く(P26)。
    """
    proc_id = status.get("proc")
    if not proc_id:
        return None
    art = project.sent.built(proc_id, dev)
    if art is None or not art.awaits:
        return None                # 登録されていないか、選択待ちがない
    # 選択待ちの間の event_index は、止まっている位置の番号である
    awt = next((a for a in art.awaits
                if a.get("index") == status.get("event_index")), None)
    if awt is None:
        return None
    return art.name, awt


def _capture_env(capture: list, paths: dict[int, str]) -> dict:
    """スクリプトへ渡す環境変数。取り込んだ画像の場所を、capture の番号で渡す。

    PADCUE_CAPTURE_COUNT は capture の数で、PADCUE_CAPTURE_i は i 番目の映像
    ソースの画像である。取り込めなかったソースの番号は置かない。取れた分を前から
    詰めると、どの画像がどのソースかがスクリプトから分からなくなる。親の
    プロセスが同じ名前の変数を持っていても(padcue を、padcue のスクリプトから
    起動したときなど)受け継がない。受け継ぐと、取り込めなかった番号に別の実行の
    画像が残る。
    """
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("PADCUE_CAPTURE_")}
    env["PADCUE_CAPTURE_COUNT"] = str(len(capture))
    for i, p in paths.items():
        env[f"PADCUE_CAPTURE_{i}"] = p
    return env


def human_only(project, status: dict, dev: str, failed=()) -> bool:
    """いま止まっている待ちが「人が選ばないと進まない」ものか。

    条件、スクリプト、時間切れ、相手の到着(合流点)で終わる待ちは、人を
    呼ばなくても進む。通知を出すか(notify)と、選択肢の外周を目立たせるか
    (画面)は同じ問いなので、規則はここ 1 か所に置く。別々に書くと、目立つのに
    知らせないレーンが出る。

    手順の成果物が読めないときは、自動の判定そのものができない
    ので、選択肢が 2 つ以上あれば進められるのは人だけである。

    failed は、この待ちで判定できなかった選択肢の番号である
    (CondWatcher.judge_failures)。条件かスクリプトの結果で進む選択肢がすべて
    判定できず、時間切れもなければ、進められるのは人だけである。知らせずに待ち
    続けると、利用者が気づくまで止まったままになる。スクリプトが 0 以外で終わるのは
    「成立しない」という普通の結果なので、判定できなかったことには数えない
    (起動できない・時間内に終わらない・エラー出力を書いて 0 以外で終わる、を数える)。
    """
    if project is None:
        return False
    found = current_await(project, status, dev)
    if found is None:
        return int(status.get("await_arms") or 0) >= 2
    _proc, awt = found
    if awt.get("join") or awt.get("script"):
        return False
    if awt.get("on_timeout"):
        return False            # 時間切れの選択肢がある = 待てば進む
    conds = list(awt.get("conds") or [])
    guards = list(awt.get("guard_scripts") or [])
    auto = ({i for i, c in enumerate(conds) if c}
            | {i for i, g in enumerate(guards) if g})
    if not auto:
        return True
    return auto <= set(failed)


def arm_kinds(awt: dict) -> list[str]:
    """選択肢ごとの「選択条件」。画面は、これで選択肢のボタンの見た目を決める。

    "manual"(人が選ぶ)・"condition"(条件)・"script"(スクリプト)・
    "timeout"(時間切れで進む先)のどれかである。どれも人は選べる。人が
    選ばなくても進む選択肢だけを、画面は主ボタンでない見た目にする。
    """
    conds = list(awt.get("conds") or [])
    guards = list(awt.get("guard_scripts") or [])
    late = int(awt.get("on_timeout") or 0) - 1      # 時間切れの先(なければ -1)
    out = []
    for i in range(len(awt.get("names") or [])):
        if i < len(conds) and conds[i]:
            out.append("condition")
        elif i < len(guards) and guards[i]:
            out.append("script")
        elif i == late:
            out.append("timeout")
        else:
            out.append("manual")
    return out


# 取り込みが待つ上限を過ぎたときの、判定できない理由。映像機器は ffmpeg が待つので、
# ここに来るのは、応答しないウィンドウ(PrintWindow が戻らない)と、開くのが遅い
# 機器である
GRAB_LATE_WHY = ("時間内に映像を取得できません。映像ソースのアプリや機器が"
                 "動いているか確認してください。")


def _wait_until(done: threading.Event, limit: float,
                stop: threading.Event | None) -> bool:
    """done が立つまで、時刻 limit(time.monotonic)まで待つ。

    stop が立てば、そこで待つのをやめる。done が立っていれば真を返す。
    """
    while True:
        left = limit - time.monotonic()
        if left <= 0 or (stop is not None and stop.is_set()):
            return done.is_set()
        if done.wait(left if stop is None else min(left, _STOP_POLL_S)):
            return True


class _Grab:
    """1 つの映像ソースで進行中の、1 回の取り込み。"""

    def __init__(self, kind: str, wait: float):
        self.kind = kind                    # "frame"(判定用)か "png"(スクリプト用)
        self.wait = wait                    # 始めた人が待つ長さ(秒)
        self.started = time.monotonic()
        self.done = threading.Event()
        self.result = None
        self.why = ""                       # 取れなかった理由
        self.error: Exception | None = None  # 映像ソースが投げた例外


class _SharedSource:
    """1 つの映像ソースを、レーンとスクリプトと画面が順番に使えるようにするための
    共有のラッパー。

    取り込みは別のスレッドで行い、呼び出し元は決めた長さだけ待つ。ウィンドウの
    取り込み(PrintWindow)は、対象のウィンドウが固まっていると戻らないことが
    あり、そのスレッドは外から終了させられない。待つのをやめても、スレッドは残る。

    そこで、1 つのソースの取り込みは同時に 1 回までにする。前の取り込みが
    終わっていなければ、新しく始めずにその終わりを待つ。その取り込みが、
    自分が待つ長さより長く続いていれば、固まっているとみなして待たずに
    時間切れにする。前の取り込みを始めた人の待つ長さでは測らない。締め切りが
    迫った選択肢のスクリプトが始めた取り込みは、すぐにその長さを過ぎるが、
    固まっているとは限らない。こうすると、固まった
    ソースがあっても取り込みのスレッドはソースごとに 1 本で増えず、同じ
    ソースを 2 つのスレッドから同時に使うこともない。映像機器は 2 つの
    ffmpeg から同時には開けず、ウィンドウの映像ソースは決めたウィンドウを中に持つ。

    進行中の取り込みが同じ種類なら、その結果を分けてもらう。1 つの映像を
    切り出して 2 台を映すときは、2 本のレーンが同じソースを同じ周期に見る。
    画面(装置の映像のダイアログと、条件の編集のいまの映像)も、実行中のレーンと同じ
    映像機器を見ることがある(CondWatcher.take_frame)。
    """

    # 映像機器の取り込みは、ffmpeg が自分の待ち時間で戻る。その戻りをこの秒数
    # だけ余分に待ってから打ち切る。こうすると、時間切れの理由は、ほかのアプリで
    # 使用していないかの確認を促す映像機器の文になる
    DSHOW_SLACK_S = 2.0

    def __init__(self, src):
        self.src = src
        self._lock = threading.Lock()
        self._grab: _Grab | None = None      # 進行中の取り込み
        # 映像機器の映像ソースが持つ、ffmpeg を待つ長さ
        self._dshow_timeout = (src.timeout if isinstance(src, DShowSource)
                               else None)
        # 最後に使った時刻(time.monotonic)。使わなくなった映像ソースを閉じるのに使う
        self._used = time.monotonic()

    def idle(self, since: float) -> bool:
        """時刻 since(time.monotonic)から使われておらず、取り込みも進行中でないか。

        固まって戻らない取り込みも、進行中に数える。
        """
        with self._lock:
            return self._grab is None and self._used < since

    def take(self, kind: str, wait: float,
             stop: threading.Event | None = None,
             raising: bool = False) -> tuple[object, str]:
        """kind("frame" か "png")の 1 枚を取り、(結果, 取れなかった理由) を返す。

        待つのは wait 秒までである。取れなかったときの結果は None で、理由は
        空でない。stop が立ったときだけは、すぐに (None, "") を返す。映像ソースが
        例外を投げたときは、その文を理由にする。raising が真なら、その例外を
        投げ直す。
        """
        end = time.monotonic() + wait
        while True:
            with self._lock:
                self._used = time.monotonic()
                g = self._grab
                if g is None:
                    g = self._grab = _Grab(kind, max(0.0, end - time.monotonic()))
                    threading.Thread(target=self._run, args=(g,), daemon=True,
                                     name="condwatch-grab").start()
            slack = self.DSHOW_SLACK_S if self._dshow_timeout is not None else 0.0
            if not _wait_until(g.done, min(end, g.started + wait) + slack, stop):
                if stop is not None and stop.is_set():
                    return None, ""
                return None, GRAB_LATE_WHY
            if g.kind == kind:
                if raising and g.error is not None:
                    raise g.error
                return g.result, g.why
            # 種類の違う取り込みが終わった。続けて自分の取り込みを始める

    def _run(self, g: _Grab) -> None:
        """取り込みのスレッド。終わったら、待っている人に知らせる。"""
        src = self.src
        try:
            if self._dshow_timeout is not None:
                # ffmpeg を待つのも、始めた人が待つ長さまでにする。取り込みは
                # 同時に 1 回なので、映像ソースの値を書き換えてよい
                src.timeout = max(0.01, min(self._dshow_timeout, g.wait))
            g.result = src.grab_png() if g.kind == "png" else src.grab()
            if g.result is None:
                g.why = getattr(src, "last_error", "") or "映像を取得できません"
        except Exception as e:   # noqa: BLE001  映像ソースが契約を破っても待つ人へ返す
            g.result = None
            g.error = e
            g.why = error_text(e)
        finally:
            with self._lock:
                if self._grab is g:
                    self._grab = None
            g.done.set()

    def close(self) -> None:
        try:
            self.src.close()
        except Exception:   # noqa: BLE001  後始末は best effort
            pass


class CondWatcher:
    """選択待ちの自動の判定(条件と選択肢のスクリプト)。coupler、RunWatcher と同じく、
    操作画面のサーバーが 1 つ持ち、自分のスレッドで動く。"""

    POLL_S = 1.0
    # 条件だけの選択待ちを待たずに判定し直すときの、判定の始まりどうしの最小の
    # 間隔。映像機器から受け取るのは毎秒 10 枚(sources._DShowStream.FPS)なので、
    # これより速く判定しても新しい画面はない。デスクトップ・ウィンドウ・画像
    # ファイルには次の 1 枚を待つ所がないので、間を置かないと連続して判定し続ける
    AGAIN_S = 0.1

    # wait=false で起動したスクリプトが読む一時フォルダーは、スクリプトが終わったら
    # 消す。終わらないスクリプトのぶんも、作ってからこの秒数を過ぎたら消す。
    # 個数で片付けると、短い間隔で続けて起動したときに、1 回目のフォルダーが
    # まだ読まれている最中に消える。10 分は、取り込みを伴うスクリプトが読み
    # 終わるのに十分で、なお溜まり続けない長さである
    BG_TMP_KEEP_S = 600.0
    # wait が真のスクリプトを待つ上限(秒)。終わらないコマンドで、そのレーンが
    # 選択待ちのまま残らないようにする
    SCRIPT_TIMEOUT_S = 300.0
    # 選択肢のスクリプトを待つ、1 周期あたりの合計の上限(秒)。
    # 選択肢の数ぶんではなく合計である。1 本ずつ順に待つと選択肢の数に比例して
    # 長くなり、人が選ぶより遅くなる。requirements.md の許容条件「判断点では数秒の遅延を
    # 許容する」に収まらない。並行に実行し、映像の取り込みも含めて合計 5 秒で
    # 打ち切れば、選択肢の数を増やしても 1 周期の長さは変わらない
    GUARD_BUDGET_S = 5.0
    # 映像の取り込みを 1 回待つ上限(秒)。条件の判定と、手順のブロックの
    # スクリプトに渡す取り込みに使う(選択肢のスクリプトに渡す取り込みは、
    # GUARD_BUDGET_S の締め切りの中で行う)。映像機器(ffmpeg)を待つ既定の
    # 長さと同じである。過ぎたら、その選択肢はこの周期では判定できないものと
    # し、次の周期で取得し直す
    GRAB_TIMEOUT_S = 5.0
    # close が、レーンの判定の仕事が終わるのを待つ上限(秒)。取り込みと
    # 選択肢のスクリプトの待ちは終了の合図ですぐに切り上げるので、普通は
    # すぐに終わる。上限を設けるのは、画像の比較の計算は途中で打ち切れないから
    # である。上限を過ぎて残った仕事も、終了の合図の後は装置に選択の指示を
    # 送らない
    CLOSE_WAIT_S = 2.0
    # 使わなくなった映像ソースを閉じるまでの時間(秒)。使っている映像ソースは
    # 判定の周期ごとに使うので、1 分使わなかったものは、定義を変えたか、
    # 選択待ちを抜けたものである
    SOURCE_IDLE_S = 60.0
    # スクリプトへ渡す取り込み画像を置く一時フォルダーの名前の頭
    TMP_PREFIXES = ("padcue_script_", "padcue_guard_")

    def __init__(self, project, pool, autostart: bool = True):
        self.project = project
        self.pool = pool
        # スクリプトのブロックの待ちを進めた直後に予約する止め方を返す関数
        # (装置 ID -> mask。0 なら予約しない)。実行中に周回数を変えるとき、
        # スクリプトの途中では停止させず、終わってから続きを実行し始める前に停止させる
        # (coupler.lap_ends.after_script)。操作画面のサーバーが渡す
        self.after_select = None
        # 終了の合図。close が立てる。立つと、監視のスレッドの周期の待ちと、
        # レーンの判定の中の取り込みと選択肢のスクリプトの待ちがすぐに終わる
        self._halt = threading.Event()
        # 監視が失敗した直近の理由(同じ理由でコンソールが埋まらないよう、
        # 変わったときだけ記録する。coupler.py / notify.py と同じ流儀)。
        # 監視のスレッドだけが読み書きする
        self._tick_error = ""
        # ここから _sources までのキャッシュは、監視のスレッド、レーンの判定の仕事、
        # 選択肢のスクリプトを並行に実行するスレッド、手順のブロックの
        # スクリプトのスレッド、操作画面のサーバーと通知(judge_failures)が
        # 読み書きする。_memo_lock を取って読み書きし、ロックを持ったまま待たない
        # (取り込み、スクリプトの実行、記録、装置の操作はロックの外で行う)
        self._memo_lock = threading.Lock()
        # レーンの判定の仕事(装置の ID -> スレッド)。1 本のレーンに 1 つまで
        self._judge_jobs: dict[str, threading.Thread] = {}
        # レーンの次の判定を始めてよい時刻(装置の ID -> time.monotonic)。条件だけの
        # 選択待ちは判定が終わった時刻、それ以外は前の判定を始めてから POLL_S 後
        self._next_at: dict[str, float] = {}
        # 判定が終わったことを監視のスレッドに知らせ、待たずに次の判定を始めさせる
        self._kick = threading.Event()
        # 手順のブロックのスクリプトを起動した選択待ち(装置の ID -> await_gen)。
        # 選択の指示を送れずに次の周期でまた見ても、任意のコマンドを 2 回実行
        # しないための目印。選択待ちを抜けたら _check_link が消す
        self._script_ran: dict[str, int] = {}
        # 手順のブロックのスクリプトを実行しているスレッド(装置の ID -> スレッド)
        self._script_jobs: dict[str, threading.Thread] = {}
        # そのスクリプトを終了させる合図(装置の ID -> 合図)。レーンがその選択待ちを
        # 抜けたら立てる(_stop_script)
        self._script_stops: dict[str, threading.Event] = {}
        # レーンごとの、いまの選択待ちで判定できなかった条件。{レーンの名前:
        # {"gen": その選択待ちの await_gen, "fails": [{"arm": 番号,
        # "condition": 条件の名前, "why": 理由}], "aspects": [基準画像と映像の
        # 縦横の比が違う条件の名前]}}。選択待ちを抜けたら消す
        self._judge_failed: dict[str, dict] = {}
        # レーンごとの、前の周期に見た選択待ちの await_gen。周期の合間に選択待ちを
        # 抜けて次の選択待ちへ入ると、抜けたことは状態に現れない。これと比べて知る
        self._wait_gen: dict[str, object] = {}
        # 記録に書いた判定の失敗。{(レーンの名前, 条件の名前): 理由}。判定は毎秒
        # くり返すので、同じレーンの同じ条件は理由が変わったときだけ書く。キーを
        # レーンと条件の組にするのは、失敗が 2 つ以上あるときに、毎周期それぞれを
        # 書き直さないためである。選択待ちを抜けたら、そのレーンの分を消す
        self._cond_errors: dict[tuple[str, str], str] = {}
        # 記録に書いた、選択肢のスクリプトで判定できなかった理由。
        # {(レーンの名前, コマンド): 理由}。_cond_errors と同じく、理由が変わった
        # ときだけ書き、選択待ちを抜けたらそのレーンの分を消す
        self._guard_errors: dict[tuple[str, str], str] = {}
        # 記録に書いた、スクリプトに渡せなかった映像ソース。{(レーンの名前, 名前):
        # 理由}。選択肢のスクリプトは毎周期実行するので、_cond_errors と同じく、
        # 理由が変わったときだけ書き、選択待ちを抜けたらそのレーンの分を消す
        self._capture_skips: dict[tuple[str, str], str] = {}
        # 名前から探した映像ソース。{レーンの名前: {名前: 映像ソースの指定}}。
        # 選択待ちに入って最初に探したものを、抜けるまで使う。見つからなかった名前は
        # キャッシュせず、次の周期で探し直す(待っている間に OBS を起動したときに
        # 見つける)
        self._resolved: dict[str, dict[str, dict]] = {}
        # この PC で持ち回す映像ソース(定義 -> _SharedSource)。周期ごとに作り
        # 直すと、ウィンドウの映像ソースが毎回探し直すことになり、実行の途中で
        # 同じ名前の別のウィンドウに入れ替わる。キーは解決した後の定義
        # なので、レーンの映像を変えれば次の周期から新しい映像ソースになる。
        # 同じ定義を使うレーンとスクリプトは、同じ _SharedSource を順番に使う
        self._sources: dict[str, _SharedSource] = {}
        # wait が偽で作った一時フォルダーを、(作った時刻, フォルダー, 起動した
        # プロセス)で古い順に持つ。掃除に使う。スクリプトのスレッドと close の
        # 両方が使うので、読み書きは _tmp_lock の下で行う
        self._bg_tmpdirs: list[tuple[float, str, subprocess.Popen]] = []
        self._tmp_lock = threading.Lock()
        # 監視のスレッド。start と close だけが読み書きする
        self._thread: threading.Thread | None = None
        # 前回までの実行が残した一時フォルダーを片付ける。wait=false のぶんは、
        # スクリプトがまだ動いていれば終了のときにも残すので(下の close)、ここで
        # 消さないと PC の一時フォルダーに溜まり続ける
        self._sweep_stale_tmp()
        # autostart=False はテスト用(tick と pick_index を手で呼んで確認する)
        if autostart:
            self.start()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._halt.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="condwatch")
        self._thread.start()

    def close(self) -> None:
        """監視を終了する。レーンの判定の仕事が終わるのを CLOSE_WAIT_S 秒まで待つ。

        戻らない取り込みのスレッドは終了させられないので待たない(_SharedSource)。
        その結果は誰も使わない。手順のブロックのスクリプトも待たない。
        閉じた後は装置との接続も閉じるので、実行を終えたスクリプトの選択の指示は
        送られない。装置はその選択待ちに残り、次に padcue を起動したときに、
        スクリプトをもう一度実行してから選ぶ(どの選択待ちで実行したかは、
        このプロセスの中にしか持たない)。
        """
        self._halt.set()
        self._kick.set()
        end = time.monotonic() + self.CLOSE_WAIT_S
        with self._memo_lock:
            jobs = list(self._judge_jobs.values())
        for job in [self._thread, *jobs]:
            if job is not None and job is not threading.current_thread():
                job.join(max(0.0, end - time.monotonic()))
        self._close_sources()
        # 終わったスクリプトと、残す時間を過ぎた一時フォルダーを消す。まだ動いて
        # いるスクリプトのものは、読んでいる最中かもしれないので残し、次の起動で
        # 片付ける
        self._drop_old_bg_tmp()

    def _drop_old_bg_tmp(self) -> None:
        """wait=false の一時フォルダーのうち、スクリプトが終わったものと、残す時間を
        過ぎたものを消す。"""
        cutoff = time.monotonic() - self.BG_TMP_KEEP_S
        old = []
        with self._tmp_lock:
            keep = []
            for item in self._bg_tmpdirs:
                made, _d, proc = item
                (old if made < cutoff or proc.poll() is not None
                 else keep).append(item)
            self._bg_tmpdirs = keep
        for _made, d, _proc in old:
            shutil.rmtree(d, ignore_errors=True)

    def _sweep_stale_tmp(self) -> None:
        """一時フォルダーに残った、残す時間を過ぎた取り込み用のフォルダーを消す。

        名前の頭と古さの両方で絞り込む。別の padcue(デモ用など)がいま使って
        いるフォルダーは新しいので、消さない。
        """
        try:
            base = Path(tempfile.gettempdir())
            limit = time.time() - self.BG_TMP_KEEP_S
            for d in base.iterdir():
                if not d.name.startswith(self.TMP_PREFIXES) or not d.is_dir():
                    continue
                if d.stat().st_mtime < limit:
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass            # 片付けは本筋ではない。読めなければ次の起動に任せる

    def _source_for(self, resolved: dict) -> _SharedSource:
        """解決した定義の _SharedSource を返す。同じ定義なら同じものを使い回す。

        定義の書き方の違い(条件に書いた定義と、レーンの映像の設定)で別の
        _SharedSource にならないよう、正規化してからキーにする。壊れた定義は
        ValueError。
        """
        spec = validate_source(resolved)
        key = json.dumps(spec, ensure_ascii=False, sort_keys=True)
        idle = []
        with self._memo_lock:
            got = self._sources.get(key)
            if got is None:
                # 定義を変えるたびに増えるので、しばらく使っていないものを畳む。
                # 取り込みが進行中のものは残す。畳んで作り直すと、固まった
                # 取り込みの上に 2 番目の取り込みを重ねることになる
                since = time.monotonic() - self.SOURCE_IDLE_S
                for k, s in list(self._sources.items()):
                    if s.idle(since):
                        idle.append(self._sources.pop(k))
                got = self._sources[key] = _SharedSource(make_source(spec))
        for s in idle:
            s.close()
        return got

    def _close_sources(self) -> None:
        with self._memo_lock:
            dropped = list(self._sources.values())
            self._sources.clear()
        for s in dropped:
            s.close()

    def take_frame(self, source: dict) -> tuple[object, str]:
        """映像ソースのいまの 1 枚を取り、(Frame, 取れなかった理由) を返す。

        操作画面のサーバーが、装置の映像のダイアログと、条件の編集のいまの映像に使う。
        実行中のレーンの判定と同じ _source_for で取り込むので、同じ映像ソースを判定と
        同時には開かない。判定の取り込みが進行中なら、その結果を分けてもらう。
        待つのは GRAB_TIMEOUT_S 秒までで、画面の要求のスレッドで待つ。_SharedSource の
        ロックを持ったまま待たないので、判定のスレッドは画面の待ちに付き合わない。

        壊れた定義は ValueError を投げる。映像ソースが投げた例外は、そのまま投げ直す
        (想定外の失敗として、操作画面のサーバーが報告を促す)。
        """
        return self._source_for(source).take(
            "frame", self.GRAB_TIMEOUT_S, raising=True)

    # ---- 監視 ----

    def _loop(self) -> None:
        while not self._halt.is_set():
            # 先にクリアする。判定の仕事が _dispatch の最中に終わって立てた合図を、
            # 下の待ちで受け取る
            self._kick.clear()
            wait = self.POLL_S
            try:
                wait = self._dispatch(paced=True)[1]
                # 終わった wait=false のスクリプトの一時フォルダーを、次に起動する
                # のを待たずに消す
                self._drop_old_bg_tmp()
                self._tick_error = ""
            except Exception as e:   # noqa: BLE001  監視は続ける
                msg = f"{type(e).__name__}: {e}"
                if msg != self._tick_error:
                    self._tick_error = msg
                    # 登録情報が壊れているときは、画面の見出しが理由を示す
                    if not isinstance(e, ConfigError):
                        traceback.print_exc()
            self._kick.wait(wait)

    def tick(self) -> None:
        """プールのすべての装置を 1 周見て、始めた判定が終わるまで待つ。

        判定を 1 周だけ手で行うときに使う(テスト)。監視のスレッドは、判定の
        終わりを待たない _dispatch を使う。待つと、遅いレーンの判定の分だけ、
        ほかのレーンの次の判定が遅れる。
        """
        for job in self._dispatch()[0]:
            job.join()

    def _dispatch(self, paced: bool = False) -> tuple[list[threading.Thread], float]:
        """前の判定が終わっているレーンごとに、判定の仕事を 1 つ始める。

        前の判定が終わっていないレーンは、この周期を飛ばす。判定の中の
        取り込み、画像の比較、選択肢のスクリプトのどれが遅れても、遅れるのは
        そのレーンだけである。レーンは装置の ID(registered_id)で区別する。名前は
        変えられるので、名前で区別すると、変えた直後に 1 本のレーンの判定が
        2 つ並ぶ。

        paced が真なら、次の判定を始めてよい時刻(_next_at)の来ていないレーンも
        飛ばす(監視のスレッド)。偽ならすべてのレーンを見る(tick)。返すのは、
        始めた仕事のスレッドと、飛ばしたレーンの時刻が来るまでの秒数(POLL_S まで)
        である。
        """
        started: list[threading.Thread] = []
        seen = set()
        now = time.monotonic()
        wait = self.POLL_S
        for link in self.pool.links():
            key = link.registered_id
            seen.add(key)
            with self._memo_lock:
                if self._halt.is_set():
                    break
                job = self._judge_jobs.get(key)
                if job is not None and job.is_alive():
                    continue        # 前の判定が終わっていない
                at = self._next_at.get(key, now)
                if paced and at > now:
                    wait = min(wait, at - now)
                    continue
                job = threading.Thread(target=self._judge_lane, args=(link,),
                                       daemon=True, name="condwatch-judge")
                self._judge_jobs[key] = job
                job.start()
            started.append(job)
        with self._memo_lock:
            for key in [k for k, t in self._judge_jobs.items()
                        if k not in seen and not t.is_alive()]:
                del self._judge_jobs[key]     # プールから外れたレーン
                self._next_at.pop(key, None)
        return started, wait

    def _judge_lane(self, link) -> None:
        """1 本のレーンの判定の仕事(レーンごとのスレッド)。"""
        began = time.monotonic()
        again = False
        try:
            again = self._check_link(link)
        except Exception:   # noqa: BLE001  1 台の想定外でほかを妨げない
            traceback.print_exc()
        finally:
            # 待たずに判定し直すときは、始まりどうしを AGAIN_S 空ける。それ
            # 以外は、終わってから POLL_S 空ける(1 秒以上かかる選択肢の
            # スクリプトを、終わるたびにすぐ起動し直さない)
            with self._memo_lock:
                self._next_at[link.registered_id] = (
                    max(time.monotonic(), began + self.AGAIN_S) if again
                    else time.monotonic() + self.POLL_S)
            self._kick.set()

    def _note_failures(self, lane: str, got: dict | None) -> None:
        """そのレーンの、いまの選択待ちで判定できなかった条件と、縦横の比が違う
        条件を記録する(None で消す)。
        """
        with self._memo_lock:
            if got is None:
                self._judge_failed.pop(lane, None)
            else:
                self._judge_failed[lane] = got

    def _started_with(self, link, st: dict) -> tuple:
        """実行中の手順の、開始した時点の定義の場所と、装置に設定した映像。

        判定は開始した時点の条件と映像で行う(coupling.md D12)。コマンドから転送した
        手順には保存物がないので、今のフォルダーと今の設定を使う。
        """
        art = self.project.sent.built(st.get("proc") or "", link.registered_id)
        if art is None or not art.sources:
            return self.project.root, link.cfg.get("video")
        return self.project.sent.frozen_root(art.sources), art.video

    def _check_link(self, link) -> bool:
        """レーンの選択待ちを 1 回判定する。待たずに次の判定を始めてよいなら真。"""
        if link.error:
            return False
        if not link.status_at:
            # まだ 1 度も状態を取れていない(装置の改名でリンクを作り直した直後
            # など)。「分からない」を「選択待ちでない」とみなすと、下でスクリプトを
            # 2 回実行しないための目印を消してしまう
            return False
        st = link.status or {}
        name = link.cfg.get("name") or ""
        if not (st.get("running") and st.get("awaiting")):
            self._leave_wait(name)
            # どの選択待ちにもいない(停止した、実行を終えた、選ばれて先へ
            # 進んだ)。スクリプトを 2 回実行しないための目印は、その選択待ちの
            # 間だけ必要なので、抜けたら消す。装置が再起動すると実行は必ず終わり、
            # await_gen は起動からの通し番号として 0 から数え直すので、再起動の
            # 後に gen がたまたま記録した値と同じになっても、目印は既に消えている
            with self._memo_lock:
                self._script_ran.pop(link.registered_id, None)
            self._stop_script(link.registered_id)
            return False
        gen = st.get("await_gen")
        with self._memo_lock:
            moved = self._wait_gen.get(name, gen) != gen
        if moved:
            self._leave_wait(name)      # 周期の合間に、次の選択待ちへ移っていた
            self._stop_script(link.registered_id)
        with self._memo_lock:
            self._wait_gen[name] = gen
        found = current_await(self.project, st, link.registered_id)
        # 判定しない選択待ちでは、判定できなかった条件を持たない(残すと、
        # 条件のない選択肢に古い理由が付く)
        if found is None:
            self._note_failures(name, None)
            return False
        # proc は手順の名前である(スクリプトへ渡す PADCUE_PROC に使う)
        proc, awt = found
        script = awt.get("script")
        if script:
            self._note_failures(name, None)
            self._run_script(link, st, proc, script)
            return False
        conds = list(awt.get("conds") or [])
        guard_scripts = list(awt.get("guard_scripts") or [])
        if not any(conds) and not any(guard_scripts):
            self._note_failures(name, None)
            return False           # どの選択肢も人が選ぶ。自動の判定はしない
        fails: list[dict] = []
        aspects: list[str] = []
        root, video = self._started_with(link, st)
        idx = self.pick_index(conds, guard_scripts,
                              video=video,
                              lane=name, proc=proc, fails=fails,
                              dev_id=link.registered_id, root=root,
                              aspects=aspects)
        if self._halt.is_set():
            return False           # 終了の合図の後は、記録も選択の指示も残さない
        self._note_failures(name, {"gen": gen, "fails": fails, "aspects": aspects})
        if idx is None:
            # どれも一致しない。条件だけの選択待ちで、どの条件も判定できたなら、
            # 待たずに次の判定を始める。判定できなかった条件(映像が取れない)
            # があるときと、選択肢のスクリプトを含むときは、次の周期で判定し直す
            return not fails and not any(guard_scripts)
        # 選ぶ前に、装置の記録を読んで書く。装置は選択待ちに入ったときに、待ちに移った
        # 状態の行を書いている。待ちに入った時点で条件がもう当てはまっているとすぐ
        # 選ぶので、次の収集を待つと、その行を受け取る時刻が選んだ行より後になり、
        # 選んでから待ちに入ったように読める(合流点を進める coupler の _tick_joins と
        # 同じ)。選んだ行の時刻は SELECT を送る前の時刻にする。送った後に装置が書く
        # 「待ち → 実行中」はそれより後に受け取るので、選んだ行の後に並ぶ
        link.read_logs_now()
        at = time.time()

        def go(c, idx=idx, gen=gen):
            c.select(idx, gen=gen)
            # どの選択肢へ進んだかは装置の状態に出ないので、ここで覚える(レーンの図を
            # その選択肢で描き、止まる先をその選択肢でたどる。DeviceLink.note_pick)
            link.note_pick(gen, idx, st)
            return c.status()
        try:
            link.write_through(status=link.call(go))
        except (DeviceError, OSError):
            return False           # 次の周期でまた判定し直す
        names = awt.get("names") or []
        # 名前のない選択肢は、画面と同じ既定名で残す(空だとログで「」になる)
        arm_name = (names[idx] if idx < len(names) else "") or f"選択肢 {idx + 1}"
        if idx < len(conds) and conds[idx]:
            self._log(link, conds[idx], arm_name, at)
        else:
            guard = guard_scripts[idx] if idx < len(guard_scripts) else {}
            self._log_guard(link, guard.get("cmd") or "", arm_name, at)
        return False               # 選んだ。次の選択待ちは次の周期で見る

    def _leave_wait(self, lane: str) -> None:
        """そのレーンが選択待ちを抜けたときに、その選択待ちの間だけの記録を消す。

        判定できなかった条件、記録に書いた失敗、名前から探した映像ソースは、
        どれも 1 つの選択待ちの間だけのものである。次の選択待ちへ持ち越すと、
        同じ失敗を記録に書かず、条件のない選択肢に古い理由が付く。
        """
        with self._memo_lock:
            self._judge_failed.pop(lane, None)
            self._wait_gen.pop(lane, None)
            for key in [k for k in self._cond_errors if k[0] == lane]:
                del self._cond_errors[key]
            for key in [k for k in self._guard_errors if k[0] == lane]:
                del self._guard_errors[key]
            for key in [k for k in self._capture_skips if k[0] == lane]:
                del self._capture_skips[key]
            self._resolved.pop(lane, None)

    def _run_guards(self, guard_scripts: list, video: dict | None,
                    lane: str, proc: str, dev_id: str = "") -> dict:
        """選択肢のスクリプトを並行に実行し、{何番目: (成立したか, 理由)} を返す。

        理由は、判定できなかったとき(起動できない・締め切りまでに終わらない・
        エラー出力を書いて 0 以外で終わる)だけ空でない(_run_guard)。

        待つのは 1 周期あたり合計 GUARD_BUDGET_S で、選択肢の数ぶんではない。
        締め切りまでに返らなかったものは、その周期では不一致として扱う
        (次の周期でまた確認し直すので、取りこぼしは残らない)。

        先にすべてまとめて実行するのは、選ぶ順(上から最初に成立したもの)と
        実行する順を切り離すためである。順に実行して待つと、上の選択肢が
        遅いだけで下の選択肢の判定が遅れる。

        映像の取り込みとスクリプトの実行は、どちらも締め切りまでで打ち切り、
        終了の合図が立てばすぐに打ち切る(_run_guard)。だから、ここですべての
        終わりを待っても周期は延びず、close も待たされない。
        """
        jobs = [(i, g) for i, g in enumerate(guard_scripts)
                if isinstance(g, dict) and g.get("cmd")]
        if not jobs:
            return {}
        deadline = time.monotonic() + self.GUARD_BUDGET_S
        out: dict = {}
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(jobs)) as pool:
            futs = {
                pool.submit(self._run_guard, g, video, lane, proc, deadline,
                            dev_id): i
                for i, g in jobs}
            for fut in futs:
                i = futs[fut]
                left = max(0.0, deadline - time.monotonic())
                try:
                    out[i] = fut.result(timeout=left)
                except Exception:   # noqa: BLE001  返らない・例外を出したは不一致
                    out[i] = (False, "")
        return out

    def judge_failures(self, lane: str, gen=None) -> list[dict]:
        """そのレーンの、いまの待ちで判定できなかった条件と選択肢のスクリプト
        (直近の周期)。

        gen を渡すと、その世代の待ちで判定したものだけを返す。監視の周期の
        合間に次の待ちへ移っていたら、前の待ちの失敗は返さない。

        操作画面のサーバーと通知のスレッドが呼ぶ。
        """
        with self._memo_lock:
            got = self._judge_failed.get(lane)
        if not got or (gen is not None and got.get("gen") != gen):
            return []
        return list(got.get("fails") or [])

    def aspect_mismatches(self, lane: str, gen=None) -> list[str]:
        """そのレーンの、いまの待ちで判定した条件のうち、基準画像とその装置の映像の
        縦横の比が違うものの名前(直近の周期)。gen は judge_failures と同じ。

        判定は続けるので、判定できなかった条件(judge_failures)には入れない。画面は
        レーンのメッセージに出す(比が違うと困る理由は conditions.aspect_differs)。
        """
        with self._memo_lock:
            got = self._judge_failed.get(lane)
        if not got or (gen is not None and got.get("gen") != gen):
            return []
        return list(got.get("aspects") or [])

    def pick_index(self, conds: list, guard_scripts: list | None = None,
                   video: dict | None = None,
                   lane: str = "", proc: str = "",
                   fails: list | None = None,
                   dev_id: str = "", root=None,
                   aspects: list | None = None) -> int | None:
        """条件と選択肢のスクリプトを上から順に確認し、最初に成立した選択肢の
        番号を返す。どれも成立しなければ None を返す。

        1 つの選択肢が持つのは、条件(conds)とスクリプト(guard_scripts)の
        どちらか一方である(両方を書いた手順は procfmt が拒否する)。条件は映像が
        基準画像に一致するか、スクリプトは終了コードが 0 かで確認する。

        装置がなくても呼べる(project だけで動く)。条件の読み込み、映像ソースの
        作成と取得、スクリプトの起動と実行のどれが失敗しても、その選択肢を
        「成立しない」として次へ進み、例外は外へ出さない。1 つの壊れた選択肢で、
        判定の全体を中断させないためである。映像の取得とスクリプトの実行は、装置の
        ロックの外で行う(呼び出し元は装置と通信しない)。

        lane はレーンの名前、proc は手順の名前で、スクリプトに環境変数で渡す。
        dev_id は装置の ID で、記録に付ける。判定できなかった条件とスクリプトは
        fails に追加する(条件は "condition" に名前を、スクリプトは "script" を真に
        する)。root は条件を読むフォルダーで、省略するとプロジェクトのフォルダーである。
        基準画像といまの映像の縦横の比が違う条件は、名前を aspects に追加して、
        そのまま判定する(conditions.aspect_differs)。
        """
        guard_scripts = list(guard_scripts or [])
        # 選択肢のスクリプトは、選ぶ前にまとめて並行に実行する
        guards = self._run_guards(guard_scripts, video, lane, proc, dev_id)
        store = ConditionStore(root or self.project.root)

        def failed(i: int, cond_name: str, why: str) -> None:
            self._log_cond_error(lane, dev_id, cond_name, why)
            if fails is not None:
                fails.append({"arm": i, "condition": cond_name, "why": why})

        for i, cond_name in enumerate(conds):
            if self._halt.is_set():
                return None
            if not cond_name:
                ok, why = guards.get(i, (False, ""))
                if ok:
                    return i
                if why and fails is not None:
                    fails.append({"arm": i, "script": True, "why": why})
                continue
            try:
                cond = store.load(cond_name)
                resolved = resolve_lane_source(cond.source, video)
                if resolved is None:
                    # 装置に設定している映像なのに、そのレーンが映像を持たない。
                    # 判定できないので、この周期は不一致として次の選択肢へ進む
                    failed(i, cond_name, STARTED_WITHOUT_VIDEO_WHY)
                    continue
                src = self._source_for(resolved)
                crop = lane_crop(video) if is_lane_source(cond.source) else None
            except Exception as e:   # noqa: BLE001  壊れた条件定義は次へ
                failed(i, cond_name, error_text(e))
                continue
            # 映像ソースは閉じない。実行の間は最初に決めたウィンドウを保つため
            # である。取り込みを待つのは GRAB_TIMEOUT_S 秒までで、過ぎたら
            # この選択肢を判定できないものとして次へ進む
            frame, why = src.take("frame", self.GRAB_TIMEOUT_S, self._halt)
            if frame is None:
                if self._halt.is_set():
                    return None
                # 取れなかった理由を記録に書く。書かないと、映像が取れずに
                # 選択待ちのまま止まっていても、理由が分からない。装置の映像の
                # ファイルが読めないときは、修正する場所(装置の「映像」)を示す
                if is_lane_source(cond.source) and why != GRAB_LATE_WHY:
                    why = lane_file_why(lane, resolved, why) or why
                failed(i, cond_name, why)
                continue
            try:
                # 判定する範囲は切り出した後の映像に対する相対である。基準画像は
                # 条件の編集が間引いた絵から作るので、同じく間引いてから比べる
                frame = thin_to_view(crop_to(frame, crop))
                # 同じ条件を 2 つの選択肢が使っても、メッセージは 1 つにする
                if (aspects is not None and cond_name not in aspects
                        and aspect_differs(cond, frame)):
                    aspects.append(cond_name)
                if matches(frame, cond):
                    return i
            except Exception as e:   # noqa: BLE001  比較できない基準画像は次へ
                failed(i, cond_name, error_text(e))
                continue
        return None

    def _log_cond_error(self, lane: str, dev_id: str, cond_name: str,
                        why: str) -> None:
        """条件を判定できなかったことを記録に書く(coupler の PC_WATCH_ERROR に相当)。

        判定は続ける(その選択肢を不一致として扱う)。ここは後から原因を
        たどれるようにするためだけにある。判定は毎秒くり返すので、同じレーンの
        同じ条件は、理由が変わったときだけ書く。記録を書けなくても判定は続ける。
        """
        key = (lane, cond_name)
        with self._memo_lock:
            if self._cond_errors.get(key) == why:
                return
            self._cond_errors[key] = why
        self._append_log({"kind": "PC_CONDITION_ERROR", "condition": cond_name,
                          "why": why}, dev_id)

    def _run_guard(self, guard: dict, video: dict | None = None,
                   lane: str = "", proc: str = "",
                   deadline: float | None = None,
                   dev_id: str = "") -> tuple[bool, str]:
        """選択肢のスクリプトを実行し、(成立したか, 判定できなかった理由) を返す。

        読み取るだけのコマンドであることを前提にし、待っている間は判定の周期
        ごとにくり返し実行する(モジュールの説明を参照)。起動できない、
        締め切りまでに終わらない、0 以外で終わる、のどれも偽を返す。
        0 以外で終わるのは「成立しない」という普通の結果でもあるので、
        理由を返すのは、起動できない・終わらない・エラー出力を書いて 0 以外で
        終わる、のときだけである(記録にも書く)。例外は外へ出さない。1 つの
        壊れたスクリプトで、判定の全体を中断させないためである。

        capture の映像は _grab_captures で取り込み、手順のブロックのスクリプトと
        同じ環境変数(PADCUE_CAPTURE_COUNT と、capture の番号の PADCUE_CAPTURE_i)で
        渡す。装置の名前と手順の名前も、同じく PADCUE_DEVICE と PADCUE_PROC で
        渡す。2 台で同じ手順を動かし、装置ごとに違う判定をしたいとき、渡さないと
        どちらの装置かが分からない。

        deadline は、その周期で待てる時刻(time.monotonic)である。並行に
        実行したもの全体で 1 つの締め切りを分け合う。映像の取り込みも、この
        締め切りの中で行う。終了の合図が立ったら、取り込みもコマンドも
        すぐに打ち切る。
        """
        cmd = guard.get("cmd") or ""
        if not cmd:
            return False, ""
        if deadline is None:
            deadline = time.monotonic() + self.GUARD_BUDGET_S
        capture = list(guard.get("capture") or [])
        # 取り込みがなければフォルダーも作らない
        try:
            tmpdir = tempfile.mkdtemp(prefix="padcue_guard_") if capture else ""
        except OSError:
            why = "画像を渡す一時フォルダーを作成できません"
            self._log_guard_error(lane, dev_id, cmd, why)
            return False, why
        try:
            paths = self._grab_captures(capture, tmpdir, video, lane=lane,
                                        dev_id=dev_id, deadline=deadline,
                                        stop=self._halt)
            env = _capture_env(capture, paths)
            env["PADCUE_DEVICE"] = lane
            env["PADCUE_PROC"] = proc
            left = deadline - time.monotonic()
            if left <= 0 or self._halt.is_set():
                return False, ""    # 取り込みで締め切りを使い切ったか、終了の合図が来た
            # 出力は見せない。結果は終了コードだけで決まり、待っている間は毎周期
            # 実行するので、コンソールへ出すと同じ出力が約 1 秒ごとに積もる。
            # エラー出力は、判定できなかった理由を記録に残すために受け取る
            with tempfile.TemporaryFile() as err:
                try:
                    code = _run_shell(cmd, env, left, self._halt,
                                      cwd=self.project.root,
                                      stdout=subprocess.DEVNULL, stderr=err)
                except subprocess.TimeoutExpired:
                    if self._halt.is_set():
                        return False, ""
                    why = f"判定の時間({self.GUARD_BUDGET_S:g} 秒)内に終わりません"
                    self._log_guard_error(lane, dev_id, cmd, why)
                    return False, why
                except (OSError, ValueError):
                    # ValueError はコマンドに NUL 文字があるとき(Popen が起動の
                    # 前に拒否する)。起動できないのと同じく扱う
                    why = "起動できません"
                    self._log_guard_error(lane, dev_id, cmd, why)
                    return False, why
                if code != 0:
                    # 0 以外で終わるのは「成立しない」という普通の結果でも
                    # ある。エラー出力に何か書いたときだけ、壊れたスクリプトと
                    # みて理由を残す
                    line = _last_line(err)
                    if line:
                        self._log_guard_error(lane, dev_id, cmd, line)
                    return False, line
                return True, ""
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 手順のブロックのスクリプトの実行 ----

    def _run_script(self, link, st: dict, proc: str, script: dict) -> None:
        """手順のブロックのスクリプトをその選択待ちで 1 回だけ実行し、選択肢 0 を
        選ばせる。

        実行はレーンごとに別のスレッド(_script_job)で行う。wait が真の
        スクリプトは最大 SCRIPT_TIMEOUT_S 秒かかる。監視のスレッドで待つと、
        その間ほかのレーンの判定が待たされ、2 台で動かすときは相手のレーンの
        選択待ちがそのまま延びる。1 本のレーンでは 1 つずつ実行する。前の
        スクリプトが終わるまでは、次の選択待ちのスクリプトを起動しない。

        同じ選択待ち(await_gen)では 1 回だけ実行する(任意のコマンドを 2 回
        実行しない)。実行を終えたのに選択待ちのままなら、選択の指示を送れ
        なかったので、スクリプトは実行せずに選択の指示だけを送り直す。

        実行したかどうかは装置ごとのキー(registered_id)で分ける。await_gen が
        記録した値と違えば別の選択待ちなので実行する(記録した値より小さいのは、
        装置が再起動して await_gen を 0 から数え直したときである。これも別の
        選択待ちとして扱う)。加えて、選択待ちを抜けたときに _check_link が
        記録を消すので、再起動の後に gen がたまたま記録した値と同じになっても、
        実行を漏らさない。
        """
        key = link.registered_id
        gen = st.get("await_gen")
        with self._memo_lock:
            job = self._script_jobs.get(key)
            if job is not None and job.is_alive():
                return      # 実行中。終わったら、そのスレッドが選択の指示を送る
            resend = gen is not None and self._script_ran.get(key) == gen
            if not resend:
                if gen is not None:
                    self._script_ran[key] = gen
                stop = threading.Event()
                job = threading.Thread(target=self._script_job,
                                       args=(link, proc, script, gen, stop),
                                       daemon=True, name="condwatch-script")
                self._script_jobs[key] = job
                self._script_stops[key] = stop
                job.start()
        if resend:
            self._select_first(link, gen)   # 送れなかった選択の指示を送り直す

    def _stop_script(self, key: str) -> None:
        """そのレーンの手順のブロックのスクリプトに、終了の合図を送る。

        レーンがそのスクリプトの選択待ちを抜けたとき(人が停止させた、待たずに進んだ)
        に呼ぶ。動き続けると、そのレーンの次のスクリプトを起動しない(_run_script)
        ので、停止させて開始し直した実行が、前の実行のスクリプトの終わりを最長
        SCRIPT_TIMEOUT_S 秒待つことになる。
        """
        with self._memo_lock:
            stop = self._script_stops.pop(key, None)
        if stop is not None:
            stop.set()

    def _script_job(self, link, proc: str, script: dict, gen,
                    stop: threading.Event | None = None) -> None:
        """スクリプトを実行してから、選択肢 0 を選ばせる(レーンごとのスレッド)。

        stop が立ったら、完了を待つスクリプトを終了させる(_stop_script)。
        """
        try:
            self._launch(link, proc, script, stop)
        except Exception:   # noqa: BLE001  失敗しても装置を選択待ちのまま残さない
            traceback.print_exc()
        try:
            self._select_first(link, gen)
        except Exception:   # noqa: BLE001  想定外は tick と同じくコンソールに出す
            traceback.print_exc()

    def _select_first(self, link, gen) -> None:
        """選択肢 0 を選ばせる。送れなければ、次の周期で _run_script が送り直す。

        停止の予約(after_select)があれば、同じ接続で続けて送る。送る先は、その
        時点のリンクを装置 ID で探し直す(スクリプトの実行中に装置を改名すると、
        リンクが作り直され、始めたときのリンクは閉じている)。
        """
        # pool は、装置なしで判定だけを確かめるとき(テスト)には渡さない
        if self.pool is not None:
            link = next((x for x in self.pool.links()
                         if x.registered_id == link.registered_id), link)
        mask = (self.after_select(link.registered_id)
                if self.after_select is not None else 0)

        def go(c):
            c.select(0, gen=gen)
            link.note_pick(gen, 0)
            if mask:
                c.stop_at_mark(mask)
            return c.status()
        try:
            link.write_through(status=link.call(go))
        except (DeviceError, OSError):
            pass

    def _launch(self, link, proc: str, script: dict,
                stop: threading.Event | None = None) -> None:
        """capture を取り込み、環境変数を整えてコマンドを起動する。

        装置のロックの外で行う(映像は映像ソースから直接取る)。起動や実行の
        失敗は外へ投げず、記録に残す。呼び出し元は、失敗しても選択肢 0 を選ばせる。
        完了を待つスクリプトは、stop が立ったら終了させ、終了させたこと(stopped)を
        記録に残す。人がレーンを停止させた結果なので、時間切れと分けて残す。
        """
        cmd = script.get("cmd") or ""
        wait = bool(script.get("wait", True))
        capture = list(script.get("capture") or [])
        lane = link.cfg.get("name") or ""
        # 取り込みがなければフォルダーも作らない。作れなければ(ディスクが
        # いっぱいなど)、起動できなかったものとして記録に残す
        try:
            tmpdir = tempfile.mkdtemp(prefix="padcue_script_") if capture else ""
        except OSError:
            self._log_script(link, cmd, bg=not wait, exit_code=None, ok=False)
            return
        _root, video = self._started_with(link, link.status or {})
        paths = self._grab_captures(capture, tmpdir, video,
                                    lane=lane, dev_id=link.registered_id)
        env = _capture_env(capture, paths)
        env["PADCUE_DEVICE"] = lane
        env["PADCUE_PROC"] = proc
        if wait:
            code = None
            try:
                code = _run_shell(cmd, env, self.SCRIPT_TIMEOUT_S, stop,
                                  cwd=self.project.root)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                # 時間切れ、終了させたとき、起動の失敗は、終了コードなしで記録に残す。
                # ValueError はコマンドに NUL 文字があるとき(Popen が起動の前に
                # 拒否する)
                pass
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            self._log_script(link, cmd, bg=False, exit_code=code,
                             stopped=stop is not None and stop.is_set())
        else:
            child = None
            try:
                child = subprocess.Popen(cmd, shell=True, env=env,
                                         cwd=self.project.root)
            except (OSError, ValueError):
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            if child is not None and tmpdir:
                # 起動したプロセスがまだ読むので、すぐには消さない。プロセスが
                # 終わったら消す(_drop_old_bg_tmp)
                with self._tmp_lock:
                    self._bg_tmpdirs.append((time.monotonic(), tmpdir, child))
                self._drop_old_bg_tmp()
            self._log_script(link, cmd, bg=True, ok=child is not None)

    def _grab_captures(self, capture: list, tmpdir: str,
                       video: dict | None = None, lane: str = "",
                       dev_id: str = "",
                       deadline: float | None = None,
                       stop: threading.Event | None = None) -> dict[int, str]:
        """capture の各映像ソース(名前)の絵を PNG で書き出し、{capture の番号: パス} を
        返す。

        取れなかったソースは飛ばす(返す表に、その番号を入れない)。取り込みは装置と
        関係がないので、装置のロックの外で行う。失敗は外へ投げない。

        手順に書くのは映像ソースの名前だけで、実体はこの PC の設定から探す
        (file-format.md §4-2)。「装置に設定している映像」は、そのレーンの映像に
        変換する。見つからない名前は、画像なしで実行したことが分かるよう、記録に
        書いてから飛ばす。lane はレーンの名前、dev_id は記録に付ける装置の ID
        である。

        条件の判定と同じ _source_for で取り込む。同じ映像を
        条件の判定と同時に取り込まず、固まったソースでスレッドを増やさない。
        deadline(time.monotonic の時刻)を渡すと、その時刻までに取れる分だけ
        取る。渡さなければ、1 つのソースを GRAB_TIMEOUT_S 秒まで待つ。stop が
        立ったら、残りを取らずに返す。
        """
        paths: dict[int, str] = {}
        for i, src in enumerate(capture):
            if stop is not None and stop.is_set():
                break
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                break
            resolved = self._resolve_capture(lane, src, video, left)
            if resolved is None:
                # 映像機器の一覧を確認できないときは、ないと書かずに
                # 確認できない理由を書く
                unknown = "" if is_lane_video_name(src) else dshow_unknown()
                if unknown:
                    self._log_capture_skip(lane, dev_id, src, unknown)
                else:
                    self._log_capture_skip(lane, dev_id, src)
                continue
            # 条件の判定と同じく、レーンの映像はレーンの切り出し範囲で切り出して渡す
            # (1 つの映像に 2 台の画面を並べたときに、自分の半分を渡す)
            crop = lane_crop(video) if is_lane_video_name(src) else None
            try:
                source = self._source_for(resolved)
            except Exception:   # noqa: BLE001  壊れたソース定義は飛ばす
                continue
            wait = (self.GRAB_TIMEOUT_S if deadline is None
                    else deadline - time.monotonic())
            if wait <= 0:
                break
            if crop:
                frame, why = source.take("frame", wait, stop)
                png = None if frame is None else _frame_png(crop_to(frame, crop))
            else:
                png, why = source.take("png", wait, stop)
            if png is None:
                # 名前は見つかったが取り込めなかった。黙って画像なしで実行すると、
                # 画像がない理由を記録からたどれない
                if why:
                    self._log_capture_skip(lane, dev_id, src, why)
                continue
            path = os.path.join(tmpdir, f"capture_{i}.png")
            try:
                with open(path, "wb") as f:
                    f.write(png)
            except OSError:
                continue
            paths[i] = path
        return paths

    def _resolve_capture(self, lane: str, name: str, video: dict | None,
                         left: float | None) -> dict | None:
        """スクリプトに渡す映像ソースの名前を、この PC の指定に変換する。

        見つからなければ None を返す。一度見つかった名前は、そのレーンが選択待ちを
        抜けるまで同じ指定を使う(選択肢のスクリプトは毎周期実行するので、
        そのたびに探し直さない)。left は待てる秒数で、None なら決まった上限まで
        待つ(sources.find_source_by_name)。
        """
        if is_lane_video_name(name):
            try:
                own = validate_video(video)
            except (ValueError, TypeError):
                return None     # 読めない設定は、映像がないのと同じく渡さない
            return own["source"] if own else None
        with self._memo_lock:
            got = self._resolved.get(lane, {}).get(name)
        if got is not None:
            return got
        got = (find_source_by_name(name) if left is None
               else find_source_by_name(name, wait=left))
        if got is not None:
            with self._memo_lock:
                self._resolved.setdefault(lane, {})[name] = got
        return got

    def _log_capture_skip(self, lane: str, dev_id: str, src: str,
                          why: str = "この PC でその名前の映像ソースが見つかりません",
                          ) -> None:
        """スクリプトに渡す映像を取れずに飛ばしたことを、理由とともに記録に書く。

        名前で見つからなかったときと、取り込めなかったときに書く。_log_cond_error と
        同じく、同じレーンの同じ名前は、理由が変わったときだけ書く。
        """
        key = (lane, src)
        with self._memo_lock:
            if self._capture_skips.get(key) == why:
                return
            self._capture_skips[key] = why
        self._append_log({"kind": "PC_CAPTURE_SKIP", "src": src, "why": why}, dev_id)

    # ---- ログ(PC 側の合成。装置タグ付きで logs.jsonl へ) ----

    def _append_log(self, entry: dict, dev: str) -> None:
        """記録に 1 件書く。書けなくても、判定とスクリプトの実行は続ける。"""
        try:
            self.project.logs.append([entry], dev=dev)
        except Exception:   # noqa: BLE001  ログが書けなくても判定は続ける
            pass

    def _log(self, link, cond_name: str, arm_name: str, at: float) -> None:
        """条件で選んだことを記録に書く。at は SELECT を送る前の時刻(_check_link)。"""
        self._append_log({"kind": "PC_CONDITION_SELECT", "condition": cond_name,
                          "arm": arm_name, "at": at}, link.registered_id)

    def _log_script(self, link, cmd: str, *, bg: bool, exit_code=None,
                    ok: bool = True, stopped: bool = False) -> None:
        # cmd は長いことがあるので要約して書く。exit は wait=true の終了コード
        # (時間切れ・終了させたとき・起動失敗は None)、stopped はレーンが選択待ちを
        # 抜けてスクリプトを終了させたこと、bg は wait=false(起動だけ)、
        # ok はその起動可否
        summ = _short(cmd, LOG_CMD_CHARS)
        entry = {"kind": "PC_SCRIPT", "cmd": summ, "bg": bool(bg)}
        if bg:
            entry["ok"] = bool(ok)
        else:
            entry["exit"] = exit_code
            if stopped:
                entry["stopped"] = True
        self._append_log(entry, link.registered_id)

    def _log_guard_error(self, lane: str, dev_id: str, cmd: str,
                         why: str) -> None:
        """選択肢のスクリプトで判定できなかったことを記録に書く。

        スクリプトの出力はコンソールにも画面にも出さないので、壊れたスクリプトで
        選択待ちが続く理由は、ここにしか残らない。判定は毎秒くり返すので、同じ
        レーンの同じコマンドは、理由が変わったときだけ書く(_log_cond_error と
        同じ)。記録を書けなくても判定は続ける。
        """
        key = (lane, cmd)
        with self._memo_lock:
            if self._guard_errors.get(key) == why:
                return
            self._guard_errors[key] = why
        summ = _short(cmd, LOG_CMD_CHARS)
        self._append_log({"kind": "PC_GUARD_ERROR", "cmd": summ, "why": why}, dev_id)

    def _log_guard(self, link, cmd: str, arm_name: str, at: float) -> None:
        """選択肢のスクリプトで選んだことを記録に書く。at は SELECT を送る前の時刻
        (_check_link)。"""
        summ = _short(cmd, LOG_CMD_CHARS)
        self._append_log({"kind": "PC_GUARD_SELECT", "cmd": summ, "arm": arm_name,
                          "at": at}, link.registered_id)
