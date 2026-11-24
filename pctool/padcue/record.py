"""手動操作の記録から、フレーム表(CSV)の下書きを作る。

手動操作の間に PC が送った入力の状態を時刻つきで溜め、フレーム単位に量子化して
フレーム表にする。スティックの移動やボタンの複合など、数値で手書きしづらいものを
一度実際に操作するだけでフレーム表にできる。

精度: 標本は画面が 1 秒あたり約 60 回(ほぼ 1 フレームごと)取る。時刻は
画面の時計なので、押した時刻は少しずれることがある。下書きとして使い、
フレーム単位の調整はできあがった表を修正して行う(1 フレーム精度は原理的に
確率で外れる。hardware-design.md §1-2)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .binfmt import BUTTONS
from .frametable import AXES, MAX_ROWS
from .proto import DEFAULT_FRAME_PERIOD_NS

# 記録する軸は、フレーム表の軸の列(AXES。列名、入力の状態の欄、何もしていない
# ときの値)と同じ
_AXIS_KEY = {col: key for col, key, _rest in AXES}
_AXIS_REST = {col: rest for col, _key, rest in AXES}
# デッドゾーンをかけるのはスティックだけである。ジャイロと加速度にかけると、
# 静止値が 0 でない軸で「静止しているのに小さく動いた」ことになる
_STICK_KEYS = {"lx", "ly", "rx", "ry"}
_DEFAULT_DEADZONE = 120   # これ未満のスティックのぶれは 0 とみなす(生値)
# デッドゾーンの範囲。0 にすればぶれもそのまま残り、ゆっくり倒す操作を取りこぼさない。
# 上限の 500 は 2048 のおよそ 4 分の 1 で、これ以上切り捨てると操作そのものが
# 記録に残らなくなる
DEADZONE_MIN, DEADZONE_MAX = 0, 500
# 記録できる長さを超えたときの文(Recorder.add)。長さはフレーム表の行数の上限で
# ある(60 fps で 1 分は 3600 フレーム)
FULL_TEXT = (f"記録できるのは約 {MAX_ROWS // 3600} 分までです。"
             "これより後の操作は記録されません。")


@dataclass
class Recorder:
    """時刻つきの入力状態を溜めて、フレーム表に変換する。"""

    frame_period_ns: int = DEFAULT_FRAME_PERIOD_NS
    deadzone: int = _DEFAULT_DEADZONE
    samples: list = field(default_factory=list)   # [(t_sec, state dict)]
    paused: bool = False    # 記録を停止したが、中身は保存できるよう残している
    # 記録している画面の識別子(画面を読み込むたびに新しい)。記録はサーバーに
    # 1 つだけなので、別のタブの記録を黙って混ぜたり捨てたりしない
    owner: str = ""
    # どの装置の手動操作の記録か(装置の識別子)。その装置の手動操作が
    # 終わったら、記録も停止した扱いにする(記録した画面が閉じても詰まらない)
    dev: str = ""
    # 停止した時点で残ったフレーム数(状態に含める。毎秒の状態で表を作らない)
    kept: int = 0

    def add(self, t_sec: float, state: dict) -> bool:
        """標本を 1 つ追加する。記録できる長さを超える標本は追加せずに偽を返す。

        記録はフレーム表として保存するので、長さはフレーム表の行数の上限
        (frametable.MAX_ROWS)までである。上限を見ずに溜めると、保存のときに
        初めて拒否され、記録の一部も保存できない。上限で停止すれば、そこまでは
        保存できる。
        """
        if self.samples and self._quantize(t_sec) >= MAX_ROWS:
            return False
        self.samples.append((t_sec, dict(state)))
        return True

    def _quantize(self, t: float) -> int:
        return round((t - self.samples[0][0]) / (self.frame_period_ns / 1e9))

    def _clean(self, st: dict) -> dict:
        out = {"buttons": int(st.get("buttons", 0))}
        for _col, key, rest in AXES:
            v = int(st.get(key, rest))
            if key in _STICK_KEYS and abs(v) < self.deadzone:
                v = 0
            out[key] = v
        return out

    def used_columns(self) -> tuple[list, list]:
        """実際に使われたボタン列・軸列だけを返す(使わない列は書かない)。"""
        btn_mask = 0
        axes = set()
        for _t, st in self.samples:
            c = self._clean(st)
            btn_mask |= c["buttons"]
            for col, key, rest in AXES:
                if c[key] != rest:
                    axes.add(col)
        buttons = [b for b in BUTTONS if btn_mask & (1 << BUTTONS[b])]
        axis_cols = [col for col, _k, _r in AXES if col in axes]
        return buttons, axis_cols

    def to_table(self, trim: bool = True) -> dict:
        """{header, rows} を返す(そのままフレーム表として保存できる形)。

        trim=True なら、先頭と末尾の「何も押していない」区間を除く。
        """
        if len(self.samples) < 2:
            # 標本が足りなくても、保存できる形にする(入力の列が 1 つは要る)
            return {"header": ["A"], "rows": []}
        buttons, axis_cols = self.used_columns()
        header = ["F", *buttons, *axis_cols]

        total = self._quantize(self.samples[-1][0]) + 1
        # 各フレームに、その時刻以前で最も新しいサンプルを割り当てる
        frames: list = [None] * total
        for t, st in self.samples:
            f = self._quantize(t)
            if 0 <= f < total:
                frames[f] = self._clean(st)
        last = self._clean(self.samples[0][1])
        for i in range(total):
            if frames[i] is None:
                frames[i] = last
            else:
                last = frames[i]

        def is_idle(fr: dict) -> bool:
            return (fr["buttons"] == 0
                    and all(fr[k] == rest for _c, k, rest in AXES))

        start, end = 0, total
        if trim:
            while start < end and is_idle(frames[start]):
                start += 1
            while end > start and is_idle(frames[end - 1]):
                end -= 1
        rows = []
        for i in range(start, end):
            fr = frames[i]
            row = [str(i - start + 1)]
            for b in buttons:
                row.append("1" if fr["buttons"] & (1 << BUTTONS[b]) else "")
            for col in axis_cols:
                key = _AXIS_KEY[col]
                # 空欄はその軸の静止値である(CSV の規則。file-format.md §9-2)
                v = fr[key]
                row.append("" if v == _AXIS_REST[col] else str(v))
            rows.append(row)
        return {"header": header, "rows": rows}
