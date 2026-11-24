"""コマンドの export(手順を padcue ファイルにエクスポートする)。"""
from padcue import cli
from tests.helpers import make_project


def export(proj, *args):
    return cli.main(["--project", str(proj.root), "export", *args])


def test_export_puts_the_file_in_the_given_folder(tmp_path, capsys):
    """-o はフォルダーで、ファイルの名前は選んだ手順から作ること(-h の説明どおり)。"""
    proj = make_project(tmp_path / "proj", {"周回": [{"type": "wait", "frames": 30}]})
    out = tmp_path / "出す先"
    out.mkdir()
    assert export(proj, "周回", "-o", str(out)) == 0
    assert [p.name for p in out.iterdir()] == ["周回.json"]
    assert "保存しました" in capsys.readouterr().out


def test_a_file_that_cannot_be_written_ends_with_1(tmp_path, capsys):
    """書けない出力先は、ファイルの失敗(終了コード 1)であり、装置につながらない
    失敗(3)ではないこと(CONTRIBUTING.md「コマンドの出力」)。"""
    proj = make_project(tmp_path / "proj", {"周回": [{"type": "wait", "frames": 30}]})
    blocker = tmp_path / "ファイル"
    blocker.write_text("x", encoding="utf-8")
    assert export(proj, "周回", "-o", str(blocker / "下")) == 1
    err = capsys.readouterr().err
    assert "読み書きできません" in err and "装置" not in err, err


def test_a_procedure_that_cannot_be_exported_writes_nothing(tmp_path, capsys):
    """選んだ手順がない場合も、読み込めない場合も、何も書かずに 1 で終えること
    (画面のエクスポートと同じ)。

    書くと、中身のない padcue ファイルを「保存しました」と表示して残し、渡した相手は
    インポートで初めて何も入っていないと知る。
    """
    proj = make_project(tmp_path / "proj", {"周回": [{"type": "wait", "frames": 30}]})
    out = tmp_path / "出す先"
    out.mkdir()
    for name in ("周会", "a?"):
        assert export(proj, name, "-o", str(out)) == 1
        got = capsys.readouterr()
        assert f"手順「{name}」が見つかりません" in got.err
        assert "保存しました" not in got.out
    (proj.root / "procedures" / "壊れた.json").write_text("{", encoding="utf-8")
    assert export(proj, "壊れた", "-o", str(out)) == 1
    assert "手順「壊れた」を読み込めません" in capsys.readouterr().err
    assert list(out.iterdir()) == []
