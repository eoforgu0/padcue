"""手動操作の記録 → 行列部品(CSV)の下書き。

パススルー中に PC が送った入力状態を時刻つきで貯め、フレーム単位に量子化して
部品にする。スティックの移動やボタンの複合など、数値で手書きしづらいものを
「一度やってみせる」だけで形にできる。

精度について正直に: 記録の粒度は PC が送る周期(約 30Hz)であり、
1フレーム(約 16.7ms)より粗い。**下書き**として使い、フレーム単位の詰めは
できあがった表を直して行うこと(設計文書 A-1 の方針と同じ)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .binfmt import BUTTONS

# 記録する軸(部品 CSV の列名 → 入力状態のキー)
_AXES = [("LX", "lx"), ("LY", "ly"), ("RX", "rx"), ("RY", "ry")]
_AXIS_KEY = dict(_AXES)
_DEFAULT_DEADZONE = 120   # これ未満のスティックのぶれは 0 とみなす(生値)


@dataclass
class Recorder:
    """時刻つきの入力状態を貯めて、部品の表に変換する。"""

    frame_period_ns: int = 16666667
    deadzone: int = _DEFAULT_DEADZONE
    samples: list = field(default_factory=list)   # [(t_sec, state dict)]
    paused: bool = False    # 記録を止めたが、中身は保存できるよう残している

    def add(self, t_sec: float, state: dict) -> None:
        self.samples.append((t_sec, dict(state)))

    def clear(self) -> None:
        self.samples.clear()

    @property
    def duration(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1][0] - self.samples[0][0]

    def _quantize(self, t: float) -> int:
        return round((t - self.samples[0][0]) / (self.frame_period_ns / 1e9))

    def _clean(self, st: dict) -> dict:
        out = {"buttons": int(st.get("buttons", 0))}
        for _col, key in _AXES:
            v = int(st.get(key, 0))
            out[key] = 0 if abs(v) < self.deadzone else v
        return out

    def used_columns(self) -> tuple[list, list]:
        """実際に使われたボタン列・軸列だけを返す(使わない列は書かない)。"""
        btn_mask = 0
        axes = set()
        for _t, st in self.samples:
            c = self._clean(st)
            btn_mask |= c["buttons"]
            for col, key in _AXES:
                if c[key] != 0:
                    axes.add(col)
        buttons = [b for b in BUTTONS if btn_mask & (1 << BUTTONS[b])]
        axis_cols = [col for col, _k in _AXES if col in axes]
        return buttons, axis_cols

    def to_table(self, trim: bool = True) -> dict:
        """{header, rows} を返す(そのまま部品として保存できる形)。

        trim=True なら、先頭と末尾の「何も押していない」区間を落とす。
        """
        if len(self.samples) < 2:
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
            return fr["buttons"] == 0 and all(fr[k] == 0 for _c, k in _AXES)

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
                row.append(str(fr[key]) if fr[key] else "")
            rows.append(row)
        return {"header": header, "rows": rows}
