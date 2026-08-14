"""padcue コマンドライン。GUI が扱わない操作と、GUI の起動を担う。

    padcue init                    プロジェクトの雛形を作る
    padcue build <名前>            フローをコンパイル(警告も表示)
    padcue push <名前>             コンパイル -> 転送 -> 保存
    padcue run <名前> [-n 回数]    実行(転送済みの手順)
    padcue stop [--graceful]       停止
    padcue status                  状態表示
    padcue logs                    ログ回収
    padcue list                    デバイス内の手順一覧
    padcue device <IP>|auto        接続先を設定(auto で自動検出)
    padcue discover                LAN 内のマイコンを探す
    padcue mock                    模擬デバイスを起動(実機なしの動作確認)
    padcue gui                     操作画面を開く

  マイコンの保守(操作画面を開いている間と、実行中は受け付けません):

    padcue ota [ファイル]          ファームウェアを無線で更新する
    padcue mode procon|hidpad      転送方式を切り替える(再列挙が要る)
    padcue config <キー> <値>      不揮発設定を書く(frame_period_ns など)
    padcue clear-error             異常状態(赤 LED)を解除する
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from . import proto, registry
from .client import DeviceClient, DeviceError, connect_verified, is_mock
from .discover import discover
from .flowfmt import FlowError
from .project import Project


def _project(args) -> Project:
    return Project(Path(args.project).resolve())


def _gui_base(p: Project) -> str | None:
    """操作画面のサーバが動いていれば、その URL を返す(計画 D10)。

    実機は同時1接続・後着優先のため、画面が開いているときに CLI が直結
    すると、毎秒の収集と接続を奪い合って両方が間欠故障になる。装置に触る
    操作は画面のサーバを経由する。マーカーが消し忘れ(クラッシュ等)なら
    応答確認で落ちて、従来どおりの直結に戻る。
    """
    marker = p.root / "gui_server.json"
    if not marker.is_file():
        return None
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{int(info['port'])}"
        with urllib.request.urlopen(base + "/api/state", timeout=2.0) as r:
            json.loads(r.read())
        return base
    except Exception:   # noqa: BLE001  残骸マーカーは無視して直結
        return None


def _gui_get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def _gui_post(base: str, path: str, obj: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _gui_dev(base: str, args) -> tuple[dict | None, dict]:
    """state から対象装置(--device 省略時は1台目)のエントリを取る。"""
    st = _gui_get(base, "/api/state")
    devs = st.get("devices") or []
    want = getattr(args, "device", None)
    if not want:
        return (devs[0] if devs else None), st
    for d in devs:
        if d.get("name") == want or d.get("id") == want:
            return d, st
    return None, st


def _dev_arg(args) -> str:
    """--device の値(未指定なら空文字)。API へ渡す装置の指定。"""
    return getattr(args, "device", "") or ""


def _via_gui(p: Project, path: str, body: dict, done) -> int | None:
    """画面が開いていれば、その API を叩いて終了コードを返す。

    開いていなければ None を返すので、呼ぶ側は直結の処理へ進む。
    「開いているか見る → 投げる → error を見る → 印を出す」の4段が
    コマンドごとに書き写されるのを防ぐ(計画 D10 の経路選択はここだけ)。
    done は成功したときに表示する文言。応答の中身を使いたいときは関数を渡す。
    """
    base = _gui_base(p)
    if base is None:
        return None
    r = _gui_post(base, path, body)
    if r.get("error"):
        print(r["error"])
        return 1
    print(done(r) if callable(done) else done)
    return 0


def _refuse_while_gui(p: Project, what: str) -> bool:
    """装置を専有する操作(OTA・設定・方式切替)は画面と両立しない。"""
    if _gui_base(p) is None:
        return False
    print(f"padcue.bat(操作画面)が開いています。{what}は装置をまるごと"
          "預かる操作のため、画面を閉じてからやり直してください")
    return True


def _pick_device(args, cfg: dict) -> dict:
    """--device 名前(省略時は1台目)から装置台帳のエントリを選ぶ。"""
    devs = cfg.get("devices", [])
    if not devs:
        raise SystemExit("装置が登録されていません(padcue device add <IP> <名前>)")
    want = getattr(args, "device", None)
    if not want:
        return devs[0]
    for d in devs:
        if d.get("name") == want or d.get("id") == want:
            return d
    names = " / ".join(d.get("name", "?") for d in devs)
    raise SystemExit(f"装置「{want}」は登録されていません(登録済み: {names})")


def _learn_id(p, cfg: dict, dev: dict, info) -> None:
    """初回接続で個体IDを覚える(以後の接続はこのIDと照合される)。

    模擬デバイス(mock〜)は覚えない。練習で mock に繋いだだけで台帳が
    mock のIDで埋まると、実機へ戻れなくなるため。
    """
    if is_mock(info.device_id):
        return
    if not dev.get("id") and info.device_id:
        p.update_device(cfg, cfg["devices"].index(dev), id=info.device_id)
        print(f"装置 {dev.get('name')} の個体ID {info.device_id} を控えました",
              file=sys.stderr)


def _client(args) -> DeviceClient:
    """接続先を決めて繋ぎ、個体ID(MAC)を照合する。

    控えた IP で繋がらなければ LAN 内を探すが、控え直すのは**同じ個体**
    (ID が一致する応答)だけ。以前は「実際に繋がった最初の1台」へ黙って
    乗り換えていたが、2台環境では意図しない実機を操作する事故になるため
    廃止した(2026-08-04 P1)。--host 指定は照合なしの直結(復旧用の逃げ道)。
    """
    p = _project(args)
    cfg = p.load_config()
    if args.host:                       # 明示指定 = 直結(控えもしない)
        c = DeviceClient(args.host, int(cfg.get("port", proto.DEFAULT_PORT)))
        c.connect()
        return c
    dev = _pick_device(args, cfg)
    try:
        c, info = connect_verified(dev)
        _learn_id(p, cfg, dev, info)
        return c
    except DeviceError as e:
        if e.code == "DEVICE_MISMATCH":
            print(f"{dev['host']} は {dev.get('name')} ではありません。"
                  "LAN 内で本人を探します…", file=sys.stderr)
        else:
            raise
    except (OSError, ConnectionError):
        print(f"{dev['host']} に繋がりません。LAN 内を探します…", file=sys.stderr)
    found = discover()
    want_id_early = dev.get("id", "")
    if want_id_early and not any(f.device_id == want_id_early for f in found):
        # UDP 探索で本人が見えない。個体名(mDNS)でも追跡を試す
        # (ブロードキャストが通らないネットワークの保険。実機は
        # pademu-<MAC下4桁>.local を名乗る)
        name_host = f"pademu-{want_id_early[-4:]}.local"
        try:
            c, info = connect_verified(dict(dev, host=name_host))
            p.update_device(cfg, cfg["devices"].index(dev), host=name_host)
            print(f"見つかりました: {name_host}(名前で控え直しました)",
                  file=sys.stderr)
            return c
        except (OSError, ConnectionError, DeviceError):
            pass
    if not found:
        raise SystemExit(
            "マイコンが見つかりません。電源と WiFi を確認してください"
            "(接続先を指定する場合: padcue device <IPアドレス>)")
    # 探索応答の中から「登録した個体(ID一致)」だけを追跡する。
    # ID 未学習(初回)の場合のみ、繋がる相手が1台だけなら採用して学習する
    # 追跡できるのは「登録した個体(ID一致)」だけ。mock は自動採用の対象外
    # (消し忘れの mock へ黙って乗り換える偽成功を防ぐ。練習への切替は
    # device 127.0.0.1 の明示操作で行う)。ID 未学習のときも実機のみが対象
    want_id = dev.get("id", "")
    candidates = [f for f in found
                  if (f.device_id == want_id if want_id
                      else not is_mock(f.device_id))]
    connected = []
    for target in candidates:
        probe = dict(dev, host=target.host, port=target.port)
        try:
            c, info = connect_verified(probe)
        except (OSError, ConnectionError, DeviceError):
            continue
        connected.append((target, c, info))
        if want_id:
            break                        # ID 一致は本人確定
    if want_id and connected:
        target, c, info = connected[0]
        p.update_device(cfg, cfg["devices"].index(dev),
                        host=target.host, port=target.port)
        print(f"見つかりました: {target.host}(控え直しました)", file=sys.stderr)
        return c
    if not want_id and len(connected) == 1:
        target, c, info = connected[0]
        p.update_device(cfg, cfg["devices"].index(dev),
                        host=target.host, port=target.port)
        _learn_id(p, cfg, dev, info)
        print(f"見つかりました: {target.host}(控え直しました)", file=sys.stderr)
        return c
    for _t, c, _i in connected:
        c.close()
    if not want_id and len(connected) > 1:
        where = " / ".join(f"{t.host}(id={i.device_id or '不明'})"
                           for t, _c, i in connected)
        raise SystemExit(
            f"pademu が複数見つかりました: {where}\n"
            "  どれがこの装置か特定できません。IP を指定して一度接続し、"
            "個体IDを学習させてください: padcue device <IPアドレス>")
    where = " / ".join(f.host for f in found)
    raise SystemExit(
        f"{where} は見つかりましたが、登録した個体ではないか応答しません。" + "\n"
        "  ・操作画面(padcue.bat)を開いていませんか? "
        "実機は同時に1つのプログラムしか受け付けません。"
        "閉じてからやり直してください" + "\n"
        "  ・別の個体しか居ない場合は乗り換えません(誤爆防止)。"
        "新しい装置なら登録してください: padcue device add <IPアドレス> <名前>")


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
        print("手順がありません(padcue init で雛形を作れます)")
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
    rc = _via_gui(p, "/api/push", {"name": args.name, "dev": _dev_arg(args)},
                  lambda r: f"転送・保存しました(hash {r.get('hash')}・操作画面経由)")
    if rc is not None:
        return rc
    with _client(args) as c:
        h = c.put(r.name, r.blob)
        c.commit(r.name)
        print(f"転送・保存しました(hash {h})")
    return 0


def cmd_run(args) -> int:
    p = _project(args)
    base = _gui_base(p)
    if base:
        # 画面が開いている間は画面のサーバ経由(D10)。版ずれの自動転送や
        # 連結の人為停止の印も、サーバ側の同じ規則で扱われる
        r = _gui_post(base, "/api/run",
                      {"name": args.name, "loops": args.loops,
                       "dev": _dev_arg(args)})
        if r.get("error"):
            print(r["error"])
            return 1
        print(f"実行開始: {args.name} ×{args.loops}(操作画面経由)")
        if args.watch:
            try:
                while True:
                    d, _st = _gui_dev(base, args)
                    if not d or not d.get("running"):
                        print(f"\n終了: {(d or {}).get('state', '不明')}")
                        break
                    print(f"\r{d.get('session_loop', 0)}/{args.loops} 周 "
                          f"{d.get('frames_elapsed', 0)} フレーム", end="")
                    time.sleep(0.5)
            except KeyboardInterrupt:
                _gui_post(base, "/api/stop",
                          {"mode": "immediate",
                           "dev": _dev_arg(args)})
                print("\n停止しました")
        return 0
    with _client(args) as c:
        target = args.name
        listing = {e["name"]: e for e in c.list()}
        if target not in listing:
            print(f"デバイスに '{target}' がありません。padcue push してください")
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
    mode = ("cancel" if args.cancel
            else "graceful" if args.graceful else "immediate")
    said = {"cancel": "区切り停止の予約を取り消しました"
                      "(既に止まっていた場合は停止のままです)",
            "graceful": "区切り停止を指示しました",
            "immediate": "即時停止しました"}[mode]
    rc = _via_gui(_project(args), "/api/stop",
                  {"mode": mode, "dev": _dev_arg(args)},
                  said + "(操作画面経由)")
    if rc is not None:
        return rc
    with _client(args) as c:
        c.stop(mode)
        print(said)
    return 0


def _print_pairing(st: dict) -> None:
    """ペアリングの観測値を表示する(2026-08-06 の教訓)。

    本体側にこの個体の登録記録が無いと、本体は新規ペアリング(フェーズ
    0x01)を再要求し続け、完了するまで**全ての入力を無視する**。それが
    起きていることを、この行が無いと外から切り分けられない。"""
    if "pair_step" not in st:
        return   # 旧ファーム(フィールドなし)
    step = int(st.get("pair_step") or 0)
    reqs = int(st.get("pair_reqs") or 0)
    if step in (0x01, 0x02):
        print(f"ペアリング : ⚠ 未完了(フェーズ 0x{step:02x}・計 {reqs} 回受信)")
        print("             本体がコントローラー登録を完了できていません。"
              "この間は入力が無視されます")
    elif step:
        print(f"ペアリング : 受理済み(直近フェーズ 0x{step:02x}・計 {reqs} 回)")
    else:
        print("ペアリング : 要求なし(本体からまだ届いていません)")


def _print_status(d: dict) -> None:
    """状態を表示する。画面経由でも直結でも、同じ項目を同じ順で出す。

    以前は経路ごとに書き分けていたため、直結のときだけ出る行が3つあった
    (記録落ち・送出失敗・ロールバック)。同じ `padcue status` の出力が、
    操作画面を開いているかどうかで変わるのは、計器として壊れている。

    ラベルはコロンの前を11セルで揃える。印(⚠)は値の側に置く — 端末や
    フォントによって幅が変わる文字を、桁を担う位置に置かないため。
    """
    if is_mock(d.get("id", "")):
        print("⚠ 練習用の模擬デバイスに接続中です(実機は動きません)")
    print(f"ファーム   : {d.get('fw')} ({d.get('partition')})")
    print(f"転送方式   : {d.get('mode')} / bInterval={d.get('binterval')}")
    print(f"状態       : {d.get('state')}")
    print(f"USB        : {'接続' if d.get('usb_mounted') else '未接続'}"
          f" / 到達段階 0x{d.get('breadcrumb', 0):03x}")
    if "imu_enabled" in d:
        print("ジャイロ   : "
              + ("本体が有効化済み" if d["imu_enabled"]
                 else "本体からの有効化なし(値を送っても読まれません)"))
    _print_pairing(d)
    if d.get("running"):
        print(f"実行中     : {d.get('proc', '')} "
              f"{d.get('session_loop')} 周目 / "
              f"{d.get('frames_elapsed')} フレーム")
    # ずれの実測は 0 でも出す。出ていないのが「遅れていない」のか
    # 「測っていない」のか分からないと、計器として意味を成さない
    if "max_late_us" in d:
        line = f"ずれ最大   : フレームの刻み {d['max_late_us']}µs"
        if "deliver_max_us" in d:
            line += f" / 読み取り待ち {d['deliver_max_us']}µs"
        over = d.get("late_events", 0) + d.get("deliver_late", 0)
        if over:
            line += f"  ⚠ 超過 {over} 回"
        print(line)
    if d.get("log_dropped"):
        print(f"記録落ち   : ⚠ {d['log_dropped']} 件"
              "(この間の記録は残っていません)")
    lost_reply = (d.get("dropped_replies", 0) + d.get("failed_replies", 0)
                  + d.get("bad_reports", 0))
    if lost_reply or d.get("dropped_inputs"):
        print(f"送出失敗   : ⚠ 応答 {lost_reply} 件 "
              f"/ 通常入力 {d.get('dropped_inputs', 0)} 件")
    if d.get("rolled_back"):
        print("更新       : ⚠ 前回の更新はロールバックされました")


def cmd_status(args) -> int:
    base = _gui_base(_project(args))
    if base:
        d, _st = _gui_dev(base, args)
        if d is None:
            print("装置が登録されていません")
            return 1
        print("(操作画面のサーバ経由で表示)")
        if d.get("error"):
            print(f"未接続     : {d['error']}")
            return 1
        _print_status(d)
        return 0
    with _client(args) as c:
        info = c.hello()
        # 直結で取った情報を、画面経由(/api/state の装置エントリ)と同じ
        # 形に揃えてから渡す。表示側が経路を意識しなくて済む
        _print_status({**c.status(),
                       "fw": info.fw_version, "partition": info.partition,
                       "mode": info.transport_mode,
                       "binterval": info.binterval,
                       "rolled_back": info.rolled_back,
                       "id": info.device_id})
    return 0


def cmd_mode(args) -> int:
    """転送方式を切り替える(プロコン方式 ⇔ 保険モード)。

    プロコン方式が Switch 2 で受理されない場合の逃げ道。無線で切り替えられる
    ので、書き込みモードに入れ直す必要はない(切替後に USB を挿し直す)。
    """
    if _refuse_while_gui(_project(args), "方式の切り替え"):
        return 1
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
    if _refuse_while_gui(_project(args), "設定の書き込み"):
        return 1
    with _client(args) as c:
        c.config(args.key, val)
    print(f"{args.key} = {val} を保存しました")
    return 0


def cmd_clear_error(args) -> int:
    rc = _via_gui(_project(args), "/api/clear_error", {"dev": _dev_arg(args)},
                  "異常状態を解除しました(操作画面経由)")
    if rc is not None:
        return rc
    with _client(args) as c:
        c.clear_error()
    print("異常状態を解除しました")
    return 0


def cmd_logs(args) -> int:
    base = _gui_base(_project(args))
    if base:
        # 画面のサーバが毎秒回収して溜めている記録(装置タグ付き)を出す。
        # 直結で読むと回収を横取りして、画面側の記録に穴が開く
        entries = _gui_get(base, "/api/logs").get("entries", [])
        st = _gui_get(base, "/api/state")
        names = {d.get("id"): d.get("name")
                 for d in st.get("devices", []) if d.get("id")}
        if not entries:
            print("(ログなし)")
        for e in entries[-200:]:
            t = time.strftime("%H:%M:%S", time.localtime(e.get("at", 0)))
            who = names.get(e.get("dev"), "")
            print(f"[{t}] {who:<4} {e.get('kind', '?'):<14} "
                  f"a={e.get('a', 0)} b={e.get('b', 0)} c={e.get('c', 0)}")
        return 0
    with _client(args) as c:
        entries = c.logs()
        # 実機側のログは読むと消える。ここで保存しないと、この回収分は
        # 画面のログ蓄積(logs.jsonl)から永久に欠落する(装置タグ付き)
        _project(args).append_logs(entries, dev=getattr(c, "device_id", ""))
        if not entries:
            print("(ログなし)")
        for e in entries:
            print(f"[{e['t_ms'] / 1000:9.3f}s] {e['kind']:<14} "
                  f"a={e.get('a', 0)} b={e.get('b', 0)} c={e.get('c', 0)}")
    return 0


def cmd_list(args) -> int:
    base = _gui_base(_project(args))
    if base:
        d, _st = _gui_dev(base, args)
        if d is None:
            print("装置が登録されていません")
            return 1
        for name, h in sorted((d.get("listing") or {}).items()):
            print(f"{name:<24} {h}")
        return 0
    with _client(args) as c:
        for e in c.list():
            print(f"{e['name']:<24} {e['size']:>7} バイト  {e['hash']}")
    return 0


def cmd_ota(args) -> int:
    if _refuse_while_gui(_project(args), "ファームウェア更新"):
        return 1
    path = Path(args.image)
    if not path.is_file():
        # 既定パスはリポジトリ直下から見た相対。runbook の手順どおり
        # pctool から実行しても見つかるよう、リポジトリ基準でも探す
        alt = (Path(__file__).resolve().parents[2]
               / "firmware" / "build" / "pademu.bin")
        if Path(args.image) == Path("firmware/build/pademu.bin") \
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
        print("デバイスが再起動します。数秒後に padcue status で確認してください")
        print("(起動に失敗した場合は自動で前のファームへ戻ります)")
    return 0


def cmd_device(args) -> int:
    """装置台帳の管理。

    device list                    登録済み装置の一覧
    device add <IP> [名前]         新しい装置を登録(接続して個体IDを学習)
    device rename <名前> <新名前>  表示名の変更(IDで参照するため履歴は切れない)
    device auto                    1台目のIPを探索で追跡(ID一致のみ)
    device <IP>                    1台目の接続先を手で設定(従来互換)
    """
    p = _project(args)
    cfg = p.load_config()
    devs = cfg.get("devices", [])
    a = args.address

    if a == "list":
        for i, d in enumerate(devs):
            mark = "(既定)" if i == 0 else ""
            print(f"{d.get('name', '?'):<8} {d.get('host')}:{d.get('port')}"
                  f"  id={d.get('id') or '未学習'} {mark}")
        if not devs:
            print("(登録なし)")
        return 0

    if a == "add":
        if not args.extra:
            print("使い方: padcue device add <IP[:ポート]> [名前]")
            return 1
        # 受け入れモード: 接続して個体IDを確認してから登録する。
        # IDを名乗らない旧ファームは登録できない(照合できない装置を台帳に
        # 入れると誤爆防止が成り立たない)。先に有線か --host 直結で OTA する
        # ポート指定は模擬デバイス2台での練習用(実機はどれも既定 5555)
        host = args.extra[0]
        port = None
        if ":" in host and host.rsplit(":", 1)[1].isdigit():
            host, port = host.rsplit(":", 1)
        ok, msg = registry.add_device(
            p, host, args.extra[1] if len(args.extra) > 1 else "",
            port=port)
        print(msg)
        return 0 if ok else 1

    if a == "rename":
        if len(args.extra) != 2:
            print("使い方: padcue device rename <名前> <新名前>")
            return 1
        ok, msg = registry.rename_device(p, args.extra[0], args.extra[1])
        print(msg)
        return 0 if ok else 1

    if a == "forget":
        # 装置の交換(基板が変わり MAC も変わった)用: IDの控えだけを解除する
        if len(args.extra) != 1:
            print("使い方: padcue device forget <名前>")
            return 1
        ok, msg = registry.forget_device(p, args.extra[0])
        print(msg)
        return 0 if ok else 1

    if a == "remove":
        if len(args.extra) != 1:
            print("使い方: padcue device remove <名前>")
            return 1
        ok, msg = registry.remove_device(p, args.extra[0])
        print(msg)
        return 0 if ok else 1

    if a == "auto":
        # 1台目(または --device 指定)の IP を探索で追跡する。
        # ID が学習済みなら一致する応答のみ採用(別個体へは乗り換えない)
        base = _gui_base(p)
        if base:
            # 画面が開いている間は画面の「探す」と同じ経路(D10)。直結の
            # 到達確認が画面の接続を横取りしない
            r = _gui_post(base, "/api/discover",
                          {"dev": _dev_arg(args)})
            if r.get("error"):
                print(r["error"])
                return 1
            print(("いまの接続先でつながっています: " if r.get("kept")
                   else "接続先を控え直しました: ") + str(r.get("host"))
                  + "(操作画面経由)")
            return 0
        dev = _pick_device(args, cfg)
        found = discover()
        match = [f for f in found
                 if not dev.get("id") or f.device_id == dev["id"]]
        if not match:
            print("登録した個体が見つかりません(電源と WiFi を確認してください)")
            return 1
        if not dev.get("id") and len(match) > 1:
            where = " / ".join(f"{f.host}(id={f.device_id or '不明'})" for f in match)
            print(f"複数見つかりました: {where}\n"
                  "どれか特定できません。IP を指定してください")
            return 1
        p.update_device(cfg, cfg["devices"].index(dev),
                        host=match[0].host, port=match[0].port)
        print(f"接続先を {match[0].host} に設定しました")
        return 0

    addr = a.strip()
    if not addr:
        print("接続先が空です。IP か名前を指定するか、auto で探してください")
        return 1
    dev = _pick_device(args, cfg)
    fields = {"host": addr}
    # 向け先を確かめる。相手が模擬デバイス(練習)なら、IDの控えを解除して
    # 向け替える(控えたまま向けると照合で止まり練習が成立しない。解除は
    # この明示操作のときだけ。探索が黙って mock を採用することはない)
    try:
        probe = DeviceClient(addr, int(cfg.get("port", proto.DEFAULT_PORT)),
                             timeout=1.5)
        info = probe.connect()
        probe.close()
        if is_mock(info.device_id) and dev.get("id"):
            fields["id"] = ""
            print("相手は練習用の模擬デバイスです。IDの控えを解除して向けます"
                  "(実機へ戻るときは「探す」か device auto で学習し直します)")
    except (OSError, ConnectionError, DeviceError):
        pass          # まだ起動していない宛先も設定はできる(従来どおり)
    p.update_device(cfg, cfg["devices"].index(dev), **fields)
    print(f"{dev.get('name')} の接続先を {addr} に設定しました")
    return 0


def cmd_discover(args) -> int:
    found = discover(timeout=args.timeout)
    if not found:
        print("見つかりません。電源・WiFi・PC が同じネットワークに"
              "あるか確認してください")
        return 1
    for f in found:
        print(f)
    return 0


def cmd_mock(args) -> int:
    from .discover import PORT as DISCOVER_PORT
    from .mockdevice import MockDevice
    d = MockDevice(host="127.0.0.1", port=args.port, speed=args.speed,
                   device_id=args.id)
    # 本体(TCP)の待ち受けに失敗したら偽の成功を表示しない(排他 bind に
    # したため、二重起動はここで確実に失敗する。2026-08-05 レビュー)
    try:
        port = d.start()
    except OSError:
        print(f"ポート {args.port} を確保できません。既に模擬デバイス"
              "(または他のプログラム)が使っています。--port で番号を変えるか、"
              "先に動いている方を使ってください")
        return 1
    # 探索の問いかけにも応える。実機と同じく画面の「探す」で見つかるようにして、
    # 練習の手順が本番と食い違わないようにする
    try:
        d._start_discover(DISCOVER_PORT)
        findable = True
    except OSError:
        findable = False
    print(f"模擬デバイスを起動しました: 127.0.0.1:{port}(Ctrl+C で終了)")
    if findable:
        print("  操作画面の「探す」でも見つかります")
    else:
        print(f"  ※ 探索用のポート {DISCOVER_PORT} が使用中のため"
              "「探す」では見つかりません(接続先を手で 127.0.0.1 にしてください)")
    print("  別の端末で: padcue device 127.0.0.1 && padcue status")
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
    ap = argparse.ArgumentParser(prog="padcue", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=".", help="プロジェクトフォルダ")
    ap.add_argument("--host", default="", help="デバイスの IP(照合なしの直結。復旧用)")
    ap.add_argument("--device", default="",
                    help="操作する装置の名前(省略時は1台目)")
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
    p.add_argument("image", nargs="?", default="firmware/build/pademu.bin")
    p.set_defaults(func=cmd_ota)

    p = sub.add_parser("device", help="装置の登録・一覧・接続先設定")
    p.add_argument("address", help="list / add / rename / auto / IP アドレス")
    p.add_argument("extra", nargs="*", help="add <IP> [名前] / rename <旧> <新>")
    p.set_defaults(func=cmd_device)

    p = sub.add_parser("discover", help="LAN 内のマイコンを探す")
    p.add_argument("--timeout", type=float, default=1.5)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("mock", help="模擬デバイスを起動")
    p.add_argument("--port", type=int, default=proto.DEFAULT_PORT)
    p.add_argument("--speed", type=float, default=1.0, help="実行の早送り倍率")
    p.add_argument("--id", default="mock00000000",
                   help="個体ID(2台の練習では2つ目を別のIDにする。"
                        "例: mock2p000000)")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("gui", help="操作画面を開く")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", dest="open", action="store_false",
                   help="ブラウザを自動で開かない")
    p.set_defaults(func=cmd_gui, open=True)
    return ap


def main(argv=None) -> int:
    # Windows のコンソール(cp932)では µ などの文字で print が
    # UnicodeEncodeError になり、status 表示が途中で落ちる(2026-08-06 実測)。
    # 表せない文字だけ置換に落とし、表示は最後まで出す
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
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
