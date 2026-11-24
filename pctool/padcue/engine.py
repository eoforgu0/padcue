"""実行エンジンの参照実装(仕様の実行可能な定義)。

ファームウェアのコア 1 実行エンジン(C)はこの動作と一致しなければならない。
一致検証は「同じバイナリに対する出力列(フレーム番号+状態)の完全一致」で行う。

時刻の扱い(P8 時間はフレーム単位、時刻は開始からの絶対時刻):
- 出力の時刻は「開始時刻 + (base + event.frame + shift) × フレーム周期」の絶対時刻で
  決まる。shift は通った選択肢の長さの合計である
- base は Djnz 通過時に advance を加算するのみの正確な整数演算であり、誤差は蓄積しない
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .binfmt import (
    REST_AX,
    REST_AY,
    REST_AZ,
    ArmJoin,
    Await,
    Djnz,
    End,
    Event,
    SetCnt,
    State,
)

# walk がたどるレコードの数の上限。超えたら、次の位置は見つからないものとする
WALK_LIMIT = 100_000


@dataclass(frozen=True)
class Point:
    """手順をたどって見つけた、次に止まりうる位置。

    kind は次の 4 つである。mark は区切りのマークが付いたレコード、await は
    選択待ち、end は手順の終わり、none は上限までたどっても次の位置が見つから
    なかったことを表す。frame はその位置の予定時刻(実行開始からの絶対フレーム)で
    ある。base と shift はその位置に到達した時点の値で、続きをたどるときに引き継ぐ。
    """
    kind: str
    frame: int
    index: int = 0
    mark: int = 0
    arms: int = 0
    timeout_frames: int = 0
    on_timeout: int = 0
    base: int = 0
    shift: int = 0


class WalkError(Exception):
    """手順をたどれなかった。実機ならエンジンの異常にあたる。"""


def walk(events: list, idx: int, base: int, shift: int,
         counters: dict[int, int], total_frames: int,
         passed: list | None = None) -> Point:
    """次に止まりうる位置まで手順をたどる。

    解釈は参照実装(engine.py の run)と同じである。DJNZ が base を進め、
    ARMJOIN が shift を進め、SETCNT がカウンターを置く。装置と違って入力を出さない
    ので、区切りのマークが付いたレコードと、選択待ちと、手順の終わりだけを探す。
    模擬装置の実行と、PC が求める「止まる先」が同じ解釈を使うために、ここに置く。

    counters は呼び出し元と共有し、たどったぶんだけその場で書き換える。周回の
    カウンターの値は選択待ちをまたいで持ち越すので、実行ごとの辞書を渡す。

    passed を渡すと、途中で通り過ぎたマークのない STATE を、(予定時刻,
    レコード番号) にしてたどった順に追加していく。模擬装置が、いま何番のレコードまで
    出したかを答えるのに使う。
    """
    for _ in range(WALK_LIMIT):
        if not 0 <= idx < len(events):
            raise WalkError(f"レコード番号が範囲外です: {idx}")
        ev = events[idx]
        if isinstance(ev, State):
            if ev.mark:
                return Point("mark", base + ev.frame + shift, index=idx,
                             mark=ev.mark, base=base, shift=shift)
            if passed is not None:
                passed.append((base + ev.frame + shift, idx))
            idx += 1
        elif isinstance(ev, Await):
            return Point("await", base + ev.frame + shift, index=idx,
                         mark=ev.mark, arms=len(ev.targets),
                         timeout_frames=ev.timeout_frames,
                         on_timeout=ev.on_timeout, base=base, shift=shift)
        elif isinstance(ev, SetCnt):
            counters[ev.counter] = ev.value
            idx += 1
        elif isinstance(ev, Djnz):
            if ev.counter not in counters:
                raise WalkError(f"初期化されていないカウンターです: {ev.counter}")
            if counters[ev.counter] == 0:
                raise WalkError(f"回数を使い切ったカウンターです: {ev.counter}")
            counters[ev.counter] -= 1
            base += ev.advance
            idx = ev.target if counters[ev.counter] > 0 else idx + 1
        elif isinstance(ev, ArmJoin):
            shift += ev.delta
            idx = ev.target
        elif isinstance(ev, End):
            # 手順の終わりの時刻は、ヘッダーの総フレーム数に、選んだ選択肢の
            # 正準長の合計(shift)を足したものである。総フレーム数は選択肢を
            # 長さ 0 として数えてあるので、選んだぶんだけ後ろへずれる。
            # 総フレーム数 0 は総量なしである。選択待ちの選択肢の外に無限の
            # くり返しがある手順は終わりに到達せず、正準の長さが 0 の手順は
            # この式のとおり選んだ選択肢の長さで終わる
            return Point("end", total_frames + shift, index=idx,
                         base=base, shift=shift)
        else:
            raise WalkError(f"未知のレコードです: {ev!r}")
    return Point("none", 0, index=idx, base=base, shift=shift)


class EngineError(Exception):
    pass


# run の picks に書く値。その通過では、まだどの選択肢も選んでいない(図に、どの選択肢の
# 中身も描かない)
OPEN = -1


def after_arms(events: list, i: int) -> int | None:
    """選択待ち i のどの選択肢からも戻る所(共有の続き)のレコード番号。選択肢が 1 つの
    待ちと、求められない形は None。

    変換は、選択肢を飛び先の順に並べ、それぞれの終わりに共有の続きへ戻る ArmJoin を
    置く(dsl の _merge_await)。2 つ目からの選択肢の飛び先の直前が、1 つ前の選択肢の
    終わりである。
    """
    ev = events[i]
    if not isinstance(ev, Await) or len(ev.targets) < 2:
        return None
    joins = {events[t - 1].target if 0 < t <= len(events)
             and isinstance(events[t - 1], ArmJoin) else None
             for t in ev.targets[1:]}
    return joins.pop() if len(joins) == 1 and None not in joins else None


@dataclass(frozen=True)
class Emission:
    """1 回の状態切り替え。frame は実行開始からの絶対フレーム。値はすべて生値。"""
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
    # (az=4096)と一致しない(binfmt.REST_A* を参照)
    ax: int = REST_AX
    ay: int = REST_AY
    az: int = REST_AZ


def run(
    events: list[Event],
    *,
    max_steps: int = 10_000_000,
    choices: list | None = None,
    await_frames: int = 0,
    picks: dict | None = None,
    awaits_out: list | None = None,
    passes_out: dict | None = None,
    counters_out: dict | None = None,
) -> list[Emission]:
    """イベント列を解釈し、出力列を返す。

    周回は手順のイベント列そのものに入っている(変換器が周回数のくり返しを、
    書いていなければ手順全体を SETCNT と DJNZ で出す)ので、この実行は END に
    達したところで終わる。

    choices: 分岐に来たときに選ぶ選択肢の番号(0 始まり)を順に並べたもの。
    await_frames: 1 回の待機が何フレームぶんの時間を占めたとみなすか(検証用)。
    実機では待っている間は経過時間を数えないので、この値は再現テスト用の仮定。
    picks: {レコードの番号: 選ぶ選択肢の番号}。choices と違って通った順ではなく
    レコードで探すので、くり返しの中の分岐でも毎回同じ選択肢を選べる。書いて
    いないレコードでは先頭の選択肢を選び、中断しない。編集画面のタイムライン
    (手順全体の経路を描く)が使う。値を関数にすると、そのレコードを通るたびに、
    そのときのカウンターの値({番号: 値})を渡して選択肢の番号を受け取る(くり返しの
    回ごとに別の選択肢を選べる)。OPEN の通過は、どの選択肢にも入らず、選択肢の
    長さを足さずに、分岐の後ろの続きへ進む(after_arms)。レーンのタイムライン(決まって
    いる所だけを描く)が使う。
    awaits_out: 通った選択待ちを {"frame", "index"} で記録して返すリスト。
    図の上で時間軸を切る位置を出すのに使う。どの選択肢にも入らなかった通過には
    "open" を付ける。
    passes_out: 区切りのマークが付いたレコードと選択待ちを通った時刻を、
    {レコードの番号: [時刻, ...](通った順)} で記録して返す辞書。図の上に
    区切り(ラベル・現在位置・止まる先)を置くのに使う。変換が区切りに付ける
    時刻は選択肢の長さを数えないので、選択肢を通る図とは一致しない。
    counters_out: passes_out と同じ形で、通ったときの各カウンターの値
    ({番号: 値})を記録して返す辞書。くり返しの何回目に通ったかを求めるのに使う
    (選択肢の中の区切りは、その選択肢を通った回にしか記録がない)。
    """
    out: list[Emission] = []
    counters: dict[int, int] = {}
    base = 0       # 現在のセグメント時刻基準(絶対フレーム)
    idx = 0
    steps = 0
    last_frame = 0
    pending_choices = list(choices or [])

    def passed(index: int, frame: int) -> None:
        if passes_out is not None:
            passes_out.setdefault(index, []).append(frame)
        if counters_out is not None:
            counters_out.setdefault(index, []).append(dict(counters))
    shift = 0      # 通った選択肢の長さの合計。テストでは待った長さ(await_frames)も足す
    while True:
        steps += 1
        if steps > max_steps:
            raise EngineError("ステップ上限超過(無限ループの疑い)")
        if idx < 0 or idx >= len(events):
            raise EngineError(
                f"イベント index が範囲外"
                f"(END なし終端または不正ジャンプ): idx={idx}")
        ev = events[idx]
        if isinstance(ev, State):
            frame = base + ev.frame + shift
            if frame < last_frame:
                raise EngineError(
                    f"時刻の逆行: idx={idx} frame={frame} < {last_frame}"
                )
            last_frame = frame
            if ev.mark:
                passed(idx, frame)
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
                raise EngineError(f"未初期化カウンター: {ev.counter}")
            # 回数を使い切ったカウンター(0)の DJNZ は、C 実装と同じく異常にする。
            # C 実装は uint32 で数えるので、減らすとアンダーフローして最大値になり、
            # 約 42 億回くり返す(binary-format.md の DJNZ)
            if counters[ev.counter] == 0:
                raise EngineError(f"回数を使い切ったカウンター: {ev.counter}")
            counters[ev.counter] -= 1
            base += ev.advance
            idx = ev.target if counters[ev.counter] > 0 else idx + 1
        elif isinstance(ev, Await):
            # 選択待ちは入力を変えず、いまの状態を出したまま待つ。待つ間に離す
            # ときは、変換器がこの直前に全入力を離した STATE を置いている。
            # まだ何も出していなければ全入力を離した状態である(C 実装の
            # pademu_state_neutral と同じ値になること)
            frame = base + ev.frame + shift
            if frame < last_frame:
                raise EngineError(f"時刻の逆行: idx={idx}")
            now = (replace(out[-1], frame=frame) if out
                   else Emission(frame, 0, 0, 0, 0, 0))   # gx..az は既定値=静止姿勢
            out.append(now)
            last_frame = frame
            if awaits_out is not None:
                awaits_out.append({"frame": frame, "index": idx})
            passed(idx, frame)
            if picks is not None:
                arm = picks.get(idx, 0)
                if callable(arm):
                    arm = arm(counters)
                if arm == OPEN:
                    after = after_arms(events, idx)
                    if after is None:
                        raise EngineError(f"選択待ちの続きが分からない: idx={idx}")
                    if awaits_out is not None:
                        awaits_out[-1]["open"] = True
                    idx = after
                    continue
            elif pending_choices:
                arm = pending_choices.pop(0)
            elif ev.on_timeout == 0:
                break                      # 時間切れがなければ、選ばれるまで待つ
            else:
                arm = ev.on_timeout - 1
            if not (0 <= arm < len(ev.targets)):
                raise EngineError(f"選択待ちの選択が範囲外: {arm}")
            # 待っていた時間ぶん、以降の予定時刻を後ろへずらす
            shift += await_frames
            idx = ev.targets[arm]
        elif isinstance(ev, ArmJoin):
            # 選択肢の合流。時刻だけ動かす(STATE は出さない)。
            # 出力の式 emit = base + ev.frame + shift は変えない
            shift += ev.delta
            idx = ev.target
        elif isinstance(ev, End):
            break        # 手順の終わり。周回はイベント列の中の DJNZ が数える
        else:
            raise EngineError(f"未知イベント: {ev!r}")
    return out
