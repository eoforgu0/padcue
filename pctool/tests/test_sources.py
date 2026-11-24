"""映像ソース(sources.py)。FileSource / DShowSource / list_dshow_video_devices。

DShowSource は実デバイスを使わない。shutil.which の代わりに使う
`_find_ffmpeg` と ffmpeg の起動(subprocess.Popen と subprocess.run)を
monkeypatch で差し替えて確認する。本物の ffmpeg も本物のデバイス(OBS Virtual
Camera 等)もここでは呼ばない。
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata

import pytest

from padcue import sources
from padcue.frames import decode_png, encode_png
from padcue.sources import (
    DesktopSource,
    DShowSource,
    FileSource,
    WindowSource,
    list_dshow_video_devices,
    list_windows,
)
from tests.helpers import wait_until

# ---- FileSource ----


def test_file_source_present(tmp_path):
    png = encode_png(2, 2, bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255]))
    p = tmp_path / "shot.png"
    p.write_bytes(png)
    src = FileSource(p)
    f = src.grab()
    assert f is not None
    assert (f.width, f.height) == (2, 2)
    assert src.last_error == ""


def test_file_source_grab_png_keeps_color(tmp_path):
    """grab_png は、取った 1 枚を色を保ったまま PNG で返すこと。

    装置の映像の表示と、スクリプトに渡す画像に使う。復号し直して、先頭の画素が
    赤のまま(色が残っていること)を確かめる。
    """
    png = encode_png(2, 2, bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9]))
    p = tmp_path / "shot.png"
    p.write_bytes(png)
    src = FileSource(p)
    raw = src.grab_png()
    assert raw is not None and src.last_error == ""
    w, h, rgb = decode_png(raw)
    assert (w, h) == (2, 2)
    # 先頭の画素は赤(255,0,0)のまま
    assert (rgb[0], rgb[1], rgb[2]) == (255, 0, 0)


def test_file_source_grab_png_rejects_corrupt(tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"not a png")
    src = FileSource(p)
    assert src.grab_png() is None
    assert src.last_error != ""


def test_file_source_missing(tmp_path):
    src = FileSource(tmp_path / "nope.png")
    f = src.grab()
    assert f is None
    assert src.last_error != ""


def test_file_source_corrupt(tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"not a png")
    src = FileSource(p)
    f = src.grab()
    assert f is None
    assert src.last_error != ""


def test_file_source_close(tmp_path):
    FileSource(tmp_path / "x.png").close()      # 例外にならない


def test_a_file_source_decodes_the_file_once_until_it_changes(tmp_path, monkeypatch):
    """画像ファイルの映像ソースは、ファイルが変わるまで、復号した 1 枚を使い回すこと。

    条件だけの選択待ちは 0.1 秒ごとに判定し直す。そのたびに同じ PNG を読んで
    復号すると、cv2 のない PC では 1 回に秒の単位がかかり、ほかのレーンの判定と
    操作画面のサーバーも遅くなる。ファイルを書き替えたら(更新時刻か大きさが変わる)
    読み直す。
    """
    p = tmp_path / "shot.png"
    p.write_bytes(encode_png(2, 2, _RGB_2X2))
    decoded = []
    real = decode_png

    def counting(raw):
        decoded.append(1)
        return real(raw)

    monkeypatch.setattr(sources, "decode_png", counting, raising=False)
    monkeypatch.setattr(sources, "frame_from_png",
                        lambda raw: sources.Frame(*counting(raw)))
    src = FileSource(p)
    for _ in range(3):
        assert src.grab().rgb == _RGB_2X2
    assert src.grab_png() == p.read_bytes()
    assert len(decoded) == 1, decoded
    st = p.stat()
    p.write_bytes(encode_png(1, 1, b"abc"))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    assert src.grab().rgb == b"abc"
    assert len(decoded) == 2, decoded


class _CountingReads:
    """開いたファイルから読んだバイト数を数える(Path.open の差し替え)。"""

    def __init__(self, monkeypatch):
        self.read = 0
        real = sources.Path.open

        def opening(path, *a, **kw):
            f = real(path, *a, **kw)
            outer = self

            class Wrapped:
                def read(self, size=-1):
                    got = f.read(size)
                    outer.read += len(got)
                    return got

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    f.close()

            return Wrapped()

        def whole(path):
            pytest.fail(f"{path.name} を全部読んだ")

        monkeypatch.setattr(sources.Path, "open", opening)
        monkeypatch.setattr(sources.Path, "read_bytes", whole)


@pytest.mark.parametrize("head, why", [
    # IHDR の幅と高さが上限(8K の画素数)を超える
    (b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
     + bytes.fromhex("000186a0000186a0") + b"\x08\x02\x00\x00\x00" + b"\x00" * 4,
     "画像が大きすぎます(100000x100000)。7680x4320 の画素数まで受け付けます。"),
    # PNG でないファイル(動画など)
    (b"RIFF\x00\x00\x00\x00AVI LIST", sources.NOT_PNG),
])
def test_a_file_source_refuses_without_reading_the_whole_file(tmp_path, monkeypatch,
                                                              head, why):
    """大きすぎる画像と PNG でないファイルは、頭だけを読んで拒否すること。

    誤って動画などの大きなファイルを指すと、判定のたびに全部を読み込む。理由は
    分けて示す。大きすぎる画像に「PNG の画像として読み込めません」と表示すると、
    PNG で保存し直しても直らない。
    """
    p = tmp_path / "big.png"
    p.write_bytes(head + b"\x00" * 1_000_000)
    reads = _CountingReads(monkeypatch)
    src = FileSource(p)
    assert src.grab() is None and src.last_error == why
    assert src.grab_png() is None and src.last_error == why
    assert reads.read <= 2 * 64, reads.read


# ---- DShowSource ----


class FakeFfmpeg:
    """起動したままの ffmpeg の代わり。PPM を流すメソッド(send)と、終わらせる
    メソッド(end)を持つ。

    標準出力は本物のパイプにする。流れを読むスレッドは、次の 1 枚が来るまで
    読み取りで待つので、その待ち方も本物と同じにしたい。
    """

    def __init__(self, cmd, err=b""):
        """err は標準エラーに最初からある中身。None なら、後から say で書く。"""
        self.cmd = cmd
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self._w = os.fdopen(w, "wb", buffering=0)
        self._ew = None
        if err is None:
            r, w = os.pipe()
            self.stderr = os.fdopen(r, "rb")
            self._ew = os.fdopen(w, "wb", buffering=0)
        else:
            self.stderr = io.BytesIO(err)
        self.returncode = None

    def send(self, width, height, rgb):
        self._w.write(b"P6\n%d %d\n255\n" % (width, height) + rgb)

    def say(self, data):
        self._ew.write(data)

    def end(self):
        for w in (self._w, self._ew):
            if w is not None and not w.closed:
                w.close()

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = 1
        self.end()

    def wait(self, timeout=None):
        return self.returncode


RED_2X1 = bytes([255, 0, 0, 0, 0, 255])
GREEN_2X1 = bytes([0, 255, 0, 0, 255, 0])


@pytest.fixture
def ffmpeg(monkeypatch):
    """subprocess.Popen を FakeFfmpeg に差し替え、起動したものを並びで返す。"""
    made: list[FakeFfmpeg] = []
    err = {"bytes": b"", "raise": None}

    def popen(cmd, **_kw):
        if err["raise"] is not None:
            raise err["raise"]
        p = FakeFfmpeg(cmd, err["bytes"])
        made.append(p)
        return p

    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(sources.subprocess, "Popen", popen)
    # 機器ごとに覚えた色の行列は、テストごとに空から始める
    monkeypatch.setattr(sources, "_matrices", {})
    yield made, err
    sources.close_streams()


def test_dshow_streams_the_device_at_its_own_size(ffmpeg):
    """機器を 1 回だけ開き、縮めずに PPM で受け取り続けること。"""
    made, _ = ffmpeg
    src = DShowSource("OBS Virtual Camera", timeout=2.0)
    got = []
    t = threading.Thread(target=lambda: got.append(src.grab()))
    t.start()
    assert wait_until(lambda: made, 5), "ffmpeg を起動していない"
    made[0].send(2, 1, RED_2X1)
    t.join(5)
    f = got[0]
    assert f is not None and (f.width, f.height, f.rgb) == (2, 1, RED_2X1)
    cmd = made[0].cmd
    assert "video=OBS Virtual Camera" in cmd and "ppm" in cmd
    # 大きさを変える指定(scale の幅と高さ、-s)を付けない。判定の精度を下げない。
    # scale は色の行列を指定するためだけに使う
    vf = cmd[cmd.index("-vf") + 1]
    scale = re.compile(r"scale=in_color_matrix=\w+")
    assert all(not f.startswith("scale") or scale.fullmatch(f)
               for f in vf.split(",")), vf
    assert "-s" not in cmd


def test_dshow_grab_returns_a_frame_newer_than_the_last_one(ffmpeg):
    """前に渡した 1 枚は 2 度渡さず、受け取っている最新の 1 枚を渡すこと。"""
    made, _ = ffmpeg
    src = DShowSource("OBS Virtual Camera", timeout=0.3)
    stream = sources._dshow_stream("ffmpeg", "OBS Virtual Camera")
    made[0].send(2, 1, RED_2X1)
    # 読み取りのスレッドが受け取るまで待つ。grab の短い時間切れを、PC の速さに
    # 左右させない
    assert wait_until(lambda: stream._seq >= 1, 5)
    assert src.grab().rgb == RED_2X1
    # 新しい 1 枚を受け取らなければ、同じ 1 枚を渡さずに時間切れにする。流れが
    # 動いたまま受け取れないのは、ほかのアプリが使っているときではなく(そのときは
    # ffmpeg が終わる)、機器に映像が来ていないときである
    assert src.grab() is None
    assert src.last_error == ("映像機器「OBS Virtual Camera」から映像を受け取れません。"
                              "機器に映像が入力されているか確認してください。")
    # 2 枚受け取っていれば、新しいほうを渡す
    made[0].send(2, 1, RED_2X1)
    made[0].send(2, 1, GREEN_2X1)
    assert wait_until(lambda: stream._seq >= 3, 5), "2 枚目を受け取っていない"
    assert src.grab().rgb == GREEN_2X1


def test_dshow_sources_of_one_device_share_one_ffmpeg(ffmpeg):
    """同じ機器を見る映像ソースが 2 つあっても、ffmpeg は 1 本であること。

    機器によっては 2 つの ffmpeg から同時には開けない。
    """
    made, _ = ffmpeg
    a = DShowSource("OBS Virtual Camera", timeout=1.0)
    b = DShowSource("OBS Virtual Camera", timeout=1.0)
    sources._dshow_stream("ffmpeg", "OBS Virtual Camera")
    made[0].send(2, 1, RED_2X1)
    assert a.grab() is not None and b.grab() is not None
    assert len(made) == 1


def test_dshow_grab_png_keeps_the_pixels(ffmpeg):
    made, _ = ffmpeg
    src = DShowSource("OBS Virtual Camera", timeout=1.0)
    sources._dshow_stream("ffmpeg", "OBS Virtual Camera")
    made[0].send(2, 1, RED_2X1)
    w, h, rgb = decode_png(src.grab_png())
    assert (w, h, rgb) == (2, 1, RED_2X1)


def test_dshow_grab_no_ffmpeg(monkeypatch):
    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: None)
    src = DShowSource("OBS Virtual Camera")
    f = src.grab()
    assert f is None
    assert "ffmpeg" in src.last_error


def test_dshow_a_missing_device_is_named_and_not_restarted_at_once(ffmpeg,
                                                                   monkeypatch):
    """見つからない機器は理由を名前で示し、すぐには ffmpeg を起動し直さないこと。

    判定は間を空けずに続くので、待たずに起動し直すと、見つからない機器のために
    ffmpeg を起動し続ける。
    """
    made, err = ffmpeg
    err["bytes"] = b"[dshow] Could not find video device with name [X]\n"
    src = DShowSource("ない装置", timeout=2.0)
    sources._dshow_stream("ffmpeg", "ない装置")
    made[0].end()
    assert src.grab() is None
    assert src.last_error == "映像機器「ない装置」が見つかりません"
    assert src.grab() is None
    assert len(made) == 1
    monkeypatch.setattr(sources._DShowStream, "RETRY_S", 0.0)
    src.grab()
    assert len(made) == 2


def test_dshow_a_broken_stream_ends_without_raising(ffmpeg):
    """PPM でない出力が来ても、例外を投げずに理由を返すこと。"""
    made, _ = ffmpeg
    src = DShowSource("OBS Virtual Camera", timeout=2.0)
    sources._dshow_stream("ffmpeg", "OBS Virtual Camera")
    made[0]._w.write(b"not a ppm at all ")
    made[0].end()
    assert src.grab() is None
    assert "から映像を取得できません" in src.last_error


def test_dshow_grab_ffmpeg_start_failure(ffmpeg):
    _, err = ffmpeg
    err["raise"] = OSError("no such file")
    src = DShowSource("OBS Virtual Camera")
    assert src.grab() is None
    assert "ffmpeg を起動できません" in src.last_error


def test_dshow_grab_does_not_raise_when_ffmpeg_cannot_take_the_name(ffmpeg):
    """機器の名前に NUL があって ffmpeg を起動できなくても、例外を投げないこと。"""
    _, err = ffmpeg
    err["raise"] = ValueError("embedded null character")
    src = DShowSource("OBS\x00Virtual Camera")
    assert src.grab() is None
    assert "から映像を取得できません" in src.last_error


def test_dshow_an_unused_stream_closes_itself(ffmpeg, monkeypatch):
    """使われなくなった流れは自分で閉じ、次に取るときはすぐ起動し直すこと。"""
    made, _ = ffmpeg
    monkeypatch.setattr(sources._DShowStream, "IDLE_S", 0.1)
    src = DShowSource("OBS Virtual Camera", timeout=2.0)
    sources._dshow_stream("ffmpeg", "OBS Virtual Camera")
    made[0].send(2, 1, RED_2X1)
    assert src.grab() is not None
    end = time.monotonic() + 5
    while made[0].returncode is None and time.monotonic() < end:
        time.sleep(0.05)
    assert made[0].returncode is not None, "使われなくなった ffmpeg が残った"
    got = []
    t = threading.Thread(target=lambda: got.append(src.grab()))
    t.start()
    assert wait_until(lambda: len(made) >= 2, 5), "ffmpeg を起動し直していない"
    made[1].send(2, 1, GREEN_2X1)
    t.join(5)
    assert got[0] is not None and got[0].rgb == GREEN_2X1


def test_dshow_ppm_size_reads_the_header_and_stops_before_the_pixels():
    f = io.BytesIO(b"P6\n2 1\n255\n" + RED_2X1)
    assert sources._ppm_size(f) == (2, 1)
    assert f.read() == RED_2X1
    assert sources._ppm_size(io.BytesIO(b"")) is None


def test_dshow_close():
    DShowSource("OBS Virtual Camera").close()      # 例外にならない


def test_dshow_asks_ffmpeg_for_8_bit_rgb(ffmpeg):
    """ffmpeg に画素の形(8 ビットの RGB)を指定すること。

    指定しないと、10 ビットの映像を送る機器では、ffmpeg は 16 ビットの PPM
    (最大値 65535)を出す。1 枚目から読めずに流れを閉じ、原因と違う「ほかのアプリで
    使用していないか」の理由になる。
    """
    made, _ = ffmpeg
    sources._dshow_stream("ffmpeg", "OBS Virtual Camera")
    cmd = made[0].cmd
    assert "-pix_fmt" in cmd and cmd[cmd.index("-pix_fmt") + 1] == "rgb24", cmd


def test_dshow_a_stream_closed_while_waiting_is_waited_only_for_the_time_left(ffmpeg):
    """使われなくなって閉じる所と重なり、起動し直したときは、最初に決めた時間の
    残りだけ待つこと。

    決めた時間をまるごともう一度待つと、1 回の取り込みが決めた時間の 2 倍かかり、
    待っている判定の側は時間切れの理由を受け取る。
    """
    made, _ = ffmpeg
    src = DShowSource("OBS Virtual Camera", timeout=2.0)
    first = sources._dshow_stream("ffmpeg", "OBS Virtual Camera")

    def retire():
        # 1.2 秒たったところで、使われなくなったものとして閉じる(_watch と同じ手順)
        time.sleep(1.2)
        with first._cv:
            first.retired = True
        sources._forget_stream(first)
        first.close()

    t = threading.Thread(target=retire)
    t.start()
    began = time.monotonic()
    assert src.grab() is None
    took = time.monotonic() - began
    t.join(5)
    assert len(made) == 2, "起動し直していない"
    # 残りだけ待てば 2.0 秒、まるごともう一度待てば 3.2 秒かかる
    assert took < 2.6, took


def test_dshow_opens_the_device_by_the_listed_spelling(ffmpeg, monkeypatch):
    """条件に書いた機器の名前が、機器の一覧と大文字と小文字だけ違うときは、一覧の
    綴りで開くこと。

    開始の前のチェックは大文字と小文字を区別せずに「ある」と答える。ffmpeg は区別
    するので、書いたまま渡すと、取り込みは毎回「見つかりません」になる。手で書いた
    条件や、別の PC から受け取った条件で起きる。
    """
    made, _ = ffmpeg
    monkeypatch.setattr(sources, "list_dshow_video_devices",
                        lambda: ["OBS Virtual Camera"])
    assert sources.source_present({"kind": "dshow", "device": "obs virtual camera"},
                                  wait=2.0)
    src = DShowSource("obs virtual camera", timeout=2.0)
    got = []
    t = threading.Thread(target=lambda: got.append(src.grab()))
    t.start()
    assert wait_until(lambda: made, 5), "ffmpeg を起動していない"
    made[0].send(2, 1, RED_2X1)
    t.join(5)
    assert "video=OBS Virtual Camera" in made[0].cmd, made[0].cmd
    assert got[0] is not None


# ffmpeg が入力の映像を説明する行(-loglevel info)。色の情報の印は画素の形の後ろの
# 括弧に入り、範囲(tv か pc)、行列(と、違えば /原色/伝達特性)、走査の順に並ぶ
_OBS_LINE = ("  Stream #0:0: Video: rawvideo (NV12 / 0x3231564E), nv12, 2560x1440, "
             "60 fps, 60 tbr, 10000k tbn, start 695928.500000")


@pytest.mark.parametrize("line, want", [
    (_OBS_LINE, ("nv12", "", 1440)),
    ("  Stream #0:0: Video: rawvideo (YUY2 / 0x32595559), yuyv422(tv, bt709, "
     "progressive), 1920x1080, 60 fps, 60 tbr, 10000k tbn", ("yuyv422", "bt709", 1080)),
    ("  Stream #0:0: Video: rawvideo (YUY2 / 0x32595559), "
     "yuyv422(tv, bt470bg/bt709/bt709), 720x480 [SAR 8:9 DAR 4:3], 29.97 fps",
     ("yuyv422", "bt470bg", 480)),
    ("  Stream #0:0: Video: mjpeg (Baseline) (MJPG / 0x47504A4D), "
     "yuvj422p(pc, bt470bg/unknown/unknown), 1280x720, 30 fps",
     ("yuvj422p", "bt470bg", 720)),
    # 行列がわからず、原色と伝達特性だけが付いている
    ("  Stream #0:0: Video: rawvideo (YUY2 / 0x32595559), "
     "yuyv422(tv, unknown/bt709/bt709), 1920x1080", ("yuyv422", "", 1080)),
    ("  Stream #0:0: Video: rawvideo (YUY2 / 0x32595559), yuyv422(tv), 640x360",
     ("yuyv422", "", 360)),
    ("  Stream #0:0: Video: rawvideo (RGB[24] / 0x18424752), bgr24, 1280x720",
     ("bgr24", "", 720)),
    ("  Stream #0:0 -> #0:0 (rawvideo (native) -> ppm (native))", None),
    ("  Duration: N/A, start: 695928.500000, bitrate: N/A", None),
])
def test_dshow_reads_the_color_marker_and_height_from_ffmpeg(line, want):
    """ffmpeg が入力の映像を説明する行から、画素の形、色の行列の印、高さを読むこと。"""
    assert sources._describe_video(line) == want


@pytest.mark.parametrize("pix, mark, height, want", [
    ("nv12", "", 1440, "bt709"),        # 印がなく HD 以上なら BT.709
    ("nv12", "", 720, "bt709"),
    ("yuyv422", "", 480, "bt601"),      # 印がなく 720 より低ければ BT.601
    ("yuyv422", "bt709", 480, "bt709"),  # 印があればそれに従う
    ("yuyv422", "bt470bg", 1080, "auto"),
    ("bgr24", "", 480, None),           # RGB で来る映像は変換しない
])
def test_dshow_chooses_the_color_matrix(pix, mark, height, want):
    """YUV から RGB へ変換する行列は、印があればそれに従い、なければ高さで決めること。

    再生の道具の一般的な扱いに合わせる。印のない HD の映像(OBS の仮想カメラなど、
    BT.709 で作られたもの)を BT.601 で変換すると、彩度の高い色の R、G、B が別々にずれ、
    取り方の違う映像ソースどうしで同じ画面の一致度が下がる。
    """
    assert sources._matrix_for(pix, mark, height) == want


def test_dshow_restarts_with_bt601_for_a_low_unmarked_picture(ffmpeg):
    """ffmpeg は BT.709 で起動し、入力が印のない 720 より低い映像なら、BT.601 で
    起動し直すこと。"""
    made, err = ffmpeg
    err["bytes"] = (b"Input #0, dshow, from 'video=USB Video':\n"
                    b"  Duration: N/A, start: 1.000000, bitrate: N/A\n"
                    b"  Stream #0:0: Video: rawvideo (YUY2 / 0x32595559), yuyv422, "
                    b"720x480, 30 fps, 30 tbr, 10000k tbn\n")
    src = DShowSource("USB Video", timeout=3.0)
    got = []
    t = threading.Thread(target=lambda: got.append(src.grab()))
    t.start()
    assert wait_until(lambda: len(made) >= 2, 5), "起動し直していない"
    assert "scale=in_color_matrix=bt709" in made[0].cmd[made[0].cmd.index("-vf") + 1]
    assert "scale=in_color_matrix=bt601" in made[1].cmd[made[1].cmd.index("-vf") + 1]
    made[1].send(2, 1, GREEN_2X1)
    t.join(5)
    assert got[0] is not None and got[0].rgb == GREEN_2X1


def test_dshow_hands_over_no_frame_before_the_matrix_is_checked(ffmpeg):
    """入力の説明を読んで行列を確かめるまでは、受け取った 1 枚を渡さないこと。

    ffmpeg は説明を標準エラーに、映像を標準出力に書く。読むスレッドが別なので、
    説明より先に 1 枚目を受け取ることがある。その 1 枚は、起動し直す前の行列で
    変換したものである。
    """
    made, err = ffmpeg
    err["bytes"] = None                 # 標準エラーは後から書く
    low = (b"Input #0, dshow, from 'video=USB Video':\n"
           b"  Stream #0:0: Video: rawvideo (YUY2 / 0x32595559), yuyv422, "
           b"720x480, 30 fps, 30 tbr, 10000k tbn\n")
    src = DShowSource("USB Video", timeout=3.0)
    got = []
    t = threading.Thread(target=lambda: got.append(src.grab()))
    t.start()
    assert wait_until(lambda: made, 5), "ffmpeg を起動していない"
    made[0].send(2, 1, RED_2X1)         # 説明より先に受け取った 1 枚
    time.sleep(0.3)
    made[0].say(low)
    assert wait_until(lambda: len(made) >= 2, 5), "起動し直していない"
    made[1].say(low)
    made[1].send(2, 1, GREEN_2X1)
    t.join(5)
    assert got[0] is not None and got[0].rgb == GREEN_2X1


def test_dshow_waits_for_the_description_only_once(ffmpeg):
    """入力の説明が出ないまま流れが続いても、説明を待つのは 1 枚目の前の 1 回だけで
    あること。1 枚ごとに待つと、毎秒 1 枚ほどしか渡せない。"""
    made, err = ffmpeg
    err["bytes"] = None                 # 標準エラーは開いたまま、何も書かない
    src = DShowSource("OBS Virtual Camera", timeout=3.0)
    sources._dshow_stream("ffmpeg", "OBS Virtual Camera")
    made[0].send(2, 1, RED_2X1)
    assert src.grab().rgb == RED_2X1
    made[0].send(2, 1, GREEN_2X1)
    began = time.monotonic()
    got = src.grab()
    took = time.monotonic() - began
    assert got is not None and got.rgb == GREEN_2X1
    assert took < 0.5, took


def test_dshow_keeps_bt709_for_an_unmarked_hd_picture(ffmpeg):
    """印のない HD の映像(OBS の仮想カメラ)では、起動し直さないこと。"""
    made, err = ffmpeg
    err["bytes"] = (b"Input #0, dshow, from 'video=OBS Virtual Camera':\n"
                    + _OBS_LINE.encode() + b"\n")
    src = DShowSource("OBS Virtual Camera", timeout=3.0)
    got = []
    t = threading.Thread(target=lambda: got.append(src.grab()))
    t.start()
    assert wait_until(lambda: made, 5), "ffmpeg を起動していない"
    made[0].send(2, 1, RED_2X1)
    t.join(5)
    assert got[0] is not None and got[0].rgb == RED_2X1
    assert len(made) == 1


# ---- list_dshow_video_devices ----


# タグ形式の ffmpeg の出力(新しいバージョンの ffmpeg が出す形)。各デバイスを
# 1 行で `"名前" (種別)` と出し、音声専用は (audio)、映像は (video) を含む。
# 音声専用デバイス名にも「(...)」が含まれることがあるので、種別タグの判定は
# 行末の (...) だけを見る必要がある(名前の中の括弧を種別と混同しないこと)。
_FAKE_STDERR_TAGGED = """[dshow @ 0001] "Example Capture" (audio, video)
[dshow @ 0001]   Alternative name "@device_pnp_\\\\?\\example#avstream"
[dshow @ 0001] "OBS Virtual Camera" (video)
[dshow @ 0001]   Alternative name "@device_sw_{860BB310}"
[dshow @ 0001] "Line (2- Example Audio)" (audio)
[dshow @ 0001]   Alternative name "@device_cm_{33D9A762}"
"""

# 見出し形式の ffmpeg 出力(バージョンによってこちらを出す)。
_FAKE_STDERR_SECTIONED = """[dshow @ 0001] DirectShow video devices
[dshow @ 0001]  "OBS Virtual Camera"
[dshow @ 0001]     Alternative name "@device_sw_ordinal"
[dshow @ 0001] DirectShow audio devices
[dshow @ 0001]  "Microphone (Example Audio)"
"""


def _run_returning(stderr_text):
    def fake_run(cmd, capture_output, timeout):
        return subprocess.CompletedProcess(
            cmd, 1, stdout=b"", stderr=stderr_text.encode("utf-8"))
    return fake_run


def test_list_dshow_video_devices_parses_tagged_format(monkeypatch):
    """1 行に (video) か (audio) のタグが付く形式から、映像だけを抜き出すこと。

    Example Capture の (audio, video) は映像を含むので映像に数え、
    "Line (2- Example Audio)" (audio) は音声専用なので除く。名前に
    含まれる「(2-」の括弧を種別と混同して、音声を映像に数えないことを確かめる。
    """
    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(sources.subprocess, "run",
                        _run_returning(_FAKE_STDERR_TAGGED))
    names = list_dshow_video_devices()
    assert names == ["Example Capture", "OBS Virtual Camera"]


def test_list_dshow_video_devices_parses_sectioned_format(monkeypatch):
    """見出し形式の ffmpeg 出力でも映像機器だけ抜き出せること。"""
    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(sources.subprocess, "run",
                        _run_returning(_FAKE_STDERR_SECTIONED))
    assert list_dshow_video_devices() == ["OBS Virtual Camera"]


def test_list_dshow_video_devices_no_ffmpeg(monkeypatch):
    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: None)
    assert list_dshow_video_devices() == []


def test_list_dshow_video_devices_timeout(monkeypatch):
    """時間切れは、機器がないこと(空の一覧)と分けて None を返すこと。"""
    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: "ffmpeg")

    def fake_run(cmd, capture_output, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(sources.subprocess, "run", fake_run)
    assert list_dshow_video_devices() is None


# ---- DesktopSource / WindowSource(Windows GDI。実際の画面は取り込まない) ----
#
# GDI 呼び出し(_capture_rgb / _load_gdi / _grab_*_rgb / _enum_toplevel_windows)を
# monkeypatch で差し替え、既知の RGB を流す。本物の画面とウィンドウは取り込まない。

# 2x2 の既知の RGB: 赤、緑、青、白
_RGB_2X2 = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])


def test_desktop_grab_png_keeps_color(monkeypatch):
    """grab_png はカラーの PNG を返すこと(輝度化しない)。"""
    src = DesktopSource()
    monkeypatch.setattr(src, "_capture_rgb", lambda: (2, 2, _RGB_2X2))
    raw = src.grab_png()
    assert raw is not None and src.last_error == ""
    w, h, rgb = decode_png(raw)
    assert (w, h) == (2, 2)
    assert (rgb[0], rgb[1], rgb[2]) == (255, 0, 0)   # 先頭の赤が保たれる


def test_desktop_grab_keeps_color(monkeypatch):
    """grab は色のまま Frame を返すこと。"""
    src = DesktopSource()
    monkeypatch.setattr(src, "_capture_rgb", lambda: (2, 2, _RGB_2X2))
    f = src.grab()
    assert f is not None and (f.width, f.height) == (2, 2)
    assert f.rgb == _RGB_2X2


def test_desktop_capture_failure_returns_none(monkeypatch):
    """取得に失敗したときは、None と last_error で表し、例外を投げないこと。"""
    monkeypatch.setattr(sources, "_load_gdi", lambda: (object(), object()))

    def boom(user32, gdi32, monitor):
        raise sources._CaptureError("画面の取り込みに失敗しました(BitBlt)")

    monkeypatch.setattr(sources, "_grab_screen_rgb", boom)
    src = DesktopSource()
    assert src.grab() is None
    assert src.grab_png() is None
    assert "BitBlt" in src.last_error


def test_screen_and_window_capture_do_not_raise_on_unexpected_errors(monkeypatch):
    """DLL の読み込みや ctypes の呼び出しが想定外の例外を出しても、None と
    理由を返すこと。

    判定は、grab が例外を投げないことを前提に選択肢を順に見る。例外が抜けると、
    そのレーンの残りの選択肢が判定されない。
    """
    def no_dll():
        raise OSError("user32 を読み込めません")

    monkeypatch.setattr(sources, "_load_gdi", no_dll)
    desk = DesktopSource()
    assert desk.grab() is None and desk.grab_png() is None
    assert desk.last_error == "画面を取得できません"
    win = WindowSource("メモ帳")
    assert win.grab() is None and win.grab_png() is None
    assert win.last_error == "ウィンドウの映像を取得できません"
    # 取れた画素の形が合わないときも同じ
    monkeypatch.setattr(desk, "_capture_rgb", lambda: (2, 2, None))
    assert desk.grab() is None and desk.grab_png() is None
    assert desk.last_error == "取得した画像を読み込めません"


def test_window_grab_png_and_grab_keep_color(monkeypatch):
    src = WindowSource("ウィンドウA")
    monkeypatch.setattr(src, "_capture_rgb", lambda: (2, 2, _RGB_2X2))
    raw = src.grab_png()
    assert raw is not None
    _w, _h, rgb = decode_png(raw)
    assert (rgb[0], rgb[1], rgb[2]) == (255, 0, 0)
    f = src.grab()
    assert f is not None and f.rgb == _RGB_2X2


def test_window_not_found(monkeypatch):
    """タイトルが一致しなければ、None と「見つかりません」(タイトル入り)を返すこと。"""
    monkeypatch.setattr(sources, "_load_gdi", lambda: (object(), object()))
    monkeypatch.setattr(sources, "_enum_toplevel_windows",
                        lambda: [(1, True, "メモ帳"), (2, False, "隠れウィンドウ")])
    src = WindowSource("存在しないウィンドウ")
    assert src.grab() is None
    assert "存在しないウィンドウ" in src.last_error
    assert "見つかりません" in src.last_error


def test_window_found_but_capture_fails(monkeypatch):
    """部分一致で見つかっても取得に失敗すれば、None と last_error を返すこと。"""
    monkeypatch.setattr(sources, "_load_gdi", lambda: (object(), object()))
    monkeypatch.setattr(sources, "_enum_toplevel_windows",
                        lambda: [(0, False, "無題"), (55, True, "無題 - メモ帳")])

    def boom(user32, gdi32, hwnd):
        assert hwnd == 55       # 可視で部分一致した最初のもの
        raise sources._CaptureError("ウィンドウが最小化されています")

    monkeypatch.setattr(sources, "_grab_window_rgb", boom)
    src = WindowSource("メモ帳")
    assert src.grab_png() is None
    assert "最小化" in src.last_error


def test_window_find_takes_the_only_visible_partial_match(monkeypatch):
    monkeypatch.setattr(sources, "_enum_toplevel_windows",
                        lambda: [(1, False, "メモ帳(非表示)"),
                                 (2, True, "A - メモ帳"),
                                 (3, True, "別のウィンドウ")])
    assert WindowSource("メモ帳")._find_window() == 2


def test_window_find_refuses_when_two_windows_match(monkeypatch):
    """同じ名前のウィンドウが 2 つあるときは、どれかを黙って選ばずに拒否すること。

    指定はタイトルの部分一致なので、どちらを見ているかが決まらない。黙って
    最初の 1 つを選ぶと、気づかれないまま判定が間違う。
    """
    monkeypatch.setattr(sources, "_load_gdi", lambda: (object(), object()))
    monkeypatch.setattr(sources, "_enum_toplevel_windows",
                        lambda: [(2, True, "A - メモ帳"),
                                 (3, True, "B - メモ帳")])
    src = WindowSource("メモ帳")
    with pytest.raises(sources._CaptureError, match="2 個あります"):
        src._find_window()
    assert src.grab() is None
    assert "使用しないウィンドウを閉じて" in src.last_error


def test_window_keeps_the_one_it_chose(monkeypatch):
    """一度決めたウィンドウは、閉じるか隠れるまで保つこと。タイトルが変わっても保つ。

    取るたびに探し直すと、実行の途中で同じ名前の別のウィンドウが現れたときに対象が
    入れ替わり、判定が警告なしに間違う。タイトルに表示中の名前やシーンの名前を出す
    アプリでは、タイトルが変わっただけで、同じ一部を含む別のウィンドウへ警告なしに
    切り替わるか、「見つかりません」になる。
    """
    windows = [(2, True, "A - メモ帳")]
    monkeypatch.setattr(sources, "_enum_toplevel_windows", lambda: windows)
    # ウィンドウが残っていて見えているか(IsWindow と IsWindowVisible)
    monkeypatch.setattr(sources, "_window_open",
                        lambda hwnd: any(h == hwnd and v for h, v, _t in windows),
                        raising=False)
    src = WindowSource("メモ帳")
    assert src._find_window() == 2
    # 同じ名前のウィンドウが後から増えても、決めたウィンドウを見続ける
    windows.append((3, True, "B - メモ帳"))
    assert src._find_window() == 2
    # タイトルが変わっても、閉じるまでは見続ける
    windows[0] = (2, True, "A - 別の題名")
    assert src._find_window() == 2
    # 隠れたら(なくなったら)、タイトルで探し直して残った 1 つを見る
    windows[0] = (2, False, "A - 別の題名")
    assert src._find_window() == 3


def test_presence_of_a_window_says_why_two_windows_cannot_be_used(monkeypatch):
    """当てはまるウィンドウが 2 つ以上あるときは、取り込むときと同じ文で理由を
    返すこと。

    開始の前のチェックが「ある」とだけ答えると、選択待ちに入ってから毎周期
    「2 個あります」で失敗する。開始の前に分かることを、実行中に知ることになる。
    """
    monkeypatch.setattr(sources, "list_windows", lambda: [
        {"title": "A - メモ帳", "count": 1}, {"title": "無題 - メモ帳", "count": 2},
        {"title": "別のウィンドウ", "count": 1}])
    monkeypatch.setattr(sources, "_enum_toplevel_windows", lambda: [
        (1, True, "A - メモ帳"), (2, True, "無題 - メモ帳"), (3, True, "無題 - メモ帳"),
        (4, True, "別のウィンドウ")])
    with pytest.raises(sources._CaptureError) as caught:
        WindowSource("メモ帳")._find_window()
    assert sources.source_problem({"kind": "window", "title": "メモ帳"}) == str(
        caught.value)
    assert sources.source_problem({"kind": "window", "title": "別の"}) == ""
    assert sources.source_problem({"kind": "desktop"}) == ""


# ---- list_windows / list_monitors ----


def test_count_window_titles_filters_and_counts():
    """同じ名前をまとめず、いくつあるかを数えること。"""
    windows = [(1, True, "ウィンドウA"), (2, False, "隠れウィンドウ"),
               (3, True, "  "), (4, True, "ウィンドウA"), (5, True, "別ウィンドウ")]
    assert sources._count_window_titles(windows) == [
        {"title": "ウィンドウA", "count": 2}, {"title": "別ウィンドウ", "count": 1}]


def test_list_windows_uses_enum(monkeypatch):
    monkeypatch.setattr(sources, "_enum_toplevel_windows",
                        lambda: [(1, True, "ウィンドウA"), (2, False, "ウィンドウB")])
    assert list_windows() == [{"title": "ウィンドウA", "count": 1}]


def test_list_monitors_lists_every_monitor(monkeypatch):
    """モニターを 1 枚ずつ、1 から数えた番号と大きさを付けて返すこと。"""
    monkeypatch.setattr(sources, "_load_gdi", lambda: (object(), object()))
    monkeypatch.setattr(sources, "_monitor_rects",
                        lambda _u: [(0, 0, 1920, 1080), (1920, 0, 2560, 1440)])
    assert sources.list_monitors() == [
        {"index": 1, "width": 1920, "height": 1080},
        {"index": 2, "width": 2560, "height": 1440}]


def test_list_monitors_swallows_errors(monkeypatch):
    def boom():
        raise OSError("EnumDisplayMonitors 失敗")

    monkeypatch.setattr(sources, "_load_gdi", boom)
    assert sources.list_monitors() == []


# ---- find_source_by_name ----


def test_find_source_by_name_covers_each_kind(monkeypatch):
    """手順に書いた名前から、この PC の映像ソースを見つけられること。

    手順に書くのは名前だけで、実体はこの PC の設定から探す
    (file-format.md §4-2)。見つけられるのは機器の名前、ウィンドウのタイトル、
    「デスクトップ全体」である。
    """
    monkeypatch.setattr(sources, "list_dshow_video_devices",
                        lambda: ["OBS Virtual Camera"])
    monkeypatch.setattr(sources, "list_windows",
                        lambda: [{"title": "無題 - メモ帳", "count": 1}])
    find = sources.find_source_by_name
    assert find("デスクトップ全体") == {"kind": "desktop"}
    assert find("OBS Virtual Camera") == {
        "kind": "dshow", "device": "OBS Virtual Camera"}
    assert find("無題 - メモ帳") == {
        "kind": "window", "title": "無題 - メモ帳"}
    assert find("ありえない映像") is None
    assert find("") is None


def test_a_monitor_is_found_by_its_number(monkeypatch):
    """モニター 1 枚ずつも名前で見つけられること。解像度は名前に入れない。

    画面の選択肢は「モニター 2(1280x720)」と見せるが、手順に書くのは
    「モニター 2」である。解像度を変えても見つけられる。
    """
    monkeypatch.setattr(sources, "list_monitors", lambda: [
        {"index": 1, "width": 1920, "height": 1080},
        {"index": 2, "width": 1280, "height": 720}])
    find = sources.find_source_by_name
    assert find("モニター 2") == {"kind": "desktop", "monitor": 2}
    assert find("モニター3") is None


def test_find_source_by_name_ignores_case_and_form(monkeypatch):
    """名前の照合は NFC に揃え、大文字と小文字を区別しないこと。

    手順に書いた名前とこの PC の名前は、別の人が別の入力方法で書くことがある。
    どちらかだけを揃えても、もう一方の形が違えば見つけられない。
    """
    got = {"kind": "dshow", "device": "OBS Virtual Camera"}
    monkeypatch.setattr(sources, "list_dshow_video_devices",
                        lambda: ["OBS Virtual Camera"])
    monkeypatch.setattr(sources, "list_windows", lambda: [])
    # 手順の側が大文字、PC 側が混在
    assert sources.find_source_by_name("OBS VIRTUAL CAMERA") == got
    assert sources.find_source_by_name("obs virtual camera") == got
    # 分解済みの「ガ」(か + 濁点)でも、NFC の「ガ」で登録したウィンドウに一致する
    monkeypatch.setattr(sources, "list_windows",
                        lambda: [{"title": "ガイド", "count": 1}])
    assert sources.find_source_by_name(unicodedata.normalize("NFD", "ガイド")) == {
        "kind": "window", "title": "ガイド"}


def test_find_source_by_name_reads_the_device_list_from_the_copy(monkeypatch):
    """名前を探すたびに機器を列挙せず、キャッシュの一覧を使うこと。古くなったら
    取得し直す。

    列挙は ffmpeg で、返らないときは最大 10 秒かかる。選択肢のスクリプトは
    判定のたびに名前を探すので、毎回列挙すると判定 1 回が決めた長さで終わらない。
    """
    listed = []
    have = ["OBS Virtual Camera"]

    def listing():
        listed.append(1)
        return list(have)

    monkeypatch.setattr(sources, "list_dshow_video_devices", listing)
    monkeypatch.setattr(sources, "list_windows", lambda: [])
    for _ in range(5):
        assert sources.find_source_by_name("OBS Virtual Camera") is not None
    assert len(listed) == 1, listed
    # キャッシュが新しいうちは、後から挿した機器は出ない
    have.append("USB Video")
    assert sources.find_source_by_name("USB Video") is None
    # 古くなったら取得し直し、その終わりを待って探す
    sources._device_list._next = 0.0
    assert sources.find_source_by_name("USB Video") == {
        "kind": "dshow", "device": "USB Video"}
    assert len(listed) == 2, listed


def test_a_failed_listing_keeps_the_devices_found_before(monkeypatch):
    """一覧の再取得に失敗しても、前に取れた機器を名前で見つけられること。

    ffmpeg が時間切れになるのは、機器がないときではない。空の一覧として
    キャッシュすると、キャッシュを使い続ける 30 秒の間、つながっている機器を
    見つけられず、その映像を使う選択肢とスクリプトが「見つかりません」になる。
    再取得は間を置いて行う。
    """
    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(sources, "list_windows", lambda: [])
    fail = []
    runs = []

    def fake_run(cmd, capture_output, timeout):
        runs.append(1)
        if fail:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return _run_returning(_FAKE_STDERR_SECTIONED)(cmd, capture_output, timeout)

    monkeypatch.setattr(sources.subprocess, "run", fake_run)
    # 取れた一覧はすぐに古くなる(探すたびに取得し直す)
    monkeypatch.setattr(sources._DeviceList, "TTL_S", 0.0)
    got = {"kind": "dshow", "device": "OBS Virtual Camera"}
    assert sources.find_source_by_name("OBS Virtual Camera") == got
    fail.append(1)
    assert sources.find_source_by_name("OBS Virtual Camera") == got
    assert len(runs) == 2, runs
    # 失敗の後は、キャッシュの 30 秒ではなく、短い間を置いてから取得し直す
    assert sources.dshow_names() == (["OBS Virtual Camera"], False)
    assert len(runs) == 2, runs
    left = sources._device_list._next - time.monotonic()
    assert 0 < left <= sources._DeviceList.RETRY_S, left
    # 前に取れた一覧があるので、ない機器は「ない」と判断できる
    assert sources.find_source_by_name("USB Video") is None
    assert sources.dshow_unknown() == ""


@pytest.mark.parametrize("ffmpeg_path, listing, why", [
    (None, [], "ffmpeg が必要です"),            # ffmpeg がない
    ("ffmpeg", None, "一覧を取得できません"),     # 起動できない、時間切れ
])
def test_a_device_list_never_taken_says_why_instead_of_missing(
        monkeypatch, ffmpeg_path, listing, why):
    """映像機器の一覧を確認できないときは、「見つかりません」と返さずに理由を
    返せること。

    一覧が取れない原因は ffmpeg の側にある。「見つかりません」を読んだ利用者は、
    映像機器のつなぎ方を見に行ってしまう。
    """
    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: ffmpeg_path)
    monkeypatch.setattr(sources, "list_dshow_video_devices", lambda: listing)
    monkeypatch.setattr(sources, "list_windows", lambda: [])
    assert sources.source_present({"kind": "dshow", "device": "USB Video"},
                                  wait=2.0) is False
    assert sources.find_source_by_name("USB Video", wait=2.0) is None
    assert why in sources.dshow_unknown(), sources.dshow_unknown()
    # 画面(/api/sources)は「確認している最中」でなくなったときに理由を出す。
    # 取れないまま最中と返し続けると、画面は理由の代わりに「見つかりません」を出す
    assert sources.dshow_names() == ([], False)


def test_a_device_list_taken_can_say_missing(monkeypatch):
    """一覧を取れていれば、ない機器は「ない」と判断できること(理由は空)。"""
    monkeypatch.setattr(sources, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(sources, "list_dshow_video_devices", lambda: ["OBS"])
    monkeypatch.setattr(sources, "list_windows", lambda: [])
    assert sources.find_source_by_name("USB Video", wait=2.0) is None
    assert sources.dshow_unknown() == ""


def test_list_windows_swallows_errors(monkeypatch):
    def boom():
        raise OSError("EnumWindows 失敗")

    monkeypatch.setattr(sources, "_enum_toplevel_windows", boom)
    assert list_windows() == []


# ---- GDI 後始末(失敗経路でビットマップをリークしない) ----
#
# 実 GDI は呼べないので、_grab_screen_rgb に user32/gdi32 のダミーを渡し、
# BitBlt を失敗させて例外経路を通す。DC に選択中のビットマップは削除できない
# ため、bmp を消す前に必ず SelectObject で選択を元へ戻すこと(戻さないと毎回
# リークして GDI 枯渇)を、呼び出しの順序で確認する。


class _RecUser:
    """_grab_screen_rgb が使う user32 のダミー。呼び出しを記録する。"""

    def __init__(self):
        self.log = []

    def GetSystemMetrics(self, i):
        # SM_XVIRTUALSCREEN=76 / Y=77 / CX=78 / CY=79 -> 4x4 の仮想画面
        return {76: 0, 77: 0, 78: 4, 79: 4}[i]

    def GetDC(self, h):
        self.log.append(("GetDC", h))
        return 0x999

    def ReleaseDC(self, h, dc):
        self.log.append(("ReleaseDC", h, dc))
        return 1


class _RecGdi:
    """_grab_screen_rgb が使う gdi32 のダミー。SelectObject は前の選択(OLD)を返す。"""

    OLD = 0xABCD

    def __init__(self):
        self.log = []

    def CreateCompatibleDC(self, dc):
        self.log.append(("CreateCompatibleDC", dc))
        return 0x111

    def CreateCompatibleBitmap(self, dc, w, h):
        self.log.append(("CreateCompatibleBitmap", dc, w, h))
        return 0x222

    def SelectObject(self, dc, obj):
        self.log.append(("SelectObject", dc, obj))
        return self.OLD

    def BitBlt(self, *a):
        self.log.append(("BitBlt", *a))
        return 0                      # 失敗させて例外経路へ

    def DeleteObject(self, obj):
        self.log.append(("DeleteObject", obj))
        return 1

    def DeleteDC(self, dc):
        self.log.append(("DeleteDC", dc))
        return 1


def test_grab_screen_restores_selection_before_delete_on_failure():
    g = _RecGdi()
    u = _RecUser()
    with pytest.raises(sources._CaptureError) as caught:
        sources._grab_screen_rgb(u, g, 0)
    # 失敗の文は「〜できません」の形で書く(gui-principles.md 7-13)
    assert str(caught.value) == "画面を取得できません"
    names = [c[0] for c in g.log]
    # 選ぶ -> BitBlt(失敗)-> 選択を戻す -> bmp 削除 -> DC 削除 の順
    assert names == ["CreateCompatibleDC", "CreateCompatibleBitmap",
                     "SelectObject", "BitBlt", "SelectObject",
                     "DeleteObject", "DeleteDC"], names
    sel = [c for c in g.log if c[0] == "SelectObject"]
    assert sel[0] == ("SelectObject", 0x111, 0x222), sel   # bmp を選ぶ
    assert sel[1] == ("SelectObject", 0x111, _RecGdi.OLD), sel  # old へ戻す
    i_restore = g.log.index(("SelectObject", 0x111, _RecGdi.OLD))
    i_delobj = names.index("DeleteObject")
    assert i_restore < i_delobj, "bmp を消す前に選択を戻していない(リーク)"
    assert ("ReleaseDC", 0, 0x999) in u.log, "画面 DC を解放していない"


# ---- 表示の拡大率(DPI)と、ウィンドウの見える枠 ----


class _DpiUser(_RecUser):
    """スレッドの DPI の扱いを持つ user32 のダミー。大きさを取得した時の扱いを
    記録する。"""

    UNAWARE = 0x6010

    def __init__(self, win=(0, 0, 4, 4)):
        super().__init__()
        self.ctx = self.UNAWARE
        self.win = win

    def SetThreadDpiAwarenessContext(self, ctx):
        self.log.append(("SetThreadDpiAwarenessContext", ctx))
        old, self.ctx = self.ctx, ctx
        return old

    def GetSystemMetrics(self, i):
        self.log.append(("GetSystemMetrics", self.ctx))
        return super().GetSystemMetrics(i)

    def IsIconic(self, _hwnd):
        return 0

    def GetWindowRect(self, _hwnd, ref):
        self.log.append(("GetWindowRect", self.ctx))
        r = ref._obj
        r.left, r.top, r.right, r.bottom = self.win
        return 1

    def GetWindowDC(self, _hwnd):
        return 0x998

    def PrintWindow(self, _hwnd, _dc, _flags):
        self.log.append(("PrintWindow", self.ctx))
        return 1


class _PixelGdi(_RecGdi):
    """BitBlt が通り、GetDIBits が画素の位置を色にして返す gdi32 のダミー。

    ビットマップの (x, y) の画素は B = x、G = y、R = 0x80 になる。
    """

    def __init__(self):
        super().__init__()
        self.size = (0, 0)

    def CreateCompatibleBitmap(self, dc, w, h):
        self.size = (w, h)
        return super().CreateCompatibleBitmap(dc, w, h)

    def BitBlt(self, *a):
        self.log.append(("BitBlt", *a))
        return 1

    def GetDIBits(self, _dc, _bmp, _start, lines, buf, _bmi, _usage):
        import ctypes
        w, h = self.size
        data = bytes(v for y in range(h) for x in range(w) for v in (x, y, 0x80, 0))
        ctypes.memmove(buf, data, len(data))
        return lines


def test_the_screen_is_measured_and_taken_in_real_pixels():
    """画面の大きさは、スレッドを表示の拡大率に対応させてから取得し、取り終えたら
    戻すこと。

    拡大率に対応していないプロセスには、大きさが拡大率で割った値で返る。BitBlt は
    実際の画素で描くので、拡大率が 125% の 1920x1080 の画面なら左上の 1536x864 しか
    取れない。プロセス全体ではなく、取り込みの間だけ切り替える。
    """
    u, g = _DpiUser(), _PixelGdi()
    w, h, _rgb = sources._grab_screen_rgb(u, g, 0)
    assert (w, h) == (4, 4)
    sets = [c for c in u.log if c[0] == "SetThreadDpiAwarenessContext"]
    # 取り込みの前に、モニターごとの拡大率に対応した扱い(-4)にし、後で元へ戻す
    assert sets == [("SetThreadDpiAwarenessContext", -4),
                    ("SetThreadDpiAwarenessContext", _DpiUser.UNAWARE)], u.log
    assert {c[1] for c in u.log if c[0] == "GetSystemMetrics"} == {-4}, u.log
    assert u.log[-1] == ("SetThreadDpiAwarenessContext", _DpiUser.UNAWARE), u.log


def test_the_dpi_handling_is_restored_when_taking_the_screen_fails():
    """取り込みに失敗しても、スレッドの DPI の扱いを元へ戻すこと。"""
    u, g = _DpiUser(), _RecGdi()            # BitBlt が失敗する
    with pytest.raises(sources._CaptureError):
        sources._grab_screen_rgb(u, g, 0)
    assert u.ctx == _DpiUser.UNAWARE, u.log


def test_a_windows_without_the_dpi_call_still_takes_the_screen():
    """スレッドの DPI の扱いを切り替える関数がない Windows(1607 より前)でも、
    画面を取り込むこと。"""
    w, h, _rgb = sources._grab_screen_rgb(_RecUser(), _PixelGdi(), 0)
    assert (w, h) == (4, 4)


def test_the_monitor_list_is_read_in_real_pixels(monkeypatch):
    """モニターの一覧の大きさも、取り込みと同じ扱いで読むこと。"""
    u = _DpiUser()
    monkeypatch.setattr(sources, "_load_gdi", lambda: (u, object()))

    def rects(user32):
        user32.log.append(("EnumDisplayMonitors", user32.ctx))
        return [(0, 0, 1920, 1080)]

    monkeypatch.setattr(sources, "_monitor_rects", rects)
    assert sources.list_monitors() == [{"index": 1, "width": 1920, "height": 1080}]
    assert ("EnumDisplayMonitors", -4) in u.log, u.log
    assert u.ctx == _DpiUser.UNAWARE, u.log


@pytest.mark.parametrize("window, frame, want", [
    # 最大化したウィンドウ(1080p、タスクバーの高さ 40)。見えない枠が上下左右に 8 ずつ
    ((-8, -8, 1928, 1048), (0, 0, 1920, 1040), (8, 8, 1920, 1040)),
    # 普通のウィンドウ。見えない枠は左右と下にある
    ((100, 100, 900, 700), (107, 100, 893, 693), (7, 0, 786, 593)),
    # 見える枠を取得できなかった
    ((100, 100, 900, 700), None, (0, 0, 800, 600)),
    # 見える枠がウィンドウからはみ出している(ウィンドウの中だけを使う)
    ((0, 0, 800, 600), (-5, -5, 805, 590), (0, 0, 800, 590)),
    # 見える枠がウィンドウと重ならない
    ((0, 0, 800, 600), (900, 0, 1000, 100), (0, 0, 800, 600)),
])
def test_the_visible_part_of_a_window(window, frame, want):
    """ウィンドウの長方形と、見える枠の長方形から、切り出す範囲を求めること。"""
    assert sources._visible_part(window, frame) == want


def test_a_window_is_taken_without_its_invisible_border(monkeypatch):
    """ウィンドウは、見えない枠を除いた範囲で取ること。

    Windows 10 のウィンドウの長方形(GetWindowRect)は、見えない枠の分だけ広い。
    最大化したウィンドウは 1080p の画面で 1936x1056 になり、条件の編集の画面の上限
    (1920 幅)を超えて 1 画素おきに間引かれる。枠の分の黒い縁も映像に入る。
    """
    u, g = _DpiUser(win=(-3, -2, 17, 12)), _PixelGdi()
    monkeypatch.setattr(sources, "_frame_bounds", lambda _hwnd: (0, 0, 14, 10),
                        raising=False)
    w, h, rgb = sources._grab_window_rgb(u, g, 0x55)
    assert (w, h) == (14, 10)
    # 左上の画素は、ウィンドウのビットマップの (3, 2) である
    assert rgb[:3] == bytes((0x80, 2, 3)), rgb[:3]
    assert rgb[-3:] == bytes((0x80, 11, 16)), rgb[-3:]
    # ウィンドウの大きさも、拡大率に対応した扱いで取得する
    assert ("GetWindowRect", -4) in u.log and u.ctx == _DpiUser.UNAWARE, u.log


def test_screen_and_window_png_use_the_fast_encoder(monkeypatch):
    """画面とウィンドウの PNG も、映像機器と同じ速い符号化で作ること。

    スクリプトに渡す画像は選択肢のスクリプトの締め切りの中で作る。複数モニターの
    デスクトップ全体の大きな絵を遅い符号化で作ると、締め切りの多くを取り込みが使う。
    """
    used = []

    def fast(w, h, rgb):
        used.append((w, h))
        return encode_png(w, h, rgb)

    monkeypatch.setattr(sources, "encode_png_fast", fast)
    for src in (DesktopSource(), WindowSource("ウィンドウA")):
        monkeypatch.setattr(src, "_capture_rgb", lambda: (2, 2, _RGB_2X2))
        assert decode_png(src.grab_png())[2] == _RGB_2X2
    assert used == [(2, 2), (2, 2)], used


@pytest.mark.skipif(sys.platform != "win32", reason="Windows の GDI を使う")
def test_two_readouts_at_once_do_not_break_each_other():
    """2 つの取り込みが同時に画素を読み出しても、互いの呼び出しを壊さないこと。

    GetDIBits の関数はプロセスで 1 つである。呼ぶたびに構造体の型を作って引数の型を
    書き換えると、別のスレッドの取り込みがその間に書き換えたとき、ctypes が型の違いで
    拒否し、その周期の判定が失敗する。2 台のレーンが別々のウィンドウを見ているときや、
    実行中のレーンと条件の編集のいまの映像が別の映像ソースを見ているときに起きうる。
    ここでは、画素を読み出す呼び出しの直前に別の取り込みが割り込む順を、1 つの
    スレッドで起こす。画面は取り込まない(作ったばかりのビットマップを読む)。
    """
    _user32, gdi32 = sources._load_gdi()
    dc = gdi32.CreateCompatibleDC(None)
    bmp = gdi32.CreateCompatibleBitmap(dc, 2, 1)
    assert dc and bmp

    class Interrupting:
        """GetDIBits を参照するたびに、その前に別の取り込みが 1 回読み出す。"""

        busy = False

        def __getattr__(self, name):
            if name == "GetDIBits" and not Interrupting.busy:
                Interrupting.busy = True
                try:
                    sources._dc_to_rgb(self, dc, bmp, 2, 1)
                finally:
                    Interrupting.busy = False
            return getattr(gdi32, name)

    try:
        assert len(sources._dc_to_rgb(Interrupting(), dc, bmp, 2, 1)) == 2 * 1 * 3
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)


def test_file_source_does_not_raise_on_corrupt_png(tmp_path):
    """壊れた PNG でも、grab と grab_png は例外を投げず、None と last_error を
    返すこと。struct.error や IndexError など、ValueError と zlib.error 以外の例外も
    投げない。投げると、画面のプレビューが、読めない理由の代わりに想定外の不具合を
    表示する。
    """
    p = tmp_path / "bad.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)   # 署名は正しく中身が壊れ
    src = FileSource(p)
    assert src.grab() is None and src.last_error
    src.last_error = ""
    assert src.grab_png() is None and src.last_error
