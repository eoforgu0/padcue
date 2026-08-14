"""実行エンジンの参照実装(仕様の実行可能な定義)。

ファームウェアのコア1実行エンジン(C)はこの動作と一致しなければならない。
一致検証は「同じバイナリに対する送出列(フレーム番号+状態)の完全一致」で行う。

時刻の扱い(設計文書 A-3):
- 送出時刻は「開始時刻 + (base + event.frame) × フレーム周期」の絶対時刻で決まる
- base は Djnz 通過時に advance を加算するのみの正確な整数演算であり、誤差は蓄積しない
"""
from __future__ import annotations

from dataclasses import dataclass

from .binfmt import (
    REST_AX,
    REST_AY,
    REST_AZ,
    Await,
    Djnz,
    End,
    Event,
    Jmp,
    SetCnt,
    State,
)


class EngineError(Exception):
    pass


def resume_start_frame(events: list[Event], start_index: int,
                       start_base: int) -> int:
    """再開点から最初に送出されるはずの絶対フレーム。

    部分実行は「その位置がフレーム 0」として走らせる(でないと飛ばした前半ぶん
    何も出さずに待ってしまう)。ここで求めた値を全送出時刻から引く。
    再開点の直前にカウンタ初期化(SETCNT)が挟まることがあるので読み飛ばす。
    """
    i = start_index
    while i < len(events) and isinstance(events[i], SetCnt):
        i += 1
    if i < len(events) and isinstance(events[i], (State, Await)):
        return start_base + events[i].frame
    return start_base


def resume_is_valid(events: list[Event], start_index: int) -> bool:
    """その位置から実行を始めてよいか(最初に到達する時間消費が全状態か)。

    SETCNT はループのカウンタを積むだけで時間を消費しないので、
    再開点の直前に来ていてもよい。
    """
    if start_index == 0:
        return True
    if not (0 <= start_index < len(events)):
        return False
    i = start_index
    while i < len(events) and isinstance(events[i], SetCnt):
        i += 1
    return i < len(events) and isinstance(events[i], State)


@dataclass(frozen=True)
class Emission:
    """1回の状態切り替え。frame は実行開始からの絶対フレーム。値はすべて生値。"""
    frame: int
    buttons: int
    lx: int
    ly: int
    rx: int
    ry: int
    gx: int = 0
    gy: int = 0
    gz: int = 0
    # 既定は静止姿勢。加速度 0 は自由落下で、C 実装の pademu_state_neutral
    # (az=4096)と食い違う(binfmt.REST_A* を参照)
    ax: int = REST_AX
    ay: int = REST_AY
    az: int = REST_AZ


def run(
    events: list[Event],
    total_frames: int,
    session_loops: int = 1,
    max_steps: int = 10_000_000,
    start_index: int = 0,
    start_base: int = 0,
    choices: list | None = None,
    await_frames: int = 0,
) -> list[Emission]:
    """イベント列を解釈し、送出列を返す。

    session_loops: 手順全体の繰り返し回数(RUN コマンドの loop_n)。1 以上。
    2周目以降は base を total_frames ずつ進めて先頭から再実行する
    (ブロックモデル: 各周回は手順定義の正確な反復であり、状態の持ち越しはない)。

    choices: 待機分岐に来たときに選ぶ腕の番号(0 始まり)を順に並べたもの。
    await_frames: 1回の待機が何フレームぶんの時間を占めたとみなすか(検証用)。
    実機では待っている間タイミングを刻まないので、この値は再現テスト用の仮定。
    """
    if session_loops < 1:
        raise EngineError(f"session_loops は 1 以上: {session_loops}")
    out: list[Emission] = []
    counters: dict[int, int] = {}
    # 部分実行では再開点を時刻 0 に寄せる(飛ばした前半ぶん待たされないように)
    skip = resume_start_frame(events, start_index, start_base)
    pass_frames = total_frames - skip
    if pass_frames < 0:
        raise EngineError(f"再開点が手順長を超えています: skip={skip}")
    base = start_base   # 現在のセグメント時刻基準(絶対フレーム)
    pass_start = 0      # 現在の周回の開始絶対フレーム(skip を引いた座標)
    loops_left = session_loops
    idx = start_index
    steps = 0
    last_frame = 0
    pending_choices = list(choices or [])
    shift = 0      # 待機で消費した時間の累計(以降の予定時刻をずらす)
    while True:
        steps += 1
        if steps > max_steps:
            raise EngineError("ステップ上限超過(無限ループの疑い)")
        if idx < 0 or idx >= len(events):
            raise EngineError(f"イベント index が範囲外(END なし終端または不正ジャンプ): idx={idx}")
        ev = events[idx]
        if isinstance(ev, State):
            frame = base + ev.frame + shift - skip
            if frame < 0:
                raise EngineError(f"再開点より前の時刻へ戻りました: idx={idx}")
            if frame < last_frame:
                raise EngineError(
                    f"時刻の逆行: idx={idx} frame={frame} < {last_frame}"
                )
            last_frame = frame
            out.append(Emission(
                frame, ev.buttons, ev.lx, ev.ly, ev.rx, ev.ry,
                ev.gx, ev.gy, ev.gz, ev.ax, ev.ay, ev.az,
            ))
            idx += 1
        elif isinstance(ev, SetCnt):
            counters[ev.counter] = ev.value
            idx += 1
        elif isinstance(ev, Djnz):
            if ev.counter not in counters:
                raise EngineError(f"未初期化カウンタ: {ev.counter}")
            counters[ev.counter] -= 1
            base += ev.advance
            idx = ev.target if counters[ev.counter] > 0 else idx + 1
        elif isinstance(ev, Await):
            # 待つ間はニュートラル(入力を出しっぱなしにしない)。
            # ニュートラル = ボタンなし・スティック中央・回転なし・重力あり
            # (C 実装の pademu_state_neutral と同じ値になること)
            frame = base + ev.frame + shift - skip
            if frame < 0:
                raise EngineError(f"再開点より前の時刻へ戻りました: idx={idx}")
            if frame < last_frame:
                raise EngineError(f"時刻の逆行: idx={idx}")
            out.append(Emission(frame, 0, 0, 0, 0, 0))   # gx..az は既定値=静止姿勢
            last_frame = frame
            if pending_choices:
                arm = pending_choices.pop(0)
            elif ev.on_timeout == 0:
                break                      # 選ばれなければ中断(既定)
            else:
                arm = ev.on_timeout - 1
            if not (0 <= arm < len(ev.targets)):
                raise EngineError(f"待機分岐の選択が範囲外: {arm}")
            # 待っていた時間ぶん、以降の予定時刻を後ろへずらす
            shift += await_frames
            idx = ev.targets[arm]
        elif isinstance(ev, Jmp):
            idx = ev.target
        elif isinstance(ev, End):
            loops_left -= 1
            if loops_left <= 0:
                break
            # 部分実行のときは各周回もその位置から始める(区間の繰り返し)
            pass_start += pass_frames
            base = pass_start + start_base
            idx = start_index
        else:
            raise EngineError(f"未知イベント: {ev!r}")
    return out
