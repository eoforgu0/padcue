"""switchctl コマンドライン。GUI が扱わない操作と、GUI の起動を担う。

    switchctl init                    プロジェクトの雛形を作る
    switchctl build <名前>            フローをコンパイル(警告も表示)
    switchctl push <名前>             コンパイル → 転送 → 保存
    switchctl run <名前> [-n 回数]    実行(転送済みの手順)
    switchctl stop [--graceful]       停止
    switchctl status                  状態表示
    switchctl logs                    ログ回収
    switchctl list                    デバイス内の手順一覧
    switchctl device <IP>|auto        接続先を設定(auto で自動検出)
    switchctl discover                LAN 内のマイコンを探す
    switchctl mock                    模擬デバイスを起動(実機なしの動作確認)
    switchctl gui                     操作画面を開く
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .client import DeviceClient, DeviceError
from .discover import discover
from .flowfmt import FlowError
from .project import Project


def _project(args) -> Project:
    return Project(Path(args.project).resolve())


def _client(args) -> DeviceClient:
    """接続先を決めて繋ぐ。控えた IP で繋がらなければ LAN 内を探し直す。

    マイコンの IP は DHCP で決まるため変わりうる。変わっていたら自動で
    見つけ直して控え直すので、手で IP を探す必要はない。
    """
    p = _project(args)
    cfg = p.load_config()
    port = int(cfg.get("port", 5555))
    host = args.host or cfg.get("host") or ""
    if host:
        c = DeviceClient(host, port)
        try:
            c.connect()
            return c
        except (OSError, ConnectionError, DeviceError):
            c.close()
            print(f"{host} に繋がりません。LAN 内を探します…", file=sys.stderr)
    found = discover()
    if not found:
        raise SystemExit(
            "マイコンが見つかりません。電源と WiFi を確認してください"
            "(接続先を指定する場合: switchctl device <IPアドレス>)")
    # 探索の返事は「届いた経路の住所」で見えるため、PC に仮想アダプタ(VPN・
    # 仮想マシン)があると自分の別の住所が候補に混じる。実際に繋がるものだけを
    # 採用する(確かめずに控え直すと、以後ずっと繋がらない住所を覚えてしまう)
    for target in found:
        c = DeviceClient(target.host, target.port)
        try:
            c.connect()
        except (OSError, ConnectionError, DeviceError):
            c.close()
            continue
        cfg["host"], cfg["port"] = target.host, target.port
        p.save_config(cfg)
        print(f"見つかりました: {target.host}(控え直しました)", file=sys.stderr)
        return c
    where = " / ".join(f.host for f in found)
    raise SystemExit(
        f"{where} は見つかりましたが、つないでも応答しません。" + "\n"
        "  ・操作画面(padctl.bat)を開いていませんか? "
        "実機は同時に1つのプログラムしか受け付けません。"
        "閉じてからやり直してください" + "\n"
        "  ・同じ PC の別のネットワーク口が答えた可能性もあります。"
        "その場合は実機の IP を指定してください: switchctl device <IPアドレス>")


def _print_build(r) -> None:
    print(f"{r.name}: {r.total_frames} フレーム "
          f"({r.seconds:.1f} 秒) / {r.events} イベント / hash {r.hash}")
    if r.pre:
        print(f"  前提条件: {r.pre}")
    for w in r.warnings:
        print(f"  ⚠ {w['line']}行目: {w['msg']}")


def cmd_init(args) -> int:
    p = _project(args)
    p.init_sample()
    print(f"雛形を作成しました: {p.root}")
    print("  procedures/サンプル.flow.json, parts/サンプル部品.csv")
    return 0


def cmd_build(args) -> int:
    p = _project(args)
    names = [args.name] if args.name else p.procedure_names()
    if not names:
        print("手順がありません(switchctl init で雛形を作れます)")
        return 1
    failed = 0
    for name in names:
        try:
            _print_build(p.build(name))
        except (FlowError, ValueError) as e:
            print(f"{name}: エラー {e}")
            failed += 1
    return 1 if failed else 0


def cmd_push(args) -> int:
    p = _project(args)
    try:
        r = p.build(args.name)
    except (FlowError, ValueError) as e:
        print(f"コンパイル失敗: {e}")
        return 1
    _print_build(r)
    with _client(args) as c:
        h = c.put(r.name, r.blob)
        c.commit(r.name)
        print(f"転送・保存しました(hash {h})")
    return 0


def cmd_run(args) -> int:
    p = _project(args)
    with _client(args) as c:
        target = args.name
        listing = {e["name"]: e for e in c.list()}
        if target not in listing:
            print(f"デバイスに '{target}' がありません。switchctl push してください")
            return 1
        c.run(target, listing[target]["hash"], loop_n=args.loops)
        print(f"実行開始: {target} ×{args.loops}")
        if args.watch:
            try:
                while True:
                    st = c.status()
                    if not st.get("running"):
                        print(f"\n終了: {st.get('state')}")
                        break
                    print(f"\r{st.get('session_loop', 0)}/{args.loops} 周 "
                          f"{st.get('frames_elapsed', 0)} フレーム", end="")
                    time.sleep(0.5)
            except KeyboardInterrupt:
                c.stop("immediate")
                print("\n停止しました")
    return 0


def cmd_stop(args) -> int:
    with _client(args) as c:
        if args.cancel:
            c.stop("cancel")
            print("区切り停止の予約を取り消しました"
                  "(既に止まっていた場合は停止のままです)")
        else:
            c.stop("graceful" if args.graceful else "immediate")
            print("区切り停止を指示しました" if args.graceful else "即時停止しました")
    return 0


def cmd_status(args) -> int:
    with _client(args) as c:
        info = c.hello()
        st = c.status()
        print(f"ファーム   : {info.fw_version} ({info.partition})")
        print(f"転送方式   : {info.transport_mode} / bInterval={info.binterval}")
        print(f"状態       : {st.get('state')}")
        print(f"USB        : {'接続' if st.get('usb_mounted') else '未接続'}"
              f" / 到達段階 0x{st.get('breadcrumb', 0):03x}")
        if "imu_enabled" in st:
            print("ジャイロ   : "
                  + ("本体が有効化済み" if st["imu_enabled"]
                     else "本体からの有効化なし(値を送っても読まれません)"))
        if st.get("running"):
            print(f"実行中     : {st.get('proc', '')} "
                  f"{st.get('session_loop')} 周目 / {st.get('frames_elapsed')} フレーム")
        # ずれの実測は 0 でも出す。出ていないのが「遅れていない」のか
        # 「測っていない」のか分からないと、計器として意味を成さない
        if "max_late_us" in st:
            line = f"ずれ最大   : 切り替え {st['max_late_us']}µs"
            if "deliver_max_us" in st:
                line += f" / 送出まで {st['deliver_max_us']}µs"
            over = st.get("late_events", 0) + st.get("deliver_late", 0)
            if over:
                line += f"  ⚠ 超過 {over} 回"
            print(line)
        if st.get("log_dropped"):
            print(f"⚠ 記録落ち : {st['log_dropped']} 件"
                  "(この間の記録は残っていません)")
        lost_reply = (st.get("dropped_replies", 0) + st.get("failed_replies", 0)
                      + st.get("bad_reports", 0))
        if lost_reply or st.get("dropped_inputs"):
            print(f"⚠ 送出失敗 : 応答 {lost_reply} 件 "
                  f"/ 通常入力 {st.get('dropped_inputs', 0)} 件")
        if info.rolled_back:
            print("⚠ 前回の更新はロールバックされました")
    return 0


def cmd_mode(args) -> int:
    """転送方式を切り替える(プロコン方式 ⇔ 保険モード)。

    プロコン方式が Switch 2 で受理されない場合の逃げ道。無線で切り替えられる
    ので、書き込みモードに入れ直す必要はない(切替後に USB を挿し直す)。
    """
    with _client(args) as c:
        c.set_mode(args.mode)
    name = "プロコン方式" if args.mode == "procon" else "保険モード(HIDパッド)"
    print(f"{name} に切り替えました。USB を一度抜き挿ししてください")
    return 0


def cmd_config(args) -> int:
    """設定値を書く(電源を切っても残る)。

    frame_period_ns: 1 フレームの長さ。60.00Hz=16666667 / 59.94Hz=16683333
    """
    val = int(args.value) if args.value.lstrip("-").isdigit() else args.value
    with _client(args) as c:
        c.config(args.key, val)
    print(f"{args.key} = {val} を保存しました")
    return 0


def cmd_clear_error(args) -> int:
    with _client(args) as c:
        c.clear_error()
    print("異常状態を解除しました")
    return 0


def cmd_logs(args) -> int:
    with _client(args) as c:
        entries = c.logs()
        if not entries:
            print("(ログなし)")
        for e in entries:
            print(f"[{e['t_ms'] / 1000:9.3f}s] {e['kind']:<14} "
                  f"a={e.get('a', 0)} b={e.get('b', 0)} c={e.get('c', 0)}")
    return 0


def cmd_list(args) -> int:
    with _client(args) as c:
        for e in c.list():
            print(f"{e['name']:<24} {e['size']:>7} バイト  {e['hash']}")
    return 0


def cmd_ota(args) -> int:
    path = Path(args.image)
    if not path.is_file():
        # 既定パスはリポジトリ直下から見た相対。runbook の手順どおり
        # pctool から実行しても見つかるよう、リポジトリ基準でも探す
        alt = (Path(__file__).resolve().parents[2]
               / "firmware" / "build" / "padctl.bin")
        if Path(args.image) == Path("firmware/build/padctl.bin") \
                and alt.is_file():
            path = alt
        else:
            print(f"ファームウェアが見つかりません: {path}")
            return 1
    image = path.read_bytes()
    with _client(args) as c:
        before = c.hello()
        print(f"更新前: {before.fw_version} ({before.partition})")

        def show(sent, total):
            print(f"\r転送中 {sent * 100 // total}% "
                  f"({sent // 1024}/{total // 1024} KB)", end="")

        r = c.ota(image, progress=show)
        print(f"\n書き込み完了: {r['written']} バイト → {r['partition']}")
        print("デバイスが再起動します。数秒後に switchctl status で確認してください")
        print("(起動に失敗した場合は自動で前のファームへ戻ります)")
    return 0


def cmd_device(args) -> int:
    p = _project(args)
    cfg = p.load_config()
    if args.address == "auto":
        found = discover()
        if not found:
            print("マイコンが見つかりません(電源と WiFi を確認してください)")
            return 1
        cfg["host"], cfg["port"] = found[0].host, found[0].port
        p.save_config(cfg)
        print(f"接続先を {found[0].host} に設定しました")
        return 0
    addr = args.address.strip()
    if not addr:
        # 空にすると以後どのコマンドも「接続先が未設定です」になる
        print("接続先が空です。IP か名前(padctl.local)を指定するか、"
              "auto で探してください")
        return 1
    cfg["host"] = addr
    p.save_config(cfg)
    print(f"接続先を {addr} に設定しました")
    return 0


def cmd_discover(args) -> int:
    found = discover(timeout=args.timeout)
    if not found:
        print("見つかりません。電源・WiFi・PC が同じネットワークにあるか確認してください")
        return 1
    for f in found:
        print(f)
    return 0


def cmd_mock(args) -> int:
    from .discover import PORT as DISCOVER_PORT
    from .mockdevice import MockDevice
    d = MockDevice(host="127.0.0.1", port=args.port, speed=args.speed)
    # 探索の問いかけにも応える。実機と同じく画面の「探す」で見つかるようにして、
    # 練習の手順が本番と食い違わないようにする
    try:
        port = d.start(discover_port=DISCOVER_PORT)
        findable = True
    except OSError:
        # 5557 が使用中(別の模擬デバイスが動いている等)。
        # この時点で本体の待ち受けは既に始まっているので作り直さない
        port = d.port
        findable = False
    print(f"模擬デバイスを起動しました: 127.0.0.1:{port}(Ctrl+C で終了)")
    if findable:
        print("  操作画面の「探す」でも見つかります")
    else:
        print(f"  ※ 探索用のポート {DISCOVER_PORT} が使用中のため"
              "「探す」では見つかりません(接続先を手で 127.0.0.1 にしてください)")
    print("  別の端末で: switchctl device 127.0.0.1 && switchctl status")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        d.stop()
    return 0


def cmd_gui(args) -> int:
    from .gui import serve
    return serve(_project(args), args.host or "", args.port, args.open)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="switchctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=".", help="プロジェクトフォルダ")
    ap.add_argument("--host", default="", help="デバイスの IP(設定を上書き)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="雛形を作る").set_defaults(func=cmd_init)

    p = sub.add_parser("build", help="コンパイル")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("push", help="コンパイル→転送→保存")
    p.add_argument("name")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("run", help="実行")
    p.add_argument("name")
    p.add_argument("-n", "--loops", type=int, default=1)
    p.add_argument("-w", "--watch", action="store_true", help="進捗を表示し続ける")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("stop", help="停止")
    p.add_argument("--graceful", action="store_true", help="現在の周回を終えてから")
    p.add_argument("--cancel", action="store_true",
                   help="--graceful の予約を取り消す(止まる前なら間に合う)")
    p.set_defaults(func=cmd_stop)

    sub.add_parser("status", help="状態表示").set_defaults(func=cmd_status)

    p = sub.add_parser("mode", help="転送方式を切り替える(無線で可)")
    p.add_argument("mode", choices=["procon", "hidpad"],
                   help="procon=プロコン方式 / hidpad=保険モード(ジャイロ不可)")
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("config", help="設定値を書く(例: frame_period_ns 16683333)")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(func=cmd_config)

    sub.add_parser("clear-error", help="異常状態(赤 LED)を解除"
                   ).set_defaults(func=cmd_clear_error)
    sub.add_parser("logs", help="ログ回収").set_defaults(func=cmd_logs)
    sub.add_parser("list", help="デバイス内の手順一覧").set_defaults(func=cmd_list)

    p = sub.add_parser("ota", help="ファームウェアを無線で更新")
    p.add_argument("image", nargs="?", default="firmware/build/padctl.bin")
    p.set_defaults(func=cmd_ota)

    p = sub.add_parser("device", help="接続先を設定(auto で自動検出)")
    p.add_argument("address", help="IP アドレス、または auto")
    p.set_defaults(func=cmd_device)

    p = sub.add_parser("discover", help="LAN 内のマイコンを探す")
    p.add_argument("--timeout", type=float, default=1.5)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("mock", help="模擬デバイスを起動")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--speed", type=float, default=1.0, help="実行の早送り倍率")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("gui", help="操作画面を開く")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", dest="open", action="store_false",
                   help="ブラウザを自動で開かない")
    p.set_defaults(func=cmd_gui, open=True)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DeviceError as e:
        print(f"デバイスが拒否しました: {e}", file=sys.stderr)
        return 2
    except (ConnectionError, OSError) as e:
        print(f"接続できません: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
