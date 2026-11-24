"""Windows の通知(トースト)を 1 通出す。

PowerShell から出す。PowerShell は OS に付属するツールなので、PC 側は標準
ライブラリだけで動くという決まり(P29)を守れる。押したときは、トーストの
activationType="protocol" で操作画面の URL を開く。Windows 以外では何もしない。

きっかけを決めるのは notify.RunWatcher で、ここは「1 通出す」だけを持つ。
"""
from __future__ import annotations

import base64
import subprocess
import sys

# トーストを出す名義。自前の名義はスタートメニューへの登録が必要なので、
# 登録なしで使える PowerShell 自身の名義を借りる
APP_ID = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
          "\\WindowsPowerShell\\v1.0\\powershell.exe")

# 別のウィンドウを出さずに実行する(Windows にしかないフラグ)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_WINDOWS = sys.platform == "win32"


def _xml(s: str) -> str:
    """XML の文字参照。手順名やレーンの名前は利用者の文字列である。"""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _ps_str(s: str) -> str:
    """PowerShell の単引用符の文字列。中の単引用符は 2 つ重ねる。"""
    return "'" + s.replace("'", "''") + "'"


def script(title: str, text: str, url: str = "") -> str:
    """トーストを 1 通出す PowerShell の本文。"""
    launch = f' launch="{_xml(url)}"' if url else ""
    xml = (f'<toast activationType="protocol"{launch}>'
           '<visual><binding template="ToastGeneric">'
           f'<text>{_xml(title)}</text><text>{_xml(text)}</text>'
           '</binding></visual></toast>')
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager,"
        " Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument,"
        " Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null",
        "$doc = [Windows.Data.Xml.Dom.XmlDocument]::new()",
        f"$doc.LoadXml({_ps_str(xml)})",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)",
        "[Windows.UI.Notifications.ToastNotificationManager]"
        f"::CreateToastNotifier({_ps_str(APP_ID)}).Show($toast)",
    ])


def command(title: str, text: str, url: str = "") -> list[str]:
    """PowerShell の起動の並び。

    本文は -EncodedCommand(UTF-16LE の base64)で渡す。文字列の引用の
    escape がそもそも起きないので、利用者の文字列がそのまま渡される。
    """
    enc = base64.b64encode(
        script(title, text, url).encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]


def show(title: str, text: str, url: str = "", *, runner=None) -> bool:
    """トーストを 1 通出す。出せない環境では何もせず False を返す。

    待たない(Popen)。通知の監視は 0.5 秒ごとにくり返されるので、PowerShell の起動を
    待つとその周期が崩れる。
    """
    if not _WINDOWS:
        return False
    run = runner or _popen
    try:
        run(command(title, text, url))
    except OSError:
        return False        # PowerShell がない環境。知らせないだけで先へ進む
    return True


def _popen(args: list[str]) -> None:
    subprocess.Popen(args, creationflags=_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
