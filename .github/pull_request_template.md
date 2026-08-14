## 何を変えたか

<!-- 変わった事実を1〜2行で。理由は次の欄に -->

## なぜそうしたか

<!-- 採らなかった案があれば、その理由も -->

## 通した検査

<!-- 件数を書いてください(件数は増え続けるので、どの時点の話か辿れます)
     画面を触ったなら uicheck、通信やコンパイラを触ったなら pytest。
     迷ったら両方。実機が無くても両方通ります。 -->

- [ ] `python -m pytest -q` … ___ 件
- [ ] `python pctool/tools/uicheck.py <出力先>` … ___ 項目
- [ ] `python -m ruff check pctool`
- [ ] ファームウェアを触った場合: `cd firmware` → `idf.py build`

<!-- 詳しくは CONTRIBUTING.md -->
