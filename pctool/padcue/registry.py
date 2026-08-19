"""装置台帳の操作(登録・改名・記録の解除・削除)。CLI と GUI の共通実装。

規則(docs/specs/coupling.md D2):
- 登録は「接続して個体IDを確認してから」。IDを名乗らない旧ファームは断る
  (照合できない装置を台帳に入れると誤操作防止が成り立たない)
- 名前は一意(重複すると指名で取り違える)。改名してもID参照は切れない
- 記録の解除(forget)は装置交換(MACが変わった)ときの正規手順
"""
from __future__ import annotations

import json

from . import proto
from .client import DeviceClient, DeviceError


def add_device(project, host: str, name: str = "", port=None,
               client_cls=None) -> tuple[bool, str]:
    """新しい装置を登録する。戻り値は (成否, 人向けメッセージ)。

    port は普通は省略(実機はどれも既定の 5555)。mock を2台立てる練習では
    同一 IP の別ポートになるため、そのときだけ指定する。
    """
    cfg = project.load_config()
    devs = cfg.get("devices", [])
    if len(devs) >= 2:
        return False, "いまは2台までです(3台以上は未検証)"
    name = (name or "").strip() or f"{len(devs) + 1}P"
    if any(d.get("name") == name for d in devs):
        return False, f"名前「{name}」は使用済みです"
    port = int(port or proto.DEFAULT_PORT)
    cls = client_cls or DeviceClient
    try:
        c = cls(host, port, timeout=3.0)
        c.connect()
        info = c.hello()
        c.close()
    except (OSError, ConnectionError, DeviceError) as e:
        return False, f"{host} に接続できません: {e}"
    if not info.device_id:
        return False, (f"{host} は個体IDを名乗らない古いファームです。"
                       "先に更新してください: "
                       f"padcue --host {host} ota firmware/build/pademu.bin")
    if any(d.get("id") == info.device_id for d in devs):
        other = next(d for d in devs if d.get("id") == info.device_id)
        return False, f"この個体は「{other.get('name')}」として登録済みです"
    devs.append({"id": info.device_id, "name": name,
                 "host": host, "port": port})
    cfg["devices"] = devs
    project.save_config(cfg)
    return True, f"登録しました: {name} = {host} (id={info.device_id})"


def rename_device(project, old: str, new: str) -> tuple[bool, str]:
    cfg = project.load_config()
    devs = cfg.get("devices", [])
    new = (new or "").strip()
    if not new:
        return False, "新名前が空です"
    # 連結実行の運転記録(runstate.json)は装置名で追っている。実行中に
    # 改名すると監視(連動停止・自動合流)が対象を見失うため、どの入口
    # (GUI/CLI)からでもここで断る
    try:
        run = json.loads((project.root / "runstate.json")
                          .read_text(encoding="utf-8")).get("run")
        if run and run.get("active") and old in run.get("members", []):
            return False, f"{old} は連結実行中です。止めてから改名してください"
    except (OSError, ValueError):
        pass
    if any(d.get("name") == new for d in devs):
        return False, f"名前「{new}」は使用済みです(重複すると指名で取り違えます)"
    for d in devs:
        if d.get("name") == old:
            d["name"] = new
            project.save_config(cfg)
            return True, f"{old} → {new} に変更しました(個体IDでの参照は不変)"
    return False, f"装置「{old}」は登録されていません"


def forget_device(project, name: str) -> tuple[bool, str]:
    """ID の記録だけを解除する(装置交換=MACが変わったときの正規手順)。"""
    cfg = project.load_config()
    for d in cfg.get("devices", []):
        if d.get("name") == name:
            d["id"] = ""
            project.save_config(cfg)
            return True, (f"{name} の ID の記録を解除しました"
                          "(次の接続で学習し直します)")
    return False, f"装置「{name}」は登録されていません"


def remove_device(project, name: str) -> tuple[bool, str]:
    cfg = project.load_config()
    devs = cfg.get("devices", [])
    for i, d in enumerate(devs):
        if d.get("name") == name:
            devs.pop(i)
            project.save_config(cfg)
            return True, f"{name} を台帳から外しました"
    return False, f"装置「{name}」は登録されていません"
