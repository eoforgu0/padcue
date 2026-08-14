"""Markdown の表の桁を、表示セル幅で揃える。

日本語を含む表は、文字数で揃えても見た目が揃わない。East Asian Width が
W/F の文字を 2 セルとして数える。コードブロックの中は触らない。

使い方:
    python pctool/tools/mdtable.py docs/hardware-design.md [--check]
"""

import re
import sys
import unicodedata


def cells(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - cells(s))


# 列区切りの `|` と、セルの中身として書かれた `\|` を見分ける。
# 見分けないと `mode=immediate\|graceful\|cancel` のような値が3列に割れる。
_CELL_SEP = re.compile(r"(?<!\\)\|")


def split_row(line: str) -> list[str] | None:
    t = line.strip()
    if not t.startswith("|") or not t.endswith("|") or len(t) < 2:
        return None
    return [c.strip() for c in _CELL_SEP.split(t[1:-1])]


def is_sep(cols: list[str]) -> bool:
    return all(c and set(c) <= set("-:") for c in cols)


def format_table(rows: list[list[str]]) -> list[str]:
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    widths = [0] * n
    for r in rows:
        if is_sep(r):
            continue
        for i, c in enumerate(r):
            widths[i] = max(widths[i], cells(c))
    out = []
    for r in rows:
        if is_sep(r):
            out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
        else:
            cols = " | ".join(pad(c, w)
                             for c, w in zip(r, widths, strict=True))
            out.append("| " + cols + " |")
    return out


def process(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    buf: list[list[str]] = []
    raw: list[str] = []
    in_code = False

    def flush() -> None:
        if not buf:
            return
        # 表とみなすのは「区切り行を持つ」ものだけ
        if any(is_sep(r) for r in buf) and len(buf) >= 2:
            out.extend(format_table(buf))
        else:
            out.extend(raw)
        buf.clear()
        raw.clear()

    for line in lines:
        if line.strip().startswith("```"):
            flush()
            in_code = not in_code
            out.append(line)
            continue
        cols = None if in_code else split_row(line)
        if cols is None:
            flush()
            out.append(line)
        else:
            buf.append(cols)
            raw.append(line)
    flush()
    return "\n".join(out)


def main() -> int:
    path = sys.argv[1]
    check = "--check" in sys.argv
    with open(path, encoding="utf-8", newline="") as f:
        src = f.read()
    dst = process(src)
    if src == dst:
        print("変更なし:", path)
        return 0
    if check:
        print("桁がずれている表があります:", path)
        return 1
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(dst)
    print("整形:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
