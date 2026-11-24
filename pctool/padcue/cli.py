"""padcue のコマンドライン。操作画面の起動と、画面と同じ操作、保守の操作を担当する。

    python -m padcue init                               プロジェクトのひな形を作成
    python -m padcue build [<名前>]                     手順を変換し、警告を表示
    python -m padcue push <名前>                        手順を変換して、装置に転送・保存
    python -m padcue run <名前> [-n 周回数]             手順を実行
    python -m padcue select <番号>                      選択待ちの選択肢を選ぶ
    python -m padcue lap-end <終え方>                   実行中に周回数を変える
    python -m padcue stop [--graceful | --cancel]       停止
    python -m padcue status                             状態を表示
    python -m padcue logs                               装置のログを取得
    python -m padcue list                               装置に保存した手順の一覧を表示
    python -m padcue export <名前>...                   手順をエクスポート
    python -m padcue import <ファイル> [--yes]          padcue ファイルなどをインポート
    python -m padcue device ...                         装置の登録と接続先の設定
    python -m padcue discover                           LAN 内の装置を検索
    python -m padcue mock                               模擬装置を起動
    python -m padcue gui                                操作画面を開く

  装置の登録:

    python -m padcue device list                        登録した装置の一覧を表示
    python -m padcue device add <IP アドレス> [名前]    新しい装置を登録
    python -m padcue device rename <名前> <新しい名前>  名前を変更
    python -m padcue device forget <名前>               割り当てを解除
    python -m padcue device remove <名前>               登録を削除
    python -m padcue device auto                        接続先を検索して更新
    python -m padcue device <IP アドレス>               接続先を設定

  装置の保守:

    python -m padcue identify                           装置の LED を 5 秒点滅させる
    python -m padcue reconnect-usb [--hold ミリ秒]      装置の USB を切断して再接続する
    python -m padcue ota [ファイル]                     ファームウェアを無線で更新
    python -m padcue config <キー> [<値>]               設定値を書き込む

  異常の解除と操作権:

    python -m padcue clear-error                        異常停止(赤 LED)を解除
    python -m padcue take-owner [--yes]                 別の padcue から操作権を取得
"""
from __future__ import annotations

import argparse
import codecs
import getpass
import json
import re
import socket
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from . import __version__, firmware, owner, pack, proto, registry, unpack
from .binfmt import RECORD_CAPACITY
from .client import (
    DeviceClient,
    DeviceError,
    LogReader,
    connect_verified,
    failure_of,
    failure_text,
    firmware_problem,
    is_mock,
)
from .devicepool import keep_only, unwritten
from .discover import discover
from .procfmt import ProcedureError
from .project import (
    Project,
    default_root,
    needs_server,
    padcue_root,
    running_gui_base,
)
from .statustext import arm_list, status_lines
from .words import error_text, for_user, sentences, state_ja, unexpected_text

# 足りないものを知らせるときの種類の呼び名
_PACK_JA = {"procedure": "手順", "frametable": "フレーム表",
            "condition": "条件"}


def _project(args) -> Project:
    """操作の対象になるプロジェクト。

    --project を書いたらそこ。書かなければ padcue のフォルダーの data
    (project.default_root)。どこで実行しても同じプロジェクトを使う。
    """
    if args.project is not None:
        return Project(Path(args.project).resolve())
    return Project(default_root())


def _gui_base(p: Project, args=None) -> str | None:
    """操作画面のサーバーが動いていれば、その URL を返す(coupling.md D10)。

    サーバーを経由するのは、実行の状態を変える指示と記録の読み出しだけで
    ある(D5)。マーカーが残っていても(サーバーが異常終了したときなど)、
    応答を確認して、応答がなければ直結に戻る。--host を書いたコマンドは
    経由しない。--host は、書いた接続先へ直接送る復旧のための指定である
    (coupling.md D2)。経由すると、サーバーが登録した別の装置を選ぶ。
    """
    if args is not None and getattr(args, "host", None):
        return None
    return running_gui_base(p.root)


def _exit_code(failure: str) -> int:
    """失敗の種類(client.failure_of)からコマンドの終了コードを決める。

    装置が拒否したときは 2、装置につながらないときは 3、それ以外の失敗は 1 で
    ある(CONTRIBUTING.md「コマンドの出力」)。操作画面を経由しても直結でも、
    同じ失敗には同じ終了コードを返す。
    """
    return {"refused": 2, "unreachable": 3}.get(failure, 1)


def _exit_of(e: BaseException) -> int:
    """装置へ送って失敗したときの終了コード。"""
    if isinstance(e, Unreachable):
        return 3
    return _exit_code(failure_of(e))


def _gui_get(base: str, path: str) -> dict:
    return _gui_call(urllib.request.Request(base + path), 10)


def _gui_post(base: str, path: str, obj: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return _gui_call(req, 30)


def _gui_call(req, timeout: float) -> dict:
    """操作画面のサーバーへ要求を送る。失敗には gui_server のフラグを立てる。

    フラグは、失敗の文で装置の接続先を示さずに、操作画面のサーバーのことを示すのに
    使う(_fail_text)。
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except OSError as e:
        e.gui_server = True
        raise


def _gui_dev(base: str, args) -> dict | None:
    """state から対象装置(--device 省略時は 1 台目)のエントリを取る。"""
    devs = _gui_get(base, "/api/state").get("devices") or []
    want = getattr(args, "device", None)
    if not want:
        return devs[0] if devs else None
    for d in devs:
        if d.get("name") == want or d.get("id") == want:
            return d
    return None


def _dev_arg(args) -> str:
    """--device の値(未指定なら空文字)。API へ渡す装置の指定。"""
    return getattr(args, "device", "") or ""


def _via_gui(p: Project, path: str, body: dict, done, args) -> int | None:
    """画面が開いていれば、その API を呼んで終了コードを返す。

    開いていなければ None を返すので、呼ぶ側は直結の処理へ進む。
    「開いているか見る → 送る → error を見る → フラグを立てる」の 4 段が
    コマンドごとにコピーされるのを防ぐ(coupling.md D10 の経路選択はここだけ)。
    done は成功したときに表示する文言。応答の中身を使いたいときは関数を渡す。

    経由させる理由は接続の奪い合いではない(装置は制御の接続を 4 本受ける)。
    実行に関わる判断と記録をサーバーが持つからである。
    直結で開始すると、その実行はどのグループにも属さず、連動停止も周の記録も付かない。
    異常停止の理由や手動操作の記録も、サーバーだけが持っている。
    だから実行の状態を変える指示(開始・停止・選択・異常の解除・操作権・
    手動操作)と、状態と記録の読み出し(装置の記録を読み出す側を 1 つに限る。
    2 つだと同じ記録を logs.jsonl へ 2 度書く。D5)はサーバーを通す。装置
    そのものを変えるだけの指示(設定、ファームウェアの更新、探索)は直結でよい。
    """
    base = _gui_base(p, args)
    if base is None:
        return None
    r = _gui_post(base, path, body)
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        return _exit_code(r.get("failure", ""))
    print(done(r) if callable(done) else done)
    return 0


def _no_device_named(p: Project, args, what: str, hint: str) -> bool:
    """装置を動かすコマンドで、装置が 2 台以上あるのに --device がなければ、
    案内して真を返す(coupling.md D4)。

    並び順で 1 台目に決めると、利用者が選んでいない装置が動く(select は、
    2P だけが選択待ちのときに 1P を見て「選択待ちではありません」と答え、
    両方が待っていれば 1P が進む)。操作画面のサーバーも同じ要求を拒否する。画面を
    開いているかどうかで振る舞いを変えないよう、経路を選ぶ前に拒否する。--host は
    書いた接続先へ直接送るので、拒否しない。what は「実行する」のような装置の
    言い方、hint は案内に含めるコマンドの全体である。コマンドの名前は呼ぶ所で
    字のまま書く(test_docs が、案内しているコマンドが打って通るかを確かめる)。
    """
    devs = p.load_config().get("devices") or []
    if args.host or _dev_arg(args) or len(devs) < 2:
        return False
    names = "・".join(d.get("name", "?") for d in devs)
    print(f"{what}装置が指定されていません(登録済み: {names})\n"
          f"  指定するには: {hint}", file=sys.stderr)
    return True


def _pick_device(args, cfg: dict) -> dict:
    """--device 名前(省略時は 1 台目)から装置の登録情報のエントリを選ぶ。"""
    devs = cfg.get("devices", [])
    if not devs:
        raise SystemExit("装置が登録されていません。\n"
                         "  登録するには: python -m padcue device add "
                         "<IP アドレス> <名前>")
    want = getattr(args, "device", None)
    if not want:
        return devs[0]
    for d in devs:
        if d.get("name") == want or d.get("id") == want:
            return d
    names = "・".join(d.get("name", "?") for d in devs)
    raise SystemExit(f"装置「{want}」は登録されていません(登録済み: {names})")


def _take_owner(c: DeviceClient) -> None:
    """操作権を取得。取れなくても、停止と状態の読み出しはできる。

    同じ padcue(このコードのツリー)は同じ識別子を送るので、画面が持っていても
    コマンドは拒否されない。
    """
    try:
        c.acquire()
    except DeviceError:
        pass                # 拒否されただけで、接続は使える。通信の失敗は上へ伝える


def _client(args, own: bool = True) -> DeviceClient:
    """接続先を決めてつなぎ、装置 ID(MAC)を照合する。

    own が真なら操作権も取得する。装置の中を読むだけの命令は偽を渡す。
    """
    c = _connect(args)
    if own:
        _take_owner(c)
    return c


def _split_address(text: str) -> tuple[str, int | None]:
    """「アドレス」か「アドレス:ポート」を分ける。ポートがなければ None。

    discover は「アドレス:ポート」の形で出すので、それをコピーしてもそのまま使える
    ようにする。ポートを書くのは、同じ PC で模擬装置を複数動かすデモのときである
    (実機はどれも既定の 5555)。
    """
    host, sep, port = text.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return text, None


class Unreachable(Exception):
    """登録した装置につながらない(終了コード 3。CONTRIBUTING.md「コマンドの出力」)。

    文は修正方法まで含む。SystemExit の文で終えると終了コードが 1 になり、手順や引数の
    誤りと区別できない。
    """


def _unassigned(dev: dict) -> SystemExit:
    """未割り当ての装置を指したときの拒否(終了コード 1)。割り当て方を示す。"""
    return SystemExit(
        f"{dev.get('name')} は未割り当てです。\n"
        "  割り当てるには: python -m padcue --device "
        f"{registry.command_arg(dev.get('name', ''))} device <IP アドレス>\n"
        "  LAN 内の装置を検索するには: python -m padcue discover")


def _not_in_config_text(name: str) -> str:
    """接続先を書こうとした装置が、登録情報になかったときの文(終了コード 1)。

    読んだ後に、ほかで登録を削除したか、名前を変えたか、割り当てを解除している
    (project.update_device が偽を返したとき)。何も書いていないので、設定したとは
    書かない。
    """
    return f"装置「{name}」が登録情報に見つからないため、接続先を保存できません"


def _mock_unreachable(p: Project, dev: dict) -> Unreachable:
    """登録した模擬装置につながらないときの拒否(終了コード 3)。

    模擬装置はこの PC の決まったポートで動くので、探しても見つからない。探すと
    LAN の実機が見つかり、その登録を勧める文になる。デモのフォルダーの模擬装置は
    デモの操作画面の中で動くので、その開き方を示す。ほかのフォルダーの模擬装置は、
    操作画面と一緒にも(gui --mock)単独でも(mock)起動できるので、どちらかは示さない。
    """
    head = f"模擬装置の {dev.get('name')} に接続できません。"
    if p.root.resolve() == (padcue_root() / "demo").resolve():
        return Unreachable(head + "\n  デモで使うには: padcue-demo.bat を開く")
    return Unreachable(head + "模擬装置を起動してから、もう一度実行してください。")


def _connect(args) -> DeviceClient:
    """_client の接続の部分。

    記録した IP でつながらなければ LAN 内を探すが、記録し直すのは同じ装置
    (ID が一致する応答)だけ。「実際につながった最初の 1 台」へ黙って乗り
    換えると、複数台の環境では意図しない実機を操作する事故になる。模擬装置は
    探さない(_mock_unreachable)。--host 指定は照合なしの直結(復旧用の代替手段)。
    """
    p = _project(args)
    cfg = p.load_config()
    if args.host:                       # 明示指定 = 直結(記録もしない)
        host, port = _split_address(args.host)
        c = DeviceClient(host, port or proto.DEFAULT_PORT)
        c.connect()
        return c
    dev = _pick_device(args, cfg)
    want_id = dev.get("id", "")
    if not want_id:
        # どの装置を使うかが決まっていない装置は操作しない。登録情報に残っている
        # 接続先は指し直していない古い値なので、そこにいた装置へつなぐと、
        # 交換で外すはずの装置が動く
        raise _unassigned(dev)
    # 登録した接続先に別の装置がいたときの例外。登録した装置が探索でも
    # 見つからなければ、これを投げ直す(main が割り当ての解除を案内する)。
    # 探索の結果だけで終えると「装置が見つかりません」のように、いる装置を
    # いないと表示してしまう
    mismatch = None
    try:
        c, _info = connect_verified(dev)
        return c
    except DeviceError as e:
        if e.code != "DEVICE_MISMATCH":
            raise
        mismatch = e
        print(f"{dev['host']} は {dev.get('name')} ではありません。"
              "LAN 内で登録した装置を検索しています…", file=sys.stderr)
    except OSError:
        if is_mock(want_id):
            raise _mock_unreachable(p, dev) from None
        print(f"{dev['host']} に接続できません。LAN 内で登録した装置を検索しています…",
              file=sys.stderr)
    found = discover()
    if not any(f.device_id == want_id for f in found):
        # UDP 探索では登録した装置が見えない。装置 ID の下 4 桁(mDNS)でも追跡を試す。
        # UDP ブロードキャストは届かないネットワークがあるが、装置は
        # pademu-<MAC 下 4 桁>.local を公開するので、名前でなら辿れる
        name_host = f"pademu-{want_id[-4:]}.local"
        try:
            c, _info = connect_verified(dict(dev, host=name_host))
            moved = dev["host"] != name_host
            if not p.update_device(cfg, cfg["devices"].index(dev), host=name_host):
                c.close()
                raise SystemExit(_not_in_config_text(dev.get("name", "")))
            print(_found_again_text(name_host, moved), file=sys.stderr)
            return c
        except (OSError, DeviceError):
            pass
    # 探索の応答から追いかけるのは、登録情報に記録した装置(ID 一致)だけである。
    # アドレスは無線の再接続で変わるので追いかける値打ちがあるが、どの装置を
    # 使うかは追いかけた結果で変えない
    for target in (f for f in found if f.device_id == want_id):
        probe = dict(dev, host=target.host, port=target.port)
        try:
            c, _info = connect_verified(probe)
        except (OSError, DeviceError):
            continue
        moved = ((dev["host"], int(dev.get("port", proto.DEFAULT_PORT)))
                 != (target.host, target.port))
        if not p.update_device(cfg, cfg["devices"].index(dev),
                               host=target.host, port=target.port):
            c.close()
            raise SystemExit(_not_in_config_text(dev.get("name", "")))
        print(_found_again_text(target.host, moved), file=sys.stderr)
        return c
    if mismatch is not None:
        raise mismatch
    if not found:
        raise Unreachable(
            "装置が見つかりません。電源と Wi-Fi を確認してください。\n"
            "  接続先を指定するには: python -m padcue device <IP アドレス>")
    where = "、".join(f.host for f in found)
    raise Unreachable(
        f"{where} が見つかりましたが、登録した装置ではありません。\n"
        "  新しい装置を登録するには: python -m padcue device add <IP アドレス> <名前>")


def _found_again_text(host: str, moved: bool) -> str:
    """探索で見つけ直した装置のメッセージ。

    再起動の途中だった装置は、1 度つながらず、前と同じ接続先で見つかる。変わって
    いないのに「更新しました」と表示すると、登録情報が書き換わったと読める。
    """
    return f"見つかりました: {host}。" + ("接続先を更新しました。" if moved else "")


def _print_build(r) -> None:
    # 1 行に 1 つの値を出す(status と同じく、値を切り分けさせない)
    print(f"{r.name}: {r.total_frames} フレーム({r.seconds:.1f} 秒)")
    print(f"  装置の容量: {round(100 * r.events / RECORD_CAPACITY)}%"
          f"({r.events} / {RECORD_CAPACITY} 件)")
    print(f"  id: {r.hash}")
    if r.precondition:
        print(f"  前提条件: {r.precondition}")
    _print_warnings(r.warnings)


def _print_warnings(warnings) -> None:
    """変換と開始の警告を、1 行に 1 つ標準エラーへ出す。

    build と push は変換の結果の警告を、run は開始の警告を出す(file-format.md §8。
    手で書いた手順は、編集画面を通らずに開始できる)。グループの開始はレーンごとに
    同じ警告を返すので、同じ行は 1 度だけ出す。
    """
    seen = set()
    for w in warnings or []:
        # 番号は、変換した手順の中で、列に見えている順の番号である(無効にした
        # ブロックも数える)。呼び出し先の中の警告は、そこへ入っていく呼び出しの
        # ブロックの番号で示す。編集画面の「N 番目(別の手順)」と同じである
        # (file-format.md §8)。手順の中の位置を指せない警告は番号を付けない
        n = w.get("shown") or 0
        kind = "(別の手順)" if w.get("path") else ""
        at = f"{n} 番目のブロック{kind}: " if n else ""
        line = f"  ⚠ {at}{w['msg']}"
        if line not in seen:
            seen.add(line)
            print(line, file=sys.stderr)


def cmd_init(args) -> int:
    p = _project(args)
    p.init_sample()
    made = ["procedures/サンプル.json", "frametables/サンプル.csv"]
    if not p.config_path.is_file():
        # 装置の登録情報(空)も置く。.bat はこれがあるかで初回かを判別するので、
        # 消したサンプルを次の起動で作り直さない
        p.save_config(p.load_config())
        made.append("padcue.json")
    print(f"ひな形を作成しました: {p.root}")
    print(f"  {', '.join(made)}")
    return 0


def cmd_build(args) -> int:
    p = _project(args)
    names = [args.name] if args.name else p.procedure_names()
    if not names:
        print("手順がありません。\n  ひな形を作成するには: python -m padcue init",
              file=sys.stderr)
        return 1
    failed = 0
    for name in names:
        try:
            _print_build(p.build(name))
        except (ProcedureError, ValueError, RecursionError) as e:
            # 文は画面と同じく error_message で作る(呼び出しの長い連鎖で
            # 再帰の上限に当たったときも、トレースバックにしない)
            print(f"{name}: エラー {p.error_message(name, e)}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def cmd_push(args) -> int:
    p = _project(args)
    try:
        r = p.build(args.name)
    except (ProcedureError, ValueError, RecursionError) as e:
        print(f"変換に失敗しました: {p.error_message(args.name, e)}", file=sys.stderr)
        return 1
    _print_build(r)
    rc = _via_gui(p, "/api/push", {"name": args.name, "dev": _dev_arg(args)},
                  lambda r: f"転送・保存しました(id {r.get('hash')})", args)
    if rc is not None:
        return rc
    # 画面経由(/api/push)と同じく、送った手順のコピーを残し、装置にはこの手順
    # だけを残す。送った手順のコピーがないと、実行中の装置が報告する id を名前に戻せない
    dev = "" if args.host else _pick_device(args, p.load_config()).get("id", "")
    p.sent.remember(r, r, dev)
    with _client(args) as c:
        pid = c.put(r.blob)
        c.commit(pid)   # 保存キーは内容の id
        keep_only(c, {e["id"] for e in c.list()}, {pid})
        print(f"転送・保存しました(id {pid})")
    return 0


# 実行の終わり方(STATUS の stop_reason)。画面の記録(web/lists.js の
# STOP_WHY_JA)と同じ言い方にする
_END_JA = {proto.STOP_REASON_FINISHED: "完了", proto.STOP_REASON_STOPPED: "停止",
           proto.STOP_REASON_AT_MARK: "区切りで停止",
           proto.STOP_REASON_BUTTON: "装置のボタンで停止",
           proto.STOP_REASON_ERROR: "異常停止"}


def _watch_progress(poll, stop) -> int:
    """停止するまで進捗(周・フレーム)を上書き表示する。終了コードを返す。

    poll は状態の辞書(取れなければ None)を返し、stop は直ちに停止して、
    停止できなかった理由と失敗の種類(client.failure_of)の組を返す(停止できたら
    どちらも空)。
    画面経由と直結で書き分けると、同じ `padcue run -w` の見え方が操作画面を
    開いているかどうかで変わる。経路の違いはこの 2 つの関数だけに閉じる。
    """
    said = ""
    try:
        while True:
            st = poll()
            # 応答がなくなったら終える。/api/state には最後に読めた値が
            # 残るが、それを進捗として出し続けても進み具合にはならない
            if st and st.get("error"):
                print(f"\n応答がありません: {st['error']}", file=sys.stderr)
                return _exit_code(st.get("failure") or "unreachable")
            if not st or not (st.get("running") or st.get("awaiting")):
                st = st or {}
                how = (_END_JA.get(st.get("stop_reason") or "")
                       or state_ja(st.get("state", "不明")))
                print(f"\n終了: {how}", file=sys.stderr)
                # 異常停止で終えた実行は失敗として 1 を返す(結果を受け取るツールが、
                # 最後まで実行できたかを終了コードで分けられるように)
                return 1 if st.get("stop_reason") == proto.STOP_REASON_ERROR else 0
            if st.get("awaiting"):
                # 進みが止まって見えるので、待っていることと選べるものを示す
                wait = "選択待ち"
                if st.get("arm_names"):
                    wait += f": {arm_list(st['arm_names'])}"
                if wait != said:
                    print(f"\n{wait}", file=sys.stderr)
                    said = wait
            else:
                said = ""
                print(f"\r{st.get('frames_elapsed', 0)} フレーム", end="",
                      file=sys.stderr)
            time.sleep(0.5)
    except KeyboardInterrupt:
        why, failure = stop()
        if why:
            # 停止したつもりで動き続ける装置を残さない。終了コードは失敗の種類で
            # 決める(つながらなければ 3)
            print(f"\n停止できません: {why}", file=sys.stderr)
            return _exit_code(failure)
        print("\n停止しました", file=sys.stderr)
        return 0


def _laps_arg(value) -> int | None:
    """-n の値を周回数に変換する。空か「無限」なら None(停止するまで)である。

    周回数は合計であり、無限は空で表す。0 は使わない。数をそのまま
    渡す呼び出し(テストやほかのツール)もあるので、文字列でなくても受ける。
    """
    v = str(value if value is not None else "").strip()
    if v in ("", "無限", "inf"):
        return None
    try:
        n = int(v)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(f"周回数「{v}」が正しくありません。"
                         "1 以上の整数を指定してください。\n"
                         "  無限にくり返すには: -n 無限")
    return n


# 操作画面のサーバーが持つ仕組みを、サーバーなしで使おうとしたときの案内
_NEEDS_SERVER = "  起動するには: python -m padcue gui --no-open"


def cmd_run(args) -> int:
    p = _project(args)
    try:
        laps = _laps_arg(getattr(args, "laps", "1"))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    shown = f"{laps} 周" if laps else "停止するまで"
    if getattr(args, "group", None):
        # グループは、合流点と連動停止を担当するサーバーの仕組みである(D10)
        base = _gui_base(p, args)
        if base is None:
            print(f"グループの開始には、操作画面のサーバーが必要です。\n{_NEEDS_SERVER}",
                  file=sys.stderr)
            return 1
        r = _gui_post(base, "/api/run", {"plan": [
            {"name": args.name, "laps": laps, "dev": n} for n in args.group]})
        if r.get("error"):
            print(r["error"], file=sys.stderr)
            return _exit_code(r.get("failure", ""))
        print(f"実行開始: {args.name}({shown}、{'・'.join(args.group)})")
        _print_warnings(r.get("warnings"))
        return 0
    if _no_device_named(p, args, "実行する", "python -m padcue --device <名前> run "
                        f"{registry.command_arg(args.name)}"):
        return 1
    base = _gui_base(p, args)
    if base:
        # 画面が開いている間は操作画面のサーバー経由(D10)。リビジョンのずれの自動転送も
        # サーバー側の同じ規則で扱われる。開始の経路はグループの開始 1 本にまとまって
        # いるので、1 台でも同じ道を通る
        r = _gui_post(base, "/api/run",
                      {"plan": [{"name": args.name, "laps": laps,
                                 "dev": _dev_arg(args)}]})
        if r.get("error"):
            print(r["error"], file=sys.stderr)
            return _exit_code(r.get("failure", ""))
        print(f"実行開始: {args.name}({shown})")
        # 開始の警告(変換の警告、映像ソースが見つからない、周回数と合流点の
        # 不一致など)。サーバーは実行の記録にも残すが、端末にも出す
        _print_warnings(r.get("warnings"))
        if args.watch:
            def stop():
                r = _gui_post(base, "/api/stop",
                              {"mode": "immediate", "dev": _dev_arg(args)})
                return r.get("error") or "", r.get("failure") or ""

            return _watch_progress(lambda: _gui_dev(base, args), stop)
        return 0
    with _client(args) as c:
        # 画面を開いていないときも、画面の経路(coupler)と同じく、この padcue で
        # 使えないファームウェアでは始めない
        why = firmware_problem(c.info.fw_version) if c.info is not None else ""
        if why:
            print(why, file=sys.stderr)
            return 1
        try:
            sid = p.sent.freeze(args.name)
            frozen = p.sent.frozen(sid)
            r = frozen.build(args.name, "infinite" if laps is None else laps,
                             write=False)
            # 実行中のレーンの図は、送る手順と同じ時点の中身から、この実行の周回数の
            # 1 周目として描く(画面から始めた実行と同じ。Coupler._build_lane)
            view = frozen.build(args.name, write=False, drawn_laps=r.laps)
        except (ProcedureError, ValueError, RecursionError) as e:
            print(f"変換に失敗しました: {p.error_message(args.name, e)}",
                  file=sys.stderr)
            return 1
        if needs_server(r.awaits):
            # 判定や選択をするサーバーがないので、始めると選択待ちで止まったままになる
            print(f"手順「{args.name}」には PC の判定や選択が必要なので、"
                  "操作画面のサーバーがないと実行できません。\n"
                  "  サーバーを起動するには: python -m padcue gui --no-open",
                  file=sys.stderr)
            return 1
        ids = {e["id"] for e in c.list()}
        if r.hash not in ids:
            # 周回数は手順の中のくり返しの回数として入るので、周回数が違えば
            # 別の id になる。まだ送っていない id の手順データはここで送る
            c.put(r.blob)
            c.commit(r.hash)
            ids.add(r.hash)
        # これから実行する手順だけを装置に残す
        # 描画用の変換結果は装置ごとに残す(接続先の直接指定では、どの装置か分からない)
        dev = "" if args.host else _pick_device(args, p.load_config()).get("id", "")
        p.sent.remember(r, view, dev, sources=sid)
        keep_only(c, ids, {r.hash})
        # 内容のハッシュである id を指定して実行する。周回数は手順の変換で SETCNT に
        # 入るので、実行では渡さない
        c.run(r.hash)
        print(f"実行開始: {args.name}({shown})")
        # 直結では実行の記録がないので、変換の警告はここでしか出ない
        _print_warnings(r.warnings)
        if args.watch:
            # 直結の停止の失敗は例外のまま main へ伝え、main が終了コードを決める
            return _watch_progress(c.status, lambda: (c.stop(), ("", ""))[1])
    return 0


def cmd_stop(args) -> int:
    """停止する。--device で指した装置を停止する。操作画面のサーバーが動いていれば
    サーバーを経由し、画面のレーンのボタンと同じく、そのレーンだけを停止する(一緒に
    開始したほかのレーンは動き続ける)。--host で接続先へ直接送ったときは、サーバーから
    見ると別のツールからの停止なので、そのレーンのグループの残りも連動して停止する。
    --device を省略すると、割り当てた登録装置のすべてへ送る。

    画面が開いていてもいなくても、指定のない停止の範囲は同じにする
    (停止の命令の範囲が経路で変わると、停止したつもりで停止していない装置が
    残る)。画面を経由するときも、グループに属さない実行(画面を開く前に
    コマンドで始めたものなど)まで停止するよう、すべての装置を指定して渡す。
    どれを停止したかを出力に書く。
    """
    mode = ("cancel" if args.cancel
            else "graceful" if args.graceful else "immediate")
    said = {"cancel": "区切りでの停止予約を取り消しました",
            "graceful": "次の区切りでの停止を予約しました",
            "immediate": "停止しました"}[mode]
    # 失敗の文は画面と同じく「〇〇できません」の形にする(gui-principles.md §7-13)
    not_done = {"cancel": "予約を取り消せません",
                "graceful": "停止を予約できません",
                "immediate": "停止できません"}[mode]
    cfg = _project(args).load_config()
    if _dev_arg(args) and not args.host:
        # 登録していない名前は、画面を経由するときも直結と同じ文で拒否する
        _pick_device(args, cfg)
    names = ([_dev_arg(args)] if _dev_arg(args) else
             [d.get("name") for d in cfg.get("devices") or [] if d.get("id")])
    if not args.host and not names:
        if cfg.get("devices"):
            print("割り当てた装置がありません。\n"
                  "  割り当てるには: python -m padcue --device <名前> device "
                  "<IP アドレス>",
                  file=sys.stderr)
        else:
            print("装置が登録されていません。\n"
                  "  登録するには: python -m padcue device add <IP アドレス> <名前>",
                  file=sys.stderr)
        return 1
    # 対象のレーンがどれも周回の終え方を選んだグループのレーンなら、操作画面のサーバーは
    # 何も送らず、lanes を空で返す(終え方は予約の取り消しでは取り消さない)。そのまま
    # 「…取り消しました: 」と出すと後ろが空になるので、別の文にする
    rc = _via_gui(_project(args), "/api/stop_lanes", {"mode": mode, "devs": names},
                  lambda r: ("取り消す予約がありません"
                             if mode == "cancel" and not r.get("lanes")
                             else f"{said}: {'・'.join(r.get('lanes') or [])}"), args)
    if rc is not None:
        return rc

    def send(c) -> None:
        if mode == "graceful":
            c.stop_at_mark()
        elif mode == "cancel":
            c.stop_at_mark(release=True)
        else:
            c.stop()

    # 停止と停止の予約は、操作権がなくてもできる。取れば、所有者の
    # 接続から操作権の生存の確認を奪う(装置は所有者の接続の通信しか数えない)。
    # 予約の取り消しだけは操作権が必要(所有者の予約を別の padcue が消さない)
    own = mode == "cancel"
    if args.host:
        # 接続先の直接指定(復旧用)は、登録情報の台数に関係なく、その 1 か所へ送る
        with _client(args, own=own) as c:
            send(c)
        print(f"{said}: {args.host}")
        return 0
    # 1 台の失敗で残りを停止させずに終わらない。停止させたい装置ほど、隣の 1 台が
    # 故障しているときに停止できないと困る。つながらない装置(Unreachable)も
    # 理由を記録して次へ進む
    done, failed = [], []
    for name in names:
        args.device = name
        try:
            with _client(args, own=own) as c:
                send(c)
        except (DeviceError, OSError, Unreachable, SystemExit) as e:
            why = _fail_text(e)
            if isinstance(e, DeviceError) and e.code == "NOT_OWNER":
                why = ("区切りでの停止予約は、操作権を持つ padcue だけが"
                       "取り消せます。\n"
                       "    操作権を取得するには: python -m padcue --device "
                       f"{registry.command_arg(name)} take-owner")
            failed.append((name, why, _exit_of(e)))
            continue
        done.append(name)
    if done:
        print(f"{said}: {'・'.join(done)}")
    if failed:
        # 理由は装置ごとに 1 行で示す(括弧に入れて名前の後ろに並べると読めない)
        print(f"{not_done}: {'・'.join(n for n, _, _ in failed)}",
              file=sys.stderr)
        for name, why, _ in failed:
            print(f"  {name}: {why}", file=sys.stderr)
        # 終了コードは、最初に失敗した装置の失敗の種類で決める
        return failed[0][2]
    return 0


def _fail_text(e: BaseException) -> str:
    """失敗の理由を、利用者が読む文にする。OS の英語の理由は出さない。

    装置へつなぎに行った失敗には、client がつなぎに行った先を付けている
    (device_host)。その理由は、画面の装置の行と同じ文(client.failure_text)で、
    接続先と原因ごとの確認の方法を示す。操作画面のサーバーへの失敗には、
    _gui_call がフラグを立てている。
    """
    if isinstance(e, SystemExit):
        return str(e)               # このツールが自分で書いた文
    if isinstance(e, Unreachable):
        # このツールが書いた文の 1 行目(つながらなかったこと)。2 行目の
        # 次にできることは 1 台に向けたものなので、台ごとの行には入れない
        return str(e).split("\n")[0]
    if isinstance(e, DeviceError):
        return e.message            # 装置が返した理由
    if isinstance(e, OSError) and getattr(e, "filename", None):
        # ファイルの読み書きの失敗で、装置の失敗ではない(client.failure_of)
        return f"ファイル「{e.filename}」を読み書きできません。"
    if getattr(e, "gui_server", False):
        return ("操作画面から応答がありません。操作画面を起動し直してから、"
                "もう一度実行してください。")
    target = getattr(e, "device_host", "")
    if target and isinstance(e, Exception):
        return failure_text(e, target)
    return "装置に接続できません。電源とネットワークを確認してください。"


def _print_status(d: dict) -> None:
    """状態を表示する。画面経由でも直結でも、同じ項目を同じ順で出す。

    経路ごとに書き分けると、直結のときだけ出る行(記録落ち・送信失敗・
    ロールバック)が生まれる。同じ `padcue status` の出力が、操作画面を
    開いているかどうかで変わるのは、計測値として成り立たない。
    """
    if is_mock(d.get("id", "")):
        print("⚠ デモ用の模擬装置に接続しています。実機は動きません。", file=sys.stderr)
    for line in status_lines(d):
        print(line)


def cmd_status(args) -> int:
    p = _project(args)
    base = _gui_base(p, args)
    if base:
        # 登録していない名前と、装置が 1 台もないことは、直結と同じ文で拒否する
        _pick_device(args, p.load_config())
        d = _gui_dev(base, args)
        if d is None:
            print("装置が登録されていません", file=sys.stderr)
            return 1
        if d.get("mismatch"):
            # 登録した接続先に別の装置がいる。直結のとき(main の DeviceError)と
            # 同じく、装置が拒否したものとして 2 で終える
            print(d.get("error", ""), file=sys.stderr)
            print("  割り当てを解除するには: python -m padcue device forget "
                  f"{registry.command_arg(d.get('name', ''))}", file=sys.stderr)
            return 2
        if d.get("error"):
            # 終了コードは失敗の種類で決める(padcue の装置ではない相手は 2、
            # つながらないときは 3。直結と同じ)
            print(f"未接続        : {d['error']}", file=sys.stderr)
            return _exit_code(d.get("failure") or "unreachable")
        if not d.get("id"):
            # 直結のとき(_connect)と同じく、割り当て方を示す。未割り当ての行の
            # 装置にはつながないので、表示する状態がない
            print(f"{d.get('name')} は未割り当てです。\n"
                  "  割り当てるには: python -m padcue --device "
                  f"{registry.command_arg(d.get('name', ''))} device <IP アドレス>",
                  file=sys.stderr)
            return 1
        _print_status(d)
        return 0
    # 装置の中を読むだけなので、操作権は取らない
    with _client(args, own=False) as c:
        info = c.hello()
        st = c.status()
    # 直結で取った情報を、画面経由(/api/state の装置エントリ)と同じ
    # 形に揃えてから渡す。表示側が経路を意識しなくて済む。実行中の手順は
    # 装置が id で報告するので、送った手順のコピーから名前に戻す(画面経由と同じ)
    if st.get("running") or st.get("awaiting"):
        dev = "" if args.host else info.device_id
        art = p.sent.built(st.get("proc") or "", dev)
        st["proc"] = art.name if art is not None else st.get("proc", "")
    _print_status({**st,
                   "fw": info.fw_version, "partition": info.partition,
                   "binterval": info.binterval,
                   "rolled_back": info.rolled_back,
                   "crash": info.crash,
                   "reset_reason": info.reset_reason,
                   "id": info.device_id})
    return 0


# 値が数の設定項目と、装置が受け付ける範囲と、拒否するときに付ける例。ほかは文字列の
# まま送る。数字だけの Wi-Fi パスワードを数に変換すると、装置は未知の設定項目として
# 拒否する
# 数で受ける設定項目の範囲と、範囲の外のときの文(装置と同じ文。proto.py)
_NUMERIC_KEYS = {
    "frame_period_ns": (proto.FRAME_PERIOD_NS_MIN, proto.FRAME_PERIOD_NS_MAX,
                        proto.FRAME_PERIOD_BAD
                        + "60.00Hz は 16666667、59.94Hz は 16683333 です。"),
    "usb_binterval": (proto.USB_BINTERVAL_MIN, proto.USB_BINTERVAL_MAX,
                      proto.USB_BINTERVAL_BAD),
}
# 値を画面に出さない設定項目
_SECRET_KEYS = ("wifi_pass",)


def cmd_config(args) -> int:
    """設定値を書く(電源を切っても残る)。

    frame_period_ns: 1 フレームの長さ。60.00Hz=16666667 / 59.94Hz=16683333
    usb_binterval: USB で宣言する問い合わせの間隔(ミリ秒)。次の起動から使われる
    wifi_ssid / wifi_pass: 装置がつなぐ Wi-Fi。次の起動から使われる。パスワードの
    ない Wi-Fi では、wifi_pass を空にする

    値を省略すると、実行してから聞く。wifi_pass はこの形で入れる。コマンドに書いた
    値はシェルの履歴に残る。
    """
    key, raw = args.key, args.value
    if raw is None:
        try:
            # SSID は前後の空白も名前のうちなので、そのまま使う(引数で書いたときと
            # 同じ値にする)
            raw = (getpass.getpass(f"{key}(非表示): ") if key in _SECRET_KEYS
                   else input(f"{key}: ") if key == "wifi_ssid"
                   else input(f"{key}: ").strip())
        except EOFError:
            print("\n値が入力されていません。\n"
                  f"  値を指定するには: python -m padcue config {key} <値>",
                  file=sys.stderr)
            return 1
    if key in _NUMERIC_KEYS:
        # 装置が使える範囲を、装置につなぐ前に確認する。装置がつながっていなくても、
        # 打ち間違いがすぐ分かる
        lo, hi, bad = _NUMERIC_KEYS[key]
        try:
            val = int(raw)
        except ValueError:
            val = None
        if val is None or not lo <= val <= hi:
            print(bad, file=sys.stderr)
            return 1
    else:
        val = raw
    # 装置が拒否する値は、装置につなぐ前に同じ文で拒否する(proto.py)。装置の電源が
    # 入っていなくても、打ち間違いがその場で分かる
    why = proto.wifi_value_problem(key, val) if isinstance(val, str) else None
    if why:
        print(why, file=sys.stderr)
        return 1
    with _client(args) as c:
        c.config(key, val)
    print(f"{key} を保存しました" if key in _SECRET_KEYS
          else f"{key} = {val} を保存しました")
    return 0


def cmd_identify(args) -> int:
    # どの実物かを判別するための命令。装置の動きを変えないので、操作権は取らない
    # (別の padcue が操作権を持っていても光らせられる)。光ること自体が結果なので、
    # 成功しても何も出さない
    with _client(args, own=False) as c:
        c.identify()
    return 0


def cmd_reconnect_usb(args) -> int:
    # Switch に、装置を挿し直したときと同じように認識し直させる。認識し直したかは
    # Switch しだいで、装置の応答には含まれないので、status で見るよう案内する
    hold = args.hold
    if not proto.USB_RECONNECT_HOLD_MS_MIN <= hold <= proto.USB_RECONNECT_HOLD_MS_MAX:
        print(proto.USB_RECONNECT_HOLD_BAD, file=sys.stderr)
        return 1
    with _client(args) as c:
        c.usb_reconnect(hold)
    print(f"USB を切断しました。{hold} ミリ秒後に再接続します。")
    print("  Switch が認識し直したかを確認するには: python -m padcue status")
    return 0


def cmd_clear_error(args) -> int:
    rc = _via_gui(_project(args), "/api/clear_error", {"dev": _dev_arg(args)},
                  "異常停止を解除しました", args)
    if rc is not None:
        return rc
    with _client(args) as c:
        c.clear_error()
    print("異常停止を解除しました")
    return 0


def _warn_take_owner(args) -> int:
    print("操作権は別の padcue にあります。取得すると、別の padcue では実行も"
          "選択もできなくなります。", file=sys.stderr)
    dev = _dev_arg(args)
    opt = f"--device {registry.command_arg(dev)} " if dev else ""
    print(f"  取得するには: python -m padcue {opt}take-owner --yes", file=sys.stderr)
    return 1


def cmd_take_owner(args) -> int:
    """操作権を取得する(強制)。

    別の padcue(別のツリーの padcue)が持っているところへ割り込むので、確認を
    取る。同じ padcue なら取り上げるまでもなく通るので、この操作は必要ない。
    """
    p = _project(args)
    base = _gui_base(p, args)
    if base is not None:
        # 登録していない名前・未割り当て・つながらない装置は、直結と同じに拒否する。
        # 最後に読めた状態だけで「取得済み」と表示しない
        _pick_device(args, p.load_config())
        d = _gui_dev(base, args)
        if d is None:
            print("装置が登録されていません", file=sys.stderr)
            return 1
        if not d.get("id"):
            print(f"{d.get('name')} は未割り当てです。\n"
                  "  割り当てるには: python -m padcue --device "
                  f"{registry.command_arg(d.get('name', ''))} device <IP アドレス>",
                  file=sys.stderr)
            return 1
        if d.get("error"):
            print(d["error"], file=sys.stderr)
            return _exit_code(d.get("failure") or "unreachable")
        if not d.get("owned_by_other"):
            print("この padcue で操作権を取得済みです")
            return 0
        if not args.yes:
            return _warn_take_owner(args)
        return _via_gui(p, "/api/acquire", {"dev": _dev_arg(args)},
                        "操作権を取得しました", args)
    with _client(args) as c:
        own = (c.status().get("owner") or {}).get("id", "")
        if own == owner.client_id():
            print("この padcue で操作権を取得済みです")
            return 0
        if own and not args.yes:
            return _warn_take_owner(args)
        c.acquire(force=True)
    print("操作権を取得しました")
    return 0


def cmd_logs(args) -> int:
    p = _project(args)
    base = _gui_base(p, args)
    if base:
        # 操作画面のサーバーが毎秒回収して溜めている記録(装置タグ付き)を出す。
        # 読み出す側はサーバー 1 つに限る(直結でも読むと、同じ記録を 2 度書く)。
        # 画面と同じく 1 行 1 周にまとまって返るので、平坦にして並べる
        # (ここは種別と値をそのまま見るツールである)。--device があれば、
        # 直結と同じくその装置の記録だけを出す
        want = ""
        if _dev_arg(args):
            dev = _pick_device(args, p.load_config())
            want = dev.get("id") or ""
            if not want:
                # 未割り当ての装置の記録はない。絞り込みを外してすべての装置の記録を
                # 出さず、直結と同じ文で拒否する
                raise _unassigned(dev)
        laps = _gui_get(base, "/api/logs").get("laps", [])
        entries = [e for g in laps for e in g.get("entries", [])
                   if not want or e.get("dev") == want]
        st = _gui_get(base, "/api/state")
        names = {d["id"]: d.get("name") for d in st.get("devices", [])
                 if d.get("id")}
        if not entries:
            print("ログはありません")
        for e in entries[-200:]:
            t = time.strftime("%H:%M:%S", time.localtime(e.get("at", 0)))
            who = names.get(e.get("dev"), "")
            print(f"[{t}] {who:<4} {e.get('kind', '?'):<14} "
                  f"a={e.get('a', 0)} b={e.get('b', 0)} c={e.get('c', 0)}")
        return 0
    # 装置の中を読むだけなので、操作権は取らない
    with _client(args, own=False) as c:
        shown = 0
        reader = LogReader()

        def keep(entries):
            # logs.jsonl へ書いてから出す。書けた分だけ装置に受け取りを知らせる
            # ので、書けなかった記録は装置に残り、次に読める。最初の応答は、
            # 前の読み出す側が書いた後で受け取りを知らせていない記録を含みうるので、
            # 既に書いた記録は書き直さない(出すのはすべて)
            nonlocal shown
            new = (unwritten(p, c.device_id, entries) if reader.ack is None
                   else entries)
            p.logs.append(new, dev=c.device_id)
            for e in entries:
                print(f"[{e['t_ms'] / 1000:9.3f}s] {e['kind']:<14} "
                      f"a={e.get('a', 0)} b={e.get('b', 0)} c={e.get('c', 0)}")
            shown += len(entries)

        # 溜まっている記録を、空になるまで読む(1 回の応答は 48 件まで)
        reader.read(c.logs, keep)
        if not shown:
            # 装置の中は、受け取り済みの分が消えて空になる。操作画面が受け取った
            # 記録は logs.jsonl にあるので、「ない」と表示せずにそれを出す
            kept = [e for e in p.logs.read(200) if e.get("dev") == c.device_id]
            for e in kept:
                t = time.strftime("%H:%M:%S", time.localtime(e.get("at", 0)))
                print(f"[{t}] {e.get('kind', '?'):<14} "
                      f"a={e.get('a', 0)} b={e.get('b', 0)} c={e.get('c', 0)}")
            if not kept:
                print("ログはありません")
    return 0


def cmd_list(args) -> int:
    """装置に保存した手順を出す。

    画面が開いていても直結で読む。装置の中を読むだけで、実行の状態も記録も
    変えないので、サーバーを通す理由がない(_via_gui)。操作権も取らない。
    """
    with _client(args, own=False) as c:
        procs = c.list()
    if not procs:
        print("装置に保存した手順はありません")
    sent = _project(args).sent
    for e in procs:
        # 別の PC から送った手順は、名前が分からない
        print(f"{e['id']}  {e['size']:>7} バイト  {sent.name_of(e['id'])}".rstrip())
    if procs.truncated:
        # 装置は一覧を 64 件で切る。何も表示しないと、残りの手順に気づけない
        print(f"ほかにも手順があります。一覧に表示するのは {len(procs)} 件までです。",
              file=sys.stderr)
    return 0


def cmd_select(args) -> int:
    """選択待ちの選択肢を選ぶ。番号は、分岐の選択肢の上から 1 始まりで数える。"""
    arm = args.number - 1
    p = _project(args)
    if _no_device_named(p, args, "選択する",
                        f"python -m padcue --device <名前> select {args.number}"):
        return 1
    base = _gui_base(p, args)
    if base:
        d = _gui_dev(base, args)
        if d is None or not d.get("awaiting"):
            print("選択待ちではありません", file=sys.stderr)
            return 1
        r = _gui_post(base, "/api/select", {"dev": d.get("name"), "arm": arm,
                                            "gen": d.get("await_gen")})
        if r.get("error"):
            print(r["error"], file=sys.stderr)
            return _exit_code(r.get("failure", ""))
    else:
        with _client(args) as c:
            st = c.status()
            if not st.get("awaiting"):
                print("選択待ちではありません", file=sys.stderr)
                return 1
            c.select(arm, gen=st.get("await_gen"))
    print(f"選択肢 {args.number} を選びました")
    return 0


def cmd_lap_end(args) -> int:
    """実行中に周回数を変える(runbook.md §2-4)。

    停止して続きを始めるのは、操作画面のサーバーの仕事である(coupling.md D11)。
    """
    p = _project(args)
    base = _gui_base(p, args)
    if base is None:
        print(f"周回を終えるには、操作画面のサーバーが必要です。\n{_NEEDS_SERVER}",
              file=sys.stderr)
        return 1
    if _no_device_named(p, args, "周回数を変える",
                        "python -m padcue --device <名前> lap-end " + args.mode):
        return 1
    d = _gui_dev(base, args)
    if d is None or not (d.get("running") or d.get("awaiting")):
        print("実行中ではありません", file=sys.stderr)
        return 1
    r = _gui_post(base, "/api/lap_end", {"devs": [d.get("name")], "mode": args.mode,
                                         "lap": d.get("lap")})
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        modes = _lap_end_modes(base, d)
        if modes:
            print(f"  選択できる終え方: {' '.join(modes)}", file=sys.stderr)
        return _exit_code(r.get("failure", ""))
    print("周回の終え方を変更しました")
    return 0


def _lap_end_modes(base: str, d: dict) -> list[str]:
    """今選べる終え方。手順の形と今の周で変わるので、拒否するときに付ける。"""
    for g in _gui_get(base, "/api/state").get("groups") or []:
        if g.get("id") == d.get("group"):
            options = (g.get("lap_end") or {}).get("options") or []
            return [o["mode"] for o in options if not o.get("why")]
    return []


def cmd_import(args) -> int:
    """padcue ファイルと、手順の .json、フレーム表の .csv をインポートする。

    同じ名前で中身の違うものは、番号を付けた別の名前で入れ、元からあるものは
    置き換えない。扱いを 1 件ずつ選ぶときは、操作画面でインポートする。スクリプトの
    コマンドを含むファイルは、コマンドを見せて、--yes を付けたときだけ入れる。
    padcue ファイルの中に読めない件があれば、残りを入れて、読めなかった件と理由を
    出し、終了コード 1 で終える。
    """
    proj = _project(args)
    if not Path(args.file).is_file():
        print(f"ファイル「{args.file}」が見つかりません", file=sys.stderr)
        return 1
    try:
        pf = unpack.read(Path(args.file))
    except unpack.PackError as e:
        print(sentences("インポートできません", error_text(e)), file=sys.stderr)
        return 1
    except (zipfile.BadZipFile, OSError):
        print("インポートできません。padcue ファイルが壊れているか、読み込めません。",
              file=sys.stderr)
        return 1
    commands = unpack.script_commands(pf)
    if commands and not args.yes:
        print("このファイルの手順は、次のコマンドを実行します", file=sys.stderr)
        for name, cmd in commands:
            print(f"  {name}: {cmd}", file=sys.stderr)
        print("信頼できるファイルだけをインポートしてください。\n"
              "  インポートするには: python -m padcue import "
              f"{registry.command_arg(args.file)} --yes",
              file=sys.stderr)
        return 1
    try:
        items = unpack.plan(proj, pf)
        got = unpack.apply(proj, pf, items)
    except unpack.ChoiceError as e:
        for kind, name, why in e.problems:
            print(f"{pack.KIND_JA.get(kind, kind)}「{name}」: {why}", file=sys.stderr)
        return 1
    except unpack.ApplyError as e:
        print(e, file=sys.stderr)
        return 1
    for kind, name, *_rest in got["wrote"]:
        print(f"{_PACK_JA[kind]}「{name}」をインポートしました")
    for it in items:
        if it.state == "same":
            print(f"{_PACK_JA[it.kind]}「{it.name}」は同じ内容のため、"
                  "インポートしませんでした")
    for _top, kind, name in got["dangling"]:
        print(f"  {_PACK_JA[kind]}「{name}」が見つかりません", file=sys.stderr)
    # padcue ファイルの中の読めなかった件と理由(file-format.md §10-1)。それを
    # 使用している手順の参照がどれを指すかも、画面と同じ文で付ける(file-format.md §10-5)
    taking = {it.name for it in items
              if it.kind == "procedure" and it.action != unpack.SKIP}
    left = unpack.left_out(proj, pf)
    for u in left:
        print(sentences(f"{_PACK_JA[u['kind']]}「{u['name']}」はインポートできません",
                        u["why"]), file=sys.stderr)
        who = "".join(f"「{n}」" for n in u["pack_users"] if n in taking)
        if who and u["here_name"]:
            print(f"  {who}は元からある「{u['here_name']}」を使用します",
                  file=sys.stderr)
        elif who:
            print(f"  {who}が使用する「{u['name']}」が見つかりません", file=sys.stderr)
    # 1 件でも読めなかったら、残りを入れても 1 で終える(呼び出し元のツールが、
    # すべてを入れられたかを終了コードで分けられるように)
    return 1 if left else 0


def cmd_export(args) -> int:
    """手順を padcue ファイルにエクスポートする(file-format.md §10)。

    参照しているフレーム表と条件も一緒に入る。

    出す先のフォルダーを指さなければ、いまいる場所に置く。ファイルの名前は選んだ
    手順から作る。選んだ手順そのものがないか読めなければ、書かずに拒否する(画面の
    エクスポートと同じ。pack.lost_tops)。呼び出し先などの足りないものがあっても
    中止しない。構成情報から外して、何が足りないかを出す。
    """
    proj = _project(args)
    tops = list(args.names)
    got = pack.collect(proj, tops)
    lost = pack.lost_tops(tops, got)
    if lost:
        for x in lost:
            print(pack.lost_text(*x), file=sys.stderr)
        return 1
    dest = pack.dest_for(proj, tops, got, Path(args.out) if args.out else Path.cwd())
    pack.write(proj, tops, dest)
    print(f"{dest} に保存しました")
    for kind, name in got.missing:
        print(f"  {_PACK_JA[kind]}「{name}」が見つかりません", file=sys.stderr)
    for kind, name in got.unreadable:
        print(f"  {_PACK_JA[kind]}「{name}」を読み込めないため、含まれていません",
              file=sys.stderr)
    for kind, names in (("手順", got.procedures),
                        ("フレーム表", got.frametables),
                        ("条件", got.conditions)):
        if names:
            print(f"  {kind} {len(names)} 件: {'、'.join(names)}")
    return 0


def cmd_ota(args) -> int:
    # 画面が開いていても拒否しない。装置は接続を 4 本受け、同じ padcue の識別子なら
    # 操作権も競合せずに通る。更新の間に装置が入れ替わるので、
    # 画面のレーンは一度「未検出」に見えてから戻る
    # 省いたときは、自分でビルドしたもの(padcue のフォルダーの
    # firmware/build/pademu.bin)。
    # 実行したフォルダーからは決めない
    path = (Path(args.image) if args.image
            else padcue_root() / "firmware" / "build" / "pademu.bin")
    if not path.is_file():
        print(f"ファームウェアが見つかりません: {path}", file=sys.stderr)
        return 1
    image = path.read_bytes()
    # 画面の更新と同じ確認と文。ケーブルで書き込むイメージ(リリースの -ota.bin で
    # ない方)や大きすぎるイメージを、装置へ送る前に拒否する
    try:
        firmware.ota_version(image)
    except firmware.FirmwareError as e:
        print(e, file=sys.stderr)
        return 1
    with _client(args) as c:
        before = c.hello()
        print(f"更新前: {before.fw_version} ({before.partition})")

        def show(sent, total):
            print(f"\r転送中 {sent * 100 // total}% "
                  f"({sent // 1024}/{total // 1024} KiB)", end="", file=sys.stderr)

        try:
            r = c.ota(image, progress=show)
        except BaseException:
            # 進み具合の行は改行せずに上書きしている。拒否の理由がその行に
            # 続かないよう、改行してから上へ伝える
            print(file=sys.stderr)
            raise
        print(f"\n書き込み完了: {r['written']} バイト(書き込み先 {r['partition']})")
        print("装置が再起動します。起動に失敗した場合は、自動で更新前のファームウェアに"
              "戻ります。")
        print("  再起動後の状態を確認するには: python -m padcue status")
    return 0


def cmd_device(args) -> int:
    """装置の登録情報の管理。使い方の一覧はモジュール docstring(--help)にある。"""
    p = _project(args)
    cfg = p.load_config()
    devs = cfg.get("devices", [])
    a = args.address

    if a == "list":
        for i, d in enumerate(devs):
            mark = "(既定)" if i == 0 else ""
            print(f"{d.get('name', '?'):<8} {d.get('host')}:{d.get('port')}"
                  f"  id={d.get('id') or '未割り当て'} {mark}")
        if not devs:
            print("装置が登録されていません")
        return 0

    if a == "add":
        if not args.extra:
            print("使用法: python -m padcue device add <IP アドレス>[:<ポート>] [名前]",
                  file=sys.stderr)
            return 1
        # 接続して装置 ID を確認してから登録する。ID を報告しない
        # 相手は登録しない(照合できない装置を登録情報に入れると誤操作防止が
        # 成り立たない。P23)
        host, port = _split_address(args.extra[0])
        ok, msg, failure = registry.add_device(
            p, host, args.extra[1] if len(args.extra) > 1 else "",
            port=port)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else _exit_code(failure)

    if a == "rename":
        if len(args.extra) != 2:
            print("使用法: python -m padcue device rename <名前> <新しい名前>",
                  file=sys.stderr)
            return 1
        ok, msg = registry.rename_device(p, args.extra[0], args.extra[1])
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if a == "forget":
        # 装置を交換したとき(基板が変わり MAC も変わる)に、割り当てを解除する。
        # 消すのは登録情報の装置 ID だけである
        if len(args.extra) != 1:
            print("使用法: python -m padcue device forget <名前>", file=sys.stderr)
            return 1
        ok, msg = registry.forget_device(p, args.extra[0])
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if a == "remove":
        if len(args.extra) != 1:
            print("使用法: python -m padcue device remove <名前>", file=sys.stderr)
            return 1
        ok, msg = registry.remove_device(p, args.extra[0])
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if a == "auto":
        # 1 台目(または --device 指定)のアドレスを探索で追いかける。
        # 追いかけるのは記録した装置(ID 一致)だけで、別の装置へは乗り換えない
        dev = _pick_device(args, cfg)
        if not dev.get("id"):
            print(f"{dev.get('name')} は未割り当てです。\n"
                  "  割り当てるには: python -m padcue --device "
                  f"{registry.command_arg(dev.get('name', ''))} device <IP アドレス>",
                  file=sys.stderr)
            return 1
        match = [f for f in discover() if f.device_id == dev["id"]]
        if not match:
            # 模擬装置も探索の問い合わせに応えるが、電源と Wi-Fi の話は当てはまらない
            print(_mock_unreachable(p, dev) if is_mock(dev["id"])
                  else "登録した装置が見つかりません。"
                       "電源と Wi-Fi を確認してください。",
                  file=sys.stderr)
            return 3
        if not p.update_device(cfg, cfg["devices"].index(dev),
                               host=match[0].host, port=match[0].port):
            print(_not_in_config_text(dev.get("name", "")), file=sys.stderr)
            return 1
        print(f"接続先を {match[0].host} に設定しました")
        return 0

    addr = a.strip()
    if not addr:
        print("接続先(IP アドレス・名前・auto)を指定してください", file=sys.stderr)
        return 1
    if args.extra:
        # 接続先の後ろに引数は取らない。余分な引数は打ち間違いのしるしである
        # (「device lsit」「device forgt 2P」)。黙って捨てると、1 台目の接続先が
        # 打ち間違いの文字列になる
        print(f"「{' '.join([a, *args.extra])}」は使用できません。\n"
              "  ヘルプを表示するには: python -m padcue device --help", file=sys.stderr)
        return 1
    # discover が出す「アドレス:ポート」をコピーしても使えるようにする(--host と
    # device add と同じ書き方)
    addr, port = _split_address(addr)
    dev = _pick_device(args, cfg)
    if not dev.get("id"):
        # 未割り当ての行なら、アドレスを打ったのは人なので、そこにいた装置を
        # この行の装置として覚える(どの装置を使うかを人が決める、ということ
        # そのもの)。画面の「割り当て」と同じチェックを通す。別の行に登録済みの
        # 装置を覚えると、同じ装置を 2 つの行が指す
        ok, msg, failure = registry.pick_device(p, dev.get("name", ""), addr,
                                                port=port or dev.get("port"))
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else _exit_code(failure)
    port = port or int(dev.get("port", proto.DEFAULT_PORT))
    # 向け先を確認する。登録した装置 ID と違う装置がいれば、模擬装置でも拒否する
    try:
        probe = DeviceClient(addr, port, timeout=1.5)
        info = probe.connect()
        probe.close()
        if info.device_id and info.device_id != dev["id"]:
            # 別の装置がいる宛先へ向け替えるのは、この装置を別の装置に
            # 差し替えることである。人が決めた装置を黙って捨てない
            print(f"{addr} に接続されているのは、{dev.get('name')} の装置"
                  f"(装置 ID {dev['id'][-4:].upper()})ではなく、別の装置"
                  f"(装置 ID {info.device_id[-4:].upper()})です。"
                  "装置を交換したときは、割り当てを解除してください。\n"
                  "  割り当てを解除するには: python -m padcue device forget "
                  f"{registry.command_arg(dev.get('name', ''))}", file=sys.stderr)
            return 2      # 登録との不一致(装置が拒否したのと同じ扱い)
    except socket.gaierror as e:
        # 名前が見つからない。打ち間違いの多くはここに来る。保存すると、以後の
        # コマンドがすべてこの接続先で失敗する。操作の名前の打ち間違い(device lsit)
        # でもあるので、使い方の案内も付ける
        print(failure_text(e, addr), file=sys.stderr)
        print("  ヘルプを表示するには: python -m padcue device --help", file=sys.stderr)
        return 3
    except (OSError, DeviceError):
        pass          # まだ起動していない宛先も設定はできる
    if not p.update_device(cfg, cfg["devices"].index(dev), host=addr, port=port):
        print(_not_in_config_text(dev.get("name", "")), file=sys.stderr)
        return 1
    print(f"{dev.get('name')} の接続先を {addr} に設定しました")
    return 0


def cmd_discover(args) -> int:
    found = discover(timeout=args.timeout)
    if not found:
        # 装置につながらないときと同じ終了コードにする(device auto と同じ)
        print("装置が見つかりません。装置の電源が入っているか、PC と同じ LAN の "
              "Wi-Fi に接続されているかを確認してください。", file=sys.stderr)
        return 3
    for f in found:
        print(f)
    return 0


def cmd_mock(args) -> int:
    # 模擬装置と操作画面は、その付随部品(HTTP サーバー・ブラウザー起動・
    # 記録)の読み込みを連鎖的に伴う。使う命令のときだけ読み込んで、
    # `padcue status` のような短い命令の起動を遅くしない
    from .discover import PORT as DISCOVER_PORT
    from .mockdevice import MockDevice
    if not is_mock(args.id):
        # mock で始まらない装置 ID は実機として扱われ、「デモ用の模擬装置」で
        # あることが画面にもコマンドにも出ない
        print(f"装置 ID「{args.id}」は使用できません。mock で始まる ID を指定して"
              "ください(例: mock2p000000)。", file=sys.stderr)
        return 1
    d = MockDevice(host="127.0.0.1", port=args.port, speed=args.speed,
                   device_id=args.id)
    # 本体(TCP)の待ち受けに失敗したら偽の成功を表示しない(排他 bind に
    # したため、二重起動はここで確実に失敗する)
    try:
        port = d.start()
    except OSError:
        print(f"ポート {args.port} は使用中です。模擬装置が起動済みなら、そのまま"
              "接続できます。\n"
              "  別のポートで起動するには: python -m padcue mock --port 5556",
              file=sys.stderr)
        return 1
    # 探索の問い合わせにも応える(padcue discover に出る)。start(discover_port=...)
    # にまとめないのは、ここだけ失敗しても登録すれば使えるため
    try:
        d.start_discover(DISCOVER_PORT)
    except OSError:
        print(f"  探索用のポート {DISCOVER_PORT} は使用中です。"
              "python -m padcue discover には表示されません。", file=sys.stderr)
    print(f"模擬装置を起動しました: 127.0.0.1:{port}")
    print(f"  登録するには: python -m padcue device add 127.0.0.1:{port} 1P")
    print("  終了するには: Ctrl+C")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        d.stop()
    return 0


# 操作画面の既定のポート
GUI_PORT = 8765


# デモ用の模擬装置が使うポート。1 台目が装置の既定のポートと同じなのは、
# `padcue mock` を単独で起動したときと登録情報の形を揃えるためである
DEMO_PORT = proto.DEFAULT_PORT


def start_demo_mocks(project, count: int) -> list:
    """デモ用の模擬装置を同じプロセスの中で起動し、登録情報になければ登録する。

    別のウィンドウで起動すると、デモのたびにウィンドウが台数ぶん残り、2 回目からは登録の
    コマンドが「既にあります」と拒否される文を出す。操作画面と同じ
    プロセスに置けば、ウィンドウは 1 つで、Ctrl+C でまとめて終わる。
    """
    from .mockdevice import MockDevice
    mocks = []
    try:
        for i in range(count):
            name, dev_id = f"{i + 1}P", f"mock{i + 1:08d}"
            port = DEMO_PORT + i
            # デモは実時間で動かす。MockDevice の既定の速さはテスト用の早送りで、
            # そのままだと 3 周が 1 秒で終わり、進行の線が周をまたいで飛び飛びになる
            d = MockDevice(host="127.0.0.1", port=port, device_id=dev_id,
                           speed=1.0)
            try:
                d.start()
            except OSError:
                raise SystemExit(
                    f"ポート {port} は使用中です。模擬装置か別の padcue が"
                    "起動していないか確認してください。") from None
            mocks.append(d)
            known = {x.get("id") for x in project.load_config().get("devices", [])}
            if dev_id not in known:
                ok, msg, _failure = registry.add_device(project, "127.0.0.1",
                                                        name, port)
                if not ok:
                    raise SystemExit(msg)
    except BaseException:
        for d in mocks:
            d.stop()
        raise
    return mocks


def cmd_gui(args) -> int:
    from .gui import serve  # cmd_mock と同じ理由で、ここで読み込む
    project = _project(args)
    if args.host:
        # 画面は登録情報の装置に接続する。黙って無視すると、指定した装置に
        # 接続したと思わせる
        print("操作画面は登録した装置に接続するため、--host は指定できません。\n"
              "  登録した装置の接続先を変更するには: python -m padcue device "
              "<IP アドレス>",
              file=sys.stderr)
        return 1
    # 実機と模擬装置を混ぜない(registry._mix_refusal)。登録は混ざらないように拒否する
    # ので、ここで見るのは手で編集した登録情報と、実機のあるフォルダーでのデモである
    devs = [d for d in project.load_config().get("devices", []) if d.get("id")]
    real = [d.get("name", "?") for d in devs if not is_mock(d["id"])]
    if args.mock and real:
        print("このプロジェクトには実機が登録されているため、模擬装置では起動できません"
              f"({'・'.join(real)})。\n"
              "  実機で使うには: padcue.bat を開く\n"
              "  デモで使うには、実機の登録を削除する: "
              "python -m padcue --project <フォルダー> device remove <名前>",
              file=sys.stderr)
        return 1
    if real and len(real) < len(devs):
        print("登録情報に、実機と模擬装置が混ざっています。\n"
              "  どちらかを削除するには: python -m padcue device remove <名前>",
              file=sys.stderr)
        return 1
    mocks = start_demo_mocks(project, args.mock) if args.mock else []
    try:
        # ポートを指定しなかったときだけ、使用中なら続くポートを試す。指定した
        # ポートで開けないのに黙って別のポートで開くと、指定した意味がなくなる
        port = GUI_PORT if args.port is None else args.port
        return serve(project, port, args.open, fallback=args.port is None,
                     demo=bool(args.mock))
    finally:
        for d in mocks:
            d.stop()


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """使用法の行の頭を日本語にし、引数の説明を必ず次の行に出す。

    argparse は説明の列を文字の数で揃えるので、全角の名前(フォルダー・周回数)を
    含む行だけ列がずれる。説明を次の行に下げれば、揃える列が必要ない。
    説明の位置を字下げ(2)と間(2)の和の 4 にすると、どんなに短い引数(値)も
    同じ行に収まらず、必ず下がる。
    """

    def __init__(self, prog, **kw):
        super().__init__(prog, max_help_position=4, **kw)

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups,
                          "使用法: " if prefix is None else prefix)


class _Parser(argparse.ArgumentParser):
    """ヘルプの見出しと -h の説明と、引数の誤りの文を日本語にした ArgumentParser。

    下位コマンドの parser も、親と同じこの型で作られる(add_subparsers の既定)。
    """

    def __init__(self, **kw):
        super().__init__(formatter_class=_HelpFormatter, add_help=False, **kw)
        self._positionals.title = "引数"
        self._optionals.title = "オプション"
        self.add_argument("-h", "--help", action="help", help="ヘルプを表示して終了")

    def error(self, message):
        """引数の誤り。使用法の行と日本語の文を出し、終了コード 1 で終える。

        argparse の既定は英語の文と終了コード 2 で、2 は「装置が拒否した」と
        読める(CONTRIBUTING.md「コマンドの出力」)。
        """
        self.print_usage(sys.stderr)
        self.exit(1, f"{_arg_error_ja(message)}\n"
                     f"  ヘルプを表示するには: {self.prog} --help\n")


def _arg_error_ja(message: str) -> str:
    """argparse の引数の誤りの文を日本語にする。知らない形は、誤りがあることだけを
    示す(英語の文は出さない)。"""
    m = re.match(r"the following arguments are required: (.*)", message)
    if m:
        return f"引数が足りません: {m.group(1)}"
    # 使用できるコマンドは並べない。次の行の --help の案内が一覧を出す。
    # Python 3.15 からは間に近いコマンドの案内(", maybe you meant 'run'?")が入る
    m = re.search(r"invalid choice: '?([^']*)'?(?:, maybe you meant .*)?"
                  r" \(choose from .*\)", message)
    if m:
        return f"「{m.group(1)}」というコマンドはありません"
    m = re.match(r"argument (\S+): invalid (float|int) value: '(.*)'", message)
    if m:
        what = "整数" if m.group(2) == "int" else "数値"
        return f"{m.group(1)} の値「{m.group(3)}」は{what}ではありません"
    m = re.match(r"argument (\S+): expected one argument", message)
    if m:
        return f"{m.group(1)} の値を指定してください"
    m = re.match(r"argument (\S+): not allowed with argument (\S+)", message)
    if m:
        return f"{m.group(2)} と {m.group(1)} は同時に指定できません"
    m = re.match(r"unrecognized arguments: (.*)", message)
    if m:
        return f"使用できない引数があります: {m.group(1)}"
    return "引数が正しくありません"


# config の -h に出す、設定できるキーの一覧
_CONFIG_HELP = """装置の設定値を書き込む

  frame_period_ns  1 フレームの長さ(ナノ秒、1000 から 100000000 の整数)
                   60.00Hz は 16666667、59.94Hz は 16683333
  usb_binterval    USB の問い合わせの間隔(ミリ秒、1 から 16 の整数、既定は 1)
                   次の起動から使用
  wifi_ssid        装置が接続する Wi-Fi の名前(次の起動から使用)
  wifi_pass        その Wi-Fi のパスワード(パスワードのない Wi-Fi では空)"""


def build_parser() -> argparse.ArgumentParser:
    # コマンドの一覧は __doc__ の 1 つだけにする。下位コマンドの説明は、そのコマンドの
    # --help に出す description に書く(help に書くと、argparse が 2 つ目の一覧を作る)
    ap = _Parser(prog="python -m padcue", description=__doc__)
    ap.add_argument("--version", action="version", version=f"padcue {__version__}",
                    help="バージョンを表示して終了")
    ap.add_argument("--project", default=None, metavar="フォルダー",
                    help="プロジェクトのフォルダー"
                         "(既定: padcue のフォルダーの data)")
    ap.add_argument("--host", default="", metavar="アドレス",
                    help="装置 ID を照合せずに、この IP アドレス"
                         "(「アドレス:ポート」も可)の装置へ接続する。"
                         "復旧のときに使用する。")
    ap.add_argument("--device", default="", metavar="名前",
                    help="操作する装置の名前。省略すると 1 台目になる。"
                         "ただし stop は、省略するとすべての装置になる。"
                         "装置が 2 台以上あるときの run、select、lap-end では"
                         "省略できない。")
    sub = ap.add_subparsers(dest="cmd", required=True, title="コマンド",
                            metavar="<コマンド>", help="上の一覧のいずれか")

    sub.add_parser("init", description="プロジェクトのひな形を作成").set_defaults(
        func=cmd_init)

    p = sub.add_parser("build", description="手順を変換し、警告を表示")
    p.add_argument("name", nargs="?", metavar="名前",
                   help="手順の名前(既定: すべての手順)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("push", description="手順を変換して、装置に転送・保存")
    p.add_argument("name", metavar="名前", help="手順の名前")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("run", description="手順を実行")
    p.add_argument("name", metavar="名前", help="手順の名前")
    p.add_argument("-n", "--laps", default="1", metavar="周回数",
                   help="周回数の合計。「無限」を指定すると、停止するまでくり返す。")
    # グループの開始は進捗を表示しないので、-w と同時には指定できない
    # (指定しても有効にならないまま戻ると、有効にならなかったことに気づかない)
    how = p.add_mutually_exclusive_group()
    how.add_argument("-w", "--watch", action="store_true",
                     help="進捗を表示し続ける。"
                          "表示中の Ctrl+C は、装置の実行も停止する。")
    how.add_argument("--group", nargs="+", metavar="装置",
                     help="指定した装置を 1 つのグループとして開始する"
                          "(操作画面のサーバーが必要)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("select", description="選択待ちの選択肢を選ぶ")
    p.add_argument("number", type=int, metavar="番号",
                   help="選択肢の番号(上から 1 始まり)")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("lap-end", description="実行中に周回数を変える"
                                              "(操作画面のサーバーが必要)")
    p.add_argument("mode", choices=("end", "next", "now", "skip", "revert"),
                   metavar="終え方",
                   help="end(今の周で終える)、next(次の周を最後の回にして終える)、"
                        "now(今から最後の回にして今の周で終える)、"
                        "skip(最後の回を飛ばして今の周で終える)、"
                        "revert(元の周回数に戻す)")
    p.set_defaults(func=cmd_lap_end)

    p = sub.add_parser("stop",
                       description="停止(--device の装置だけ。既定: すべての装置)")
    how = p.add_mutually_exclusive_group()
    how.add_argument("--graceful", action="store_true",
                     help="次のラベル・周の境・選択待ちのいずれかで停止する")
    how.add_argument("--cancel", action="store_true",
                     help="--graceful で予約した停止を取り消す")
    p.set_defaults(func=cmd_stop)

    sub.add_parser("status", description="状態を表示").set_defaults(func=cmd_status)

    p = sub.add_parser("config", description=_CONFIG_HELP)
    p.add_argument("key", metavar="キー", help="設定の名前(上の一覧のいずれか)")
    p.add_argument("value", nargs="?", metavar="値",
                   help="省くと、実行してから入力を求める。"
                        "wifi_pass は、入力した文字を表示しない。")
    p.set_defaults(func=cmd_config)

    sub.add_parser("identify", description="装置の LED を 5 秒点滅させる"
                   ).set_defaults(func=cmd_identify)
    p = sub.add_parser("reconnect-usb",
                       description="装置の USB を切断して再接続する。Switch に、装置を"
                                   "挿し直したときと同じように認識し直させる。")
    p.add_argument("--hold", type=int, default=proto.USB_RECONNECT_HOLD_MS_DEFAULT,
                   metavar="ミリ秒",
                   help=f"切断時間({proto.USB_RECONNECT_HOLD_MS_MIN} から "
                        f"{proto.USB_RECONNECT_HOLD_MS_MAX} の整数、既定は "
                        f"{proto.USB_RECONNECT_HOLD_MS_DEFAULT})")
    p.set_defaults(func=cmd_reconnect_usb)
    sub.add_parser("clear-error", description="異常停止(赤 LED)を解除"
                   ).set_defaults(func=cmd_clear_error)

    p = sub.add_parser("take-owner",
                       description="別の padcue から操作権を取得")
    p.add_argument("--yes", action="store_true",
                   help="確認せずに取得する")
    p.set_defaults(func=cmd_take_owner)
    sub.add_parser("logs", description="装置のログを取得").set_defaults(func=cmd_logs)
    sub.add_parser("list", description="装置に保存した手順の一覧を表示"
                   ).set_defaults(func=cmd_list)

    p = sub.add_parser("export", description="手順を padcue ファイルにエクスポート")
    p.add_argument("names", nargs="+", metavar="名前",
                   help="エクスポートする手順の名前")
    p.add_argument("-o", "--out", metavar="フォルダー",
                   help="保存先のフォルダー(既定: 現在のフォルダー)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", description="padcue ファイル、手順の .json、"
                                             "フレーム表の .csv をインポート")
    p.add_argument("file", metavar="ファイル", help="インポートするファイル")
    p.add_argument("--yes", action="store_true",
                   help="スクリプトのコマンドを含むファイルもインポートする")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("ota", description="ファームウェアを無線で更新")
    p.add_argument("image", nargs="?", default=None, metavar="ファイル",
                   help="リリースの padcue-firmware-<バージョン>-ota.bin"
                        "(既定: 自分でビルドした firmware/build/pademu.bin)")
    p.set_defaults(func=cmd_ota)

    p = sub.add_parser("device", description="装置の登録と接続先の設定")
    p.add_argument("address", metavar="操作",
                   help="list、add、rename、forget、remove、auto、または IP アドレス")
    p.add_argument("extra", nargs="*", metavar="引数",
                   help="add <IP アドレス> [名前]、rename <名前> <新しい名前>、"
                        "forget <名前>、remove <名前>")
    p.set_defaults(func=cmd_device)

    p = sub.add_parser("discover", description="LAN 内の装置を検索")
    p.add_argument("--timeout", type=float, default=1.5, metavar="秒",
                   help="応答を待つ秒数(既定: 1.5)")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("mock", description="模擬装置を起動")
    p.add_argument("--port", type=int, default=proto.DEFAULT_PORT, metavar="ポート",
                   help=f"模擬装置の待ち受けポート(既定: {proto.DEFAULT_PORT})")
    p.add_argument("--speed", type=float, default=1.0, metavar="倍率",
                   help="実行の早送りの倍率")
    p.add_argument("--id", default="mock00000000", metavar="ID",
                   help="装置 ID。mock で始める。複数の模擬装置を同時に起動する"
                        "ときは、別々の ID にする(例: mock2p000000)。")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("gui", description="操作画面を開く")
    p.add_argument("--port", type=int, default=None, metavar="ポート",
                   help=f"操作画面の待ち受けポート。指定しなければ {GUI_PORT} を"
                        f"使用し、ほかのプログラムが使用中なら {GUI_PORT + 1} から順に"
                        "空いているポートを使用する。指定したポートが使用中なら"
                        "開かない。")
    p.add_argument("--no-open", dest="open", action="store_false",
                   help="ブラウザーを自動で開かない")
    p.add_argument("--mock", type=int, default=0, metavar="台数",
                   help="デモ用の模擬装置を、この操作画面と一緒に起動する。"
                        "模擬装置は、このプロジェクトの登録情報に追加される。"
                        "実機を登録したプロジェクトでは使用できない。")
    p.set_defaults(func=cmd_gui, open=True)
    return ap


# 出力先の文字コードで表せない文字の代わり(_standin)。status の出力を
# ファイルに保存するときに、記号と単位が「?」にならないようにする
_STANDIN = {"⚠": "※", "µ": "μ"}


def _standin(err: UnicodeEncodeError):
    """表せない文字を、表せる代わりの文字にする。代わりもなければ「?」にする。"""
    out = []
    for ch in err.object[err.start:err.end]:
        rep = _STANDIN.get(ch, "?")
        try:
            rep.encode(err.encoding)
        except (UnicodeEncodeError, LookupError):
            rep = "?"
        out.append(rep)
    return "".join(out), err.end


codecs.register_error("padcue-standin", _standin)


def main(argv=None) -> int:
    # 出力先を pipe やファイルにしたときの Python は、Windows では cp932 で
    # 書くので、表せない文字(⚠・µ)で print が UnicodeEncodeError になり、
    # 表示が途中で異常終了する。表せない文字は代わりの文字にして、表示は最後
    # まで出す(端末に出すときは UTF-16 で書くので、文字は失われない)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="padcue-standin")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Ctrl+C でやめた。終了コードは中断の慣習の 130 で、失敗の 1〜3 と分ける
        print("\n中断しました", file=sys.stderr)
        return 130
    except DeviceError as e:
        # 「装置が拒否しました」と表示するのは、装置が拒否したときだけである。登録との
        # 不一致のように PC 側で拒否したときは、理由の文だけを出す。どちらも
        # 内部のコード(NOT_PADEMU など)は出さない
        said = e.message or e.code
        print(f"装置が拒否しました: {said}" if e.by_device else said,
              file=sys.stderr)
        if e.code == "NOT_OWNER":
            # 別の padcue が操作権を持っている。取り方を付ける
            dev = _dev_arg(args)
            opt = f"--device {registry.command_arg(dev)} " if dev else ""
            print(f"  操作権を取得するには: python -m padcue {opt}take-owner",
                  file=sys.stderr)
        if e.code == "DEVICE_MISMATCH":
            # 修正方法はここで付ける。例外の文言に混ぜると、割り当てを解除する
            # ボタンを持つ画面でも同じ指示が二重に出る
            name = getattr(e, "name", "")
            name = registry.command_arg(name) if name else "<名前>"
            print("装置を交換したときや、デモ用の模擬装置に切り替えるときは、"
                  "割り当てを解除してください。\n"
                  f"  割り当てを解除するには: python -m padcue device forget {name}",
                  file=sys.stderr)
        return 2
    except OSError as e:
        # ファイルの読み書きの失敗は 1、装置につながらないときは 3 である
        print(_fail_text(e), file=sys.stderr)
        return _exit_of(e)
    except Unreachable as e:
        print(e, file=sys.stderr)
        return 3
    except ValueError as e:
        # 名前の誤りや、手で書き換えて読めなくなった padcue.json など。文は
        # 理由を示す日本語である(project.ConfigError、names.validate_name)。
        # Python が書いた英語の文は、そのまま出さず、操作画面のサーバーと同じく想定して
        # いない失敗の文にする(unexpected_text)
        print(e if for_user(e) else unexpected_text(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
