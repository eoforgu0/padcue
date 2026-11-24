"""条件(Condition)の保存と照合(conditions.py)。

画像は「背景色地に色つき矩形」や細かい模様の合成画像を frames.encode_png で作る。
実写真は必要ない。照合の決まり(明るさと濃さのずれを打ち消した色の差、位置の
探索、範囲と基準画像の組み合わせ方)をテストできれば足りる。
"""
from __future__ import annotations

import errno
import json
import os
import random
import re
import time

import pytest

from padcue import conditions as conditions_mod
from padcue.conditions import (
    AFTER_COUNT,
    COMPARE_H,
    COMPARE_W,
    DEFAULT_THRESHOLD,
    FORMAT_VERSION,
    Condition,
    ConditionStore,
    after_rel,
    agreement,
    judge,
    make_source,
    matches,
    score,
    suggest_threshold,
    validate_source,
)
from padcue.frames import Frame, encode_png, frame_from_png
from padcue.sources import DesktopSource, DShowSource, FileSource, WindowSource
from tests.helpers import add_images

BG = (30, 30, 30)
FG = (220, 220, 220)
_DUMMY_SOURCE = {"kind": "file", "path": "dummy.png"}


def _solid_rgb(w: int, h: int, color: tuple[int, int, int]) -> bytes:
    return bytes(color) * (w * h)


def _rect_rgb(w: int, h: int, bg: tuple[int, int, int], fg: tuple[int, int, int],
              rect_xywh: tuple[int, int, int, int]) -> bytes:
    """bg 色地に fg 色の矩形を描いた RGB(w*h*3 バイト)。"""
    rx, ry, rw, rh = rect_xywh
    out = bytearray(_solid_rgb(w, h, bg))
    for y in range(ry, min(ry + rh, h)):
        for x in range(rx, min(rx + rw, w)):
            i = (y * w + x) * 3
            out[i:i + 3] = bytes(fg)
    return bytes(out)


def make_sample_png(w: int = COMPARE_W, h: int = COMPARE_H,
                    rect: tuple[int, int, int, int] | None = None) -> bytes:
    if rect is None:
        rect = (0, 0, w // 2, h)
    return encode_png(w, h, _rect_rgb(w, h, BG, FG, rect))


def _pattern_rgb(seed: int, w: int = COMPARE_W, h: int = COMPARE_H) -> bytes:
    """細かい模様(8 画素四方のまだら)。位置のずれや明るさのずれが差に出やすい。"""
    rnd = random.Random(seed)
    cells = [[bytes((rnd.randrange(40, 216), rnd.randrange(40, 216),
                     rnd.randrange(40, 216))) for _ in range(w // 8 + 1)]
             for _ in range(h // 8 + 1)]
    return b"".join(b"".join(cells[y // 8][x // 8] for x in range(w))
                    for y in range(h))


def _map_rgb(rgb: bytes, fn) -> bytes:
    return bytes(min(255, max(0, round(fn(v)))) for v in rgb)


def _shift_rgb(rgb: bytes, dx: int, w: int = COMPARE_W, h: int = COMPARE_H) -> bytes:
    """横に dx 画素ずらす(空いた所は端の画素で埋める)。"""
    rows = []
    for y in range(h):
        row = rgb[y * w * 3:(y + 1) * w * 3]
        if dx > 0:
            row = row[:3] * dx + row[:-dx * 3]
        elif dx < 0:
            row = row[-dx * 3:] + row[-3:] * -dx
        rows.append(row)
    return b"".join(rows)


def frame_of(rgb: bytes) -> Frame:
    return Frame(COMPARE_W, COMPARE_H, rgb)


def make_condition(refs: list[bytes],
                   regions: list[tuple[float, float, float, float]] | None = None,
                   threshold: int = DEFAULT_THRESHOLD) -> Condition:
    """保存せずに Condition を作る(照合のテスト用)。refs は比較の大きさの RGB。"""
    return Condition(name="x", source=dict(_DUMMY_SOURCE),
                     regions=list(regions or []),
                     refs=[frame_of(r) for r in refs], threshold=threshold)


# ---- 一致度 ----


def test_the_same_image_agrees_fully():
    rgb = _pattern_rgb(1)
    assert score(frame_of(rgb), make_condition([rgb])) == 100


def test_a_different_image_agrees_less():
    a = _rect_rgb(COMPARE_W, COMPARE_H, BG, FG, (0, 0, COMPARE_W // 2, COMPARE_H))
    b = _rect_rgb(COMPARE_W, COMPARE_H, BG, FG,
                  (COMPARE_W // 2, 0, COMPARE_W // 2, COMPARE_H))
    assert score(frame_of(b), make_condition([a])) < 50


def test_agreement_is_100_minus_the_difference_rounded_down():
    """一致度は 100 から差を引いて小数を切り捨てたもの(0 未満は 0)であること。

    切り捨てるのは、画面に出す一致度(整数)としきい値の大小が判定と矛盾しない
    ようにするためである。87.6 を 88 と出して不一致、ということが起きない。
    """
    assert agreement(0.0) == 100
    assert agreement(12.0) == 88
    assert agreement(12.4) == 87
    assert agreement(255.0) == 0


def test_the_threshold_counts_as_a_match():
    """一致度がしきい値と同じなら一致であること(「この値以上で一致」)。"""
    rgb = _pattern_rgb(2)
    got = score(frame_of(_pattern_rgb(3)), make_condition([rgb]))
    assert matches(frame_of(_pattern_rgb(3)), make_condition([rgb], threshold=got))
    if got < 100:
        assert not matches(frame_of(_pattern_rgb(3)),
                           make_condition([rgb], threshold=got + 1))


def test_the_default_threshold_means_a_difference_of_12_or_less():
    """既定のしきい値 88 は、範囲ごとの差が 12 以下で一致にあたること。

    値の根拠は observe-and-decide.md の「設計判断」にある。
    """
    assert DEFAULT_THRESHOLD == agreement(12.0)
    assert agreement(12.01) < DEFAULT_THRESHOLD


def test_compare_size_is_a_third_of_1080p():
    """比較の大きさは 640x360 であること。

    1080p の 3 分の 1 である。横 3 画素が 1 画素になるので、1 文字の幅が
    およそ 20 画素の表示なら縮めた後も 6 画素残り、形が保たれる。
    """
    assert (COMPARE_W, COMPARE_H) == (640, 360)
    assert (1920 // COMPARE_W, 1080 // COMPARE_H) == (3, 3)


def test_a_brightness_and_contrast_drift_is_ignored():
    """取り込みの明るさと濃さのずれ(一律の倍率と足し算)は打ち消すこと。

    キャプチャーの設定や配信ソフトの色の扱いで、同じ画面が少し明るく、
    または濃く取れることがある。画素の差だけで比べると、これが一致を取りこぼす。
    """
    rgb = _pattern_rgb(4)
    drifted = _map_rgb(rgb, lambda v: v * 1.2 + 15)
    assert score(frame_of(drifted), make_condition([rgb])) >= 98


def test_a_brightness_change_beyond_the_range_is_seen():
    """打ち消す幅を超える明るさの違い(白い字と灰色の字)は、違いとして残すこと。

    押せる項目は白、押せない項目は灰色、のように明るさだけで状態を表す画面がある。
    """
    w, h = COMPARE_W, COMPARE_H
    white = _rect_rgb(w, h, (20, 20, 20), (255, 255, 255), (0, 0, w // 2, h))
    gray = _rect_rgb(w, h, (20, 20, 20), (150, 150, 150), (0, 0, w // 2, h))
    assert score(frame_of(gray), make_condition([white])) < DEFAULT_THRESHOLD


def test_flat_areas_far_apart_in_brightness_are_seen():
    """模様のない範囲どうしでも、打ち消す幅(±40 段)を超えた明るさの違いは残ること。

    真っ暗な画面と灰色の画面のように、模様のない範囲の違いが明るさだけのことがある。
    明るさのずれを幅の制限なしに打ち消すと、どちらも一様なので完全に一致してしまう。
    """
    w, h = COMPARE_W, COMPARE_H
    got = score(frame_of(_solid_rgb(w, h, (120, 120, 120))),
                make_condition([_solid_rgb(w, h, (20, 20, 20))]))
    assert got < DEFAULT_THRESHOLD, got


def test_a_nearly_flat_reference_does_not_guess_the_contrast_from_noise():
    """ほぼ一様な基準画像は、一様な基準画像と同じ一致度になること。

    暗い画面の 1 段のノイズのように、模様のない範囲には、濃さの倍率を求める
    手がかりがない。ノイズから倍率を求めると、ノイズがいまの映像の明るい所と
    たまたま重なっただけで倍率が制限の端まで振れ、一致度がゆるくなる。
    """
    w, h = COMPARE_W, COMPARE_H
    rect = (0, 0, w // 10, h)
    shot = _rect_rgb(w, h, (20, 20, 20), (255, 255, 255), rect)
    flat = _solid_rgb(w, h, (20, 20, 20))
    noisy = _rect_rgb(w, h, (20, 20, 20), (21, 21, 21), rect)
    a = score(frame_of(shot), make_condition([flat]))
    b = score(frame_of(shot), make_condition([noisy]))
    assert abs(a - b) <= 1, (a, b)


@pytest.mark.parametrize("fast", [True, False], ids=["numpy", "純 Python"])
def test_a_bright_nearly_flat_area_with_its_own_noise_still_matches(monkeypatch, fast):
    """明るくほぼ一様な範囲は、取り込みごとの小さなばらつきだけなら一致すること。

    ばらつきが基準画像といまの映像で別々だと、濃さの倍率は 0 近くに出て下限の 0.7 に
    切り詰められ、続く足し算も上限の 40 で切り詰められる。その組では明るさ 200 の
    範囲が 180 に写り、同じ画面の一致度が 71 になる。倍率 1 と足し算(上限の中)の
    組のほうが差が小さいなら、そちらを使う。
    """
    if fast:
        pytest.importorskip("numpy")
    else:
        monkeypatch.setattr(conditions_mod, "_np", None)
    w, h = COMPARE_W, COMPARE_H
    rnd_ref, rnd_cur = random.Random(1), random.Random(2)
    # 平均 200、分散 2.5(倍率を合わせる下限の 1.0 を超える)。ばらつきは互いに無関係
    ref = bytes(200 + rnd_ref.choice((-2, -1, 1, 2)) for _ in range(w * h * 3))
    cur = bytes(200 + rnd_cur.choice((-2, -1, 1, 2)) for _ in range(w * h * 3))
    got = score(frame_of(cur), make_condition([ref], [(0.4, 0.4, 0.2, 0.2)]))
    assert got >= 95, got


def test_a_color_difference_with_the_same_brightness_is_seen():
    """明るさが同じで色だけ違う 2 枚は、一致しないこと。

    同じ位置に出る色違いの表示や、色で種類を分けるアイコンを区別するため。
    明るさと濃さは R、G、B に一律で合わせるので、色の違いは打ち消されない。
    """
    w, h = COMPARE_W, COMPARE_H

    def lum(c):
        return (77 * c[0] + 150 * c[1] + 29 * c[2]) >> 8

    brown, green = (130, 90, 60), (0, 168, 0)
    assert lum(brown) == lum(green), (lum(brown), lum(green))
    got = score(frame_of(_solid_rgb(w, h, green)),
                make_condition([_solid_rgb(w, h, brown)]))
    assert got < 50, got


def test_a_shift_of_up_to_three_pixels_is_tolerated():
    """前後左右 3 画素までの位置のずれは、探して合わせること。

    取り込みのたびに 1〜2 画素ずれる映像がある。合わせないと、範囲の中の縁で
    差が跳ねて一致を取りこぼす。
    """
    rgb = _pattern_rgb(5)
    region = [(0.2, 0.2, 0.5, 0.5)]
    for dx in (1, 3, -3):
        got = score(frame_of(_shift_rgb(rgb, dx)), make_condition([rgb], region))
        assert got == 100, (dx, got)
    far = score(frame_of(_shift_rgb(rgb, 8)), make_condition([rgb], region))
    assert far < 90, far


def test_the_lowest_region_decides():
    """範囲が複数なら、最も低い一致度がその基準画像の一致度であること。

    小さい範囲だけがずれた画面は、大きい範囲が一致していても一致ではない。
    平均にすると、小さい範囲のずれが大きい範囲の一致に埋もれる。
    """
    w, h = COMPARE_W, COMPARE_H
    ref = _pattern_rgb(6)
    off = bytearray(ref)
    for y in range(0, h // 10):
        for x in range(0, w // 10):
            i = (y * w + x) * 3
            off[i:i + 3] = bytes((255, 0, 255))
    small, large = (0.0, 0.0, 0.1, 0.1), (0.5, 0.5, 0.4, 0.4)
    got = judge(frame_of(bytes(off)), make_condition([ref], [small, large]))
    assert got.regions[1] == 100
    assert got.regions[0] < 50
    assert got.score == got.regions[0]


def test_a_thin_region_at_the_right_edge_stays_inside_the_frame():
    """右端と下端の、比べる大きさで 1 画素に満たない範囲も、絵の中で比べること。

    幅と高さは 1 画素に上げる。位置をそのまま丸めると範囲が絵の外へ出て、同じ絵
    どうしでも一致度が 0 になる。
    """
    rgb = _pattern_rgb(5)
    for region in ((0.9995, 0.2, 0.0005, 0.3), (0.2, 0.9995, 0.3, 0.0005)):
        assert score(frame_of(rgb), make_condition([rgb], [region])) == 100, region


def test_the_highest_reference_decides():
    """基準画像が複数なら、最も高いものが条件の一致度であること。

    どれか 1 枚に一致すれば一致である。背景が昼と夜で違う場面などを、1 つの
    条件で判定するためである。
    """
    day, night = _pattern_rgb(7), _pattern_rgb(8)
    got = judge(frame_of(night), make_condition([day, night]))
    assert got.score == 100
    assert got.best == 1
    assert got.refs[1] == 100 and got.refs[0] < 100
    assert matches(frame_of(night), make_condition([day, night]))


def test_no_region_means_the_whole_frame():
    """範囲を指定しない条件は画面全体を比べること。"""
    rgb = _pattern_rgb(9)
    cond = make_condition([rgb])
    assert cond.regions == []
    off = bytearray(rgb)
    for i in range(0, len(off) // 2):      # 上半分を真っ黒にする
        off[i] = 0
    assert score(frame_of(bytes(off)), cond) < 80


def test_judging_without_a_reference_is_refused():
    """基準画像のない条件は採点を拒否すること。呼び出し元はその選択肢を不一致にする。"""
    with pytest.raises(ValueError, match="基準画像がありません"):
        score(frame_of(_pattern_rgb(1)), make_condition([]))


def test_numpy_and_pure_python_give_the_same_values(monkeypatch):
    """numpy の経路と純 Python の経路が、同じ一致度を出すこと。

    環境(numpy の有無)で値が変わると、調整したしきい値の一致と不一致が
    入れ替わる。和をどちらも整数で取り、同じ式で倍率を求めるので、値は一致する。
    """
    pytest.importorskip("numpy")
    ref = _pattern_rgb(10)
    cur = _shift_rgb(_map_rgb(_pattern_rgb(10), lambda v: v * 0.8 + 30), 2)
    regions = [(0.0, 0.0, 0.2, 0.2), (0.9, 0.85, 0.1, 0.15), (0.3, 0.3, 0.3, 0.2)]
    cond = make_condition([ref, _pattern_rgb(11)], regions)
    fast = judge(frame_of(cur), cond)
    monkeypatch.setattr(conditions_mod, "_np", None)
    slow = judge(frame_of(cur), cond)
    assert fast == slow


# ---- しきい値の目安 ----


def test_the_suggestion_lies_between_the_two_kinds():
    """目安は、一致する側の最小と一致しない側の最大の真ん中であること。"""
    got, why = suggest_threshold([97, 95], [60, 70])
    assert (got, why) == (83, "")
    assert 70 < got <= 95


def test_adjacent_values_still_separate():
    got, why = suggest_threshold([71], [70])
    assert (got, why) == (71, "")


def test_no_suggestion_without_mismatching_screens():
    """一致しないはずの画面がなければ、目安も理由も出さないこと(計算に使う画像が
    ないだけ)。"""
    assert suggest_threshold([95], []) == (None, "")


def test_no_suggestion_without_the_frames_after_registering_says_why():
    # 基準画像はあり、続けて取得した画像がない。足りない物を書く(「基準画像を追加して
    # ください」だけでは、もう入っているのにと迷う)
    assert suggest_threshold([], [70]) == (
        None, ("しきい値の目安を表示できません。基準画像の追加に続けて取得した画像が"
               "ありません。判定したい場面を表示した状態で、基準画像をもう 1 枚"
               "追加してください。"))


def test_overlapping_values_give_the_reason_instead():
    """一致しないはずの画面がばらつきと重なれば、目安の代わりに理由を返すこと。"""
    for hits, misses in (([80], [85]), ([80], [80])):
        assert suggest_threshold(hits, misses) == (
            None, "一致しないはずの画面と分けられません")


# ---- Condition のテスト ----


def test_condition_accepts_no_region_as_the_whole_frame():
    cond = Condition(name="x", source=dict(_DUMMY_SOURCE), regions=[], refs=[])
    assert cond.regions == []


def test_condition_region_bad_tuple_length_rejected():
    with pytest.raises(ValueError, match="4 つの数値"):
        Condition(name="x", source=dict(_DUMMY_SOURCE), regions=[(0, 0, 1)], refs=[])


@pytest.mark.parametrize("regions", [
    5, "全体", None, [5], [None], [["0", 0, 0.5, 0.5]], [[None, 0, 0.5, 0.5]],
    [[True, 0, 0.5, 0.5]], [{"x": 0}]])
def test_condition_regions_of_another_shape_are_refused_as_values(regions):
    """形の違う判定する範囲は、ValueError で拒否すること(TypeError にしない)。

    condition.json を手で書き間違えると、ここに数でも配列でもない値が来る。型ごとの
    例外が漏れると、条件の一覧と開く処理が、読めない条件として扱えずに失敗する。
    """
    with pytest.raises(ValueError, match=r"regions は範囲の配列|4 つの数値"):
        Condition(name="x", source=dict(_DUMMY_SOURCE), regions=regions, refs=[])


def test_condition_region_out_of_range_rejected():
    with pytest.raises(ValueError):
        Condition(name="x", source=dict(_DUMMY_SOURCE), regions=[(0, 0, 1.5, 1)],
                  refs=[])


# 10 ** 400 は float にできない(予期しないエラーにせず、範囲の外として拒否する)
@pytest.mark.parametrize("bad", [0, 101, 87.5, True, "88a", None, float("nan"),
                                 10 ** 400])
def test_condition_threshold_must_be_an_integer_from_1_to_100(bad):
    with pytest.raises(ValueError, match="threshold"):
        Condition(name="x", source=dict(_DUMMY_SOURCE), regions=[], refs=[],
                  threshold=bad)


@pytest.mark.parametrize("bad", ["88", "dshow"])
def test_condition_values_are_not_read_from_strings(bad):
    """数の欄に書いた文字列は、数に変換して受けずに拒否すること。手で書いた誤りを
    黙って通さない(file-format.md §9-1)。"""
    with pytest.raises(ValueError, match="threshold"):
        Condition(name="x", source=dict(_DUMMY_SOURCE), regions=[], refs=[],
                  threshold=bad)
    with pytest.raises(ValueError, match="monitor"):
        validate_source({"kind": "desktop", "monitor": bad})
    with pytest.raises(ValueError, match="文字列"):
        validate_source({"kind": "window", "title": 5})


def test_condition_threshold_accepts_an_integral_float():
    cond = Condition(name="x", source=dict(_DUMMY_SOURCE), regions=[], refs=[],
                     threshold=88.0)
    assert cond.threshold == 88 and isinstance(cond.threshold, int)


def test_condition_source_unknown_kind_rejected():
    with pytest.raises(ValueError, match="source"):
        Condition(name="x", source={"kind": "obs"}, regions=[], refs=[])


def test_condition_source_file_requires_path():
    with pytest.raises(ValueError, match="path"):
        Condition(name="x", source={"kind": "file"}, regions=[], refs=[])


def test_condition_source_dshow_requires_device():
    with pytest.raises(ValueError, match="device"):
        Condition(name="x", source={"kind": "dshow"}, regions=[], refs=[])


def test_condition_accepts_desktop_source():
    c = Condition(name="d", source={"kind": "desktop"}, regions=[], refs=[])
    assert c.source == {"kind": "desktop"}
    c2 = Condition(name="d", source={"kind": "desktop", "monitor": 2},
                   regions=[], refs=[])
    assert c2.source == {"kind": "desktop", "monitor": 2}


def test_condition_accepts_window_source():
    c = Condition(name="w", source={"kind": "window", "title": "メモ帳"},
                  regions=[], refs=[])
    assert c.source == {"kind": "window", "title": "メモ帳"}


def test_condition_source_window_requires_title():
    with pytest.raises(ValueError, match="title"):
        Condition(name="x", source={"kind": "window"}, regions=[], refs=[])


def test_condition_source_desktop_unknown_key_rejected():
    with pytest.raises(ValueError, match="使用できない項目"):
        Condition(name="x", source={"kind": "desktop", "moniter": 0},
                  regions=[], refs=[])


def test_condition_source_window_unknown_key_rejected():
    with pytest.raises(ValueError, match="使用できない項目"):
        Condition(name="x", source={"kind": "window", "title": "A", "extra": 1},
                  regions=[], refs=[])


def test_condition_source_unknown_key_rejected():
    """未知のキーは黙って捨てず、エラーにすること(手編集の打ち間違いを見つけるため)。"""
    with pytest.raises(ValueError, match="使用できない項目"):
        Condition(name="x",
                  source={"kind": "dshow", "device": "Cam", "devcie": "typo"},
                  regions=[], refs=[])


# ---- make_source ----


def test_make_source_file():
    src = make_source({"kind": "file", "path": "shot.png"})
    assert isinstance(src, FileSource)
    assert str(src.path) == "shot.png"


def test_make_source_dshow():
    src = make_source({"kind": "dshow", "device": "OBS Virtual Camera"})
    assert isinstance(src, DShowSource)
    assert src.device_name == "OBS Virtual Camera"


def test_make_source_desktop():
    src = make_source({"kind": "desktop"})
    assert isinstance(src, DesktopSource)
    assert src.monitor == 0


def test_make_source_desktop_with_monitor():
    src = make_source({"kind": "desktop", "monitor": 2})
    assert isinstance(src, DesktopSource)
    assert src.monitor == 2


def test_make_source_window():
    src = make_source({"kind": "window", "title": "メモ帳"})
    assert isinstance(src, WindowSource)
    assert src.title == "メモ帳"


def test_make_source_window_requires_title():
    with pytest.raises(ValueError, match="title"):
        make_source({"kind": "window"})


def test_make_source_unknown_kind_rejected():
    with pytest.raises(ValueError):
        make_source({"kind": "obs"})


# ---- ConditionStore: 保存、読み込み、名前、削除 ----


def _store_with_ref(tmp_path, png: bytes | None = None):
    """基準画像 images/0001.png を 1 枚持つ条件を置く。

    追加した画像の名前は乱数になるので、テストが名前で指定できるように、ファイルを置いて
    から並びに入れる(手で書いた条件やインポートした条件と同じく、画像の名前は
    images/ の中なら何でもよい)。
    """
    store = ConditionStore(tmp_path)
    store.save("条件1", _DUMMY_SOURCE, [(0, 0, 1, 1)], DEFAULT_THRESHOLD)
    path = store.image_path("条件1", "images/0001.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png or make_sample_png())
    _keep(store, "条件1", ["images/0001.png"], [])
    return store


def _keep(store, name: str, refs: list, negatives: list) -> list[str]:
    """条件 name の画像の並びを refs と negatives にして保存する(設定はそのまま)。"""
    doc = json.loads(store.doc_path(name).read_text(encoding="utf-8"))
    return store.save(name, doc["source"], [tuple(r) for r in doc["regions"]],
                      doc["threshold"], refs=refs, negatives=negatives)


def test_a_failed_folder_replacement_keeps_the_old_condition(tmp_path, monkeypatch):
    """フォルダーの置き換えが途中で失敗しても、元の条件がそのまま残ること。

    元のフォルダーを消してから書くと、1 枚でも書けなかったときに、元の条件も
    新しい条件も失う。書きかけの場所も残さない。
    """
    store = _store_with_ref(tmp_path)
    before = store.doc_path("条件1").read_bytes()
    real = type(tmp_path).write_bytes

    def full_disk(self, data):
        if self.name == "0002.png":
            raise OSError(28, "空き容量がありません", str(self))
        return real(self, data)

    monkeypatch.setattr(type(tmp_path), "write_bytes", full_disk)
    images = {"images/0001.png": make_sample_png(), "images/0002.png": b"x"}
    with pytest.raises(OSError):
        store.put_folder("条件1", "{}", images)
    assert store.doc_path("条件1").read_bytes() == before
    assert store.refs("条件1") == ["images/0001.png"]
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_a_replacement_that_cannot_be_undone_keeps_the_backup(tmp_path, monkeypatch):
    """入れ替えも元に戻すのも失敗したら、元の条件のバックアップを消さずに場所を
    示すこと。

    バックアップを消すと、元の条件はどこにも残らない。場所は画面にそのまま出す文で、
    プロジェクトのフォルダーからの位置で示す(OSError にすると、画面の文が決まり
    文句に置き換わり、場所が消える)。
    """
    store = _store_with_ref(tmp_path)
    before = store.doc_path("条件1").read_bytes()
    real = type(tmp_path).replace

    def locked(self, target):
        # バックアップへ退ける 1 回目だけ通し、新しい条件の置き換えと、元に戻すのは
        # 拒否する
        if self.name == "条件1":
            return real(self, target)
        raise PermissionError(13, "ほかのアプリが使っています", str(self))

    monkeypatch.setattr(type(tmp_path), "replace", locked)
    with pytest.raises(ValueError) as e:
        store.put_folder("条件1", "{}", {})
    monkeypatch.undo()
    [kept] = tmp_path.glob(".padcue-*/old")
    # 失敗の文は「〜できません」の形で書く(gui-principles.md 7-13)
    assert str(e.value).startswith(
        "条件「条件1」を置き換えられず、元に戻すこともできません。"), e.value
    assert f"「{kept.relative_to(tmp_path)}」に残っています" in str(e.value), e.value
    assert (kept / "condition.json").read_bytes() == before, e.value


def test_left_over_replacement_folders_are_cleared_but_a_backup_is_kept(tmp_path):
    """置き換えの作業用フォルダーの残りは片付け、元に戻せなかったコピーは残すこと。

    置き換えた後に古い中身(old)を消せなかったときと、書いている途中で中断した
    ときは、プロジェクトの直下に「.padcue-」と 16 進 8 桁の名前のフォルダーが残る。
    利用者には何のフォルダーか分からない。new と old の両方が残っているものは、元に
    戻せなかったと文で場所を知らせたコピーなので消さない。
    """
    store = ConditionStore(tmp_path)
    done = tmp_path / ".padcue-0a1b2c3d"       # 置き換えた後に old を消せなかった
    stopped = tmp_path / ".padcue-4e5f6a7b"    # new を書いている途中で中断した
    backup = tmp_path / ".padcue-11111111"     # 元に戻せなかった
    for d in (done / "old", stopped / "new", backup / "new", backup / "old",
              tmp_path / ".padcue-notes"):
        d.mkdir(parents=True)
        (d / "condition.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".padcue-client-id").write_text("x", encoding="utf-8")
    assert store.sweep_work() == 2
    left = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".padcue-"))
    assert left == [".padcue-11111111", ".padcue-client-id", ".padcue-notes"]


def test_store_save_load_names_rename_delete(tmp_path):
    store = ConditionStore(tmp_path)
    png = make_sample_png()
    src = {"kind": "dshow", "device": "OBS Virtual Camera"}
    assert store.names() == []
    assert not store.exists("条件1")

    store.save("条件1", src, [(0.0, 0.0, 1.0, 1.0)], 90)
    assert store.exists("条件1")
    assert store.names() == ["条件1"]
    assert store.load("条件1").refs == []
    rel = add_images(store, "条件1", refs=[png])[0]

    cond = store.load("条件1")
    assert cond.name == "条件1"
    assert cond.source == src
    assert cond.regions == [(0.0, 0.0, 1.0, 1.0)]
    assert cond.threshold == 90
    assert [(r.width, r.height) for r in cond.refs] == [(COMPARE_W, COMPARE_H)]
    assert cond.ref_files == [rel]

    store.rename("条件1", "条件2")
    assert store.names() == ["条件2"]
    assert not store.exists("条件1")
    # 名前はフォルダー名なので、画像もそのまま付いてくる
    assert store.image_bytes("条件2", rel) == png
    assert store.load("条件2").name == "条件2"

    store.delete("条件2")
    assert store.names() == []
    assert not store.cond_dir("条件2").exists()


def test_store_rename_missing_raises(tmp_path):
    store = ConditionStore(tmp_path)
    with pytest.raises(ValueError, match="が見つかりません"):
        store.rename("ない", "新")


def test_store_rename_collision_raises(tmp_path):
    store = ConditionStore(tmp_path)
    src = {"kind": "file", "path": "a.png"}
    store.save("A", src, [], DEFAULT_THRESHOLD)
    store.save("B", src, [], DEFAULT_THRESHOLD)
    with pytest.raises(ValueError, match="既にあります"):
        store.rename("A", "B")


def test_a_brief_hold_on_a_condition_does_not_stop_the_rename(tmp_path, monkeypatch):
    """条件の中のファイルをほかのスレッドやプロセスが一瞬開いていても、条件の名前を
    変えられること。

    条件の名前はフォルダーの名前である。Windows は、ほかのスレッドやプロセスが中の
    ファイルを開いている間、フォルダーの名前の変更を PermissionError(WinError 5)で
    拒否する。ここでは、その拒否を 1 度だけ起こす。
    """
    store = ConditionStore(tmp_path)
    store.save("条件1", {"kind": "file", "path": "a.png"}, [(0.0, 0.0, 1.0, 1.0)], 90)
    real = type(tmp_path).rename
    refused = []

    def held_once(self, target):
        if not refused and self.name == "条件1":
            refused.append(self)
            raise PermissionError(errno.EACCES, "アクセスが拒否されました。",
                                  str(self), 5, str(target))
        return real(self, target)
    monkeypatch.setattr(type(tmp_path), "rename", held_once)
    store.rename("条件1", "条件2")
    assert refused, "名前の変更を拒否できていない(このテストの前提)"
    assert store.names() == ["条件2"]


def test_store_load_missing_raises(tmp_path):
    store = ConditionStore(tmp_path)
    with pytest.raises(ValueError, match="が見つかりません"):
        store.load("ない")


def test_store_invalid_name_rejected(tmp_path):
    store = ConditionStore(tmp_path)
    with pytest.raises(ValueError):
        store.save("bad/name", {"kind": "file", "path": "a.png"}, [], 88)


def test_store_save_json_shape(tmp_path):
    store = ConditionStore(tmp_path)
    png = make_sample_png()
    src = {"kind": "dshow", "device": "OBS Virtual Camera"}
    store.save("条件1", src, [(0.1, 0.2, 0.3, 0.4)], 85)
    ref = add_images(store, "条件1", refs=[png])[0]
    neg = add_images(store, "条件1", negatives=[make_sample_png(rect=(0, 0, 4, 4))])[0]
    doc = json.loads(
        (store.cond_dir("条件1") / "condition.json").read_text(encoding="utf-8"))
    assert doc == {
        "kind": "condition", "format": 1, "source": src,
        "regions": [[0.1, 0.2, 0.3, 0.4]],
        "threshold": 85,
        "refs": [ref],
        "negatives": [neg],
    }
    # 画像の名前は 16 桁の 16 進の乱数である
    assert re.fullmatch(r"images/[0-9a-f]{16}\.png", ref), ref
    # 名前はフォルダー名で、文書の中には書かない
    assert "name" not in doc
    assert store.image_path("条件1", ref).read_bytes() == png


def test_store_save_rejects_bad_source(tmp_path):
    store = ConditionStore(tmp_path)
    with pytest.raises(ValueError):
        store.save("条件1", {"kind": "obs"}, [], 88)


def test_store_save_rejects_a_bad_threshold(tmp_path):
    store = ConditionStore(tmp_path)
    with pytest.raises(ValueError, match="threshold"):
        store.save("条件1", _DUMMY_SOURCE, [], 12.5)


def test_broken_images_are_reported_in_the_readers_words(tmp_path):
    """読めない画像は、何の画像かを示して拒否すること。

    PNG の内部の用語(署名、CRC、IHDR)は、読む人には何のことか分からない。
    条件の編集画面に、この文がそのまま出る。
    """
    store = ConditionStore(tmp_path)
    store.save("条件1", _DUMMY_SOURCE, [], 88)
    with pytest.raises(ValueError) as e:
        add_images(store, "条件1", refs=[b"not a png"])
    assert str(e.value) == "基準画像を読み込めません"
    with pytest.raises(ValueError) as e:
        add_images(store, "条件1", negatives=[b"not a png"])
    assert str(e.value) == "画像を読み込めません"
    # ファイルの側で壊れた基準画像も、条件の名前と場所を示す
    rel = add_images(store, "条件1", refs=[make_sample_png()])[0]
    store.image_path("条件1", rel).write_bytes(b"not a png")
    with pytest.raises(ValueError) as e:
        store.load("条件1")
    assert str(e.value) == f"条件「条件1」の基準画像を読み込めません({rel})"


def test_missing_images_are_refused_not_raised(tmp_path):
    """画像のファイルがないときも、何の画像かを示して拒否すること(例外を抜けさせない)。

    条件の編集画面は毎秒採点を問い合わせる。条件を消している途中に読むと、
    ファイルがない。拒否せずに例外が抜けると、操作画面のサーバーは応答を返さずに
    接続を閉じる。
    """
    store = _store_with_ref(tmp_path)
    (store.cond_dir("条件1") / "images" / "0001.png").unlink()
    with pytest.raises(ValueError) as e:
        store.load("条件1")
    assert str(e.value) == "条件「条件1」の基準画像が見つかりません(images/0001.png)"
    with pytest.raises(ValueError) as e:
        store.image_bytes("条件1", "images/0001.png")
    assert str(e.value) == "画像「images/0001.png」が見つかりません"


@pytest.mark.parametrize("head, why", [
    ({"kind": "condition", "format": FORMAT_VERSION - 1},
     f"の format は {FORMAT_VERSION} である必要があります: {FORMAT_VERSION - 1}"),
    ({"kind": "condition", "format": str(FORMAT_VERSION)},
     f"の format は {FORMAT_VERSION} である必要があります: \"{FORMAT_VERSION}\""),
    ({"kind": "condition", "format": True},
     f"の format は {FORMAT_VERSION} である必要があります: true"),
    ({"kind": "condition"},
     f"の format は {FORMAT_VERSION} である必要があります: null"),
    ({}, "は条件ファイルの形式ではありません"),
])
def test_store_refuses_a_document_of_another_format(tmp_path, head, why):
    """頭の 2 欄が今の条件の形でなければ、形式の誤りとして拒否すること。"""
    store = ConditionStore(tmp_path)
    d = store.cond_dir("条件1")
    d.mkdir(parents=True)
    (d / "condition.json").write_text(json.dumps(
        {**head, "source": _DUMMY_SOURCE, "regions": [[0, 0, 1, 1]],
         "threshold": 88, "refs": [], "negatives": []}),
        encoding="utf-8")
    with pytest.raises(ValueError) as e:
        store.load("条件1")
    assert str(e.value) == f"条件「条件1」{why}"


def test_store_refuses_a_newer_document(tmp_path):
    """読めるバージョンより新しい条件は、padcue を更新するよう求めること。"""
    store = ConditionStore(tmp_path)
    d = store.cond_dir("新しい")
    d.mkdir(parents=True)
    (d / "condition.json").write_text(json.dumps(
        {"kind": "condition", "format": 99, "source": _DUMMY_SOURCE,
         "regions": [[0, 0, 1, 1]], "threshold": 88}),
        encoding="utf-8")
    with pytest.raises(ValueError, match="padcue を更新してください"):
        store.load("新しい")


def _rewrite_doc(store, name, **changes):
    """condition.json を手で書き換える(手編集を模す)。"""
    path = store.cond_dir(name) / "condition.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(changes)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_store_refuses_an_unknown_field(tmp_path):
    """使用できない項目は拒否すること。

    黙って捨てると、手で書いた設定が反映されない理由が分からなくなる。
    """
    store = _store_with_ref(tmp_path)
    _rewrite_doc(store, "条件1", thresold=88)
    with pytest.raises(ValueError, match="使用できない項目"):
        store.load("条件1")


def test_store_refuses_an_image_listed_twice(tmp_path):
    """同じ画像を 2 か所から指していたら拒否すること。

    片方を消すと、もう片方が読めなくなる。
    """
    store = _store_with_ref(tmp_path)
    _rewrite_doc(store, "条件1", negatives=["images/0001.png"])
    with pytest.raises(ValueError, match="2 回指しています"):
        store.load("条件1")


def test_store_refuses_refs_that_are_not_positions(tmp_path):
    store = _store_with_ref(tmp_path)
    _rewrite_doc(store, "条件1", refs=[{"file": "images/0001.png"}])
    with pytest.raises(ValueError, match="文字列"):
        store.load("条件1")


def test_store_refuses_an_image_outside_the_folder(tmp_path):
    """画像はフォルダーの中しか指定できないこと。

    位置を確認せずに読むと、手で書いた condition.json がフォルダーの外のファイルを
    読ませられる。「ファイルがない」で拒否するのでは足りないので、外にある実在の
    ファイルを指しても読めないことまで見る。
    """
    store = _store_with_ref(tmp_path)
    outside = store.dir / "外.png"
    outside.write_bytes(make_sample_png(rect=(0, 0, 4, 4)))
    # 「images/D:外.png」は、Windows では D ドライブの今のフォルダーの「外.png」を指す。
    # 「0001.png:s」は代替データストリームである
    for bad in ("images/../../外.png", "../外.png", "外.png",
                "images/sub/外.png", "/tmp/外.png", "images/D:外.png",
                "images/0001.png:s"):
        with pytest.raises(ValueError, match=r"images|使用できない"):
            store.image_path("条件1", bad)
        with pytest.raises(ValueError, match=r"images|使用できない"):
            store.image_bytes("条件1", bad)


# ---- ConditionStore: 基準画像と、一致しないはずの画面 ----


def test_images_join_and_leave_the_condition_only_by_saving(tmp_path):
    """追加した画像も外した画像も、保存するまで条件の並びを変えないこと。

    追加した画像はその場でフォルダーに置くが、condition.json は書き換えない。
    外して保存しても画像のファイルは残し、元に戻して保存し直せる。
    """
    store = _store_with_ref(tmp_path)
    before = store.doc_path("条件1").read_bytes()
    ref2 = store.add_image("条件1", make_sample_png(rect=(0, 0, 8, 8)))
    neg = store.add_image("条件1", make_sample_png(rect=(0, 0, 4, 4)))
    assert store.image_path("条件1", ref2).is_file()
    assert store.doc_path("条件1").read_bytes() == before, "追加しただけで書いた"
    assert store.refs("条件1") == ["images/0001.png"]
    assert _keep(store, "条件1", ["images/0001.png", ref2], [neg]) == []
    assert store.refs("条件1") == ["images/0001.png", ref2]
    assert store.negatives("条件1") == [neg]
    assert _keep(store, "条件1", ["images/0001.png", ref2], []) == []
    assert store.negatives("条件1") == []
    assert store.image_path("条件1", neg).is_file(), "外して保存しただけで消えた"
    assert _keep(store, "条件1", ["images/0001.png", ref2], [neg]) == []
    assert store.negatives("条件1") == [neg]
    _keep(store, "条件1", [], [])
    assert not store.has_refs("条件1")
    assert store.load("条件1").refs == []


def test_saving_leaves_out_images_that_are_gone(tmp_path):
    """並びのうち、フォルダーにない画像は外して保存し、外した位置を返すこと。

    ほかで条件や画像が消されたときに起きる。消えた画像は作り直せないので、
    拒否すると、ほかの設定の変更まで保存できなくなる。
    """
    store = _store_with_ref(tmp_path)
    neg = store.add_image("条件1", make_sample_png())
    store.image_path("条件1", neg).unlink()
    assert _keep(store, "条件1", ["images/0001.png"], [neg]) == [neg]
    assert store.negatives("条件1") == []
    assert store.refs("条件1") == ["images/0001.png"]


def test_a_reference_added_before_saving_keeps_its_follow_ups(tmp_path):
    """保存する前の基準画像にも、直後に取得した画像を隣に置けること。

    画面は基準画像を追加した直後から続けて取得する。保存はそれより後になることがある。
    """
    store = _store_with_ref(tmp_path)
    a1, a2 = make_sample_png(rect=(0, 0, 2, 2)), make_sample_png(rect=(0, 0, 3, 3))
    rel = store.add_image("条件1", make_sample_png())
    assert store.add_after("条件1", rel, a1) == 1
    _keep(store, "条件1", ["images/0001.png", rel], [])
    assert store.add_after("条件1", rel, a2) == 2
    assert store.after_files("条件1", rel) == [
        after_rel(rel, 1), after_rel(rel, 2)]
    assert store.image_bytes("条件1", after_rel(rel, 2)) == a2


@pytest.mark.parametrize("refs, negatives, words", [
    (["images/0001.png", "images/0001.png"], [], "2 回"),
    (["images/0001.png"], ["images/0001.png"], "2 回"),
    ([""], [], "指定が正しくありません"),
    (["../外.png"], [], "images"),
])
def test_saving_refuses_an_order_that_does_not_fit(tmp_path, refs, negatives, words):
    """保存する並びの形が合わなければ、何も書かずに拒否すること。

    同じ画像を 2 か所には置けない。画像はフォルダーの中しか指定できない。
    """
    store = _store_with_ref(tmp_path)
    before = store.doc_path("条件1").read_bytes()
    with pytest.raises(ValueError, match=words):
        _keep(store, "条件1", refs, negatives)
    assert store.doc_path("条件1").read_bytes() == before
    assert store.image_path("条件1", "images/0001.png").exists()


def test_a_broken_image_is_not_added(tmp_path):
    """壊れた画像は、追加するときに拒否し、フォルダーに置かないこと。"""
    store = _store_with_ref(tmp_path)
    before = sorted((store.cond_dir("条件1") / "images").iterdir())
    with pytest.raises(ValueError, match="画像を読み込めません"):
        store.add_image("条件1", b"not a png")
    assert sorted((store.cond_dir("条件1") / "images").iterdir()) == before


def test_image_names_are_not_reused(tmp_path):
    """消えた画像と片付けた画像の名前を、別の画像に使わないこと。

    編集の画面の元に戻すの履歴は、画像を位置で指す。消えた画像の位置に別の
    画像が来ると、戻したときに別の絵になり、そのまま保存できてしまう。ほかで
    ファイルを消されても、起動のときに片付けても同じである。
    """
    store = _store_with_ref(tmp_path)
    ref = store.refs("条件1")[0]
    first = store.add_image("条件1", make_sample_png())
    store.image_path("条件1", first).unlink()
    second = store.add_image("条件1", make_sample_png())
    assert second != first, second
    _keep(store, "条件1", [ref], [])
    _age_images(store, "条件1")
    assert store.sweep() == 1
    third = store.add_image("条件1", make_sample_png())
    assert third not in (first, second), third


def test_the_follow_up_images_sit_beside_the_reference(tmp_path):
    """登録の直後に取得した画像は、基準画像の隣に空いた番号で置くこと。

    condition.json には書かない。名前で基準画像と結び付くので、一覧を持つと同じ事実が
    2 か所になる。揃ったら、それ以上は置かない。
    """
    store = _store_with_ref(tmp_path)
    before = (store.cond_dir("条件1") / "condition.json").read_bytes()
    assert AFTER_COUNT == 2
    for k in (1, 2):
        assert store.add_after("条件1", "images/0001.png", make_sample_png()) == k
    assert store.add_after("条件1", "images/0001.png", make_sample_png()) == 0
    assert store.after_files("条件1", "images/0001.png") == [
        "images/0001.a1.png", "images/0001.a2.png"]
    assert (store.cond_dir("条件1") / "condition.json").read_bytes() == before
    neg = add_images(store, "条件1", negatives=[make_sample_png(rect=(0, 0, 4, 4))])[0]
    assert store.files("条件1") == [
        "images/0001.png", "images/0001.a1.png", "images/0001.a2.png", neg]
    # 基準画像を外して保存すると、条件の画像からは外れる(ファイルは片付けるまで残る)
    _keep(store, "条件1", [], [neg])
    assert store.files("条件1") == [neg]
    # ない基準画像の続きは置かない
    assert store.add_after("条件1", "images/0099.png", make_sample_png()) == 0
    assert not store.image_path("条件1", "images/0099.a1.png").exists()


@pytest.mark.parametrize("rel", [
    "images/0001", "images/0001.jpg", "images/0001.png.", "images/0001.png ",
    "images/a<b.png", "images/.png", "images\\0001.png", "images/0001.PNG",
    "images/" + "a" * 65 + ".png", "images/0001.a1.png", "images/日本.png",
    "images/Abc.png", "images/con.png",
])
def test_store_refuses_an_image_name_outside_the_rules(tmp_path, rel):
    """画像の位置は、決まった 1 つの綴りだけを受けること。

    小文字の英字、数字、ハイフン、下線の 1〜64 文字に .png を付け、区切りは / と
    する。Windows が予約している名前と、直後の画像の名前は並びに書けない。
    Windows は大文字と小文字の違い、末尾の「.」や空白でも同じファイルを開くので、
    別の綴りを通すと、片付けやインポートが別の画像として扱ってしまう。
    """
    store = _store_with_ref(tmp_path)
    _rewrite_doc(store, "条件1", negatives=[rel])
    with pytest.raises(ValueError, match=r"使用できない名前|images/ 内のファイル"):
        store.load("条件1")


def test_sweeping_leaves_a_condition_with_another_spelling_alone(tmp_path):
    """画像を別の綴りで指す条件は読めない条件として扱い、片付けで画像を消さないこと。

    Windows は大文字と小文字が違う綴りでも同じファイルを開く。綴りで照合して
    消すと、手で書き換えた条件の画像を失う。
    """
    store = _store_with_ref(tmp_path)
    path = store.image_path("条件1", "images/0001.png")
    _rewrite_doc(store, "条件1", refs=["images/0001.PNG"])
    with pytest.raises(ValueError, match="使用できない名前"):
        store.load("条件1")
    assert store.sweep() == 0
    assert path.is_file()


def _age_images(store, name: str, days: float = 8) -> None:
    """条件 name の images/ の中のファイルを、days 日前に置いたことにする。

    片付けは、置いてから 7 日たった画像だけを消す。
    """
    old = time.time() - days * 24 * 3600
    for path in (store.cond_dir(name) / "images").iterdir():
        os.utime(path, (old, old))


def test_sweeping_keeps_an_image_added_within_a_week(tmp_path):
    """片付けは、どの条件からも指されていなくても、置いてから 7 日たたない画像を
    消さないこと。

    編集の画面は、追加した画像を保存する前に置き、外した画像も元に戻せるように残す。
    画面は padcue を起動し直してもつながり直して編集を続けられるので、起動のときに
    消すと、開いたままのタブの編集の画像が消える。
    """
    store = _store_with_ref(tmp_path)
    loose = store.add_image("条件1", make_sample_png())
    assert store.sweep() == 0
    assert store.image_path("条件1", loose).is_file()
    _age_images(store, "条件1", days=6)
    assert store.sweep() == 0
    _age_images(store, "条件1")
    assert store.sweep() == 1
    assert not store.image_path("条件1", loose).exists()
    assert store.image_path("条件1", "images/0001.png").is_file()


def test_sweeping_keeps_an_image_for_a_week_after_it_leaves_the_list(tmp_path):
    """保存で並びから外した画像は、外してから 7 日たつまで片付けで消さないこと。

    置いてから日がたった画像を外すと、置いた時刻で数えれば次の起動で消え、
    起動し直した後の元に戻すで戻せない。外した時点から数え直す。
    """
    store = _store_with_ref(tmp_path)
    _age_images(store, "条件1", days=30)
    _keep(store, "条件1", [], [])                 # 基準画像を並びから外して保存
    assert store.sweep() == 0, "外したばかりの画像を消した"
    assert store.image_path("条件1", "images/0001.png").is_file()
    _age_images(store, "条件1")
    assert store.sweep() == 1


def test_sweeping_removes_only_images_nothing_points_to(tmp_path):
    """片付けは、どの条件からも指されていない画像だけを消すこと。

    基準画像とその直後の画像、一致しないはずの画面は残す。condition.json を
    読めない条件のフォルダーは変えない(どれが何の画像か分からない)。
    """
    store = _store_with_ref(tmp_path)
    store.add_after("条件1", "images/0001.png", make_sample_png())
    neg = add_images(store, "条件1", negatives=[make_sample_png()])[0]
    loose = store.add_image("条件1", make_sample_png())
    store.add_after("条件1", loose, make_sample_png())
    images = store.cond_dir("条件1") / "images"
    (images / "0001.png.tmp").write_bytes(b"x")
    broken = store.cond_dir("壊れた")
    (broken / "images").mkdir(parents=True)
    (broken / "condition.json").write_text("{", encoding="utf-8")
    (broken / "images" / "0001.png").write_bytes(make_sample_png())
    _age_images(store, "条件1")
    assert store.sweep() == 3
    assert sorted(x.name for x in images.iterdir()) == [
        "0001.a1.png", "0001.png", neg.split("/")[1]]
    assert (broken / "images" / "0001.png").is_file()
    assert store.sweep() == 0


def test_a_new_reference_does_not_inherit_left_over_follow_up_images(tmp_path):
    """新しい基準画像は、残っていた直後の画像を引き継がないこと。

    条件のフォルダーを消しきれずに残った画像があるところへ、同じ名前で作り直すと
    起きる。残った画像を数えると、しきい値の目安が別の場面から出る。名前は
    乱数なので、残った画像と同じ名前にならない。
    """
    store = ConditionStore(tmp_path)
    stale = store.cond_dir("条件1") / "images" / "0001.a1.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(make_sample_png())
    store.save("条件1", dict(_DUMMY_SOURCE), [], DEFAULT_THRESHOLD)
    rel = add_images(store, "条件1", refs=[make_sample_png()])[0]
    assert store.after_files("条件1", rel) == []


def test_retaking_the_reference_is_not_served_from_the_cache(tmp_path):
    """基準画像の中身が変わったら、次の読み込みから新しい画像になること。

    画像の読み込みは重いのでキャッシュに取る。キャッシュを更新時刻で探すと、同じ時刻の
    中で書き替わったときに古い画像のまま判定し続ける。
    """
    first = make_sample_png(rect=(0, 0, COMPARE_W // 2, COMPARE_H))
    second = make_sample_png(rect=(COMPARE_W // 2, 0, COMPARE_W // 2, COMPARE_H))
    store = _store_with_ref(tmp_path, first)
    assert score(frame_from_png(first), store.load("条件1")) == 100
    (store.cond_dir("条件1") / "images" / "0001.png").write_bytes(second)
    assert score(frame_from_png(second), store.load("条件1")) == 100


# ---- 映像ソースの定義と値の検査 ----


def test_a_device_source_takes_no_timeout():
    """映像機器の定義に timeout を書くと拒否すること。

    待つ長さは padcue が決める(判定と画面の取り込みが同じ上限で待つ)。書けて
    しまうと、使われない値を書いたことに気づけない。
    """
    with pytest.raises(ValueError, match="timeout"):
        validate_source({"kind": "dshow", "device": "X", "timeout": 3.0})


@pytest.mark.parametrize("source, why", [
    ('{"kind": "desktop", "monitor": 1e999}',
     "monitor は整数である必要があります: Infinity"),
    ('{"kind": "desktop", "monitor": NaN}',
     "monitor は整数である必要があります: NaN"),
    ('{"kind": "desktop", "monitor": [1]}',
     "monitor は整数である必要があります: [1]"),
])
def test_validate_source_says_what_a_number_must_be(source, why):
    """数にならない monitor は、文を付けた ValueError で拒否すること。

    int() と float() の例外(OverflowError、TypeError と英語の ValueError)が
    そのまま出ると、読む人に何が悪いのか分からず、開始の前のチェックも読み込めない
    条件として扱えない。
    """
    with pytest.raises(ValueError) as caught:
        validate_source(json.loads(source))
    assert str(caught.value) == why


def test_numbers_too_large_for_a_float_are_refused_with_a_message():
    """判定する範囲と切り出し範囲に、float で表せない大きさの整数を書いても、
    文を付けた ValueError で拒否すること(JSON はどんな桁の整数も書ける)。
    OverflowError のまま呼び出し元に渡されると、読み込めない条件として扱えない。"""
    from padcue.conditions import _validate_regions, validate_crop
    huge = 10 ** 400
    with pytest.raises(ValueError, match="region の x と y"):
        _validate_regions([[huge, 0, 1, 1]])
    with pytest.raises(ValueError, match="region の w と h"):
        _validate_regions([[0, 0, huge, 1]])
    with pytest.raises(ValueError, match="切り出し範囲の x と y"):
        validate_crop([huge, 0, 0.5, 0.5])
    for bad in (["0", 0, 0.5, 0.5], [False, 0, 0.5, 0.5], [0, 0, 0.5]):
        with pytest.raises(ValueError) as caught:
            validate_crop(bad)
        assert str(caught.value) == ("切り出し範囲は x、y、w、h の 4 つの数値である"
                                     f"必要があります: {json.dumps(bad)}")


def test_regions_and_the_crop_are_checked_by_one_rule():
    """判定する範囲と切り出し範囲は、同じ決まりで確かめること。

    どちらも映像に対する [x, y, w, h] の割合である。小数の誤差で和が 1 をわずかに
    超える値(0.5 + 0.5000000000000002)を片方だけが拒否すると、同じ値が範囲には
    書けて切り出しには書けない。拒否の文も、主語のほかは同じにする。
    """
    from padcue.conditions import _validate_regions, validate_crop
    edge = [0.5, 0.0, 0.5000000000000002, 1.0]
    assert _validate_regions([edge]) == [tuple(edge)]
    assert validate_crop(edge) == tuple(edge)
    for bad in ([0, 0, 0.5], [1.0, 0, 0.5, 0.5], [0, 0, 0, 0.5], [0.8, 0, 0.5, 1]):
        with pytest.raises(ValueError) as region:
            _validate_regions([bad])
        with pytest.raises(ValueError) as crop:
            validate_crop(bad)
        said = str(crop.value).replace("切り出し範囲", "region ", 1)
        assert said == str(region.value), (str(crop.value), str(region.value))


def test_a_large_reference_is_thinned_like_the_live_picture_before_shrinking():
    """大きな基準画像も、いまの映像と同じく間引いてから比較の大きさへ縮めること。

    インポートした条件の画像は大きさのまま置く。8K の画像をそのまま縮めると、縦の
    累積和だけで 400 MB ほどを一時に使う。いまの映像は間引いてから縮めるので、同じ
    処理にすれば判定の値も揃う。
    """
    w, h = 3840, 2160
    # 1 列おきの黒と白の縞。間引くと黒だけが残り、そのまま縮めると灰色になる
    rgb = (b"\x00\x00\x00\xff\xff\xff" * (w // 2)) * h
    frame = Frame(w, h, rgb)
    got = conditions_mod.shrunk_frame(encode_png(w, h, rgb))
    want = conditions_mod.thin_to_view(frame).shrink(COMPARE_W, COMPARE_H)
    assert (got.width, got.height) == (COMPARE_W, COMPARE_H)
    assert got.rgb == want.rgb, got.rgb[:6]


@pytest.mark.parametrize("video, why", [
    ([1], "source と crop を持つオブジェクト"),
    ({"source": {"kind": "lane"}}, "「装置に設定している映像」は使用できません"),
    ({"source": {"kind": "desktop"}, "crop": ["0", 0, 1, 1]}, "4 つの数値"),
])
def test_a_lane_video_setting_is_read_in_one_place(video, why):
    """装置の映像の設定の誤りは、条件の判定でも切り出しでも、文を付けた
    ValueError になること。経路ごとに読むと、ある経路は拒否し、別の経路は
    AttributeError で失敗する。"""
    from padcue.conditions import lane_crop, resolve_lane_source, validate_video
    for read in (validate_video, lambda v: resolve_lane_source({"kind": "lane"}, v),
                 lane_crop):
        with pytest.raises(ValueError, match=why):
            read(video)


def test_lane_source_resolves_to_the_lane_video():
    """「装置に設定している映像」が、そのレーンの映像へ解決されること。"""
    from padcue.conditions import lane_crop, resolve_lane_source
    video = {"source": {"kind": "desktop"}, "crop": [0.0, 0.0, 0.5, 1.0]}
    assert resolve_lane_source({"kind": "lane"}, video) == {"kind": "desktop"}
    assert lane_crop(video) == (0.0, 0.0, 0.5, 1.0)
    # 映像を持たないレーンでは解決できない(呼び出し元は判定しない)
    assert resolve_lane_source({"kind": "lane"}, None) is None
    assert lane_crop(None) is None
    # 映像ソースを指定した条件は、レーンに依らずそのまま使う
    named = {"kind": "dshow", "device": "OBS Virtual Camera"}
    assert resolve_lane_source(named, video) == named


def test_lane_source_cannot_be_made_without_resolving():
    """解決せずに映像ソースを作ろうとしたら、理由つきで拒否すること。"""
    with pytest.raises(ValueError, match="この装置に映像が設定されていません"):
        make_source({"kind": "lane"})


@pytest.mark.parametrize("w, h", [(640, 360), (641, 359), (640, 480), (1000, 700)])
def test_the_fast_shrink_gives_the_same_values_as_the_plain_one(w, h):
    """速く縮める経路は、純 Python のボックス平均と同じ値になること。

    縮め方が環境(画像処理のライブラリの有無)で違うと、同じ映像、基準画像、
    しきい値で一致度が変わり、調整した条件の一致と不一致が入れ替わる。縮小比が
    整数でないときに違いが出やすい。
    """
    rnd = random.Random(w * h)
    # 細い縞は、境界の重み付けの違いが差の値に強く出る
    rows = [bytes([255 if y % 2 else 0] * 3) * w for y in range(h)]
    data = bytearray(b"".join(rows))
    for i in range(0, len(data), 97):
        data[i] = rnd.randrange(256)
    f = Frame(w, h, bytes(data))
    assert f.shrink(COMPARE_W, COMPARE_H).rgb == f.resize(COMPARE_W, COMPARE_H).rgb
