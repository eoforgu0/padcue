"""画面の区画ごとの検査本体。区画の切り方は画面の見出しと同じ。

どの区画も、前の区画の後片づけに依存しない(自分で必要な状態を作ってから
確かめる)。区画の中の項目どうしは順に繋がっている — 理由は _harness を参照。
入口と流す順は tools/uicheck.py にある。
"""
from .coupling import run_coupling, run_formations
from .devices import run_devices, run_disconnected
from .flow import run_flow_branch_and_folders, run_flow_editor, run_flow_list
from .home import run_home
from .manual import run_manual_and_branch
from .multi import run_multi
from .part import run_part_editor, run_part_keys_and_files
from .procedures import run_look_and_alerts, run_procedures, run_stop_and_partial

__all__ = [
    "run_coupling",
    "run_devices",
    "run_disconnected",
    "run_flow_branch_and_folders",
    "run_flow_editor",
    "run_flow_list",
    "run_formations",
    "run_home",
    "run_look_and_alerts",
    "run_manual_and_branch",
    "run_multi",
    "run_part_editor",
    "run_part_keys_and_files",
    "run_procedures",
    "run_stop_and_partial",
]
