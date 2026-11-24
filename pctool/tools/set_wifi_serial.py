"""装置がつなぐ Wi-Fi を、ケーブルから装置の保存領域(NVS)に入れる。

    python pctool/tools/set_wifi_serial.py [COM ポート]

装置は Wi-Fi の値を保存領域からだけ読む(getting-started.md §2-1)。値は装置に直接
保存するので、パスワードは画面にもファイルにも残らない。

書き込んだばかりの装置と、新しく保存した接続先につながらなかった装置は、自動で
Wi-Fi の設定待ちになる(LED が白くゆっくり点滅)。それ以外の装置は、先に診断モードで
立ち上げる。コントローラーとして動き始めた装置はシリアルを閉じるので、普通に
立ち上げた装置とはシリアルで通信できない。

1. ケーブルを抜く
2. 装置の上面のボタンを押したまま、ケーブルを挿す
3. LED が白く光ったら、ボタンを離す

パスワードのない Wi-Fi では、パスワードを聞かれたら何も入力せずに Enter を押す。
ポートを省略すると、ESP32-S3 のポート(VID 0x303A)を探す。
"""
from __future__ import annotations

import getpass
import re
import sys
import time
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:                     # pragma: no cover - 環境の話
    serial = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from padcue import proto

ESPRESSIF_VID = 0x303A
PROMPT = "pademu>"
DIAG_HOWTO = ("診断モードで起動するには: ボタンを押したままケーブルを挿し、"
              "LED が白く光ったら離す")
# 診断モードでは、装置の記録(「I (1234) app_wifi: …」の行)も同じシリアルに
# 出る。記録の行は、打った文字のエコーの途中にも割り込む
_LOG_RE = re.compile(r"[EWIDV] \(\d+\) ")
# 保存できたときに装置が出す行(app_provision.c の do_wifi_set)
SAVED_RE = re.compile(r"SSID「.*」を保存しました")
# 新しく保存した接続先につながらずに設定待ちになった装置が、wifi show で出す行
# (app_provision.c の out_new_failure)
FAILED_RE = re.compile(r"「.*」に接続できません: .+")


def find_port() -> str | None:
    for p in list_ports.comports():
        if p.vid == ESPRESSIF_VID:
            return p.device
    return None


def read_until(s: serial.Serial, needle: str, timeout: float = 5.0) -> str:
    """needle が出るまで読む。出なければそこまでを返す。

    1 回の read は日本語の字の途中で切れることがあるので、バイトのまま溜めて
    最後に文字へ戻す。
    """
    end = time.monotonic() + timeout
    want = needle.encode("utf-8")
    buf = b""
    while time.monotonic() < end:
        buf += s.read(256)
        if want in buf:
            break
    return buf.decode("utf-8", "replace")


def send(s: serial.Serial, line: str, timeout: float = 5.0) -> str:
    s.write((line + "\r\n").encode("utf-8"))
    s.flush()
    return read_until(s, PROMPT, timeout)


def quote(v: str) -> str:
    """空白を含む値と空の値は二重引用符で囲む(装置側の split_args と同じ規則)。

    空の値をそのまま並べると引数が 1 つ減り、装置は使い方を返す。パスワードのない
    Wi-Fi は、空のパスワードを "" で送って保存する。
    """
    return f'"{v}"' if (not v or " " in v or "\t" in v) else v


def result_lines(reply: str, sent: str) -> list[str]:
    """`wifi set` の応答から、装置が出した結果の行だけを取り出す。

    装置は打った文字をそのまま返すので、応答には送った行 sent(パスワードを
    含む)が戻ってくる。記録の行がエコーの途中に割り込むと、送った行は 2 つに
    切れ、後ろの断片が 1 行になる。記録の行(とその前に付いた断片)を除き、
    送った行の一部である行とプロンプトを除く。
    """
    out = []
    for ln in reply.splitlines():
        m = _LOG_RE.search(ln)
        if m:
            ln = ln[:m.start()]
        ln = ln.strip()
        if (ln and not ln.startswith("wifi set") and not ln.startswith(PROMPT)
                and ln not in sent):
            out.append(ln)
    return out


def sendable(v: str) -> bool:
    """装置が元どおりに取り出せる値か。

    装置は引用符の中の引用符を書く方法を持たない。" を含む値は、" で始まらず
    空白も含まないときだけ、元の値のまま装置に渡される。
    """
    if '"' not in v:
        return True
    return not (v.startswith('"') or " " in v or "\t" in v)


def main() -> int:
    if serial is None:
        print("pyserial がインストールされていません")
        print("  インストールするには: python -m pip install pyserial")
        return 2
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    if not port:
        print("装置のポートが見つかりません")
        print("  " + DIAG_HOWTO)
        return 1
    print(f"ポート: {port}")
    try:
        s = serial.Serial(port, 115200, timeout=0.3)
    except OSError:
        print(f"{port} を開けません。ほかのアプリがこのポートを使用していないか確認して"
              "ください。")
        return 1
    try:
        # 改行だけ送ると、設定コンソールならプロンプトが返る
        got = send(s, "", timeout=3.0)
        if PROMPT not in got:
            print("Wi-Fi 設定コンソールが応答しません")
            print("受け取った文字:")
            print("  " + got.strip()[-200:].replace("\n", "\n  "))
            print("  " + DIAG_HOWTO)
            return 1
        # 新しく保存した接続先につながらずに設定待ちになった装置は、その理由を先に
        # 出す(値を修正するための材料になる)
        for ln in result_lines(send(s, "wifi show"), "wifi show"):
            if FAILED_RE.fullmatch(ln):
                print(ln)
        # 装置が拒否する値は送らない。装置と同じ文で、打った直後に拒否する(SSID は
        # パスワードを聞く前に分かる)
        ssid = input("SSID(2.4GHz): ").strip()
        why = proto.wifi_value_problem("wifi_ssid", ssid)
        if why:
            print(why)
            return 1
        pw = getpass.getpass("パスワード(入力中は非表示): ")
        why = proto.wifi_value_problem("wifi_pass", pw)
        if why:
            print(why)
            return 1
        if not (sendable(ssid) and sendable(pw)):
            print('" で始まる値と、" と空白の両方を含む値は、ケーブルからは'
                  "送信できません。Wi-Fi に接続している装置には、Wi-Fi から"
                  "設定できます。")
            print("  SSID を設定するには: python -m padcue config wifi_ssid")
            print("  パスワードを設定するには: python -m padcue config wifi_pass")
            return 1
        line = f"wifi set {quote(ssid)} {quote(pw)}"
        out = result_lines(send(s, line, timeout=8.0), line)
        saved = [ln for ln in out if SAVED_RE.fullmatch(ln)]
        if not saved:
            # 装置が拒否した理由(SSID が長すぎるなど)か、保存に失敗した理由を出す。
            # 装置の行は、何ができないかから書く
            if not out:
                print("保存できません。装置から応答がありません。")
            for ln in out:
                print(ln)
            return 1
        print(saved[0])
        print("再起動します…")
        s.write(b"reboot\r\n")
        s.flush()
        time.sleep(1.0)
    finally:
        s.close()
    print("保存しました。装置が Wi-Fi に接続すると、"
          "操作画面の「見つかった装置」に表示されます。")
    return 0


if __name__ == "__main__":
    # 日本語を出す。コンソールの文字コードで表せない字は置き換えて、異常終了させない
    sys.stdout.reconfigure(errors="replace")
    raise SystemExit(main())
