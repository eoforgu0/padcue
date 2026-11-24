"""padcue の PC 側のツール(Switch を自動で操作する)。

バージョン番号は pctool/pyproject.toml の version の 1 か所で定義する(P26)。
ここは値を持たずそこを読むので、書き換える場所は 1 か所で済む。

装置のファームウェアのバージョンは firmware/main/app_config.h が別に持ち、変えた
ときだけ上げる。この padcue が使えるファームウェアの範囲は client.FIRMWARE_MIN と
FIRMWARE_BELOW で、このツリーのファームウェアがその範囲に入ることを
tests/test_version.py が求める。
"""
import re
import sys
from pathlib import Path

# padcue が対応する Python は 3.11 以上(pyproject.toml の requires-python と同じ。
# CONTRIBUTING.md「バージョンと公開」)。古い Python で開くと、読み込みの途中で
# 意味の分からないエラーで異常終了することがあるので、最初に確認して理由を表示する。
# このファイルには新しい書き方を入れない(ここより前で構文エラーになる)
if sys.version_info < (3, 11):  # noqa: UP036  padcue.bat は pip を経ず、requires-python を見ない
    sys.exit("padcue には Python 3.11 以上が必要です(この Python は "
             + sys.version.split()[0] + ")")

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _read_version() -> str:
    """定義元(pyproject.toml)からバージョン番号を読む。

    普段の使い方(padcue.bat)は pip install を経ず PYTHONPATH だけで動くので、
    まずソースツリーの pyproject.toml をそのまま読む。pip install したときは配布物に
    pyproject.toml が入らないので、そのときだけ配布物に記録されたバージョンを読む
    (古い `pip install -e` が残っていても、ソースツリー側が先に読まれる)。
    """
    try:
        m = re.search(r'^version\s*=\s*"([^"]+)"',
                      _PYPROJECT.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _installed_version
    try:
        return _installed_version("padcue")
    except PackageNotFoundError:
        raise RuntimeError(
            f"バージョンを読み込めません: {_PYPROJECT} がなく、パッケージ情報も"
            "ありません") from None


__version__ = _read_version()
