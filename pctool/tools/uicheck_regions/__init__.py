"""画面の区画ごとのテスト本体。区画の切り方は画面の見出しと同じ。

どの区画も、前の区画の後始末に依存しない。最初に _harness.start_region で
画面をその区画だけを実行したときの形に戻し、必要な状態は自分で作ってから
確認する。区画の中の項目どうしは順につながっている(理由は _harness を参照)。
エントリーポイントと実行する順は tools/uicheck.py にある。
"""
from .condition import run_cond_editor
from .devices import run_devices, run_disconnected
from .frametable import run_frametable_editor, run_frametable_keys_and_files
from .home import run_home
from .manual import run_manual_and_branch
from .multi import run_multi
from .newdevice import run_new_device
from .presets import run_presets
from .procedure import (
    run_pack,
    run_procedure_branch_and_folders,
    run_procedure_editor,
    run_procedure_list,
)
from .procedures import run_look_and_alerts, run_procedures, run_stop_and_partial

__all__ = [
    "run_cond_editor",
    "run_devices",
    "run_disconnected",
    "run_frametable_editor",
    "run_frametable_keys_and_files",
    "run_home",
    "run_look_and_alerts",
    "run_manual_and_branch",
    "run_multi",
    "run_new_device",
    "run_pack",
    "run_presets",
    "run_procedure_branch_and_folders",
    "run_procedure_editor",
    "run_procedure_list",
    "run_procedures",
    "run_stop_and_partial",
]
