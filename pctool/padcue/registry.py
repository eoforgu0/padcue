"""装置の登録情報の操作(登録・改名・割り当ての解除・削除)。CLI と GUI の共通実装。

登録情報は 1 台ごとに装置 ID・名前・接続先を持つ(concepts.md §7):
- 登録は「接続して装置 ID を確認してから」。ID を報告しない相手は拒否する
  (照合できない装置を登録情報に入れると誤操作防止が成り立たない)
- 名前は一意(重複すると名前で指定したときに混同する)。名前は表示のためだけのもので、
  改名しても ID 参照は切れない(P23)
- 割り当ての解除(forget)は装置交換(MAC が変わった)ときの正規手順

登録情報は、どれも Project.edit_config の中で読み直してから変える。読んでから
書くまでの間に、裏の探索が接続先を、収集が本体の識別子を書く。読んだときのコピーを
丸ごと書き戻すと、それを古い値で消す。装置への接続の確認は時間がかかるので、
edit_config の外で済ませ、中では読み直した登録情報で拒否する理由をもう一度確認する。
"""
from __future__ import annotations

from typing import NamedTuple

from . import proto
from .client import DeviceClient, DeviceError, failure_of, failure_text, is_mock
from .names import same_name, validate_label


class Outcome(NamedTuple):
    """装置につないで登録する操作の結果。failure は失敗の種類(client.failure_of)で、
    装置につながる前に拒否したときは空である。コマンドは終了コードをこれで決める。"""
    ok: bool
    msg: str
    failure: str = ""


def command_arg(name: str) -> str:
    """コマンドの案内に入れる装置の名前。空白を含む名前は引用符で囲む。

    囲まないと、案内をコピーして打ったときに、名前が空白で 2 つの引数に分かれる。
    """
    return f'"{name}"' if any(c.isspace() for c in name) else name


def default_name(devs: list[dict]) -> str:
    """名前を指定しないで登録するときの名前。使われていない一番小さい「nP」。

    台数に 1 を足すだけだと、途中の 1 台を削除した後に、残っている台の名前と
    重なって登録できなくなる。画面の「追加」は名前を指定しないので、そこで
    行き詰まる。
    """
    names = [d.get("name", "") for d in devs]
    k = 1
    while same_name(names, f"{k}P") is not None:
        k += 1
    return f"{k}P"


def _name_taken(names, name: str) -> str:
    """name が names のどれかと重なるときの拒否の理由。重ならなければ空。

    画面で区別する名前なので、大文字と小文字だけ違う名前も同じと数える
    (手順やフレーム表の名前と同じ)。
    """
    hit = same_name(names, name)
    if hit is None:
        return ""
    return (f"「{hit}」は既にあります" if hit == name
            else f"「{hit}」は既にあります。名前の大文字と小文字は区別しません。")


def _mix_refusal(devs: list, new_id: str, target=None) -> Outcome | None:
    """実機と模擬装置を同じプロジェクトに登録させない。

    混ざると、停止の操作や開始が停止している模擬装置にも送られ、模擬装置のつもりの
    操作で実機が動く。target は割り当てる先の行で、比べる対象から外す。
    """
    for d in devs:
        if d is target or not d.get("id") or is_mock(d["id"]) == is_mock(new_id):
            continue
        if is_mock(d["id"]):
            return Outcome(False, "このプロジェクトには模擬装置が登録されているため、"
                           "実機を登録できません。実機は padcue.bat で開いた画面で"
                           "登録してください。")
        return Outcome(False, "このプロジェクトには実機が登録されているため、"
                       "模擬装置を登録できません。")
    return None


def register_refusal(devs: list, device_id: str) -> str:
    """その装置を登録(追加・割り当て)できない理由。登録できるなら空。

    画面の「見つかった装置」は、押す前にボタンを押せなくして、この理由を出す。押せる
    見た目のまま押してから拒否すると、押せない理由が押すまで分からない。追加と割り当ての
    どちらも同じ理由で拒否する(割り当てる先の行は装置 ID を持たないので、比べる対象に
    入らない)。
    """
    mixed = _mix_refusal(devs, device_id)
    return mixed.msg if mixed else ""


def _connect(host: str, port: int, client_cls):
    """装置につないで HELLO の応答を受け取る。(装置の情報, 拒否する結果) のどちらかを
    返す。

    登録情報のロックの外で呼ぶ。つながらない相手には最長で数秒かかる。
    """
    c = (client_cls or DeviceClient)(host, port, timeout=3.0)
    try:
        return c.connect(), None   # 内部で hello() 済み(二重送信しない)
    except (OSError, DeviceError) as e:
        return None, Outcome(False, failure_text(e, host), failure_of(e))
    finally:
        c.close()            # 失敗時もソケットを残さない(close は多重呼び出し可)


def add_device(project, host: str, name: str = "", port=None,
               client_cls=None) -> Outcome:
    """新しい装置を登録する。戻り値は (成否, 人向けメッセージ, 失敗の種類)。

    port は普通は省略(実機はどれも既定の 5555)。模擬装置を同一 IP に複数
    起動するデモでは別ポートになるため、そのときだけ指定する。
    """
    devs = project.load_config()["devices"]
    try:
        name = validate_label(name or default_name(devs))
    except ValueError as e:
        return Outcome(False, str(e))
    # つなぐ前に拒否できるものは拒否する(つながらない相手を待たせない)
    taken = _name_taken([d.get("name") or "" for d in devs], name)
    if taken:
        return Outcome(False, taken)
    port = int(port or proto.DEFAULT_PORT)
    info, refused = _connect(host, port, client_cls)
    if refused:
        return refused
    with project.edit_config() as cfg:
        # つないでいる間に、ほかで同じ名前か同じ装置が登録されたかもしれない
        devs = cfg["devices"]
        taken = _name_taken([d.get("name") or "" for d in devs], name)
        if taken:
            return Outcome(False, taken)
        other = next((d for d in devs if d.get("id") == info.device_id), None)
        if other is not None:
            return Outcome(False, f"「{other.get('name')}」として登録済みです")
        mixed = _mix_refusal(devs, info.device_id)
        if mixed:
            return mixed
        _take_over(devs, info.device_id)
        devs.append({"id": info.device_id, "name": name,
                     "host": host, "port": port})
    return Outcome(True, f"{name} を登録しました({host}、装置 ID "
                         f"{info.device_id[-4:].upper()})")


def rename_device(project, old: str, new: str) -> tuple[bool, str]:
    try:
        new = validate_label(new)
    except ValueError as e:
        return False, str(e)
    # 前後の空白を除くと今の名前と同じなら、変えるものがない。自分の名前を
    # 「使われています」と拒否しない
    if new == old:
        return True, f"名前は「{old}」のままです"
    with project.edit_config() as cfg:
        devs = cfg["devices"]
        # 装置の名前はファイル名ではないので、自分の名前の大文字と小文字だけを
        # 変えるのは構わない
        taken = _name_taken(
            [d.get("name") or "" for d in devs if d.get("name") != old], new)
        if taken:
            return False, taken
        for d in devs:
            if d.get("name") == old:
                d["name"] = new
                return True, f"{old} の名前を {new} に変更しました"
    return False, f"装置「{old}」は登録されていません"


def former_ids(d: dict) -> list[str]:
    """その行に前に割り当てていた装置 ID(古い順)。手で書き換えて形が違えば空。"""
    got = d.get("former_ids")
    if not isinstance(got, list):
        return []
    return [x for x in got if isinstance(x, str) and x]


def _take_over(devs: list, device_id: str) -> None:
    """装置 ID を、ほかの行の「前に割り当てていた装置」から外す。

    記録の名前とログの絞り込みには、その装置を割り当てた行を使う。
    """
    for d in devs:
        if device_id in former_ids(d):
            left = [x for x in former_ids(d) if x != device_id]
            if left:
                d["former_ids"] = left
            else:
                d.pop("former_ids", None)


def forget_device(project, name: str) -> tuple[bool, str]:
    """割り当てを解除する。装置 ID を消し、前に割り当てていた装置 ID として残す。

    装置を取り替えた(MAC が変わった)ときは、これで解除してから割り当て直す。
    記録(ログ)は装置 ID で持つので、残さないと、その装置の過去の記録が行の名前で
    出なくなり、ログの絞り込みでも選べなくなる(確認のダイアログは「名前と映像の
    設定は残ります」と表示する)。割り当て直した後も残す(交換する前の記録も、
    その行の記録である)。行の登録を削除すると、一緒に消える。
    """
    with project.edit_config() as cfg:
        for d in cfg["devices"]:
            if d.get("name") == name:
                if d.get("id"):
                    d["former_ids"] = [*(x for x in former_ids(d) if x != d["id"]),
                                       d["id"]]
                d["id"] = ""
                # 解除したあとは自動では決まらないので、次にできることを付ける
                return True, (f"{name} の割り当てを解除しました。\n"
                              "  割り当てるには: python -m padcue --device "
                              f"{command_arg(name)} device <IP アドレス>")
    return False, f"装置「{name}」は登録されていません"


def pick_device(project, name: str, host: str, port=None,
                client_cls=None, screen: bool = False) -> Outcome:
    """記録のない装置に、この装置を使うと決める(基板を交換したとき)。

    add_device との違いは、行を増やさずに既にある行へ装置を結び付けること
    である。名前と映像の割り当てを保ったまま、中身の基板だけを入れ替えられる。
    既に装置が決まっている行は拒否する。人が決めた装置を、別の操作で黙って
    差し替えない。入れ替えるなら先に割り当てを解除する。screen は操作画面から
    呼ぶときで、拒否の理由にコマンドの案内を入れない(画面には解除のボタンがある)。
    """
    def refusal(devs) -> Outcome | None:
        target = next((d for d in devs if d.get("name") == name), None)
        if target is None:
            return Outcome(False, f"装置「{name}」は登録されていません")
        if not target.get("id"):
            return None
        if screen:
            return Outcome(False, f"{name} には、既に別の装置が割り当てられています。"
                           "先に割り当てを解除してください。")
        return Outcome(False, f"{name} は割り当て済みです。先に割り当てを解除して"
                       "ください。\n  割り当てを解除するには: "
                       "python -m padcue device forget "
                       f"{command_arg(name)}")

    devs = project.load_config()["devices"]
    refused = refusal(devs)
    if refused:
        return refused
    target = next(d for d in devs if d.get("name") == name)
    port = int(port or target.get("port") or proto.DEFAULT_PORT)
    info, refused = _connect(host, port, client_cls)
    if refused:
        return refused
    with project.edit_config() as cfg:
        # つないでいる間に、ほかで削除・割り当てされたかもしれない
        devs = cfg["devices"]
        refused = refusal(devs)
        if refused:
            return refused
        target = next(d for d in devs if d.get("name") == name)
        other = next((d for d in devs
                      if d.get("id") == info.device_id and d is not target), None)
        if other is not None:
            return Outcome(False, f"「{other.get('name')}」として登録済みです")
        mixed = _mix_refusal(devs, info.device_id, target)
        if mixed:
            return mixed
        _take_over(devs, info.device_id)
        target.update(id=info.device_id, host=host, port=port)
    return Outcome(True, f"{name} に装置 ID {info.device_id[-4:].upper()} を"
                         "割り当てました")


def remove_device(project, name: str) -> tuple[bool, str]:
    with project.edit_config() as cfg:
        devs = cfg["devices"]
        for i, d in enumerate(devs):
            if d.get("name") == name:
                devs.pop(i)
                return True, f"{name} の登録を削除しました"
    return False, f"装置「{name}」は登録されていません"
