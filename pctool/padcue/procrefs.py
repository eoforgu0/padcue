"""手順の文書がどの手順、フレーム表、条件を参照するかを読む。"""
from __future__ import annotations


def blocks(nodes, enabled_only: bool = False):
    """ブロックの配列を、中身まで 1 つずつ返す。

    中身を持つのは 3 か所、つまりくり返しの `body`、選択肢の `body`、
    「それ以外」の `otherwise` である(file-format.md §10-2)。手順の中を
    たどる処理は、どれもこれを通す。たどり方を 1 か所で決めておかないと、
    中身を持つ場所が増えたときに、どれかの処理がたどり漏らす。

    返すのはブロックのコピーではなく、そのものである。参照を書き換える処理は、
    返ったブロックを直接書き換える。並びは書いてある順(ブロック、その中身の順)で
    ある。enabled_only が真なら、無効にしたブロックはその中身ごと飛ばす(実行される
    ブロックだけを見る)。
    """
    # 形の崩れた手順(body が配列でないなど)でも異常終了しない。毎秒の状態が数える
    # (usage_and_joins)ので、1 本の壊れた手順ですべての一覧が消えないようにする。
    # 再帰しないのも同じ理由である。毎秒の状態は変換より先に参照をたどるので、
    # 読み込みが拒否する深さ(procfmt.NEST_MAX)の手順もここを通る
    todo = [iter(nodes)] if isinstance(nodes, list) else []
    done = object()
    while todo:
        nd = next(todo[-1], done)
        if nd is done:
            todo.pop()
            continue
        if not isinstance(nd, dict):
            continue
        if enabled_only and nd.get("disabled"):
            continue
        yield nd
        inner = [nd.get("body"), *(arm.get("body") for arm in arms_of(nd)),
                 nd.get("otherwise")]
        # 後から積んだ物を先にたどるので、逆の順に積む
        todo.extend(iter(x) for x in reversed(inner) if isinstance(x, list))


def arms_of(nd: dict) -> list[dict]:
    """分岐のブロックの選択肢。形の崩れたものは飛ばす。"""
    arms = nd.get("arms")
    return [a for a in arms if isinstance(a, dict)] if isinstance(arms, list) else []


def condition_whats(nd: dict) -> list[dict]:
    """選択肢の「選択条件」のうち、条件を指すもの({kind: condition, ref: 名前})。"""
    return [arm["what"] for arm in arms_of(nd)
            if isinstance(arm.get("what"), dict)
            and arm["what"].get("kind") == "condition"]


def block_refs(nd: dict) -> set[tuple[str, str]]:
    """1 つのブロックが参照する名前を {(種類, 名前)} で返す。

    種類はブロックの種類と同じ語で、"call"(別の手順)、"frametable"(フレーム表)、
    "condition"(選択肢の「選択条件」が指す条件)である。条件は分岐の
    ブロックの選択肢が指すので、分岐のブロック 1 つが複数の条件を参照しうる。
    """
    out = set()
    ref = nd.get("ref")
    if nd.get("type") in ("call", "frametable") and isinstance(ref, str) and ref:
        out.add((nd["type"], ref))
    for what in condition_whats(nd):
        if isinstance(what.get("ref"), str) and what["ref"]:
            out.add(("condition", what["ref"]))
    return out


def refs_of(doc) -> tuple[set[str], set[str], set[str]]:
    """手順の文書が参照する名前を (手順, フレーム表, 条件) で返す。

    無効にしたブロック(`disabled` が真)の参照も入れる。有効に戻せばまた
    実行されるので、エクスポートで参照先を一緒に入れておかないと、戻した先で
    参照が切れる(file-format.md §10-2)。
    """
    got: dict[str, set[str]] = {"call": set(), "frametable": set(), "condition": set()}
    for nd in blocks(doc.get("body") if isinstance(doc, dict) else None):
        for kind, name in block_refs(nd):
            got[kind].add(name)
    return got["call"], got["frametable"], got["condition"]
