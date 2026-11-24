"""リントの閾値(1 フレームだけ警告する)の根拠を、計算で確認する。

hardware-design.md §1-2 の図は、2 フレーム押す指示が 1 回しか
読まれない例を「短く見える」と呼んでいる。これを「入力そのものが消える」と読むと、
2 フレームの押下にまで警告を出す閾値になる。同じ節の本文のとおり、N フレームの
指示は N-1 から N+1 フレームの範囲でばらつき、下限が 0 になるのは N=1 のとき
だけである。ここではその主張を数え上げで確認し、閾値が広げられたら失敗するように
しておく。

モデル(hardware-design.md §1-2・§1-3 の前提をそのまま数式にしたもの):
- こちらは押下から解放までをフレーム周期 P のちょうど整数倍 N·P で出す
- ゲームは周期 P で一度ずつ読む。読む時刻の位相 φ は不明(0..P の一様)。周期が違う
  ゲームは read で渡す
- こちらの切り替えがゲームに見えるまでの遅れ d は 0..D(送信の粒度。
  bInterval=1ms なら D/P≈0.06、1ms が使えないときの 8ms でも D/P≈0.48)。
  押下と解放の遅れは独立に決まる(hardware-design.md §1-2 の「押す時刻と離す
  時刻は、それぞれ前後どちらにも転ぶ」)
- 観測フレーム数 = 半開区間 [押下+d1, 解放+d2) に入るサンプリング点の数

確認するのは、このモデルでの結論である。実機のすべての環境で前提が成立する証拠ではない。
"""
import pytest

P = 10_000          # フレーム周期(整数の単位で扱う)


def observed(n_frames: int, phase: int, d_press: int, d_release: int,
             read: int = P) -> int:
    """この条件でゲームが「押されている」と見るフレーム数。"""
    a = d_press                       # 押下が見えた時刻(押下指示を 0 とする)
    b = n_frames * P + d_release      # 解放が見えた時刻
    # 位相 phase のサンプリング点 phase + k*read のうち [a, b) に入る数
    lo = -(-(a - phase) // read)      # ceil((a-phase)/read)
    hi = -(-(b - phase) // read)      # ceil((b-phase)/read)
    return max(0, hi - lo)


def sweep(n_frames: int, delay_ratio: float, steps: int = 60, read: int = P):
    """位相と遅れを総当たりして、観測フレーム数の最小・最大を返す。"""
    d_max = int(P * delay_ratio)
    lo, hi = None, None
    for i in range(steps):
        phase = read * i // steps
        for j in range(steps + 1):
            d1 = d_max * j // steps
            for k in range(steps + 1):
                d2 = d_max * k // steps
                v = observed(n_frames, phase, d1, d2, read)
                lo = v if lo is None else min(lo, v)
                hi = v if hi is None else max(hi, v)
    return lo, hi


# 1 フレームに対する遅れの比。0.06 = 実機の送信粒度(bInterval=1ms)相当。
# 0.48 はその 8 倍で、粒度が荒くても結論が変わらないことを見るための余裕
REAL_DELAYS = [0.06, 0.48]


@pytest.mark.parametrize("ratio", REAL_DELAYS)
def test_one_frame_can_vanish(ratio):
    """1 フレームの指示は、位相によっては 0 回、つまりまったく押されないことが
    あること。"""
    lo, hi = sweep(1, ratio)
    assert lo == 0, f"1F で 0 回になる位相がない(遅れ {ratio}): 最小 {lo}"
    assert hi == 2, f"1F の上限は 2 のはず: {hi}"


@pytest.mark.parametrize("ratio", REAL_DELAYS)
@pytest.mark.parametrize("n", [2, 3, 4, 10])
def test_two_or_more_frames_never_vanish(n, ratio):
    """2 フレーム以上は、どの位相、どの遅れでも必ず 1 回以上押されること。

    これが「2 フレームには警告を出さない」根拠。長さは N−1〜N+1 でばらつくが、
    「押されない」は起きない。
    """
    lo, hi = sweep(n, ratio)
    assert lo >= 1, f"{n}F なのに 0 回になる場合がある(遅れ {ratio})"
    assert (lo, hi) == (n - 1, n + 1), (
        f"{n}F の観測範囲が N−1〜N+1 でない: {lo}〜{hi}"
        "(hardware-design.md §1-2 と矛盾する)")


def test_guarantee_depends_on_delay_being_under_one_frame():
    """遅れが 1 フレームを超えると、2F でも押されない場合がありうること。

    実機では遅れは送信の粒度(1ms)であり、1 フレーム(16.7ms)より十分
    小さい。この前提が崩れる構成にしたら閾値も見直すこと。
    """
    lo, _ = sweep(2, 1.2)      # 遅れが 1.2 フレームある異常な構成
    assert lo == 0, "前提(遅れ<1フレーム)の現れ方が想定と違う"


def test_a_game_that_reads_more_slowly_can_miss_two_frames():
    """読み取りの周期が 2 フレームぶんのゲームでは、2 フレームは読まれないことがあり、
    3 フレームは 1 回以上読まれること。

    見える長さの最小(指示の長さから遅れの上限を引いた長さ)と読み取りの周期を比べる
    (hardware-design.md §1-2)。遅れの上限は 8ms(0.48 フレーム)である。
    """
    assert sweep(2, 0.48, read=2 * P)[0] == 0
    assert sweep(3, 0.48, read=2 * P)[0] >= 1


def test_exact_multiple_is_stable_without_delay():
    """遅れがなければ、位相によらず指示どおりの回数になること
    (hardware-design.md §1-3)。"""
    for n in (1, 2, 5):
        assert sweep(n, 0.0) == (n, n)

