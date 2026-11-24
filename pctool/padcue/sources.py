"""映像ソース(状態を取り込む口)。

種類を問わない一般の抽象(観測の手段は複数から選べる。P15)。
どのソースも装置に紐づかない
(装置番号とは無関係。条件(conditions.py)が「どこから見るか」として
持つだけで、ソース自身は誰の映像かを知らない)。

共通の契約(すべてのソースが満たす):
- ``grab() -> Frame | None``: 1 枚取る(判定用)。取れなければ
  None を返す(理由は ``last_error: str`` に残す)。例外を投げない。失敗は
  必ず None を返し、理由を last_error に残す。判定ループが 1 つのソースの失敗で
  全体を中断させずに済むようにするための形。
- ``grab_png() -> bytes | None``: 1 枚を PNG で返す(装置の映像の表示と、
  スクリプトに渡す画像に使う)。grab() と同じ映像を PNG にしたものである。
- ``close()``: 後始末。不要なら何もしない。

DirectShow の映像ソース(DShowSource と list_dshow_video_devices)は ffmpeg を
使う。利用者が別に入れる物で、なければ理由を last_error に入れて None を返す。
デスクトップ、ウィンドウ、画像ファイルの映像ソースには必要ない。
ffmpeg は外部バイナリを subprocess で呼ぶだけで import はしない。
パッケージの宣言依存は標準ライブラリのままを保つ。

デスクトップとウィンドウのキャプチャー(DesktopSource と WindowSource)は
Windows の GDI を ctypes(user32 と gdi32)で呼ぶ。ffmpeg も外部パッケージも
必要ない(標準ライブラリのみ)。GDI の呼び出しは grab の中だけで行い、
import 時には user32/gdi32 を使わない(遅延ロード)。Windows 以外でも
import と単体テストが通るようにするため。
"""
from __future__ import annotations

import atexit
import contextlib
import ctypes
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

from .frames import (
    PNG_HEAD_SIZE,
    Frame,
    ImageTooLarge,
    check_png_head,
    encode_png_fast,
    frame_from_png,
)

# ffmpeg のサブプロセス呼び出しに共通で使うタイムアウト(一覧取得用)。
# grab() 側のタイムアウトは呼び出し側が指定する(DShowSource.timeout)
_LIST_TIMEOUT = 10.0


def _find_ffmpeg() -> str | None:
    """ffmpeg の場所を探す。PATH になければ、WinGet が置く場所(Links)も見る。

    README は winget install Gyan.FFmpeg で入れる案内をしている。入れた直後は、既に
    開いているコンソールの PATH にまだ入っていないことがある。
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    p = Path(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"))
    return str(p) if p.is_file() else None


# PNG として読めないファイルの文。拡張子が .png でも中身が別の形式のことがある
NOT_PNG = "PNG の画像として読み込めません。PNG で保存したファイルを指定してください。"


# 画像のファイルの映像ソースを読めない理由(FileSource.problem)
FILE_IS_DIR = "フォルダーが指定されています。PNG のファイルを指定してください。"
FILE_MISSING = "ファイルが見つかりません。場所を確認してください。"


class FileSource:
    """指定した PNG ファイルを読む映像ソース(画像ファイル。file-format.md §9-1)。

    grab() は path を読んで Frame を返す(なければ None、last_error に理由)。

    読んだ結果は、ファイルの更新時刻と大きさが変わるまで使い回す。条件だけの
    選択待ちは 0.1 秒ごとに取得し直すので、そのたびに同じ PNG を読んで復号すると、
    cv2 のない PC では 1 回に秒の単位がかかり、ほかのレーンの判定と操作画面のサーバーも
    遅くなる。先にヘッダー(署名と IHDR)だけを読み、PNG でないものと大きすぎる画像は、
    残りを読まずに拒否する(誤って動画などの大きなファイルを指したとき)。
    """

    def __init__(self, path: str | Path):
        # エクスプローラーの「パスのコピー」は前後に " を付ける。付いたまま
        # 貼られても読む(前後の空白は既に詰めている)
        text = str(path).strip()
        if len(text) >= 2 and text[0] == text[-1] == '"':
            text = text[1:-1].strip()
        self.path = Path(text)
        self.last_error = ""
        # 最後に読んだファイルの (更新時刻, 大きさ) と、そのときの中身(PNG)、
        # 復号した 1 枚、読めなかった理由
        self._key: tuple | None = None
        self._raw: bytes | None = None
        self._frame: Frame | None = None
        self._why = ""

    # 読めない理由は、修正方法で書き分ける。場所は欄に出ているのでくり返さない
    def problem(self) -> str:
        """開かずに分かる、読めない理由(読めそうなら空)。"""
        if self.path.is_dir():
            return FILE_IS_DIR
        if not self.path.is_file():
            return FILE_MISSING
        return ""

    def _load(self) -> bool:
        """ファイルを読んで復号した結果を持つ。読めなければ偽(理由は last_error)。

        更新時刻と大きさが前に読んだときと同じなら、読み直さずに前の結果を使う
        (PNG として読めなかった結果も使う)。開けなかったとき(ほかのアプリが
        開いている)は結果を持たず、次に読み直す。
        """
        self.last_error = self.problem()
        if self.last_error:
            return False
        try:
            st = self.path.stat()
            key = (st.st_mtime_ns, st.st_size)
            if key != self._key:
                raw, frame, why = None, None, ""
                with self.path.open("rb") as f:
                    head = f.read(PNG_HEAD_SIZE)
                    try:
                        check_png_head(head)
                    except ImageTooLarge as e:
                        why = str(e)
                    except ValueError:
                        why = NOT_PNG
                    else:
                        raw = head + f.read()
                if raw is not None:
                    try:
                        frame = frame_from_png(raw)
                    except Exception:   # noqa: BLE001  契約: grab は例外を投げない。
                        # 壊れた PNG は ValueError/zlib.error だけでなく struct.error・
                        # IndexError(パレット範囲外)なども投げうるので、広く捕捉する。
                        # 中身のどこが壊れているか(チャンク・署名)は、修正する人の役に
                        # 立たない
                        raw, why = None, NOT_PNG
                self._key, self._raw, self._frame, self._why = key, raw, frame, why
        except OSError:
            self.last_error = ("ファイルを読み込めません。ほかのアプリで開いていないか"
                               "確認してください。")
            return False
        self.last_error = self._why
        return not self._why

    def grab(self) -> Frame | None:
        return self._frame if self._load() else None

    def grab_png(self) -> bytes | None:
        # 渡すのは、判定に使えると確かめた(復号できた)ファイルの中身そのもの
        return self._raw if self._load() else None

    def close(self) -> None:
        pass


class DShowSource:
    """OBS 仮想カメラなどの DirectShow の映像機器から 1 枚取る。

    実装は ffmpeg のサブプロセスで、機器ごとに 1 本を起動したままにし、映像を
    受け取り続ける(_DShowStream)。grab は、前に渡した 1 枚より後に受け取った
    最新の 1 枚を返し、まだ受け取っていなければ次の 1 枚を待つ。1 回ごとに ffmpeg を
    起動すると、そのたびに機器を開く時間がかかり、選択待ちの判定が画面の変化に
    遅れる。映像は機器が送る大きさのまま受け取り、縮めない(基準画像にする
    1 枚と画面に出す 1 枚も、ここから取る)。

    ffmpeg が見つからないときと失敗したときは None を返し、last_error に理由を
    残す(例外は投げない)。映像を受け取るのを待つのは timeout 秒までである。ほかの
    プロセスがデバイスを開いているときなどに、映像を受け取れないことがあるため。
    """

    def __init__(self, device_name: str, timeout: float = 5.0):
        self.device_name = device_name
        self.timeout = timeout
        self.last_error = ""
        # 最後に渡した 1 枚の、流れと通し番号。流れが作り直されたら、番号は
        # 0 から数え直すので、流れと組で覚える
        self._stream = None
        self._seen = 0

    def _next_frame(self) -> Frame | None:
        """前に渡した 1 枚より後に受け取った 1 枚。取れなければ None と理由。

        待つのは、呼ばれてから timeout 秒までである(起動し直して取得し直すときも、
        残りの時間だけ待つ)。
        """
        self.last_error = ""
        end = time.monotonic() + self.timeout
        exe = _find_ffmpeg()
        if not exe:
            self.last_error = ("映像機器から映像を取得するには ffmpeg が必要です。"
                               "ffmpeg をインストールしてください。")
            return None
        device = _listed_name(self.device_name)
        try:
            stream = _dshow_stream(exe, device)
        except OSError:
            self.last_error = "ffmpeg を起動できません。再インストールしてください。"
            return None
        except Exception:   # noqa: BLE001  契約: grab は例外を投げない。
            # 機器の名前に使えない文字(NUL)があると、起動の前に ValueError になる
            self.last_error = f"映像機器「{self.device_name}」から映像を取得できません"
            return None
        if stream is not self._stream:
            self._stream, self._seen = stream, 0
        got = stream.frame_after(self._seen, end - time.monotonic())
        if got is None and stream.retired:
            # 使われなくなって閉じる処理と重なったか、色の行列を替えて起動し直す。
            # 起動し直して、残りの時間で取得し直す
            try:
                stream = _dshow_stream(exe, device)
            except Exception:   # noqa: BLE001  契約: grab は例外を投げない
                stream = None
            if stream is not None:
                self._stream, self._seen = stream, 0
                got = stream.frame_after(self._seen, end - time.monotonic())
        if got is None:
            # 流れが動いたまま映像を受け取れない。ほかのアプリが機器を開いているときは
            # ffmpeg が終わる(why が理由を示す)ので、ここに来るのは機器に
            # 映像が来ていないときである
            stream = stream or self._stream
            self.last_error = stream.why() or (
                f"映像機器「{self.device_name}」から映像を受け取れません。"
                "機器に映像が入力されているか確認してください。")
            return None
        self._seen, frame = got
        return frame

    def grab(self) -> Frame | None:
        return self._next_frame()

    def grab_png(self) -> bytes | None:
        frame = self._next_frame()
        if frame is None:
            return None
        return encode_png_fast(frame.width, frame.height, frame.rgb)

    def close(self) -> None:
        # 流れは同じ機器を見るほかの映像ソースと共有するので、ここでは閉じない。
        # 使われなくなった流れは、自分で閉じる(_DShowStream.IDLE_S)
        pass


def _ppm_size(f) -> tuple[int, int] | None:
    """ffmpeg が出力する PPM(P6)のヘッダーを読み、(幅, 高さ) を返す。

    流れが終わっていれば None。ヘッダーは「P6 幅 高さ 255」を空白で区切ったもので、
    最後の 255 の後の空白 1 文字の直後から画素が始まる。その空白は、4 つ目の語の
    終わりとして読み終えている。
    """
    words: list[bytes] = []
    word = b""
    while len(words) < 4:
        c = f.read(1)
        if not c:
            return None
        if c.isspace():
            if word:
                words.append(word)
                word = b""
        else:
            word += c
    magic, width, height, top = words
    if magic != b"P6" or top != b"255":
        raise ValueError("PPM のヘッダーを読み込めません")
    return int(width), int(height)


def _split_top(text: str) -> list[str]:
    """「, 」で区切った項目に分ける。括弧の中の「,」では区切らない。"""
    out, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


_VIDEO_LINE = re.compile(r"Stream #\d+:\d+\S*: Video: (.*)")
_SIZE = re.compile(r"(\d+)x(\d+)\b")
_PIX = re.compile(r"([0-9a-z_]+)(?:.*\((.*)\))?$")
# 色の情報の括弧の中で、色の行列の印ではない項目(範囲と走査)
_NOT_MATRIX = frozenset({"tv", "pc", "progressive"})


def _describe_video(line: str) -> tuple[str, str, int] | None:
    """ffmpeg が映像の流れを説明する行から、(画素の形, 色の行列の印, 高さ) を読む。

    行は「Stream #0:0: Video: rawvideo (NV12 / 0x3231564E), nv12, 2560x1440, …」の
    形で、色の情報があれば画素の形の後ろの括弧に、範囲(tv か pc)、行列(原色と
    伝達特性が違えば「行列/原色/伝達特性」)、走査の順に入る(ffmpeg の
    avcodec_string)。行列の印がない(unknown を含む)ときは空を返す。映像の流れを
    説明する行でなければ None。
    """
    m = _VIDEO_LINE.search(line)
    if not m:
        return None
    parts = _split_top(m.group(1))
    for i, part in enumerate(parts):
        size = _SIZE.match(part)
        if not size or i == 0:
            continue
        pix = _PIX.match(parts[i - 1])
        if not pix:
            return None
        mark = ""
        for item in _split_top(pix.group(2) or ""):
            if item and item not in _NOT_MATRIX and " " not in item:
                mark = item.split("/")[0]
                break
        if mark in ("unknown", "reserved"):
            mark = ""
        return pix.group(1), mark, int(size.group(2))
    return None


def _matrix_for(pix: str, mark: str, height: int) -> str | None:
    """YUV から RGB へ変換する色の行列(ffmpeg の scale の in_color_matrix の値)。

    印があればそれに従う(auto は、ffmpeg が映像の印のとおりに変換する)。印がなければ、
    再生の道具の一般的な扱いに合わせて、高さ 720 以上を BT.709、それより低い
    映像を BT.601 として変換する。印のない HD の映像(OBS の仮想カメラなど、BT.709 で
    作られたもの)を BT.601 で変換すると、彩度の高い色の R、G、B が別々にずれ、取り方の
    違う映像ソース(ウィンドウなど)と同じ画面の一致度が下がる。RGB やグレーで来る
    映像は変換しないので None。
    """
    if "rgb" in pix or "bgr" in pix or pix.startswith(("gbr", "gray", "pal8")):
        return None
    if mark:
        return "bt709" if mark == "bt709" else "auto"
    return "bt709" if height >= 720 else "bt601"


class _DShowStream:
    """1 つの映像機器から、ffmpeg を起動したまま映像を受け取り続ける。

    持つのは最新の 1 枚だけである。機器が送る映像を FPS 枚/秒に間引いて
    受け取る。10 枚/秒にするのは、判定のたびに 0.1 秒以内の新しい 1 枚があれば
    足り、それより多く受け取っても CPU を使うだけだからである。

    映像機器は 2 つの ffmpeg から同時には開けないことがあるので、機器ごとに
    1 本だけ持つ(_dshow_stream)。IDLE_S 秒使われなければ自分で閉じる。

    画素の形は 8 ビットの RGB を指定する(指定しないと、10 ビットの映像を送る
    機器では 16 ビットの PPM になり、読めない)。YUV から RGB へ変換する色の行列は
    matrix で起動し、ffmpeg が標準エラーに出す入力の説明を読んで、映像に合う行列
    (_matrix_for)と違えば、その行列で起動し直す。機器ごとに確かめた行列を
    覚えておき(_matrices)、次からはそれで起動する。
    """

    FPS = 10
    # 使われなくなってから閉じるまでの時間。選択待ちを抜けてから次の選択待ちに
    # 来るまで、手順によっては 1 分ほどかかる。そのたびに開き直すと、次の
    # 選択待ちの最初の判定が、機器を開く時間だけ遅れる
    IDLE_S = 60.0
    # 起動できなかった機器を、次に起動し直すまでの時間。判定は連続して行われるので、
    # 待たずに起動し直すと、見つからない機器のために ffmpeg を起動し続ける
    RETRY_S = 1.0
    # 入力の説明を読んで行列を確かめるまで、受け取った 1 枚を渡さずに待つ長さ。説明は
    # 映像より先に出るので、普通は待たない。説明を読めなくても、これだけ待てば渡す
    DESCRIBE_S = 1.0

    def __init__(self, exe: str, device: str, matrix: str = "bt709"):
        self.device = device
        self.matrix = matrix
        # 入力の説明を読むために、出力の水準を info にする(進み具合の行は出さない)
        cmd = [exe, "-hide_banner", "-loglevel", "info", "-nostats",
               "-f", "dshow", "-i", f"video={device}",
               "-vf", f"fps={self.FPS},scale=in_color_matrix={matrix}",
               "-pix_fmt", "rgb24",
               "-f", "image2pipe", "-vcodec", "ppm", "-"]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._cv = threading.Condition()
        self._seq = 0
        self._frame: Frame | None = None
        self.ended_at: float | None = None
        self._err = b""
        self._used = time.monotonic()
        # 使われなくなって閉じた(失敗で終わったのではない)か、色の行列を替えるために
        # 閉じた。取り込みの途中で閉じたときは、呼んだ側が流れを作り直して取得し直す
        self.retired = False
        # 入力の説明を読んで、色の行列を確かめ終えた(説明が出ずに終わったときも立てる)
        self._described = threading.Event()
        self._closing = threading.Event()
        self._drainer = threading.Thread(target=self._drain, daemon=True,
                                         name="dshow-err")
        self._drainer.start()
        for run, name in ((self._read, "dshow-read"), (self._watch, "dshow-idle")):
            threading.Thread(target=run, daemon=True, name=name).start()

    def _read(self) -> None:
        out = self._proc.stdout
        waited = False      # 入力の説明を待つのは、1 枚目の前の 1 回だけ
        try:
            while True:
                size = _ppm_size(out)
                if size is None:
                    break
                width, height = size
                n = width * height * 3
                data = out.read(n)
                if len(data) != n:
                    break
                frame = Frame(width, height, data)
                # 入力の説明より先に受け取った 1 枚は、行列を確かめるまで渡さない。
                # 行列を替えて起動し直すなら、この 1 枚は違う行列で変換したものである。
                # 説明が出ないまま流れが続くときは、待つのを 1 回でやめる
                if not waited:
                    self._described.wait(self.DESCRIBE_S)
                    waited = True
                with self._cv:
                    if self.ended_at is not None:
                        break
                    self._seq += 1
                    self._frame = frame
                    self._cv.notify_all()
        except (OSError, ValueError):
            pass            # 流れが壊れた。終わりとして扱う(理由は stderr から)
        finally:
            # 終わりの理由は stderr の末尾から判別するので、読み終えてから終える
            self._drainer.join(1.0)
            self.close()

    def _drain(self) -> None:
        """ffmpeg の stderr を読み続け、終わりの理由を判別するために末尾だけ残す。

        読み始めに、入力の説明(「Input #0」の後の映像の流れの行)から色の行列を
        確かめる(_check_matrix)。
        """
        err = self._proc.stderr
        inputs = False
        try:
            for line in err:
                self._err = (self._err + line)[-4096:]
                if self._described.is_set():
                    continue
                text = line.decode("utf-8", "replace")
                if text.startswith("Input #"):
                    inputs = True
                elif text.startswith(("Output #", "Stream mapping")):
                    self._described.set()       # 映像の流れの説明がなかった
                elif inputs and (got := _describe_video(text)) is not None:
                    self._check_matrix(*got)
                    self._described.set()
        except (OSError, ValueError):
            pass
        finally:
            self._described.set()

    def _check_matrix(self, pix: str, mark: str, height: int) -> None:
        """入力の映像に合う色の行列が、起動したときの行列と違えば、覚えて閉じる。

        使われなくなって閉じるときと同じく、一覧から外して閉じる。取り込みの途中
        なら、呼んだ側が新しい行列で起動し直して取得し直す。
        """
        want = _matrix_for(pix, mark, height)
        if want is None or want == self.matrix:
            return
        with _streams_lock:
            _matrices[self.device] = want
        with self._cv:
            self.retired = True
        _forget_stream(self)
        self.close()

    def _watch(self) -> None:
        while not self._closing.wait(1.0):
            # 使われたかは、取り込み(frame_after)と同じロックの下で見る。ロックの外で
            # 見ると、取り込みが始まった直後に閉じることがある
            with self._cv:
                idle = time.monotonic() - self._used > self.IDLE_S
                if idle:
                    self.retired = True
            if idle:
                # 一覧から外してから閉じる。次に取り込むときは、すぐ起動し直す
                # (失敗で終わった流れのように、RETRY_S 待たせない)
                _forget_stream(self)
                self.close()

    def frame_after(self, seen: int, wait: float) -> tuple[int, Frame] | None:
        """通し番号が seen より後の最新の 1 枚を (番号, 1 枚) で返す。

        まだ受け取っていなければ wait 秒まで待つ。受け取れないか流れが終わって
        いれば None。
        終わった流れの最後の 1 枚は渡さない(機器が外れた後の古い映像である)。
        """
        end = time.monotonic() + wait
        with self._cv:
            self._used = time.monotonic()
            while self._seq <= seen and self.ended_at is None:
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(left)
            if self._seq <= seen or self.ended_at is not None:
                return None
            return self._seq, self._frame

    def why(self) -> str:
        """流れが終わった理由(終わっていなければ空)。"""
        if self.ended_at is None:
            return ""
        # ffmpeg の英語の出力は画面に出さない。判別するのに使うだけ
        detail = self._err.decode("utf-8", "replace")
        if "Could not find" in detail or "no video device" in detail.lower():
            return f"映像機器「{self.device}」が見つかりません"
        return (f"映像機器「{self.device}」から映像を取得できません。"
                "ほかのアプリで使用していないか確認してください。")

    def close(self) -> None:
        with self._cv:
            if self.ended_at is None:
                self.ended_at = time.monotonic()
            self._cv.notify_all()
        self._closing.set()
        if self._proc.poll() is None:
            self._proc.kill()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


_streams: dict[str, _DShowStream] = {}
_streams_lock = threading.Lock()
# 機器ごとの、入力の説明から確かめた色の行列。次に起動するときはこれで起動する
_matrices: dict[str, str] = {}


def _dshow_stream(exe: str, device: str) -> _DShowStream:
    """その機器の流れ。ないか終わっていれば起動する。

    終わってから RETRY_S 秒たつまでは、終わった流れを返す(呼んだ側は、
    その終わりの理由を受け取る)。
    """
    with _streams_lock:
        s = _streams.get(device)
        if (s is None or (s.ended_at is not None
                          and time.monotonic() - s.ended_at >= s.RETRY_S)):
            s = _streams[device] = _DShowStream(
                exe, device, _matrices.get(device, "bt709"))
        return s


def _forget_stream(stream: _DShowStream) -> None:
    """その流れを一覧から外す(既に別の流れに替わっていれば何もしない)。"""
    with _streams_lock:
        if _streams.get(stream.device) is stream:
            del _streams[stream.device]


def close_streams() -> None:
    """起動したままの ffmpeg をすべて終了させる(終了するときとテストの後始末)。"""
    with _streams_lock:
        got = list(_streams.values())
        _streams.clear()
    for s in got:
        s.close()


atexit.register(close_streams)


_VIDEO_HEADER = "DirectShow video devices"
_AUDIO_HEADER = "DirectShow audio devices"
_QUOTED = re.compile(r'"([^"]+)"')
# 行末の種別タグ。タグ形式の ffmpeg は `"名前" (video)` `"名前" (audio, video)`
# `"名前" (audio)` の形で 1 行に出す
_TAIL_TAG = re.compile(r"\(([^)]*)\)\s*$")


def _parse_dshow_video_devices(stderr: str) -> list[str]:
    """`ffmpeg -f dshow -list_devices true -i dummy` の stderr から
    映像機器の名前だけを取り出す。

    出力形式は ffmpeg のバージョンで 2 つに分かれる(外部コマンドのバージョン差。どの
    バージョンが入っているかを利用者に強いないため両方読む):
    - タグ形式: `"OBS Virtual Camera" (video)` のように 1 行に種別タグが付く
    - 見出し形式: `DirectShow video devices` / `... audio devices` の見出しで
      節が分かれ、各行は名前だけ
    どちらでも「Alternative name」の行と音声専用デバイスは除く。
    """
    names = []
    section = ""   # 見出し形式の節(video / audio)
    for line in stderr.splitlines():
        if _VIDEO_HEADER in line:
            section = "video"
            continue
        if _AUDIO_HEADER in line:
            section = "audio"
            continue
        if "Alternative name" in line:
            continue
        m = _QUOTED.search(line)
        if not m:
            continue
        tag = _TAIL_TAG.search(line[m.end():])
        if tag is not None:
            # タグ形式: 種別タグで判定(video を含むものだけ)
            if "video" in tag.group(1):
                names.append(m.group(1))
        elif section == "video":
            # 見出し形式: video の節の中の名前だけ
            names.append(m.group(1))
    return names


def list_dshow_video_devices() -> list[str] | None:
    """DirectShow の映像機器の名前の一覧。ffmpeg なしなら空リスト。

    条件を覚えるとき、ソースを選ぶための一覧として使う。ffmpeg を起動できない
    か、時間切れになったときは None を返す。機器がないこと(空リスト)と分ける
    のは、取れなかった一覧で、つながっている機器を「見つかりません」としない
    ためである。
    """
    exe = _find_ffmpeg()
    if not exe:
        return []
    cmd = [exe, "-hide_banner", "-f", "dshow", "-list_devices", "true",
          "-i", "dummy"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=_LIST_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_dshow_video_devices(res.stderr.decode("utf-8", "replace"))


# 映像機器の一覧で名前が見つからなかった機器を、「ない」と判断できない理由
# (dshow_unknown)
_DSHOW_NO_FFMPEG = ("映像機器を確認するには ffmpeg が必要です。"
                    "ffmpeg をインストールしてください。")
_DSHOW_FAILED = ("映像機器の一覧を取得できません。"
                 "ffmpeg を起動できるか確認してください。")
_DSHOW_LISTING = "映像機器の一覧を確認しています"


class _DeviceList:
    """DirectShow の機器の一覧のキャッシュ。

    一覧は ffmpeg の列挙で取るので、返らないときは最大 _LIST_TIMEOUT 秒
    かかる。使うたびに列挙すると、条件の画面は開くたびにその間表示されず、
    選択肢のスクリプトは判定の周期ごとに待たされる。そこで一覧をキャッシュしておき、
    古くなったら裏で取得し直す。

    再取得に失敗したときは、前に取れた一覧を残し、RETRY_S 秒後に取得し直す。
    空の一覧に差し替えると、TTL_S 秒間、つながっている機器を名前で
    見つけられない。
    """

    # キャッシュを使い続ける時間。機器を挿してから一覧に出るまでの待ち時間でもある
    TTL_S = 30.0
    # 再取得に失敗した後、次に取得し直すまでの時間。失敗が続くときに、ffmpeg を
    # 間を置かずに起動し続けない
    RETRY_S = 5.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._names: list[str] = []
        # 最後に一覧を取れた時刻(time.monotonic)。0 はまだ 1 度も取れていない
        self._at = 0.0
        # 次に取得し直す時刻(time.monotonic)。0 はすぐに取りに行く
        self._next = 0.0
        # 再取得の最中なら、その終わりを知らせる Event
        self._scan: threading.Event | None = None
        # 一覧に機器がなくても「ない」と判断できない理由(unknown)。空 = 判断できる
        self._why = ""

    def get(self, wait: float = 0.0) -> tuple[list[str], bool]:
        with self._lock:
            fresh = time.monotonic() < self._next
            done = self._scan
            if not fresh and done is None:
                done = self._scan = threading.Event()
                threading.Thread(target=self._refresh, args=(done,),
                                 daemon=True, name="dshow-list").start()
        if not fresh and wait > 0:
            done.wait(wait)
        with self._lock:
            # 再取得の最中も「確認している」と答える。画面はそれを見て少し後に
            # 取得し直すので、挿したばかりの機器が、一覧を開き直さなくても出る。
            # ただし、一覧を確認できないと分かった後(ffmpeg がない、取得に
            # 失敗した)は、確認している最中とは答えない。答え続けると、画面は
            # 確認できない理由(unknown)を出す時を失う
            scanning = not self._at or self._scan is not None
            return list(self._names), scanning and not self._why

    def peek(self) -> list[str]:
        """持っている一覧。古くても取得し直さない。"""
        with self._lock:
            return list(self._names)

    def unknown(self) -> str:
        """一覧にない機器を「ない」と判断できない理由。判断できるなら空。

        一覧を 1 度も取れていない(ffmpeg を起動できない、列挙が時間内に
        終わらない、まだ確認している最中)か、ffmpeg がないので一覧が空に
        なっているときである。前に取れた一覧があるなら、再取得に失敗して
        いても、その一覧で判断できる。
        """
        with self._lock:
            if self._why:
                return self._why
            if not self._at and self._scan is not None:
                return _DSHOW_LISTING
            return ""

    def _refresh(self, done: threading.Event) -> None:
        try:
            names = list_dshow_video_devices()
        except Exception:      # noqa: BLE001  例外も、取れなかったものとして扱う
            names = None
        # ffmpeg がないと、一覧は空になる。機器がないのではなく、確認できない
        no_ffmpeg = names == [] and not _find_ffmpeg()
        with self._lock:
            now = time.monotonic()
            if names is None:
                self._next = now + self.RETRY_S
                if not self._at:
                    self._why = _DSHOW_FAILED
            else:
                self._names = names
                self._at = now
                self._next = now + self.TTL_S
                self._why = _DSHOW_NO_FFMPEG if no_ffmpeg else ""
            self._scan = None
        done.set()


_device_list = _DeviceList()


def dshow_names(wait: float = 0.0) -> tuple[list[str], bool]:
    """DirectShow の機器の一覧と、一覧を確認している最中かどうかを返す。

    確認している最中とは、まだ 1 度も取れていないか、再取得の最中である。
    確認できないと分かった後(dshow_unknown が理由を示す)は、最中として扱わない。
    キャッシュが古ければ裏で取得し直す。wait が 0 なら待たずに、持っている一覧を返す
    (画面はこれを使い、2 つ目が真なら少し後にもう一度取得し直す)。wait が正なら、
    再取得が終わるのを最大 wait 秒待つ。
    """
    return _device_list.get(wait)


def dshow_unknown() -> str:
    """映像機器の一覧にない名前を、「ない」と判断できない理由。判断できるなら空。

    source_present が偽、find_source_by_name が None を返したときに見る。
    理由は ffmpeg がない、一覧を取得できない、まだ確認している最中、のどれかで、
    利用者に見せる文である。原因は映像機器のつなぎ方ではないので、「見つかり
    ません」の代わりにこれを示す。
    """
    return _device_list.unknown()


def _listed_name(device: str) -> str:
    """機器の名前を、機器の一覧の綴りに合わせる。一覧に一致しなければそのまま。

    ffmpeg は機器の名前の大文字と小文字を区別する。開始の前のチェックと名前の
    照合は区別しない(normalize_source_name)ので、手で書いた条件や別の PC から
    受け取った条件の名前が、一覧と大文字と小文字だけ違うと、チェックは通り、取り込みは
    毎回「見つかりません」になる。一覧は持っているものを見るだけで、取得し直さない
    (取り込みのたびに ffmpeg で機器を列挙しない)。開始の前のチェックと画面の
    映像ソースの一覧が、先に取得している。
    """
    names = _device_list.peek()
    if device in names:
        return device
    want = normalize_source_name(device)
    for name in names:
        if normalize_source_name(name) == want:
            return name
    return device


# ---- デスクトップとウィンドウ(Windows GDI) ----
#
# ここから下は Windows の GDI を ctypes で呼ぶ。共通の作りは:
#   1. _capture_rgb() が (width, height, RGB バイト列) か None を返す
#      (失敗は None + last_error。例外は投げない)。
#   2. _GdiSource が grab()/grab_png() をその RGB から作る
#      (grab は Frame、grab_png は PNG)。
# ctypes(user32/gdi32)を使うのは _load_gdi 以下の関数の中だけ。
# テストはこの _capture_rgb / _find_window / _enum_toplevel_windows を
# monkeypatch で差し替え、既知の RGB を渡して整合を見る(実際の画面は取り込まない)。


class _CaptureError(Exception):
    """GDI 取得の失敗を last_error に入れるための内部例外。

    外へは出さない。_capture_rgb がここで捕捉して None と last_error にする
    (共通の契約「失敗は None と last_error で表し、例外を投げない」を守るため)。
    """


class _BitmapInfoHeader(ctypes.Structure):
    """GetDIBits に渡すビットマップの形(BITMAPINFOHEADER)。

    型はモジュールで 1 つだけ作る。GetDIBits の関数はプロセスで 1 つなので、
    呼ぶたびに型を作って引数の型を書き換えると、別のスレッドの取り込みがその間に
    書き換えたとき、ctypes が型の違いで呼び出しを拒否する。
    """

    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _load_gdi():
    """user32 / gdi32 を遅延ロードし、使う関数の argtypes/restype を設定して返す。

    import 時ではなく取得のたびに呼ぶ(Windows 以外では _CaptureError)。
    argtypes を設定しないと 64bit のハンドル(HWND/HDC 等)を既定の c_int に
    詰めようとして OverflowError か ctypes.ArgumentError になる。ハンドル系は
    必ず wintypes.HWND / HDC / HANDLE(いずれも c_void_p 相当)にする。
    関数はプロセスで共有なので、設定する型はいつも同じものにする(何度設定しても
    同じ型になる)。コールバックを取る EnumWindows / EnumDisplayMonitors は、
    型を作る呼び出し側(_enum_toplevel_windows / _monitor_rects)で argtypes を
    設定する(ctypes は同じ形のコールバックの型を使い回す)。
    """
    import sys
    if sys.platform != "win32":
        raise _CaptureError("画面とウィンドウの映像は Windows でのみ取得できます")
    from ctypes import wintypes
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    hwnd, hdc, hbitmap, hgdiobj = (wintypes.HWND, wintypes.HDC,
                                   wintypes.HBITMAP, wintypes.HGDIOBJ)
    boolt, intt, uintt, dwordt = (wintypes.BOOL, wintypes.INT,
                                  wintypes.UINT, wintypes.DWORD)
    lprect = ctypes.POINTER(wintypes.RECT)

    def sig(fn, args, res):
        fn.argtypes = args
        fn.restype = res

    sig(u.IsWindow, [hwnd], boolt)
    sig(u.IsWindowVisible, [hwnd], boolt)
    sig(u.GetWindowTextLengthW, [hwnd], intt)
    sig(u.GetWindowTextW, [hwnd, wintypes.LPWSTR, intt], intt)
    sig(u.IsIconic, [hwnd], boolt)
    sig(u.GetWindowRect, [hwnd, lprect], boolt)
    sig(u.PrintWindow, [hwnd, hdc, uintt], boolt)
    sig(u.GetWindowDC, [hwnd], hdc)
    sig(u.GetDC, [hwnd], hdc)
    sig(u.ReleaseDC, [hwnd, hdc], intt)
    sig(u.GetSystemMetrics, [intt], intt)
    u.EnumWindows.restype = boolt              # argtypes は呼び出し側で
    u.EnumDisplayMonitors.restype = boolt      # 同上
    # スレッドの DPI の扱いを切り替える関数。Windows 10 の 1607 より前にはない
    set_dpi = getattr(u, "SetThreadDpiAwarenessContext", None)
    if set_dpi is not None:
        sig(set_dpi, [ctypes.c_void_p], ctypes.c_void_p)

    sig(g.CreateCompatibleDC, [hdc], hdc)
    sig(g.CreateCompatibleBitmap, [hdc, intt, intt], hbitmap)
    sig(g.SelectObject, [hdc, hgdiobj], hgdiobj)
    sig(g.BitBlt, [hdc, intt, intt, intt, intt, hdc, intt, intt, dwordt], boolt)
    sig(g.DeleteObject, [hgdiobj], boolt)
    sig(g.DeleteDC, [hdc], boolt)
    sig(g.GetDIBits, [hdc, hbitmap, uintt, uintt, ctypes.c_void_p,
                      ctypes.POINTER(_BitmapInfoHeader), uintt], intt)
    return u, g


def _dc_to_rgb(gdi32, mem_dc, bmp, width: int, height: int) -> bytes:
    """メモリ DC 上のビットマップから GetDIBits で画素を取り、RGB に並べ替える。

    32bit・BI_RGB・top-down(biHeight を負)で取り出すと、画素は B,G,R,X の
    並びで来る。R,G,B へ詰め替えて返す(X は捨てる)。GetDIBits の引数の型は
    _load_gdi が設定する(ここでは書き換えない)。
    """
    bmi = _BitmapInfoHeader()
    bmi.biSize = ctypes.sizeof(_BitmapInfoHeader)
    bmi.biWidth = width
    bmi.biHeight = -height           # 負で top-down(行 0 が上)
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0            # BI_RGB
    buf = (ctypes.c_ubyte * (width * height * 4))()
    dib_rgb_colors = 0
    scanned = gdi32.GetDIBits(mem_dc, bmp, 0, height, buf,
                              ctypes.byref(bmi), dib_rgb_colors)
    if scanned == 0:
        raise _CaptureError("画面を取得できません")
    src = memoryview(bytes(buf))
    out = bytearray(width * height * 3)
    out[0::3] = src[2::4]            # R
    out[1::3] = src[1::4]            # G
    out[2::3] = src[0::4]            # B
    return bytes(out)


# スレッドの DPI の扱い(SetThreadDpiAwarenessContext の値)。モニターごとの拡大率に
# 対応した扱いの 2 版を使い、それがない版の Windows 10(1703 より前)では 1 版を使う
_DPI_PER_MONITOR = (-4, -3)


@contextlib.contextmanager
def _real_pixels(user32):
    """この間だけ、スレッドを表示の拡大率に対応させ、大きさと位置を実際の画素で扱う。

    python.exe は拡大率に対応していないプロセスとして動くので、拡大率を 100% 以外に
    した PC では、画面とウィンドウの大きさと位置が拡大率で割った値で返る。BitBlt と
    PrintWindow は実際の画素で描くので、取れるのは左上の一部だけになる(1920x1080 を
    125% にした画面なら 1536x864)。プロセス全体ではなく、取り込みの間だけ
    切り替えて、終わったら戻す。関数がない Windows(10 の 1607 より前)では何もしない。
    """
    set_dpi = getattr(user32, "SetThreadDpiAwarenessContext", None)
    old = None
    if set_dpi is not None:
        for ctx in _DPI_PER_MONITOR:
            old = set_dpi(ctx)
            if old:
                break
    try:
        yield
    finally:
        if old:
            set_dpi(old)


def _monitor_bounds(user32, monitor: int):
    """取り込む矩形 (x, y, w, h) を返す。なければ None。

    monitor <= 0 は仮想画面全体(全モニター)。monitor >= 1 は
    EnumDisplayMonitors が返す順の N 番目(1 始まり)。
    """
    if monitor <= 0:
        # SM_XVIRTUALSCREEN=76 / Y=77 / CX=78 / CY=79
        x = user32.GetSystemMetrics(76)
        y = user32.GetSystemMetrics(77)
        w = user32.GetSystemMetrics(78)
        h = user32.GetSystemMetrics(79)
        return x, y, w, h
    rects = _monitor_rects(user32)
    idx = monitor - 1
    if idx < 0 or idx >= len(rects):
        return None
    return rects[idx]


def _monitor_rects(user32) -> list[tuple[int, int, int, int]]:
    """各モニターの矩形 (x, y, w, h) を、EnumDisplayMonitors が返す順に並べる。"""
    import ctypes
    from ctypes import wintypes
    rects: list[tuple[int, int, int, int]] = []
    proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(_hmon, _hdc, lprc, _lparam):
        r = lprc.contents
        rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    cb = proc_type(_cb)
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, ctypes.POINTER(wintypes.RECT), proc_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors(None, None, cb, 0)
    return rects


def _enum_toplevel_windows():
    """トップレベルの各ウィンドウを (hwnd, 可視か, タイトル) で列挙する。

    可視・タイトル有無での絞り込みはしない(呼び出し側 _select_window_titles /
    _find_window が行う)。ここは EnumWindows を回すだけ(Windows のみ)。
    """
    import ctypes
    from ctypes import wintypes
    user32, _gdi32 = _load_gdi()       # argtypes 設定済みの user32 を使う
    result: list[tuple[int, bool, str]] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    # IsWindowVisible は、ストアアプリの隠れたウィンドウ(cloaked)も可視と答える。
    # 画面にないウィンドウが一覧に並ぶので、隠れているかを DWM から取得して外す
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    except OSError:
        dwm = None
    dwmwa_cloaked = 14

    def _cloaked(hwnd) -> bool:
        if dwm is None:
            return False
        v = wintypes.DWORD(0)
        ok = dwm.DwmGetWindowAttribute(hwnd, dwmwa_cloaked, ctypes.byref(v),
                                       ctypes.sizeof(v))
        return ok == 0 and v.value != 0

    def _cb(hwnd, _lparam):
        visible = bool(user32.IsWindowVisible(hwnd)) and not _cloaked(hwnd)
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        result.append((hwnd, visible, title))
        return True

    cb = proc_type(_cb)
    user32.EnumWindows.argtypes = [proc_type, wintypes.LPARAM]
    user32.EnumWindows(cb, 0)
    return result


# 常にいるのに映像ソースとしては選ばないウィンドウの名前(デスクトップのシェル)
_NOT_A_WINDOW = frozenset({"Program Manager"})


def _count_window_titles(windows) -> list[dict]:
    """(hwnd, 可視, タイトル) の列から、可視でタイトルのあるものを数える。

    項目は {"title", "count"} で、最初に出た順を保つ。同じ名前を 1 つに
    まとめない。指定はタイトルの部分一致なので、同じ名前のウィンドウが
    2 つあれば、どちらを見ているかが決まらない。まとめてしまうと、その状況が
    画面から見えなくなる。
    """
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for _hwnd, visible, title in windows:
        title = (title or "").strip()
        if not (visible and title):
            continue
        # デスクトップそのもの(Program Manager)はウィンドウとして選ぶ意味がない
        if title in _NOT_A_WINDOW:
            continue
        got = seen.get(title)
        if got is None:
            got = {"title": title, "count": 0}
            seen[title] = got
            out.append(got)
        got["count"] += 1
    return out


def list_windows() -> list[dict]:
    """可視でタイトルのあるトップレベルウィンドウの一覧。

    項目は {"title", "count"} で、count は同じ名前のウィンドウの数である。条件編集の
    ソース選択に使う。Windows 以外・失敗時は空リスト(列挙するだけで、
    ウィンドウは開かない)。
    """
    try:
        return _count_window_titles(_enum_toplevel_windows())
    except Exception:     # noqa: BLE001  一覧は取れた範囲でよい(失敗は空でよい)
        return []


def list_monitors() -> list[dict]:
    """モニターの一覧。項目は {"index", "width", "height"} で、index は 1 から数える。

    画面は、デスクトップ全体のほかに、モニター 1 枚ずつも映像ソースの選択肢に
    出す。2 枚あるうちの片方だけを見たいとき、全体を取り込んでから判定する範囲で
    切り取ると、取り込む画像が無駄に大きくなり、取得も遅くなるからである。
    Windows 以外の OS と、取得に失敗したときは空のリストを返す。大きさは、取り込みと
    同じく実際の画素で読む(_real_pixels)。
    """
    try:
        user32, _gdi32 = _load_gdi()
        with _real_pixels(user32):
            rects = _monitor_rects(user32)
        return [{"index": i, "width": w, "height": h}
                for i, (_x, _y, w, h) in enumerate(rects, 1)]
    except Exception:     # noqa: BLE001  一覧は取れた範囲でよい(失敗は空でよい)
        return []


def _grab_screen_rgb(user32, gdi32, monitor: int) -> tuple[int, int, bytes]:
    """画面(モニター)を BitBlt で取り込み (w, h, RGB) を返す。

    大きさを取得するところから取り終えるまで、実際の画素で扱う(_real_pixels)。
    """
    with _real_pixels(user32):
        return _blit_screen(user32, gdi32, monitor)


def _blit_screen(user32, gdi32, monitor: int) -> tuple[int, int, bytes]:
    """_grab_screen_rgb の中身。

    DC・ビットマップは取得の成否に関わらず必ず解放する(try/finally)。
    """
    bounds = _monitor_bounds(user32, monitor)
    if bounds is None:
        raise _CaptureError(f"モニター {monitor} が見つかりません")
    x, y, w, h = bounds
    if w <= 0 or h <= 0:
        raise _CaptureError("画面の大きさを取得できません")
    screen_dc = user32.GetDC(0)
    if not screen_dc:
        raise _CaptureError("画面を取得できません")
    mem_dc = 0
    bmp = 0
    old = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
        if not mem_dc or not bmp:
            raise _CaptureError("画面を取得できません")
        old = gdi32.SelectObject(mem_dc, bmp)
        srccopy = 0x00CC0020
        if not gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, x, y, srccopy):
            raise _CaptureError("画面を取得できません")
        return w, h, _dc_to_rgb(gdi32, mem_dc, bmp, w, h)
    finally:
        # 成功・失敗どちらの経路でも必ず後始末する。DC に選択中のビットマップは
        # 削除できないので、bmp を消す前に必ず選択を元へ戻す。失敗(BitBlt /
        # GetDIBits の例外)で戻さないまま DeleteObject すると、選択中の bmp が
        # 解放されずに毎周期溜まり、GDI のハンドルが尽きる
        if mem_dc and old is not None:
            gdi32.SelectObject(mem_dc, old)
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def _frame_bounds(hwnd) -> tuple[int, int, int, int] | None:
    """ウィンドウの見える枠の長方形 (左, 上, 右, 下)。DWM から取得できなければ None。

    DWMWA_EXTENDED_FRAME_BOUNDS(9)は、拡大率によらず実際の画素で返る。
    """
    from ctypes import wintypes
    try:
        dwm = ctypes.WinDLL("dwmapi")
    except OSError:
        return None
    fn = dwm.DwmGetWindowAttribute
    fn.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    fn.restype = ctypes.c_long
    rect = wintypes.RECT()
    dwmwa_extended_frame_bounds = 9
    if fn(hwnd, dwmwa_extended_frame_bounds, ctypes.byref(rect),
          ctypes.sizeof(rect)) != 0:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _visible_part(window: tuple[int, int, int, int],
                  frame: tuple[int, int, int, int] | None) -> tuple[int, int, int, int]:
    """ウィンドウの長方形のうち、見える枠の中を、ウィンドウの左上から測った
    (x, y, 幅, 高さ) で返す。

    長方形はどちらも (左, 上, 右, 下)。見える枠がない(取得できなかった)か、
    ウィンドウと重ならなければ、ウィンドウ全体を返す。
    """
    left, top, right, bottom = window
    whole = (0, 0, right - left, bottom - top)
    if frame is None:
        return whole
    x0, y0 = max(frame[0], left), max(frame[1], top)
    x1, y1 = min(frame[2], right), min(frame[3], bottom)
    if x1 <= x0 or y1 <= y0:
        return whole
    return x0 - left, y0 - top, x1 - x0, y1 - y0


def _crop_rgb(rgb: bytes, width: int, x: int, y: int, w: int, h: int) -> bytes:
    """幅 width の RGB から、(x, y) を左上とする w×h を切り出す。"""
    row = width * 3
    return b"".join(rgb[(y + j) * row + x * 3:(y + j) * row + (x + w) * 3]
                    for j in range(h))


def _grab_window_rgb(user32, gdi32, hwnd) -> tuple[int, int, bytes]:
    """ウィンドウの内容を PrintWindow で取り込み (w, h, RGB) を返す。

    大きさを取得するところから取り終えるまで、実際の画素で扱う(_real_pixels)。
    """
    with _real_pixels(user32):
        return _print_window(user32, gdi32, hwnd)


def _print_window(user32, gdi32, hwnd) -> tuple[int, int, bytes]:
    """_grab_window_rgb の中身。

    PW_RENDERFULLCONTENT(2)を先に試し、駄目なら 0 で取得し直す。最小化・
    0 サイズは _CaptureError。DC・ビットマップは必ず解放する(try/finally)。

    返すのは、ウィンドウの見える枠の中だけである(_visible_part)。Windows 10 の
    ウィンドウの長方形(GetWindowRect)は、左右と下の見えない枠の分だけ広い。
    最大化したウィンドウは画面より広くなり(1080p で 1936x1056)、条件の編集の
    画面の上限(1920 幅)を超えて 1 画素おきに間引かれる。枠の分の縁も映像に入る。
    """
    from ctypes import wintypes
    if user32.IsIconic(hwnd):
        raise _CaptureError(
            "ウィンドウが最小化されています。元の大きさに戻してください。")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise _CaptureError("ウィンドウの位置・大きさを取得できません")
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise _CaptureError("ウィンドウの大きさが 0 です")
    part = _visible_part((rect.left, rect.top, rect.right, rect.bottom),
                         _frame_bounds(hwnd))
    win_dc = user32.GetWindowDC(hwnd)
    if not win_dc:
        raise _CaptureError("ウィンドウの映像を取得できません")
    mem_dc = 0
    bmp = 0
    old = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(win_dc)
        bmp = gdi32.CreateCompatibleBitmap(win_dc, w, h)
        if not mem_dc or not bmp:
            raise _CaptureError("画面を取得できません")
        old = gdi32.SelectObject(mem_dc, bmp)
        pw_renderfullcontent = 2
        ok = user32.PrintWindow(hwnd, mem_dc, pw_renderfullcontent)
        if not ok:
            ok = user32.PrintWindow(hwnd, mem_dc, 0)
        if not ok:
            raise _CaptureError("ウィンドウの映像を取得できません")
        rgb = _dc_to_rgb(gdi32, mem_dc, bmp, w, h)
        if part == (0, 0, w, h):
            return w, h, rgb
        return part[2], part[3], _crop_rgb(rgb, w, *part)
    finally:
        # 成功・失敗どちらの経路でも必ず後始末する。DC に選択中のビットマップは
        # 削除できないので、bmp を消す前に必ず選択を元へ戻す。失敗(PrintWindow /
        # GetDIBits の例外)で戻さないまま DeleteObject すると、選択中の bmp が
        # 解放されずに毎周期溜まり、GDI のハンドルが尽きる
        if mem_dc and old is not None:
            gdi32.SelectObject(mem_dc, old)
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, win_dc)


class _GdiSource:
    """GDI で取った RGB から grab と grab_png を作る共通部分。

    子クラスは _capture_rgb()((w,h,RGB) か None を返す)を持つ。
    """

    last_error: str

    def _capture_rgb(self):
        raise NotImplementedError

    def grab(self) -> Frame | None:
        got = self._capture_rgb()
        if got is None:
            return None
        w, h, rgb = got
        try:
            return Frame(w, h, rgb)
        except Exception:   # noqa: BLE001  契約: grab は例外を投げない
            self.last_error = "取得した画像を読み込めません"
            return None

    def grab_png(self) -> bytes | None:
        got = self._capture_rgb()
        if got is None:
            return None
        w, h, rgb = got
        try:
            # 映像機器と同じ速い符号化にする。スクリプトに渡す画像は、選択肢の
            # スクリプトの締め切りの中で作る
            return encode_png_fast(w, h, rgb)
        except Exception:   # noqa: BLE001  契約: grab_png は例外を投げない
            self.last_error = "取得した画像を読み込めません"
            return None

    def close(self) -> None:
        pass


class DesktopSource(_GdiSource):
    """画面(モニター)全体を取る映像ソース(Windows GDI)。

    monitor=0(既定)は仮想画面全体(全モニター)。monitor>=1 は
    EnumDisplayMonitors の順の N 番目(1 始まり)。取れなければ None を返し、
    last_error に理由を残す(例外は投げない)。
    """

    def __init__(self, monitor: int = 0):
        self.monitor = int(monitor)
        self.last_error = ""

    def _capture_rgb(self):
        self.last_error = ""
        try:
            user32, gdi32 = _load_gdi()
            return _grab_screen_rgb(user32, gdi32, self.monitor)
        except _CaptureError as e:
            self.last_error = str(e)
            return None
        except Exception:   # noqa: BLE001  契約: 失敗は None と last_error。
            # DLL の読み込みや ctypes の呼び出しが想定外の例外を出すことがある
            self.last_error = "画面を取得できません"
            return None


class WindowSource(_GdiSource):
    """タイトルで見つけたウィンドウを取る映像ソース(Windows GDI)。

    title に部分一致する可視のトップレベルウィンドウを探す。見つからなければ
    None を返し last_error に理由を残す。最小化・0 サイズも None + last_error
    (例外は投げない)。

    同じ名前のウィンドウが 2 つ以上あるときは、どれかを黙って選ばずに拒否する。
    どちらを見ているかが決まらないまま判定すると、静かに間違う。
    保存できる指定はタイトルだけ(ウィンドウの識別子は再起動で変わる)なので、
    名前で 1 つに決まるようにしてもらうしかない。

    一度決めたウィンドウは、閉じるか隠れるまで保つ(タイトルが変わっても保つ)。
    取るたびに探し直すと、実行の途中で同じ名前の別のウィンドウが現れたときに対象が
    入れ替わる。タイトルに表示中の名前やシーンの名前を出すアプリでは、タイトルが
    変わっただけで、同じ一部を含む別のウィンドウへ警告なしに切り替わるか、
    「見つかりません」になる。タイトルで探すのは、まだ決めていないときと、決めた
    ウィンドウがなくなったときだけである。
    """

    def __init__(self, title: str):
        self.title = title
        self.last_error = ""
        self._hwnd = 0

    def _matching(self) -> list[int]:
        """title に部分一致する可視ウィンドウの hwnd を、見つかった順に。"""
        if not self.title:
            return []
        return [hwnd for hwnd, visible, title in _enum_toplevel_windows()
                if visible and self.title in (title or "")]

    def _find_window(self) -> int:
        """見るウィンドウの hwnd。なければ 0、1 つに決まらなければ _CaptureError。"""
        if self._hwnd and _window_open(self._hwnd):
            return self._hwnd       # 決めたウィンドウがまだある。探し直さない
        found = self._matching()
        if len(found) > 1:
            raise _CaptureError(_too_many_windows(self.title, len(found)))
        self._hwnd = found[0] if found else 0
        return self._hwnd

    def _capture_rgb(self):
        self.last_error = ""
        try:
            user32, gdi32 = _load_gdi()
            hwnd = self._find_window()
            if not hwnd:
                raise _CaptureError(f"ウィンドウ「{self.title}」が見つかりません")
            return _grab_window_rgb(user32, gdi32, hwnd)
        except _CaptureError as e:
            self.last_error = str(e)
            return None
        except Exception:   # noqa: BLE001  契約: 失敗は None と last_error。
            # DLL の読み込みや ctypes の呼び出しが想定外の例外を出すことがある
            self.last_error = "ウィンドウの映像を取得できません"
            return None


def _window_open(hwnd) -> bool:
    """ウィンドウが残っていて見えているか(IsWindow と IsWindowVisible)。"""
    user32, _gdi32 = _load_gdi()
    return bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd))


def _too_many_windows(title: str, n: int) -> str:
    """当てはまるウィンドウが 2 つ以上あって、どれを見るか決められない理由。"""
    return (f"「{title}」を名前に含むウィンドウが {n} 個あります。"
            "使用しないウィンドウを閉じてください。")


# 装置に設定している映像(実行する装置の、装置の一覧で設定した映像)を、手順の
# 中から指すときの名前。手順は装置に依らないので、装置の名前ではなくこの決まった
# 語で書く(file-format.md §9-1)。条件の判定する映像の選択肢の名前でもある
LANE_VIDEO_NAME = "装置に設定している映像"


def normalize_source_name(name: str) -> str:
    """映像ソースの名前を照合する形に正規化する。

    Unicode の NFC に正規化し、英字の大文字と小文字を区別しない。映像ソースの
    名前は、機器の名前やウィンドウのタイトルという PC 側の名前なので、
    長さや使える文字のチェックはしない(手順やフレーム表の名前とは別の規則)。
    """
    return unicodedata.normalize("NFC", str(name or "")).strip().casefold()


def source_present(source: dict, wait: float = _LIST_TIMEOUT) -> bool:
    """映像ソースの指定が、この PC で見つけられるか。開かずに確認する。

    条件は映像ソースを指定のまま持つ(file-format.md §9-1)。別の PC で作った
    条件は、機器・ウィンドウ・ファイル・モニターがこの PC にないことがある。
    照合の仕方は、取り込むときと同じにする。ウィンドウはタイトルの一部で探し、
    ファイルは前後の " を外す(FileSource)。モニターの 0 以下はすべてのモニターを
    合わせた画面なので、いつでも見つけられる。装置に設定している映像(lane)は、
    レーンの設定が決めるので、ここでは見つけられるものとする。
    """
    kind = source.get("kind")
    if kind == "dshow":
        names, _scanning = dshow_names(wait)
        want = normalize_source_name(source.get("device", ""))
        return any(normalize_source_name(dev) == want for dev in names)
    if kind == "window":
        title = source.get("title", "")
        return any(title in w["title"] for w in list_windows())
    if kind == "file":
        return FileSource(source.get("path", "")).path.is_file()
    if kind == "desktop":
        n = int(source.get("monitor", 0) or 0)
        return n <= 0 or any(x["index"] == n for x in list_monitors())
    return True


def source_problem(source: dict) -> str:
    """見つけられる映像ソースを、取り込むときに拒否する理由。拒否しなければ空。

    当てはまるウィンドウが 2 つ以上あると、取り込みはどれを見るか決められずに、
    毎回拒否する(WindowSource)。開始の前のチェックは、source_present で見つけられる
    ことを確かめた後にこれを見て、取り込むときと同じ文で知らせる。数え方は
    source_present と同じく、ウィンドウの一覧から数える。
    """
    if source.get("kind") != "window":
        return ""
    title = source.get("title", "")
    n = sum(w["count"] for w in list_windows() if title in w["title"])
    return _too_many_windows(title, n) if n > 1 else ""


def find_source_by_name(name: str, wait: float = _LIST_TIMEOUT) -> dict | None:
    """名前から映像ソースの指定を探す。見つからなければ None。

    探せる名前は、機器の名前、ウィンドウのタイトル、「デスクトップ全体」、
    「モニター N」である。手順に書くのは名前だけで、実体はこの PC の設定から
    探す。モニターは番号だけで探す(解像度は変わりうるので名前に入れない)。

    機器の一覧はキャッシュ(dshow_names)から探す。キャッシュが古いときは、再取得が
    終わるのを最大 wait 秒待つ。
    """
    want = normalize_source_name(name)
    if not want:
        return None
    if want == normalize_source_name("デスクトップ全体"):
        return {"kind": "desktop"}
    m = re.fullmatch(r"モニター\s*(\d+)", want)
    if m:
        n = int(m.group(1))
        if any(x["index"] == n for x in list_monitors()):
            return {"kind": "desktop", "monitor": n}
        return None
    names, _scanning = dshow_names(wait)
    for dev in names:
        if normalize_source_name(dev) == want:
            return {"kind": "dshow", "device": dev}
    for w in list_windows():
        if normalize_source_name(w["title"]) == want:
            return {"kind": "window", "title": w["title"]}
    return None
