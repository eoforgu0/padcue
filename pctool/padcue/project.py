"""プロジェクトのフォルダーの読み書き(手順とフレーム表、変換、ログ、並び順、
プリセット、padcue.json)。

    <プロジェクト>/
      procedures/<名前>.json      手順
      frametables/<名前>.csv      フレーム表
      conditions/<名前>/          条件。読み書きは conditions.py が行う
      presets/<名前>.json         プリセット
      order.json                  一覧の並び順と、手順のフォルダーと非表示
      sent/<id>.json, <id>.bin    装置へ送った手順のコピーと、送ったバイト列
      sent/sources/<id>/          実行を開始した時点の手順、フレーム表、条件
      build/<名前>.bin            変換の結果を書き出した物。転送には使わない
      logs.jsonl, logs-*.jsonl    ログ。書き込み中の物と、古い世代
      padcue.json                 登録情報、本体の名前、Windows の通知を出す場面
      runstate.json               動いているグループの記録。読み書きは coupler.py が行う
      gui_server.json             動いている操作画面のサーバーの目印。書くのは gui.py
"""
from __future__ import annotations

import contextlib
import copy
import csv
import hashlib
import io
import json
import os
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, binfmt
from .conditions import ConditionStore
from .dsl import CYCLE_HEAD
from .files import (
    read_text,
    remove_file,
    retry_while_in_use,
    sweep_unfinished_writes,
    write_file,
)
from .frametable import (
    FrametableError,
    check_header,
    fit_cells,
    frametable_failure,
    load_frametable,
)
from .lapplan import LapsLeft
from .names import (
    ORDER_NAME,
    apply_order,
    name_key,
    read_order,
    refuse_taken,
    validate_label,
    validate_name,
)
from .presets import PresetStore
from .procfmt import (
    FORMAT_VERSION,
    NEST_TEXT,
    ProcedureError,
    compile_procedure,
    format_problem,
    is_int,
    nest_too_deep,
)
from .procmodel import StartPos, start_pos
from .procrefs import block_refs, blocks, condition_whats, refs_of
from .runlog import RunLog
from .sent import SentStore
from .words import as_json, error_text, sentences

CONFIG_NAME = "padcue.json"


def padcue_root() -> Path:
    """padcue のフォルダー(pctool の 1 つ上)。プログラムの置かれている場所で決まる。"""
    return Path(__file__).resolve().parents[2]


def default_root() -> Path:
    """プロジェクトのフォルダーの既定。padcue のフォルダーの data である。

    本体とデータを 1 つのフォルダーに置くツールの普通の形に合わせる(VS Code の
    ポータブル版は、本体の横の data にすべてを置く)。更新では data を新しい
    padcue のフォルダーへコピーするだけで済み、データがソースと混ざらない。
    実行したフォルダーからは決めないので、どこで実行しても同じデータを使う。
    別のフォルダーを使うのは、--project で指定したとき(デモ用の .bat とテスト)
    だけである。
    """
    return padcue_root() / "data"


class StaleServer(ValueError):
    """目印を書いたサーバーは動いているが、使えない(バージョンか場所が違う、答えない)。
    文はそのまま出す。

    「サーバーなし」と扱うと、新しいサーバーの二重起動やコマンドの直結へ進み、同じ
    装置を 2 つのサーバーが動かす。
    """


# /api/whoami の答えを待つ秒数
WHOAMI_TIMEOUT = 2.0
_NOT_ANSWERING = sentences("動いている padcue が応答しません",
                           "しばらく待ってから、もう一度実行してください")


def _same_place(said, root: Path) -> bool:
    """サーバーが答えた場所が root と同じか。表記の違いでは別の場所と判定しない。"""
    try:
        return os.path.samefile(said, root)
    except (OSError, TypeError, ValueError):
        return False


def running_gui_base(root: Path) -> str | None:
    """稼働中の画面の所在。目印(gui_server.json)だけでは生死が分からないので応答で確認する。

    確認するのは、目印を書いたサーバー自身が答えているかである。サーバーが異常終了して
    目印が残り、同じポートで別のプロジェクトの画面が動いていると、ポートが答える
    だけでは区別できず、そのプロジェクトに登録した別の装置を動かしてしまう。
    目印に書いた起動ごとのトークンと、サーバーの答えを照合する。トークンが違えば、
    目印を書いたサーバーは動いていないので None を返す。

    トークンが同じでも、更新の前のサーバーが動いていることや、フォルダーごとコピー
    した目印が前のフォルダーのサーバーを指していることがある。バージョンと場所が
    違えば StaleServer を投げる。答えに欄がないときも同じである。

    接続できたのに答えない(PC が重い、サーバーが条件の判定で埋まっている)ときも
    StaleServer を投げる。いないとみなすと、コマンドは装置へ直結し、サーバーが
    動いているのに装置の記録を直接読み(読み出す側が 2 つになる。coupling.md D5)、
    グループに属さない実行を始める。いないとみなすのは、接続を拒否されたとき
    (ポートで誰も待っていない)と、答えたのが目印を書いたサーバーでないときだけ
    である。
    """
    marker = root / "gui_server.json"
    if not marker.is_file():
        return None
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        token = str(info.get("token") or "")
        base = f"http://127.0.0.1:{int(info['port'])}"
        with urllib.request.urlopen(base + "/api/whoami",
                                    timeout=WHOAMI_TIMEOUT) as r:
            said = json.loads(r.read())
        if not token or said.get("token") != token:
            return None
    except TimeoutError:
        # 接続した後の答えの待ちだけが、ここへ素の TimeoutError で来る。接続の
        # 時間切れは URLError に包まれて下へ行く(Windows は、誰も待っていない
        # ポートへの接続を数秒やり直してから拒否するので、時間切れになりうる)
        raise StaleServer(_NOT_ANSWERING) from None
    except Exception:   # noqa: BLE001  古い目印・別のサーバーは無視する
        return None
    if said.get("version") != __version__ or not _same_place(said.get("root"), root):
        raise StaleServer(
            "別のバージョンか別のフォルダーの padcue が動いています。その padcue を"
            "終了してから開いてください。\n"
            "  終了するには: 動いている padcue のウィンドウで Ctrl+C を押す")
    return base


# 装置の登録情報(padcue.json)の形式のバージョン。devices が唯一の正で、1 台目を別の
# キーにコピーして持つようなことはしない(同じ事実が 2 か所にあると必ずずれる)
CONFIG_FORMAT = 1


class ConfigError(ValueError):
    """登録情報のファイル(padcue.json)を読めない。文はそのまま画面とコマンドに出す。

    padcue.json は手で編集することがある(coupling.md D1)。壊れたのは利用者の
    ファイルなので、どのファイルの何行目かを示す。
    """


# 通知する場面。文面は notify.py が持つ。ここにあるのは、
# Windows の通知を出す場面を padcue.json に記録するためである
NOTIFY_KINDS = ("done", "error", "await", "gone")


def needs_server(awaits: list) -> bool:
    """実行に操作画面のサーバーが必要な手順か(変換の結果の awaits で判定する)。

    スクリプト、条件、選択肢のスクリプトは PC が実行して判定する。時間切れのない
    選択待ちは、PC から選ばないと先へ進まない。時間切れだけで進む分岐と、1 本で
    開始するときの合流点は、装置だけで進む。
    """
    return any(a.get("script") or any(a.get("conds") or [])
               or any(a.get("guard_scripts") or []) or not a.get("on_timeout")
               for a in awaits)


def _plain_int(v: str) -> str:
    """整数として読める値を、普通の書き方(str(int(v)))にする。読めなければそのまま。"""
    try:
        return str(int(v))
    except ValueError:
        return v


def _first_sentence(text: str) -> str:
    """文の最初の 1 文(句点を除く)。「」の中の句点(名前の一部)では切らない。"""
    depth = 0
    for i, ch in enumerate(text):
        if ch == "「":
            depth += 1
        elif ch == "」":
            depth = max(0, depth - 1)
        elif ch == "。" and depth == 0:
            return text[:i]
    return text


@dataclass
class BuildResult:
    name: str
    blob: bytes
    total_frames: int
    events: int
    warnings: list
    labels: list
    precondition: str
    resume_points: list = field(default_factory=list)
    awaits: list = field(default_factory=list)
    # 複製と回番号の対応表。形は dsl.Compiled.lap_map と同じ
    lap_map: list = field(default_factory=list)
    # 区切りの表。形は dsl.Compiled.marks と同じ。装置が返す
    # 「最後に通った区切り」のレコード番号を、利用者に見せる言葉へ戻す
    marks: list = field(default_factory=list)
    # くり返しの回番号の対応表。形は dsl.Compiled.loop_map と同じ。停止した
    # 位置を囲むくり返しのそれぞれが何回目かを出す
    loop_map: list = field(default_factory=list)
    # 変換のときに指定した周回数。1 以上の数か "infinite" である。装置は周回を
    # 数えないので、進捗の「何周中」はここから出す
    laps: int | str = 1
    # 変換のときに指定したグループのレーンの数。合流点が選択待ちに
    # なるかが変わるので、同じ手順でもレーンの数が違えば別の成果物になる
    lanes: int = 1
    # 周回数で当てはまる周が変わる「対象の回」(最後の回と残り n 回)を解決したか
    # (dsl.compile_procs の finals)。偽は「最後の回を飛ばして終える」の続きである
    finals: bool = True
    # 1 周に通る合流点の名前と個数。開始の前のチェックが使う
    joins: dict = field(default_factory=dict)
    # 周回数のくり返しの外にある合流点の名前と個数。開始の前のチェックが使う
    joins_outside: dict = field(default_factory=dict)
    # 周ごとの合流点の数の表(startpos.join_counts)。開始の前のチェックが、周回数の
    # くり返しを指す「対象の回」で周ごとに数が変わる手順を、周どうしで比べる
    lap_joins: list | None = None
    # 周回数で当てはまる周が変わる「対象の回」と、それを持つ分岐のうち待たずに
    # 決めるもの(dsl.Compiled の同じ名前の欄)。実行中に周回数を変えるときに使う
    final_whens: list = field(default_factory=list)
    final_branches: list = field(default_factory=list)
    # 変換のときに指定した開始位置。None は先頭からである。装置は
    # 開始位置を受け取らず、変換器がそこから続く手順を合成しているので、実行
    # 記録から「どこから始めた実行か」を読むにはこれが必要
    start: StartPos | None = None
    # 開始位置の周(dsl.Compiled の同じ名前の欄)。開始の前のチェックが使う
    start_lap: int | None = 1
    # 終わりのない手順か(dsl.Compiled の同じ名前の欄)。一覧の長さに使う
    endless: bool = False
    # 開始した時点の定義の id と、その中の手順の名前、装置に設定した映像。
    # 送るときに SentStore.remember が入れる
    sources: str = ""
    source_name: str = ""
    video: dict | None = None

    @property
    def hash(self) -> str:
        return binfmt.proc_hash(self.blob)

    @property
    def seconds(self) -> float:
        return self.total_frames / 60.0


class Project:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        # 以下は変換と送った手順のキャッシュである。どれもこのフォルダーの中身から
        # 作るので、Project ごとに持つ。同じ id の手順でも、別のフォルダーでは
        # 名前も送った記録も違う。キャッシュが役に立つのは長く動く操作画面のサーバーで、
        # サーバーはフォルダー 1 つにつき Project を 1 つだけ作る
        self._build_cache_lock = threading.Lock()
        # 変換の結果の使い回し。画面は毎秒の状態の中ですべての手順を変換する。
        # そのたびに変換し直すと、手順 9 本を 10 時間動かすだけで 30 万回
        # 変換し、同じ .bin を同じ回数だけ書き直す。キーは (指紋, 名前, 周回数,
        # 開始位置, レーンの数) である
        self._build_cache: dict = {}
        # 変換に失敗した結果のキャッシュ(キーは _build_cache と同じ)。失敗した手順も、
        # 中身が同じなら毎秒変換し直さない
        self._fail_cache: dict = {}
        # 変換した手順の id から名前への対応。周回数は手順の中のくり返しの
        # 回数として入るので、周回数が違えば別の id になる。記録や装置の一覧に
        # 出た id を名前へ戻すのに使う
        self._built_ids: dict[str, str] = {}
        self.logs = RunLog(self)
        self.sent = SentStore(self)
        self.presets = PresetStore(self)
        # 一覧の行に出す、読めないフレーム表と条件の理由(list_problems)。
        # 種類ごとに、名前から (リビジョン, 理由) を探す
        self._problems: dict[str, dict[str, tuple[str, str]]] = {}

    # ---- 設定 ----

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    def load_config(self) -> dict:
        """装置の登録情報を読む。なければ空の登録情報を返す(書き出さない)。

        読むだけで状態が生まれると、間違ったフォルダーでコマンドを 1 回実行しただけで
        そこに登録情報ができ、以後そちらが使われる。書くのは、実際に記録することが
        起きたとき(登録・装置 ID の記録・改名・Windows の通知の設定)だけ。
        """
        cfg = {}
        if self.config_path.is_file():
            cfg = self._read_config()
        cfg.setdefault("format", CONFIG_FORMAT)
        # 新しい登録情報は空で始まる。既定の接続先を 1 台分置いておくと、その
        # 宛先にいた装置が、人が選んでいないのに動く。装置が複数台あると、
        # どれが動くのか分からないまま実行できてしまう
        cfg.setdefault("devices", [])
        return cfg

    def _read_config(self) -> dict:
        """padcue.json を読み、形を確認する。読めなければ ConfigError。

        確認するのは、全体がオブジェクトで、バージョン(schema)が読めるバージョンで、
        devices がオブジェクトの配列で、その項目の id・name・host が文字列、
        port が 1〜65535 の整数であること。どれも装置へつなぐ所が使うので、誤りを
        「予期しないエラー」にしない。欄があれば null も確かめる(読む所は欄がない
        ときだけ既定にする)。os_notify は場面ごとの true か false、consoles は本体の
        識別子から名前(文字列)が分かるオブジェクトであること。どちらも毎秒の状態が
        読むので、形が違うと画面全体が状態を受け取れなくなる。video の誤りは使う所が
        扱う(映像がないものとして扱う。coupling.md D1)。
        """
        where = f"登録情報のファイル({CONFIG_NAME})"
        try:
            cfg = json.loads(read_text(self.config_path))
        except json.JSONDecodeError as e:
            # 手順の読み込みの失敗(error_message)と同じ言い方にする
            raise ConfigError(sentences(
                f"{where}が壊れています",
                f"{e.lineno} 行目 {e.colno} 文字目から読み込めません")) from None
        except UnicodeDecodeError:
            raise ConfigError(f"{where}の文字コードが UTF-8 ではありません") from None
        except RecursionError:
            raise ConfigError(f"{where}の形式が正しくありません") from None
        if not isinstance(cfg, dict):
            raise ConfigError(f"{where}の形式が正しくありません")
        got = cfg.get("format", CONFIG_FORMAT)
        # 読めるバージョンより新しい登録情報は、padcue を更新すれば読める(手順と同じ)。
        # 読まずに拒否する。知らない欄を残したまま書き戻すと、新しい
        # バージョンの padcue が書いた中身と一致しなくなる
        if is_int(got) and got > CONFIG_FORMAT:
            raise ConfigError(sentences(
                f"{where}は新しいバージョンの padcue で作成されています",
                "padcue を更新してください"))
        if not is_int(got) or got != CONFIG_FORMAT:
            raise ConfigError(f"{where}の format は {CONFIG_FORMAT} である必要が"
                              f"あります: {as_json(got)}")
        devs = cfg.get("devices", [])
        if not isinstance(devs, list) or not all(isinstance(d, dict) for d in devs):
            raise ConfigError(f"{where}の devices はオブジェクトの配列である必要が"
                              "あります")
        for i, d in enumerate(devs, 1):
            for key in ("id", "name", "host"):
                if key in d and not isinstance(d[key], str):
                    raise ConfigError(f"{where}の devices の {i} 番目の {key} は"
                                      f"文字列である必要があります: {as_json(d[key])}")
            if "port" in d and not (is_int(d["port"]) and 1 <= d["port"] <= 65535):
                raise ConfigError(f"{where}の devices の {i} 番目の port は 1〜65535 の"
                                  f"整数である必要があります: {as_json(d['port'])}")
        if "os_notify" in cfg:
            got = cfg["os_notify"]
            if not isinstance(got, dict):
                raise ConfigError(f"{where}の os_notify はオブジェクトである"
                                  "必要があります")
            for k in NOTIFY_KINDS:
                if k in got and not isinstance(got[k], bool):
                    raise ConfigError(f"{where}の os_notify の {k} は true か false で"
                                      f"ある必要があります: {as_json(got[k])}")
        if "consoles" in cfg:
            got = cfg["consoles"]
            if not isinstance(got, dict):
                raise ConfigError(f"{where}の consoles はオブジェクトである"
                                  "必要があります")
            for k, v in got.items():
                if not isinstance(v, str):
                    raise ConfigError(f"{where}の consoles の {as_json(k)} は文字列で"
                                      f"ある必要があります: {as_json(v)}")
        return cfg

    # 登録情報の読み直し〜書き戻しを直列化する(edit_config)。画面の要求のスレッド、
    # 接続先を追いかける探索、本体の識別子を覚える収集が、それぞれ登録情報を書く。
    # 囲まないと後に書いた方が優先されて片方の変更が消える
    _config_write_lock = threading.Lock()

    @contextlib.contextmanager
    def edit_config(self):
        """登録情報を読み直し、with の中で変えた分を書き戻す。

            with project.edit_config() as cfg:
                cfg["devices"].append(...)

        読んでから書くまでを 1 つのロックで囲む。中で何も変えなかったときと、
        例外で抜けたときは書かない(拒否するときに return で抜けてよい)。装置への
        接続のような待ちは、この外で済ませる。中で待つと、その間ほかの書き込みが
        待たされる。
        """
        with self._config_write_lock:
            cfg = self.load_config()
            before = copy.deepcopy(cfg)
            yield cfg
            if cfg != before:
                self._save_config_locked(cfg)

    def sweep_unfinished_writes(self) -> int:
        """このプロジェクトに残った、書きかけの一時ファイルを消し、その数を返す
        (files.sweep_unfinished_writes)。操作画面は起動のときに片付ける。

        見るのは padcue が書く所だけである。プロジェクトの直下と、手順、フレーム表、
        プリセット、変換結果、送った手順のコピーのフォルダーの直下、条件のフォルダーの
        中(条件ごとのフォルダーと、その画像)。利用者が置いたほかのフォルダーの中は
        見ない。
        """
        n = sweep_unfinished_writes(self.root)
        for sub in ("procedures", "frametables", "presets", "build", "sent"):
            n += sweep_unfinished_writes(self.root / sub)
        return n + sweep_unfinished_writes(ConditionStore(self.root).dir, below=True)

    def os_notify(self) -> dict:
        """Windows の通知を出す場面(この PC の設定)。

        既定はすべてオフである。Windows の通知は、利用者がオンにしてから出す。
        ブラウザーではなく padcue.json に持つのは、トーストを出すのがサーバーで、
        画面を閉じていても出すからである(音とタブへの表示は見る人の設定
        なので、そちらはブラウザーに残す)。
        """
        got = self.load_config().get("os_notify") or {}
        return {k: bool(got.get(k)) for k in NOTIFY_KINDS}

    def save_os_notify(self, want: dict) -> None:
        """Windows の通知を出す場面を記録する。知らない場面は捨てる。"""
        with self.edit_config() as cfg:
            cfg["os_notify"] = {k: bool(want.get(k)) for k in NOTIFY_KINDS}

    def save_config(self, cfg: dict) -> None:
        """登録情報の全体を書く。

        読んでから書くまでの間に、ほかのスレッドが書いた変更は消える。製品の
        コードは edit_config か update_device を使う。これを使うのは、
        登録情報を丸ごと用意するテストとツールと、まだない padcue.json を空の登録情報で
        置く `padcue init` だけである。
        """
        with self._config_write_lock:
            self._save_config_locked(cfg)

    def _save_config_locked(self, cfg: dict) -> None:
        cfg.setdefault("format", CONFIG_FORMAT)
        # 一時ファイル経由で置き換える(files.write_file。書き込み途中で異常終了すると
        # 登録情報が壊れ、全装置の登録をやり直しになる。コマンドと操作画面のサーバーが
        # 同時に書いても、一時ファイルが競合しない)。
        # フォルダーを作るのは、まだないフォルダーを --project に指して装置を登録する
        # 使い方があるため
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        write_file(self.config_path, json.dumps(cfg, ensure_ascii=False, indent=2))

    def update_device(self, cfg: dict, idx: int, **fields) -> bool:
        """登録情報の装置の 1 行を変えて書く。書いたら真を返す。

        cfg は呼び出し元が前に読んだ登録情報で、idx はその中の行の位置である。cfg の
        その行も同じに変える(呼び出し元が見ている値を揃える)。書くのは、読み直した
        登録情報の同じ装置の行だけである。cfg を丸ごと書くと、読んだ後にほかで
        書かれた変更(別のスレッドの探索や、別のコマンドの登録)を消す。

        同じ装置の行は、装置 ID が記録してあれば装置 ID で、なければ名前で探す。
        見つからなければ書かない。読んだ後に、その装置が削除されたか、割り当てを
        解除されたか、名前が変わっている。位置で当てると、別の装置の行を書き換える。
        """
        target = cfg["devices"][idx]
        # 探すのは変える前の値である(装置 ID や名前を変える呼び出し元もいる)
        key, want = (("id", target["id"]) if target.get("id")
                     else ("name", target.get("name")))
        target.update(fields)
        with self.edit_config() as fresh:
            hit = next((d for d in fresh["devices"] if d.get(key) == want), None)
            if hit is None:
                return False
            hit.update(fields)
        return True

    # ---- 並び順(order.json) ----
    # 一覧の並びは利用者がドラッグで決める。並べ方は apply_order にある

    def _order_path(self):
        return self.root / ORDER_NAME

    def _load_order(self) -> dict:
        return read_order(self.root)

    def order_error(self) -> str:
        """order.json があるのに読めないときのメッセージ(読めるか、なければ空)。

        読めないと、フォルダーと非表示が警告なしに消えたように見え、隠していた
        手順がレーンの欄に戻る。画面はこの文をそのまま手順の一覧のそばに出す。
        修正するのは利用者なので、どのファイルかを示す。
        """
        state, _e = self._order_state()
        if not state:
            return ""
        where = f"並び順のファイル({ORDER_NAME})"
        why = {"unreadable": f"{where}を読み込めない",
               "broken": f"{where}が壊れている",
               "shape": f"{where}の形式が正しくない"}[state]
        return sentences(f"{why}ため、フォルダーと非表示の設定を使用できません",
                         "標準の並び順で表示しています")

    def _order_state(self) -> tuple[str, OSError | None]:
        """order.json が読めるか。(状態, 読み出しの例外) を返す。

        状態は、読めるかないなら ""、読み出しに失敗したら "unreadable"(例外も
        返す)、JSON として壊れていれば "broken"、オブジェクトでなければ "shape"。
        置き換えの瞬間の PermissionError は files.read_text が待って読み直す。
        """
        path = self._order_path()
        if not path.is_file():
            return "", None
        try:
            d = json.loads(read_text(path))
        except OSError as e:
            return "unreadable", e
        except (ValueError, RecursionError):
            return "broken", None
        return ("" if isinstance(d, dict) else "shape"), None

    def _write_order(self, d: dict) -> str:
        """order.json を書く。読めないファイルがあれば、消さずにバックアップへ移す。

        一時ファイルに書いてから置き換える(files.write_file)。開いて書き直すと、
        書いている途中の失敗で元の中身も失い、利用者が作ったフォルダーと非表示が
        次の並べ替えで消える。

        バックアップへ移したときは、そのことを伝える文を返す(移さなければ空)。読めない
        間は手順の一覧のそばに理由が出ている(order_error)が、移した後の order.json は
        読めるので、そのメッセージは警告なしに消える。フォルダーと非表示の設定が
        バックアップのファイルに残っていることは、ここで 1 度だけ伝える。

        読み出しに失敗しただけ(ほかのアプリが開いているなど)のときは、移さずに
        その例外のまま上げる。中身は壊れていないので、移すと読めていた設定を
        外し、書かずに済ませるべき変更で並び順を置き換える(呼び出し元の d は、
        読めなかった並び順を空として作っている)。
        """
        path = self._order_path()
        note = ""
        state, failed = self._order_state()
        if failed is not None:
            raise failed
        if state:
            keep = path.with_name("order.json.broken")
            n = 1
            while keep.exists():
                n += 1
                keep = path.with_name(f"order.json.broken{n}")
            retry_while_in_use(lambda: path.replace(keep))
            note = sentences(
                f"読み込めない並び順のファイル({ORDER_NAME})の名前を {keep.name} に"
                "変更しました",
                f"フォルダーと非表示の設定は {keep.name} に残っています")
        write_file(path, json.dumps(d, ensure_ascii=False, indent=1))
        return note

    def save_order(self, kind: str, names: list[str]) -> str:
        """一覧の並び順を書く。戻り値は _write_order のメッセージ。"""
        if kind not in ("procedures", "frametables", "presets", "conditions"):
            raise ValueError(f"未知の並び順の種類: {kind}")
        d = self._load_order()
        d[kind] = list(names)
        return self._write_order(d)

    # ---- 手順の整理(非表示・フォルダー) ----
    # 一覧の並び(procedures・frametables・presets・conditions)と同じ order.json に置く:
    #   hidden: [手順名, ...]                       実行・監視の一覧に出さない
    #   folders: [{name, items: [手順名,...]}] フォルダー(表示専用のまとまり。
    #                                          実体は procedures/ 直下のまま)
    # フォルダーを開いているかは持たない。見る人の好みなので、画面がブラウザーに
    # 覚える(ここに書くと、別のブラウザーの一覧まで開閉する)
    # 表示順は「フォルダー(配列順)→フォルダー外(procedures 順)」、どこにも
    # 含まれていない手順は名前順で末尾(既存の並び順規則の延長)。
    # 存在しない手順名の参照は読み込み時に無視する(改名・削除の古い設定対策)

    def load_proc_org(self) -> dict:
        d = self._load_order()
        # 名前は同一性のキー(name_key)で照合し、手順のファイルの綴りで返す
        # (apply_order と同じ)。完全一致で照合すると、ファイル名の大文字と小文字だけを
        # 変えた手順が、非表示とフォルダーから警告なしに外れる
        by_key = {name_key(n): n for n in self.procedure_names()}

        def here(raw) -> list[str]:
            if not isinstance(raw, list):
                return []
            return [by_key[name_key(n)] for n in raw
                    if isinstance(n, str) and name_key(n) in by_key]
        hidden = here(d.get("hidden"))
        folders = []
        folders_raw = d.get("folders")
        if isinstance(folders_raw, list):
            for f in folders_raw:
                if not isinstance(f, dict):
                    continue
                fname = f.get("name")
                if not isinstance(fname, str) or not fname.strip():
                    continue
                folders.append({"name": fname, "items": here(f.get("items"))})
        return {"folders": folders, "hidden": hidden}

    def save_proc_org(self, folders: list, hidden: list) -> str:
        """フォルダーと非表示を書く。戻り値は _write_order のメッセージ。"""
        seen_names = set()
        norm_folders = []
        used_items = set()
        for f in folders:
            if not isinstance(f, dict):
                raise ValueError("フォルダーの指定が正しくありません")
            try:
                fname = validate_label(f.get("name", ""))
            except ValueError as e:
                raise ValueError(f"フォルダー名: {e}") from None
            if fname in seen_names:
                raise ValueError(f"フォルダー「{fname}」は既にあります")
            seen_names.add(fname)
            items = []
            for n in (f.get("items") or []):
                n = str(n)
                if n in used_items:
                    continue    # 1 手順は最大 1 フォルダー(先に出た方を優先)
                used_items.add(n)
                items.append(n)
            norm_folders.append({"name": fname, "items": items})
        d = self._load_order()
        d["folders"] = norm_folders
        d["hidden"] = [str(n) for n in hidden]
        return self._write_order(d)

    def follow_order(self, kind: str, old: str, new: str | None) -> None:
        """改名(new)・削除(None)を並び順へ追従させる(条件の画面が使う)。"""
        self.update_org_refs(old, new, kind)

    def update_org_refs(self, old: str, new: str | None,
                         kind: str = "procedures") -> None:
        """order.json の中の名前を、改名・削除に追従させる。

        rename_*/delete_* から呼ぶ。kind は並びの種類(procedures・frametables・
        presets・conditions)である。
        並び順はどの種類も名前で位置を覚えているので、追従しないと
        改名した途端に「並び順にない名前」と見なされ、ドラッグで決めた場所を
        失って一覧の末尾へ移る。フォルダー所属・非表示(手順のみ)は名前が
        そのまま意味を持つため、放置すると別物として消えてしまう。
        new が None なら削除(参照を取り除く)、それ以外は改名(名前を差し替え)。
        名前は同一性のキー(name_key)で照らす(一覧の並べ方 apply_order と同じ)。
        """
        d = self._load_order()
        changed = False
        key = name_key(old)

        def follow(lst) -> None:
            nonlocal changed
            if not isinstance(lst, list):
                return
            hits = [i for i, n in enumerate(lst)
                    if isinstance(n, str) and name_key(n) == key]
            for i in reversed(hits):
                if new is None:
                    lst.pop(i)
                else:
                    lst[i] = new
                changed = True

        follow(d.get(kind))
        if kind == "procedures":
            follow(d.get("hidden"))
            folders = d.get("folders")
            if isinstance(folders, list):
                for f in folders:
                    if isinstance(f, dict):
                        follow(f.get("items"))
        if changed:
            self._write_order(d)

    # ---- 手順 ----

    def procedure_names(self) -> list[str]:
        d = self.root / "procedures"
        if not d.is_dir():
            return []
        return apply_order(
            self.root,
            [p.stem for p in d.glob("*.json")], "procedures")

    def frametable_names(self) -> list[str]:
        d = self.root / "frametables"
        if not d.is_dir():
            return []
        return apply_order(self.root, [p.stem for p in d.glob("*.csv")], "frametables")

    # ---- 変換(build/ への書き出しと、変換の使い回し) ----

    def _sources_signature(self, name: str) -> str:
        """変換の使い回しのキーにする指紋。

        その手順の変換に関わるファイルだけから作る。つまり、その手順の
        ファイルと、呼んでいる手順(再帰)のファイルと、使っているフレーム表の
        csv である。全体から作ると、無関係な手順を 1 つ編集するだけですべての手順の
        変換をやり直すことになる。

        中身を読んでキーにする(数 KB の読み込みで、変換と書き出しよりはるかに
        安い)。更新時刻を使わないのは、時刻の粒度の粗いファイルシステムでは、
        変えたのに作り直さないことが起こりうるためである。

        読めないファイルは名前だけを混ぜる。壊れた手順は変換が拒否するので、
        ここで区別する必要はない。
        """
        h = hashlib.blake2b(digest_size=16)
        seen: set[str] = set()
        frametables: set[str] = set()
        todo = [name]
        while todo:
            pname = todo.pop()
            if pname in seen:
                continue
            seen.add(pname)
            h.update(pname.encode("utf-8"))
            # 名前の規則に合わない参照(ValueError)は、ファイルとして開かない。
            # 変換の読み込みが場所つきで拒否する(file-format.md §8-1)
            try:
                raw = self.procedure_path(pname).read_bytes()
            except (OSError, ValueError):
                continue          # 呼び出し先がないのは変換が拒否する
            h.update(raw)
            try:
                # 先頭の BOM は外して読む(files.read_text と同じ)
                doc = json.loads(raw.decode("utf-8").removeprefix("﻿"))
            except (ValueError, RecursionError):
                continue          # 壊れた手順も変換が拒否する
            more_procs, more_frametables, _conds = refs_of(doc)
            todo.extend(more_procs)
            frametables |= more_frametables
        for frametable in sorted(frametables):
            h.update(frametable.encode("utf-8"))
            try:
                h.update(self.frametable_path(frametable).read_bytes())
            except (OSError, ValueError):
                pass
        return h.hexdigest()

    def _write_artifact(self, name: str, blob: bytes,
                        only_if_missing: bool = False) -> None:
        """build/<名前>.bin を置く。

        変換結果の中身を目で確認するためのコピーである。これを読むコードはない
        (転送には、変換したばかりのメモリ上のバイト列を使う)。だから毎秒書き直す
        必要はないが、消えたままにもしない。

        書けなくても(ほかのアプリが開いている、build という名前のファイルがある、
        ディスクが一杯)変換は中止しない。中止すると、一覧の行が手順を読み込めないと
        いう事実と違う理由でエラーになり、送ることも開始もできない。
        """
        out = self.root / "build"
        dst = out / f"{name}.bin"
        if only_if_missing and dst.is_file():
            return
        try:
            out.mkdir(exist_ok=True)
            dst.write_bytes(blob)
        except OSError:
            pass

    def build(self, name: str, laps: int | str | None = None,
              start: StartPos | None = None, lanes: int = 1,
              _sig: str | None = None, write: bool = True,
              finals: bool | LapsLeft = True,
              drawn_laps: int | str | None = None,
              drawn_lap: int = 1) -> BuildResult:
        """手順を変換する。

        laps は周回数、start は実行を始める位置、lanes は一緒に開始するグループの
        レーンの数である。laps を省略すると周回数を決めない変換になり(編集画面・
        一覧・図。dsl.compile_procs の None)、周回数 1 の中身になる。
        装置は周回も開始位置もレーンの数も受け取らない。
        周回数は手順の中のくり返しの回数として入り、開始位置からの実行はその
        位置から続く手順を合成して転送し、合流点は 2 本以上のときだけ選択待ちに
        なる。したがってどれが違っても内容が違い、別の id になる。
        使い回しのキーにも 3 つとも含める。finals と drawn_laps と drawn_lap は
        dsl.compile_procs と同じ意味で、これらもキーに含める。

        名前は開く前に規則で確かめる(file-format.md §2)。確かめないと、
        procedures/ の外のファイルを手順として読み、build/ の外に .bin を書く。
        """
        validate_name(name)
        # 指紋はこの手順の変換に使われるファイルだけから作る。無関係な手順を編集した
        # だけですべてを変換し直さないようにするためである。呼び出し元が作った
        # 指紋があればそれを使う(毎秒の状態で同じファイルを読み直さない)
        sig = _sig or self._sources_signature(name)
        with self._build_cache_lock:
            hit = self._build_cache.get(
                (sig, name, laps, start, lanes, finals, drawn_laps, drawn_lap))
        if hit is not None:
            # 使い回すときも成果物は置いておく(消されていたら書き戻すだけ。
            # stat 1 回で済むので毎秒呼ばれても負担にならない)
            if write:
                self._write_artifact(name, hit.blob, only_if_missing=True)
            return hit
        c = compile_procedure(self.root, name, laps, start, lanes,
                              finals=finals, drawn_laps=drawn_laps,
                              drawn_lap=drawn_lap)
        result = self._result_of(name, c, laps, start, lanes, finals)
        # 編集画面の図のための変換(2 本の合流点つき)は書かない。成果物は、
        # 目で確認するための 1 本の変換の中身である
        if write:
            self._write_artifact(name, result.blob)
        with self._build_cache_lock:
            # キーに指紋が入っているので、古いものは使われない。ただし溜め続けると
            # 編集のたびに増えるため、同じ手順の古い指紋のものだけ捨てる
            # (ほかの手順の分は、その手順の指紋が変わるまで残る)
            self._build_cache = {
                k: v for k, v in self._build_cache.items()
                if k[1] != name or k[0] == sig}
            self._build_cache[(sig, name, laps, start, lanes, finals, drawn_laps,
                               drawn_lap)] = result
            self._built_ids[result.hash] = name
        return result

    @staticmethod
    def _result_of(name: str, c, laps, start, lanes: int, finals: bool) -> BuildResult:
        """変換の結果を、画面と転送が使う形にする。"""
        return BuildResult(
            name=name, blob=binfmt.encode(c.events, c.total_frames),
            total_frames=c.total_frames,
            events=len(c.events),
            # line は画面がブロックを探す番号、shown は人に見せる番号である
            # (dsl.LintWarning)
            warnings=[{"line": w.line, "shown": w.shown, "msg": w.msg,
                       "block": w.block, "path": list(w.path)}
                      for w in c.warnings],
            labels=[{"frame": f, "text": t} for f, t in c.labels],
            precondition=c.precondition,
            resume_points=c.resume_points,
            awaits=c.awaits,
            joins=c.joins,
            joins_outside=c.joins_outside,
            lap_joins=c.lap_joins,
            final_whens=c.final_whens,
            final_branches=c.final_branches,
            lap_map=c.lap_map,
            marks=c.marks,
            loop_map=c.loop_map,
            laps=1 if laps is None else laps,
            lanes=lanes,
            finals=finals,
            start=start,
            start_lap=c.start_lap,
            endless=c.endless,
        )

    def build_doc(self, name: str, doc: dict, lanes: int = 1
                  ) -> tuple[BuildResult | None, dict | None]:
        """保存していない手順の文書を変換する(編集画面の図)。書き出さず、覚えない。

        呼び出し先の手順とフレーム表は、保存した中身を読む。失敗したら詳細を返す。
        名前は build と同じく規則で確かめる。
        """
        try:
            validate_name(name)
            c = compile_procedure(self.root, name, None, None, lanes, docs={name: doc})
        except (ProcedureError, ValueError, RecursionError) as e:
            return None, self._error_detail(name, e)
        return self._result_of(name, c, None, None, lanes, True), None

    # ---- 変換の結果を読む(区切りの表、周の対応表、失敗の文) ----

    @staticmethod
    def mark_at(marks: list, index) -> dict | None:
        """レコード番号から区切りを探す。

        同じレコードに 2 つの区切りがあることがある。名前を持つ方(ラベルや
        合流点)を先に返す。名前でしか利用者に示せないからである。
        """
        if not isinstance(index, int):
            return None
        got = [m for m in marks or [] if m.get("index") == index]
        if not got:
            return None
        return sorted(got, key=lambda m: (not m.get("name"),
                                          m.get("kind", "")))[0]

    def start_loops_match(self, name: str, start: dict) -> bool:
        """停止した位置の回番号が、停止したときと同じくり返しを指しているか。

        turns は回番号を外側から並べるだけなので、停止した後に手順を修正して
        くり返しで包んだり、周回数のフラグをクリアしたりすると、変換は通ったまま
        別のくり返しの回として読まれ、別の回から始まる。停止したときに回番号が
        指していたくり返しの識別子(start の loops)を、今の手順の区切りの表と
        照合する。loops を持たない位置(一覧から選んだ位置)は照合しない。

        照らすのは、その位置から始める変換の区切りの表である。周回数を決めない
        変換は 1 周目に選ばれる選択肢しか変換しないので、2 周目より後に選ばれる
        選択肢の中の位置が表にない。

        build/ には書かない。開始のときは、実行を開始した時点の保存物(手順、
        フレーム表、条件だけを持つ)に対して呼ぶ。
        """
        if "loops" not in start:
            return True
        try:
            pos = start_pos(start.get("path") or [], start.get("block") or "",
                            start.get("turns") or [])
        except (TypeError, ValueError):
            return False
        # 周回数は、その位置の周に達する数にする(positions.still_startable と同じ)
        r = self.build_safe(name, max([1, *pos.turns]), pos, write=False)[0]
        if r is None:
            return False
        want = list(start.get("loops") or [])
        path = [str(x) for x in start.get("path") or []]
        block = str(start.get("block") or "")
        for m in r.marks:
            if list(m.get("path") or []) != path or m.get("block") != block:
                continue
            if m.get("kind") == "lap":
                # 周の境はくり返し自身の回を指す。周回数のくり返しは一番外側にある
                return want == [block] and not m.get("loops")
            return list(m.get("loops") or []) == want
        return False

    def error_message(self, name: str, e: Exception) -> str:
        """読み込みの失敗を、利用者に見せる文にする(GUI も使う)。"""
        if isinstance(e, json.JSONDecodeError):
            return sentences(f"手順「{name}」のファイルが壊れています",
                             f"{e.lineno} 行目 {e.colno} 文字目から読み込めません")
        if isinstance(e, UnicodeDecodeError):
            return f"手順「{name}」の文字コードが UTF-8 ではありません"
        # ファイルの場所(内部のパス)と OS の文は出さない
        if isinstance(e, FileNotFoundError):
            return f"手順「{name}」が見つかりません"
        if isinstance(e, PermissionError):
            return sentences(f"手順「{name}」のファイルを読み込めません",
                             "ほかのアプリで開いていないか確認してください")
        if isinstance(e, OSError):
            return f"手順「{name}」のファイルを読み込めません"
        if isinstance(e, RecursionError):
            # 1 本ずつは上限(procfmt.NEST_MAX)に収まっていても、別の手順の
            # 呼び出しを長くつなぐと、変換が再帰の上限に当たる
            return sentences(f"手順「{name}」を変換できません",
                             "別の手順の呼び出しが深すぎます",
                             "呼び出しの入れ子を浅くしてください")
        return str(e)

    @staticmethod
    def lap_of(lap_map: list, last_mark: dict | None) -> int | None:
        """最後に通った区切りから、何周目かを出す。

        装置は周回を数えない。周回数は手順の中のくり返しの回数として入って
        いるので、装置が返すのは「最後に通った区切りのレコード番号」と、その
        時点の各カウンターの値である。変換のときに作った対応表と照合して、
        先頭から通した回番号に戻す。

        特定できないときは None を返す。区切りをまだ 1 つも通っていない、
        対応表にないレコードで停止した、のどちらかである。
        """
        if not lap_map or not last_mark:
            return None
        index = last_mark.get("index")
        if not isinstance(index, int):
            return None
        counters = list(last_mark.get("counters") or [])
        for item in lap_map:
            if not (item["start"] <= index < item["end"]):
                continue
            lap = item["first"]
            for c in item.get("counters") or []:
                num = c["counter"]
                left = counters[num] if num < len(counters) else 0
                lap += (c["total"] - left) * c["step"]
            return lap
        return None

    @staticmethod
    def lap_exit(lap_map: list, last_mark: dict | None) -> str:
        """最後に通った区切りが、周回数のくり返しを抜けた所かどうか。

        "last" は抜けた所の後ろに終端しかないもの(実行するものは残っていない)、
        "loop" は後ろにまだブロックがあるものである。どちらでもなければ ""
        を返す。抜けた所で停止したなら、指定した周はすべて終えている。
        """
        if not lap_map or not last_mark:
            return ""
        index = last_mark.get("index")
        if not isinstance(index, int):
            return ""
        for item in lap_map:
            if item["start"] <= index < item["end"]:
                kind = item.get("exit")
                return kind if kind in ("last", "loop") else ""
        return ""

    def build_safe(self, name: str, laps: int | str | None = None,
                   start: StartPos | None = None, lanes: int = 1,
                   finals: bool | LapsLeft = True, write: bool = True,
                   drawn_laps: int | str | None = None, drawn_lap: int = 1
                   ) -> tuple[BuildResult | None, str]:
        """変換し、失敗したらエラーメッセージを返す(画面が使う)。"""
        r, detail = self.build_detail(name, laps, start, lanes, write=write,
                                      finals=finals, drawn_laps=drawn_laps,
                                      drawn_lap=drawn_lap)
        return r, (detail or {}).get("full", "")

    def _error_detail(self, name: str, e: Exception) -> dict:
        """変換の失敗の詳細(画面がブロックを探して文を出すための形)。

        {"full": 全文, "msg": 文だけ, "line": 通し番号, "path": パス,
        "block": 識別子}。失敗が呼び出し先の中なら、line は空にし、path と block
        には、開いている手順の中でそこへ入っていく呼び出しのブロックのパスと
        識別子を入れる。文には呼び出し先の名前を入れる。
        """
        full = self.error_message(name, e)
        if not isinstance(e, ProcedureError):
            return {"full": full, "msg": full, "line": None, "path": "",
                    "block": ""}
        if e.callee:
            # 輪の文は経路が名前を示すので、呼び出し先の名前を前に付けない
            # (「…: 「B」の中: …輪になっています(A → B → A)」とコロンが重なる)。
            # それ以外は、中の文の原因(最初の 1 文)に、呼び出し先を開いて修正することを
            # 続ける。中の文の対処(「別の手順を選択してください」)は呼び出し先の
            # ブロックのことで、ここに出すと、このブロックの選び直しと読める。対処は、
            # 呼び出し先を開けば、その手順の失敗の文として出る
            cycle = e.msg.startswith(CYCLE_HEAD)
            msg = (e.msg if cycle
                   else sentences(f"「{e.callee}」の中: {_first_sentence(e.msg)}",
                                  f"「{e.callee}」を開いて修正してください"))
            # 文が開いて修正するよう求める手順(実行・監視のレーンの操作が、呼び出し元
            # ではなくその手順を開く)。輪の文は、開く手順を示さない
            return {"full": full, "msg": msg,
                    "line": None, "path": (e.via or "").split(" (")[0],
                    "block": e.block, **({} if cycle else {"callee": e.callee})}
        return {"full": full, "msg": e.msg, "line": e.line, "path": e.path,
                "block": ""}

    def build_detail(self, name: str, laps: int | str | None = None,
                     start: StartPos | None = None, lanes: int = 1,
                     _sig: str | None = None, write: bool = True,
                     finals: bool | LapsLeft = True,
                     drawn_laps: int | str | None = None, drawn_lap: int = 1
                     ) -> tuple[BuildResult | None, dict | None]:
        """変換し、失敗したらその詳細を返す(編集画面が使う)。

        詳細は {"full": 全文, "msg": 文だけ, "line": 通し番号, "path": パス,
        "block": 識別子} である(_error_detail)。画面は line か path
        からブロックを探し、その要約を付けて出す。全文はファイル名やパスを
        含むので、画面にはそのまま出さない。
        """
        # 失敗も成功と同じキーで覚える。失敗した手順を毎秒変換し直さない
        sig = _sig or self._sources_signature(name)
        key = (sig, name, laps, start, lanes, finals, drawn_laps, drawn_lap)
        with self._build_cache_lock:
            failed = self._fail_cache.get(key)
        if failed is not None:
            return None, dict(failed)
        try:
            return self.build(name, laps, start, lanes, _sig=sig,
                              write=write, finals=finals,
                              drawn_laps=drawn_laps, drawn_lap=drawn_lap), None
        except (ProcedureError, ValueError, RecursionError) as e:
            # 中身から決まる失敗だけを覚える(中身が同じなら何度でも同じに失敗する)
            detail = self._error_detail(name, e)
            with self._build_cache_lock:
                # 同じ手順の古い指紋の失敗は捨てる(build のキャッシュと同じ)
                self._fail_cache = {k: v for k, v in self._fail_cache.items()
                                    if k[1] != name or k[0] == sig}
                self._fail_cache[key] = detail
            return None, dict(detail)
        except OSError as e:
            # 書き込み先が一時的に使えないなど、中身に関係のない失敗は覚えない。
            # 覚えると、原因が解消しても手順がエラーのままになる
            return None, self._error_detail(name, e)

    # ---- リビジョンと、手順の状態(画面が「元が変わったか」を知るため) ----

    def file_rev(self, kind: str, name: str) -> str:
        """1 つのファイルの中身のリビジョン(なければ "")。

        編集画面は開いた時点のリビジョンを覚え、これが変わったら「ほかで変わった」と
        知る。保存のときも比べる(ほかの変更を黙って上書きしない)。
        条件は condition.json(映像・範囲・しきい値・基準画像の一覧)のリビジョンである。
        名前の規則に合わない名前のファイルはないのと同じである(一覧に出さない。
        names.apply_order)。
        """
        where = {"procedure": self.procedure_path,
                 "frametable": self.frametable_path,
                 "condition": lambda n: self._conditions().doc_path(n)}.get(kind)
        if where is None:
            raise ValueError(f"種類が違います: {kind}")
        try:
            raw = where(name).read_bytes()
        except (OSError, ValueError):
            return ""
        return hashlib.blake2b(raw, digest_size=12).hexdigest()

    def uses_of(self, name: str) -> tuple[set[str], set[str]]:
        """手順が使う手順(自分を含む)とフレーム表の名前(呼び出し先を再帰で)。"""
        procs: set[str] = set()
        frametables: set[str] = set()
        todo = [name]
        while todo:
            pname = todo.pop()
            if pname in procs:
                continue
            procs.add(pname)
            try:
                doc = self.load_procedure_doc(pname)
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            more_procs, more_frametables, _conds = refs_of(doc)
            todo.extend(more_procs)
            frametables |= more_frametables
        return procs, frametables

    def has_input(self, name: str, doc: dict | None = None) -> bool:
        """手順のどこかに、コントローラーの入力を出すブロックがあるか(呼び出し先も含む)。

        図は 1 周目の 1 つの経路だけを変換して描くので、描いた経路に入力がないとき、
        手順にないのか、描いていない経路(ほかの選択肢、2 周目から通る選択肢)に
        あるのかを、変換したレコードだけでは区別できない。doc は保存していない
        中身(編集画面の図)で、なければ name の手順を読む。入力を出すのは、押して
        離す・押し続ける・中央でないスティック・0 でないジャイロ・フレーム表である。
        無効にしたブロックは数えない。
        """
        seen: set[str] = set()
        todo: list[tuple[str, dict | None]] = [(name, doc)]
        while todo:
            pname, pdoc = todo.pop()
            if pname in seen:
                continue
            seen.add(pname)
            if pdoc is None:
                try:
                    pdoc = self.load_procedure_doc(pname)
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
            body = pdoc.get("body") if isinstance(pdoc, dict) else None
            for nd in blocks(body, enabled_only=True):
                kind = nd.get("type")
                if kind in ("press", "hold", "frametable"):
                    return True
                if kind == "stick" and (nd.get("x") or nd.get("y")):
                    return True
                if kind == "gyro" and (nd.get("gp") or nd.get("gy") or nd.get("gr")):
                    return True
                if kind == "call" and isinstance(nd.get("ref"), str):
                    todo.append((nd["ref"], None))
        return False

    def _conditions(self):
        """条件の保存場所(conditions.ConditionStore)。

        条件の一覧と保存場所の決まりは ConditionStore だけが持つ。ここで
        conditions/ を見に行くと、決まりが 2 か所になる。
        """
        return ConditionStore(self.root)

    def missing_conditions(self, awaits, have: set | None = None) -> list[str]:
        """選択肢の「選択条件」が指す条件のうち、この PC にないもの。

        have は、ある条件の名前(毎秒の状態では 1 回だけ数えて渡す)。名前は
        同一性のキー(name_key)で照合し、指している綴りで返す。開始のときの保存と
        判定は条件をパスで開くので、Windows では綴りの違う名前でも見つかって使われる。
        """
        want = {c for a in (awaits or []) for c in (a.get("conds") or []) if c}
        if not want:
            return []
        if have is None:
            have = set(self._conditions().names())
        keys = {name_key(c) for c in have}
        return sorted(c for c in want if name_key(c) not in keys)

    def condition_warnings(self, awaits, have: set | None = None) -> list[dict]:
        """ない条件を指す選択肢の警告(変換の警告と同じ形)。

        条件は PC ごとの物なので、なくても変換は通る(その選択肢が自動では
        選ばれないだけで、人は選べる)。失敗ではなく警告として、変換の警告と
        同じ場所・同じ形で出す。
        """
        missing = set(self.missing_conditions(awaits, have))
        out, seen = [], set()
        for a in awaits or []:
            names = a.get("names") or []
            for i, c in enumerate(a.get("conds") or []):
                if not c or c not in missing:
                    continue
                arm = (names[i] if i < len(names) else "") or f"選択肢 {i + 1}"
                key = (a.get("block") or "", c, arm)
                if key in seen:
                    continue
                seen.add(key)
                # 選択肢の名前は、画面が場所(「N 番目(分岐)の選択肢「X」」)として
                # 前に付ける(選択肢の中の失敗と同じ形)
                out.append({"line": 0, "block": a.get("block") or "",
                            "path": [], "arm": arm,
                            "msg": f"条件「{c}」が見つかりません。"
                                   "この選択肢は自動で選択されません。"})
        return out

    def proc_status(self, name: str, have: set | None = None) -> dict:
        """手順 1 本の状態: 変換の結果、失敗の詳細、警告、リビジョン。

        rev はこの手順の図と一覧のリビジョンで、手順本体・呼び出し先・フレーム表の
        中身と、ない条件の名前から作る。画面はこれが変わったときだけ図や
        選択肢を作り直す(装置へ送る中身の id では、ラベル名や前提条件だけの
        変更を見つけられない)。have は、ある条件の名前(missing_conditions と同じ)。
        """
        sig = self._sources_signature(name)
        r, detail = self.build_detail(name, _sig=sig)
        warnings = []
        missing = []
        if r is not None:
            if have is None:
                have = set(self._conditions().names())
            missing = self.missing_conditions(r.awaits, have)
            warnings = list(r.warnings) + self.condition_warnings(r.awaits, have)
        h = hashlib.blake2b(digest_size=12)
        h.update(sig.encode("utf-8"))
        h.update(("|" + ",".join(missing)).encode("utf-8"))
        return {"result": r, "error_at": detail, "warnings": warnings,
                "rev": h.hexdigest(), "file_rev": self.file_rev("procedure", name)}

    def check_doc(self, name: str, doc: dict, lanes: int = 1) -> dict:
        """保存していない手順の文書を変換して、失敗と警告だけを返す。

        送った手順のコピーも build/ への書き出しも使わない(保存していない中身だから)。
        編集画面は編集のたびにこれを呼び、失敗と警告の表示を今の中身に保つ。
        名前は build と同じく規則で確かめる。
        """
        try:
            validate_name(name)
            c = compile_procedure(self.root, name, None, None, lanes,
                                  docs={name: doc})
        except (ProcedureError, ValueError, RecursionError) as e:
            # RecursionError は、別の手順の呼び出しを長くつないだ形(build_detail と
            # 同じ。_error_detail が文にする)
            return {"error_at": self._error_detail(name, e), "warnings": []}
        warnings = [{"line": w.line, "shown": w.shown, "msg": w.msg,
                     "block": w.block, "path": list(w.path)} for w in c.warnings]
        # has_waits は一覧の手順と同じ(guistate)。編集の間の見出しの長さも、
        # 待ちのある手順では下限として出す
        return {"error_at": None, "frames": c.total_frames,
                "seconds": round(c.total_frames / 60.0, 1),
                "has_waits": bool(c.awaits),
                # 終わりのない手順か。一覧の手順と同じ(guistate)。見出しは「無限」と出す
                "endless": c.endless,
                "warnings": warnings + self.condition_warnings(c.awaits)}

    # ---- 編集(GUI から使う読み書き) ----

    def procedure_path(self, name: str) -> Path:
        return self.root / "procedures" / f"{validate_name(name)}.json"

    def frametable_path(self, name: str) -> Path:
        return self.root / "frametables" / f"{validate_name(name)}.csv"

    def load_procedure_doc(self, name: str) -> dict:
        """手順の文書を読む。読めなければ OSError か ValueError。

        入れ子が深すぎる文書(procfmt.NEST_MAX)は、中をたどる前に拒否する。編集画面も
        参照の書き換えも、入れ子を再帰でたどる。
        """
        text = read_text(self.procedure_path(name))
        deep = ValueError(sentences(f"手順「{name}」を読み込めません", NEST_TEXT))
        try:
            doc = json.loads(text)
        except RecursionError:
            raise deep from None
        if nest_too_deep(doc):
            raise deep
        return doc

    def save_procedure_doc(self, name: str, doc: dict) -> None:
        """手順の文書を書く。

        名前はファイル名そのものなので、文書の中身とは照合しない
        (file-format.md §2)。書く前に確認するのは、これが手順の
        文書であることと、このツールが読み書きするバージョンであることだけである。ほかの
        欄の形は読み込みが拒否する(file-format.md §8-1)。書きかけの中身も
        保存できるようにしてある。
        """
        path = self.procedure_path(name)     # 名前のチェックもここで行う
        if not isinstance(doc, dict) or doc.get("kind") != "procedure":
            raise ValueError("手順ファイルの形式ではありません")
        problem = format_problem(doc.get("format"))
        if problem:
            raise ValueError(problem)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_file(path, json.dumps(doc, ensure_ascii=False, indent=2))

    def frametable_problem(self, name: str) -> str:
        """フレーム表を読めないときの理由(読めれば空)。

        手順の変換と同じ読み方(file-format.md §9-2)で確認するので、ここで拒否する
        フレーム表を使う手順は実行できない。編集画面は、値の誤りがあっても開ける。
        列名の誤りと、ヘッダーより多い値のある行は開けない(load_frametable_grid)。
        """
        try:
            load_frametable(
                name, self.frametable_path(name).read_text(encoding="utf-8"))
        except (FrametableError, OSError, UnicodeDecodeError) as e:
            return frametable_failure(name, e)
        return ""

    def list_problems(self, kind: str, revs: dict[str, str]) -> dict[str, str]:
        """一覧の行に出す、読めないフレーム表か条件の理由。名前で探し、読める物は
        含まれない。

        kind は "frametable" か "condition"、revs は名前からリビジョン(file_rev)が分かる
        表である。一覧は毎秒作るので、リビジョンが変わった物だけを読み直す。条件は、
        基準画像のファイルが消えたことも理由にするので、画像のフォルダーの中身が
        変わったときも読み直す(ConditionStore.problem_key)。
        """
        if kind == "frametable":
            read, key_of = self.frametable_problem, (lambda _name, rev: rev)
        else:
            store = self._conditions()
            read, key_of = store.problem, store.problem_key
        old = self._problems.get(kind, {})
        new = {}
        for name, rev in revs.items():
            key = key_of(name, rev)
            hit = old.get(name)
            new[name] = (hit if hit is not None and hit[0] == key
                         else (key, read(name)))
        # 置き換えるので、消えた物のキャッシュは残らない
        self._problems[kind] = new
        return {name: why for name, (_rev, why) in new.items() if why}

    def load_frametable_grid(self, name: str) -> dict:
        """CSV を編集しやすい形(ヘッダーと、行の二次元配列)で返す。

        値の誤りがあっても返す(画面で開いて修正できる)。列名の誤り(空・フレーム表に
        ない・2 つ目の同じ名前)と、ヘッダーより多い値のある行は、読み込みと同じチェック
        (frametable.check_header・fit_cells)で FrametableError にする。画面はその
        値を表の列に当てられず、保存すると値が警告なしに消える。列は画面では追加できない
        ので、開かずに理由を返す。
        """
        text = self.frametable_path(name).read_text(encoding="utf-8").lstrip("\ufeff")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            return {"name": name, "header": ["A"], "rows": [[""]]}
        header = check_header(rows[0])
        # 数の列(ボタンと disabled 以外)の値は、整数として読めるなら普通の書き方に
        # して返す。読み込み(frametable.py)は int で読むので「+100」や全角の数字も
        # 正しい値だが、画面の数の欄はそれを空欄として出す。ボタンと disabled の列は
        # 1・0・空欄だけを読むので、書き換えない(「+1」を 1 にすると、読み込みが拒否
        # する値が画面では正しい値に見える)
        nums = [i for i, h in enumerate(header)
                if h not in binfmt.BUTTONS and h != "disabled"]
        body = []
        # 行の番号はファイルの行(ヘッダーが 1 行目)。読み込み(load_frametable)と
        # 同じ数え方
        for lineno, r in enumerate(rows[1:], start=2):
            if not r or (all(c.strip() == "" for c in r) and len(r) < len(header)):
                continue
            cells = fit_cells(r, len(header), lineno)
            cells += [""] * (len(header) - len(cells))
            for i in nums:
                cells[i] = _plain_int(cells[i])
            body.append(cells)
        return {"name": name, "header": header, "rows": body}

    def save_frametable_grid(self, name: str, header: list, rows: list) -> None:
        """編集結果を CSV へ書く。書く前に必ず検証する。"""
        header = [str(h).strip() for h in header if str(h).strip()]
        if not header:
            raise ValueError("列がありません")
        buf = io.StringIO(newline="")
        # csv.writer を使う(1 列だけの空行が空行と区別できなくなるのを避けるため。
        # 単一の空セルは "" と引用して書かれる)
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            cells = [str(c).strip() for c in r][:len(header)]
            cells += [""] * (len(header) - len(cells))
            w.writerow(cells)
        text = buf.getvalue()
        load_frametable(name, text)   # 不正なら例外(保存しない)
        self.frametable_path(name).parent.mkdir(parents=True, exist_ok=True)
        write_file(self.frametable_path(name), text)

    def referrers(self, kind: str, name: str) -> list[str]:
        """その名前を参照している手順の名前。kind は "call"(別の手順)、
        "frametable"(フレーム表)、"condition"(選択肢の「選択条件」の条件)。
        消す前に、どの手順が壊れるかを示すのに使う。"""
        return [u["proc"] for u in self.usages(kind, name)]

    def _readable_docs(self):
        """読み書きできる手順を (名前, 中身) で返す。壊れた手順と別のバージョンは
        飛ばす。
        """
        for pname in self.procedure_names():
            try:
                doc = self.load_procedure_doc(pname)
            except (OSError, ValueError):
                continue
            if (isinstance(doc, dict) and doc.get("kind") == "procedure"
                    and format_problem(doc.get("format")) is None):
                yield pname, doc

    def usages(self, kind: str, name: str) -> list[dict]:
        """その名前を使っている手順と、その中で使っているか所の数。

        画面の「使用している場所」と、消す前の確認が使う。kind は referrers と
        同じ。どのブロックかは、開いた画面が中身をたどって探す(識別子を
        持たないブロックもあるため)。条件は選択肢の「選択条件」が指すので、
        分岐のブロック 1 つを 1 か所と数える(block_refs)。
        名前は同一性のキー(name_key)で比べる(参照の書き換え _rewrite_refs と同じ)。
        綴りの違う参照(手で書いた "retry" が手順「Retry」を呼ぶ)も、Windows では
        変換も実行も通るので、使っている場所である。
        """
        out = []
        want = (kind, name_key(name))
        for pname, doc in self._readable_docs():
            n = sum(1 for nd in blocks(doc.get("body"))
                    if want in {(k, name_key(r)) for k, r in block_refs(nd)})
            if n:
                out.append({"proc": pname, "count": n})
        return out

    def callers(self, *names: str) -> list[str]:
        """それらの手順を、呼び出しをたどって使っている手順。直接呼ぶ手順が先に並ぶ。

        手順を消すと、直接呼ぶ手順だけでなく、それを呼ぶ手順も実行できなくなる。
        フレーム表を消すと、それを使う手順(names に渡す)を呼ぶ手順も同じである。
        削除の確認がこれを挙げる(「使用している場所」は直接使う物だけを挙げる)。
        名前は同一性のキー(name_key)で比べる(usages と同じ)。呼び出しが循環して
        いる手順(変換できない手順)でも、同じ手順を 2 度挙げず、names の手順そのものも
        挙げない。
        """
        calls = {pname: {name_key(ref) for kind, ref in
                         (r for nd in blocks(doc.get("body")) for r in block_refs(nd))
                         if kind == "call"}
                 for pname, doc in self._readable_docs()}
        seen = {name_key(n) for n in names}
        frontier = set(seen)
        out: list[str] = []
        while frontier:
            found = [p for p, refs in calls.items()
                     if name_key(p) not in seen and refs & frontier]
            out += found
            frontier = {name_key(p) for p in found}
            seen |= frontier
        return out

    def usage_and_joins(self) -> tuple[dict[tuple[str, str], int], list[str]]:
        """使用している場所の数と、合流点に付けた名前。すべての手順を 1 度だけ読む。

        使用している場所の数は、種類と名前ごとの数である。数え方は「使用している
        場所」の一覧と同じで、usages のか所の数の合計に、手順ならそれを直接
        使っているプリセットのレーンの数を足したものである(名前ごとに usages を
        呼ぶと、名前の数だけすべての手順を読み直す)。キーは (種類, name_key(名前))
        で、探す側も name_key で探す(usages と同じく綴りの違う参照も数える)。

        合流点の名前は、手順の一覧の順、手順の中の順に、重ねずに並べる。合流点は、
        名前が 1 文字でも違うと(大文字と小文字も)相手と対応しない。編集の画面は、
        これを名前の欄の候補に出す。読めない手順は飛ばす。操作画面は毎秒両方を
        使うので、1 度の読み込みで集める。
        """
        out: dict[tuple[str, str], int] = {}
        joins: dict[str, None] = {}

        def add(key):
            out[key] = out.get(key, 0) + 1
        for _pname, doc in self._readable_docs():
            for nd in blocks(doc.get("body")):
                # 1 つのブロックの中の綴りの違う同じ名前は、1 か所と数える
                # (usages と同じ)
                for key in {(kind, name_key(ref)) for kind, ref in block_refs(nd)}:
                    add(key)
                name = nd.get("name")
                if nd.get("type") == "join" and isinstance(name, str) and name:
                    joins.setdefault(name, None)
        for _fname, _lane, x in self.presets._preset_lanes():
            add(("call", name_key(x["proc"])))
        return out, list(joins)

    def rename_condition_refs(self, old: str, new: str) -> int:
        """条件の名前を変えたとき、選択肢の「選択条件」が指す名前を追従させる。"""
        return self._rewrite_refs("condition", old, new)

    def _rewrite_refs(self, kind: str, old: str, new: str) -> int:
        """すべての手順の中の参照を書き換え、書き換えた手順の数を返す。

        kind は "call"(別の手順)、"frametable"(フレーム表)、"condition"(選択肢の
        「選択条件」が指す条件)である。名前を変えたのに参照が古いままだと、
        変換に失敗するか、別のものを指す。名前の変更と同時に必ず修正する。
        壊れた手順と、読み書きできないバージョンの手順は変えない(_readable_docs)。
        """
        # 綴りの違う参照(大文字と小文字、NFC と NFD)も同じ名前として書き換える
        # (file-format.md §2)。書き換えないと、改名した後に参照が切れる
        changed = 0
        key = name_key(old)

        def same(ref) -> bool:
            return isinstance(ref, str) and name_key(ref) == key

        for pname, doc in self._readable_docs():
            hit = False
            for nd in blocks(doc.get("body")):
                if kind == "condition":
                    for what in condition_whats(nd):
                        if same(what.get("ref")):
                            what["ref"] = new
                            hit = True
                elif nd.get("type") == kind and same(nd.get("ref")):
                    nd["ref"] = new
                    hit = True
            if hit:
                self.save_procedure_doc(pname, doc)
                changed += 1
        return changed

    def rename_procedure(self, old: str, new: str) -> tuple[int, int]:
        """手順の名前を変える。呼んでいる側の参照とプリセットの手順も書き換え、書き
        替えた手順の数とプリセットの数を返す(画面はどちらの件数も示す)。

        名前はファイル名そのものなので、中身は読まずにバイト列のままコピーする
        (file-format.md §2。プリセットの改名と同じ)。読めない手順も名前を
        変えられ、中身は 1 バイトも変わらない。
        """
        old, new = validate_name(old), validate_name(new)
        if old == new:
            return 0, 0
        if not self.procedure_path(old).is_file():
            raise ValueError(f"手順「{old}」が見つかりません")
        refuse_taken(self.procedure_names(), new, old, self.procedure_path(new))
        write_file(self.procedure_path(new), self.procedure_path(old).read_bytes())
        remove_file(self.procedure_path(old))
        remove_file(self.root / "build" / f"{old}.bin")
        self.update_org_refs(old, new)
        self.sent.rename(old, new)
        try:
            presets = self.presets.follow_rename(old, new)
            return self._rewrite_refs("call", old, new), presets
        except OSError as e:
            # 手順の名前はもう変わっていて、同じ改名をやり直せない。古い名前を
            # 呼び出したままの手順と、古い名前のままのプリセットを挙げる(1 つずつ
            # 開いて探させない)
            left = self.referrers("call", old)
            stale = list(dict.fromkeys(
                fname for fname, _lane, x in self.presets._preset_lanes()
                if name_key(x["proc"]) == name_key(old) and x["proc"] != new))
            if not left and not stale:
                raise
            raise ValueError(sentences(
                error_text(e),
                f"次の手順は、変更前の名前「{old}」を呼び出したままです: "
                f"{'、'.join(left)}" if left else "",
                f"次のプリセットは、変更前の名前「{old}」のままです: "
                f"{'、'.join(stale)}" if stale else "")) from e

    def rename_frametable(self, old: str, new: str) -> int:
        """フレーム表の名前を変える。使っている手順の参照も書き換える。

        中身はバイト列のままコピーする(rename_procedure と同じ)。UTF-8 でない表も
        名前を変えられる。
        """
        old, new = validate_name(old), validate_name(new)
        if old == new:
            return 0
        if not self.frametable_path(old).is_file():
            raise ValueError(f"フレーム表「{old}」が見つかりません")
        refuse_taken(self.frametable_names(), new, old, self.frametable_path(new))
        write_file(self.frametable_path(new), self.frametable_path(old).read_bytes())
        remove_file(self.frametable_path(old))
        self.update_org_refs(old, new, "frametables")
        return self._rewrite_refs("frametable", old, new)

    def copy_procedure(self, src: str, new: str) -> None:
        """手順をコピーして別名で作る(中身の参照はそのまま)。

        中身はバイト列のままコピーする(rename_procedure と同じ)。読めない手順も
        複製して、コピーの方で修正できる。
        """
        src, new = validate_name(src), validate_name(new)
        if not self.procedure_path(src).is_file():
            raise ValueError(f"手順「{src}」が見つかりません")
        refuse_taken(self.procedure_names(), new, path=self.procedure_path(new))
        write_file(self.procedure_path(new), self.procedure_path(src).read_bytes())

    def copy_frametable(self, src: str, new: str) -> None:
        """フレーム表をコピーして別名で作る。中身はバイト列のままコピーする。"""
        src, new = validate_name(src), validate_name(new)
        if not self.frametable_path(src).is_file():
            raise ValueError(f"フレーム表「{src}」が見つかりません")
        refuse_taken(self.frametable_names(), new, path=self.frametable_path(new))
        write_file(self.frametable_path(new), self.frametable_path(src).read_bytes())

    def delete_procedure(self, name: str) -> None:
        remove_file(self.procedure_path(name))
        remove_file(self.root / "build" / f"{name}.bin")
        self.update_org_refs(name, None)

    def delete_frametable(self, name: str) -> None:
        remove_file(self.frametable_path(name))
        self.update_org_refs(name, None, "frametables")

    def init_sample(self) -> None:
        """はじめて使うときのひな形を作る。"""
        (self.root / "procedures").mkdir(parents=True, exist_ok=True)
        (self.root / "frametables").mkdir(parents=True, exist_ok=True)
        procedure = self.root / "procedures" / "サンプル.json"
        if not procedure.exists():
            procedure.write_text(json.dumps({
                "kind": "procedure",
                "format": FORMAT_VERSION,
                "precondition": "ボタンの動作チェックの画面を開いた状態",
                "body": [
                    {"type": "label", "id": "lb-1", "name": "開始"},
                    {"type": "press", "buttons": ["DR"], "frames": 5},
                    {"type": "wait", "frames": 60},
                    {"type": "loop", "id": "lp-1", "count": 3, "body": [
                        {"type": "frametable", "ref": "サンプル"},
                        {"type": "wait", "frames": 30},
                    ]},
                    {"type": "label", "id": "lb-2", "name": "終了"},
                    {"type": "wait", "frames": 60},
                ],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        frametable = self.root / "frametables" / "サンプル.csv"
        if not frametable.exists():
            frametable.write_text("F,DL,DR\n1,1,\n2,1,\n3,1,\n4,,\n"
                                  "5,,1\n6,,1\n7,,1\n8,,\n", encoding="utf-8")
