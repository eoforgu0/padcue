"""文のつなぎ方(padcue.words.sentences)のテスト。

句点は、1 文なら付けず、2 文以上なら各文に付ける(gui-principles.md §7-12)。
サーバーの理由を画面の文に埋め込む所はこれを通るので、ここが崩れると
「。。」や最後の文だけ句点のない文が画面じゅうに出る。
"""
from __future__ import annotations

import pytest

from padcue.words import error_text, for_user, sentences


def test_one_sentence_gets_no_period():
    assert sentences("装置が見つかりません") == "装置が見つかりません"
    assert sentences("装置が見つかりません。") == "装置が見つかりません"


def test_two_sentences_are_both_closed():
    assert sentences("開始できません", "装置を確認してください") == \
        "開始できません。装置を確認してください。"


def test_a_part_may_already_end_with_a_period():
    assert sentences("開始できません。", "装置を確認してください。") == \
        "開始できません。装置を確認してください。"


def test_empty_parts_are_dropped():
    assert sentences("開始できません", "", None, "  ") == "開始できません"


def test_a_part_with_two_sentences_counts_as_two():
    assert sentences("映像がありません。装置の一覧で映像を設定してください") == \
        "映像がありません。装置の一覧で映像を設定してください。"


@pytest.mark.parametrize("text, ours", [
    ("登録情報のファイル(padcue.json)が壊れています", True),
    ('schema は 1 である必要があります: "1"', True),
    ("「1P」は既にあります", True),
    ("invalid literal for int() with base 10: 'abc'", False),
    # Python の英語の文が、引用した値に日本語を含むとき
    ("invalid literal for int() with base 10: 'あ'", False),
    ('could not convert string to float: "一"', False),
])
def test_only_words_written_for_the_user_are_told_as_they_are(text, ours):
    """利用者に見せるために書いた文(引用の外に日本語がある)だけを、そのまま出すこと。

    Python の英語の文をそのまま出すと、コマンドの出力に英語の理由が出る。
    """
    assert for_user(ValueError(text)) is ours


# Windows のパスの長さの上限(終端を除いて 259)を超えた場所。作る側の操作でも
# FileNotFoundError(WinError 3 か 206)になる
_LONG = "C:/data/" + "あ" * 120 + "/" + "い" * 140 + ".json"


@pytest.mark.parametrize("e", [
    FileNotFoundError(2, "No such file or directory", _LONG),
    # 置き換え(一時ファイル → 正式な名前)は 2 つ目の名前が長い
    FileNotFoundError(2, "No such file or directory", "C:/data/a.tmp", None, _LONG),
])
def test_a_path_too_long_says_so(e):
    """パスが長すぎて開けないときは、フォルダーを移していないかと尋ねる文を出さない
    こと。

    名前が長いか、プロジェクトのフォルダーが深いと、新しく作る物でも
    FileNotFoundError になる。「移動または削除していないか」では原因にたどり着けない。
    """
    assert error_text(e) == ("ファイルのパスが長すぎます。名前を短くするか、"
                             "プロジェクトのフォルダーを浅い場所に移してください。")


def test_a_short_missing_path_is_still_a_missing_file():
    e = FileNotFoundError(2, "No such file or directory", "C:/data/a.json")
    assert error_text(e).startswith("ファイルが見つかりません")
