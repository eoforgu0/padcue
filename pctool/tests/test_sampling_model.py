"""リントの閾値(1 フレームだけ警告する)の根拠を、計算で確かめる。

**なぜこのテストがあるか**: 以前は 2 フレームの押下にも警告を出していた。
設計文書 A-1 の図で「2F 押す」が 1 回しかサンプリングされない例に**「欠落」**
というラベルが付いていたのを、「入力そのものが消える」と読み違えたためである。
実際の A-1 本文は「N の指示に対し実際は **N−1〜N+1**」であり、下限が 0 に
なるのは N=1 のときだけ。ここではその主張を数え上げで検証し、閾値が
勝手に戻されたら落ちるようにしておく。

モデル(A-1・A-2 の前提をそのまま数式にしたもの):
- こちらは押下から解放までを**フレーム周期 P のちょうど整数倍** N·P で出す
- ゲームは周期 P で一度ずつ読む。読む時刻の位相 φ は不明(0..P の一様)
- こちらの切り替えがゲームに見えるまでの遅れ d は 0..D(送信の粒度。
  bInterval=1ms なら D/P≈0.06、保険の 8ms でも D/P≈0.48)。
  押下と解放の遅れは**独立**に決まる(A-1 の「それぞれ独立に前後へ倒れる」)
- 観測フレーム数 = 半開区間 [押下+d1, 解放+d2) に入るサンプリング点の数
"""
import pytest

P = 10_000          # フレーム周期(整数の刻みで扱う)


def observed(n_frames: int, phase: int, d_press: int, d_release: int) -> int:
    """この条件でゲームが「押されている」と見るフレーム数。"""
    a = d_press                       # 押下が見えた時刻(押下指示を 0 とする)
    b = n_frames * P + d_release      # 解放が見えた時刻
    # 位相 phase のサンプリング点 phase + k*P のうち [a, b) に入る数
    lo = -(-(a - phase) // P)         # ceil((a-phase)/P)
    hi = -(-(b - phase) // P)         # ceil((b-phase)/P)
    return max(0, hi - lo)


def sweep(n_frames: int, delay_ratio: float, steps: int = 60):
    """位相と遅れを総当たりして、観測フレーム数の最小・最大を返す。"""
    d_max = int(P * delay_ratio)
    lo, hi = None, None
    for i in range(steps):
        phase = P * i // steps
        for j in range(steps + 1):
            d1 = d_max * j // steps
            for k in range(steps + 1):
                d2 = d_max * k // steps
                v = observed(n_frames, phase, d1, d2)
                lo = v if lo is None else min(lo, v)
                hi = v if hi is None else max(hi, v)
    return lo, hi


# bInterval=1ms 相当(本命) / 8ms 相当(保険モード)
REAL_DELAYS = [0.06, 0.48]


@pytest.mark.parametrize("ratio", REAL_DELAYS)
def test_one_frame_can_vanish(ratio):
    """1 フレームの指示は、位相によっては 0 回=まったく押されないことがある。"""
    lo, hi = sweep(1, ratio)
    assert lo == 0, f"1F で 0 回になる位相が無い(遅れ {ratio}): 最小 {lo}"
    assert hi == 2, f"1F の上限は 2 のはず: {hi}"


@pytest.mark.parametrize("ratio", REAL_DELAYS)
@pytest.mark.parametrize("n", [2, 3, 4, 10])
def test_two_or_more_frames_never_vanish(n, ratio):
    """2 フレーム以上は、どの位相・どの遅れでも必ず1回以上押される。

    これが「2 フレームには警告を出さない」根拠。長さは N−1〜N+1 でぶれるが、
    「押されない」は起きない。
    """
    lo, hi = sweep(n, ratio)
    assert lo >= 1, f"{n}F なのに 0 回になる場合がある(遅れ {ratio})"
    assert (lo, hi) == (n - 1, n + 1), \
        f"{n}F の観測範囲が N−1〜N+1 でない: {lo}〜{hi}(A-1 と食い違う)"


def test_guarantee_depends_on_delay_being_under_one_frame():
    """成立条件を明示する: 遅れが1フレームを超えると 2F でも消えうる。

    実機では遅れは送信の粒度(1ms、保険モードでも 8ms)であり、1 フレーム
    (16.7ms)より十分小さい。この前提が崩れる構成にしたら閾値も見直すこと。
    """
    lo, _ = sweep(2, 1.2)      # 遅れが 1.2 フレームある異常な構成
    assert lo == 0, "前提(遅れ<1フレーム)の効き方が想定と違う"


def test_exact_multiple_is_stable_without_delay():
    """遅れが無ければ、位相によらず指示どおりの回数になる(A-2)。"""
    for n in (1, 2, 5):
        assert sweep(n, 0.0) == (n, n)


def test_lint_threshold_matches_this_model():
    """リントの閾値が、この検証結果と一致していること。"""
    from padcue.dsl import _SHORT_FRAMES
    assert _SHORT_FRAMES == 2, (
        "警告する下限が変わっている。0 回になりうるのは 1 フレームだけ"
        "(このファイルの検証を参照)")
