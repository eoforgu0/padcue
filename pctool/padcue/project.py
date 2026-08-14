"""プロジェクトフォルダの操作(手順の探索・コンパイル・成果物管理)。

    <プロジェクト>/
      procedures/<名前>.flow.json   フロー(正本)
      parts/<名前>.csv              行列部品
      build/<名前>.bin              コンパイル済み(転送するもの)
      padcue.json                デバイスの接続先など
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import binfmt
from .client import proc_hash
from .flowfmt import FlowError, compile_flow

# 名前はそのままファイル名になる。フォルダを跨げる文字を混ぜられると
# プロジェクトの外を読み書き・削除できてしまうので必ずここで弾く。
# 長さの上限 32 バイトは実機の保存名の制約(APP_STORE_MAX_NAME)に合わせる
_NAME_NG = set('/\\:*?"<>|')


def validate_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        raise ValueError("名前が空です")
    if len(n.encode("utf-8")) > 32:
        raise ValueError("名前は 32 バイト(日本語なら約10文字)までです")
    bad = sorted({c for c in n if c in _NAME_NG or ord(c) < 32})
    if bad or ".." in n:
        shown = " ".join(repr(c) if ord(c) < 32 else c for c in bad) or ".."
        raise ValueError(f"名前に使えない文字があります: {shown}"
                         '(/ \\ : * ? " < > | .. は不可)')
    if n.startswith("."):
        raise ValueError("名前を「.」で始めることはできません")
    return n


@dataclass
class BuildResult:
    name: str
    blob: bytes
    total_frames: int
    events: int
    warnings: list
    labels: list
    pre: str
    resume_points: list = field(default_factory=list)
    wait_branch_arms: list = field(default_factory=list)

    @property
    def hash(self) -> str:
        return proc_hash(self.blob)

    @property
    def seconds(self) -> float:
        return self.total_frames / 60.0


class Project:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    # ---- 設定 ----

    @property
    def config_path(self) -> Path:
        return self.root / "padcue.json"

    def load_config(self) -> dict:
        if self.config_path.is_file():
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            cfg = {"host": "pademu.local", "port": 5555}
        return self._migrate_config(cfg)

    def _migrate_config(self, cfg: dict) -> dict:
        """旧形式(host/port 単一)を装置台帳(devices)へ移行する(2026-08-04 P1)。

        - devices: [{id(MAC・保存キー), name(表示名), host, port}] を正とする
        - 旧キー host/port は devices[0] の写しとして併記し続ける(移行期間中の
          旧コード・外部ツールが読めるように)
        - 初回移行時は元ファイルを .bak として残す(事故時に手で戻せる)
        """
        if "devices" not in cfg:
            if self.config_path.is_file():
                bak = self.config_path.with_suffix(".json.bak")
                if not bak.exists():
                    bak.write_text(self.config_path.read_text(encoding="utf-8"),
                                   encoding="utf-8")
            cfg["devices"] = [{"id": "", "name": "1P",
                              "host": cfg.get("host", "pademu.local"),
                              "port": int(cfg.get("port", 5555))}]
            self.save_config(cfg)
        # 本体の名前(consoles)のキーを、ペアリング引数の先頭8バイトから
        # 本体 MAC の6バイトへ移す(2026-08-12)。8バイトには先頭のフェーズ
        # 番号と末尾のフェーズ依存バイトが混ざっていて、同じ本体でも登録の
        # 前後で別キーになり、付けた名前が引き継がれなかった
        cons = cfg.get("consoles")
        if isinstance(cons, dict) and any(len(k) == 16 for k in cons):
            cfg["consoles"] = {(k[2:14] if len(k) == 16 else k): v
                               for k, v in cons.items()}
            self.save_config(cfg)
        return cfg

    def save_config(self, cfg: dict) -> None:
        devs = cfg.get("devices")
        if devs:
            # 旧キー host/port を書き換えた呼び出し元(既存ツール)の意図は
            # 「1台目の接続先の変更」なので devices[0] へ取り込む。
            # 「呼び出し元が本当に旧キーを変えたのか」はディスク上の値との
            # 差で判別する(devices 側だけを差し替えた呼び出し元の変更を、
            # 読み込んだままの古い旧キーで巻き戻さないため)
            disk_host = disk_port = None
            if self.config_path.is_file():
                try:
                    disk = json.loads(
                        self.config_path.read_text(encoding="utf-8"))
                    disk_host, disk_port = disk.get("host"), disk.get("port")
                except ValueError:
                    pass
            if cfg.get("host") is not None and cfg["host"] != disk_host \
                    and cfg["host"] != devs[0]["host"]:
                devs[0]["host"] = cfg["host"]
            if cfg.get("port") is not None and cfg["port"] != disk_port \
                    and int(cfg["port"]) != devs[0]["port"]:
                devs[0]["port"] = int(cfg["port"])
            cfg["host"] = devs[0]["host"]
            cfg["port"] = devs[0]["port"]
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_device(self, cfg: dict, idx: int, **fields) -> None:
        """装置台帳のエントリを更新して保存する(新コードはこれを使う)。

        2つの事故を防ぐ:
        - 旧キー host/port との突き合わせによる巻き戻り(save_config は
          「旧キーの変更=1台目の接続先変更の意図」と解釈するため、
          ここで両方を同時に揃える)
        - 手元の cfg が古いまま全量保存して、他プロセスの変更(別端末の
          device add 等)を消すこと。保存直前にディスクから読み直し、
          対象エントリ(IDが控えてあればID、なければ名前、最後は位置で特定)
          にだけ変更を当てる
        """
        target = cfg["devices"][idx]
        target.update(fields)                      # 呼び出し元の観測も揃える
        fresh = cfg
        if self.config_path.is_file():
            try:
                fresh = self._migrate_config(json.loads(
                    self.config_path.read_text(encoding="utf-8")))
            except ValueError:
                pass                               # 壊れていれば手元を正とする
        devs = fresh.get("devices") or [target]
        hit = (next((d for d in devs
                     if target.get("id") and d.get("id") == target["id"]), None)
               or next((d for d in devs
                        if d.get("name") == target.get("name")), None)
               or devs[min(idx, len(devs) - 1)])
        hit.update(fields)
        if hit is devs[0]:
            fresh["host"] = hit["host"]
            fresh["port"] = hit["port"]
        self.save_config(fresh)

    # ---- ログ(logs.jsonl) ----
    # 実機のログは取り出すと実機側から消える(リングバッファ)。取り出した端から
    # プロジェクト直下 logs.jsonl に追記して、あとから見返せるようにする。
    # 1行1件の JSON。受け取った時刻(PC の壁時計)をここで付ける

    LOG_KEEP = 5000        # これを超えたら古い行から捨てる(肥大化防止)

    def log_path(self):
        return self.root / "logs.jsonl"

    # 書き込みは単一ライタに直列化する。装置2台のログを別スレッドが並行して
    # 追記すると、追記と間引き(全読み・全書き)が競合して行が消えるため
    # (2026-08-04 2台化 P1。今までは GUI の単一 lock が偶然守っていた)
    _log_write_lock = threading.Lock()

    def append_logs(self, entries: list[dict], dev: str = "") -> None:
        """ログを追記する。dev は装置の個体ID(2台化で「どの装置の記録か」を
        後から区別するため。保存キーは改名に耐える id、表示時に名前へ解決)。"""
        if not entries:
            return
        now = time.time()
        lines = []
        for e in entries:
            d = dict(e)
            d["at"] = now          # PC が受け取った時刻(UNIX 秒)
            if dev and "dev" not in d:
                d["dev"] = dev
            lines.append(json.dumps(d, ensure_ascii=False))
        with self._log_write_lock:
            with self.log_path().open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self._trim_logs()

    def _trim_logs(self) -> None:
        p = self.log_path()
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= self.LOG_KEEP * 2:      # 毎回は書き直さない
            return
        p.write_text("\n".join(lines[-self.LOG_KEEP:]) + "\n",
                     encoding="utf-8")

    def read_logs(self, limit: int = 1000) -> list[dict]:
        """新しい順ではなく古い順(表示の並び)で、末尾 limit 件を返す。"""
        try:
            lines = self.log_path().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def clear_logs(self, dev: str = "") -> None:
        """ログを消す。dev(装置の個体ID)を指定すると、その装置の行だけ消す
        (2台運用で片方の記録だけ整理したいとき。絞り込み表示と対応)。"""
        if not dev:
            self.log_path().unlink(missing_ok=True)
            return
        # 読み→選別→書き戻しの間に収集係が追記すると消えてしまうので、
        # 全体を書き込み lock の中で行う
        with self._log_write_lock:
            try:
                lines = self.log_path().read_text(
                    encoding="utf-8").splitlines()
            except OSError:
                return
            kept = []
            for line in lines:
                try:
                    if json.loads(line).get("dev") == dev:
                        continue
                except ValueError:
                    pass                   # 壊れた行は消す対象を誤らない
                kept.append(line)
            self.log_path().write_text(
                "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    # ---- 並び順(order.json) ----
    # 一覧の並びはユーザーが D&D で決める。保存先はプロジェクト直下の
    # order.json({"procedures": [...], "parts": [...]})。載っていない名前は
    # 従来どおり名前順で末尾に付く(ファイルが無ければ全て名前順 = 従来互換)。
    # 存在しない名前は無視する(改名・削除で残った項目が悪さをしない)

    def _order_path(self):
        return self.root / "order.json"

    def _load_order(self) -> dict:
        try:
            d = json.loads(self._order_path().read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            pass
        return {}

    def _apply_order(self, names: list[str], kind: str) -> list[str]:
        order = self._load_order().get(kind)
        if not isinstance(order, list):
            return sorted(names)
        rest = sorted(n for n in names if n not in order)
        # 同じ名前が二度載っていても一覧に二行出さない(手で order.json を
        # 直したときや、改名の追従が重なったときの保険)
        return list(dict.fromkeys(n for n in order if n in names)) + rest

    def save_order(self, kind: str, names: list[str]) -> None:
        if kind not in ("procedures", "parts"):
            raise ValueError(f"未知の並び順の種類: {kind}")
        d = self._load_order()
        d[kind] = list(names)
        self._order_path().write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 手順の整理(非表示・フォルダ) ----
    # order.json に procedures/parts と同居させる:
    #   hidden: [手順名, ...]                       実行・監視の一覧に出さない
    #   folders: [{name, open, items: [手順名,...]}] フォルダ(表示専用の入れ物。
    #                                                実体は procedures/ 直下のまま)
    # 表示順は「フォルダ(配列順)→フォルダ外(procedures 順)」、どこにも
    # 載っていない手順は名前順で末尾(既存の並び順規則の延長)。
    # 存在しない手順名の参照は読み込み時に無視する(改名・削除の残骸対策)

    def load_proc_org(self) -> dict:
        d = self._load_order()
        names = set(self.procedure_names())
        hidden_raw = d.get("hidden")
        hidden = ([n for n in hidden_raw if isinstance(n, str) and n in names]
                  if isinstance(hidden_raw, list) else [])
        folders = []
        folders_raw = d.get("folders")
        if isinstance(folders_raw, list):
            for f in folders_raw:
                if not isinstance(f, dict):
                    continue
                fname = f.get("name")
                if not isinstance(fname, str) or not fname.strip():
                    continue
                items_raw = f.get("items")
                items = ([n for n in items_raw if isinstance(n, str) and n in names]
                         if isinstance(items_raw, list) else [])
                folders.append({"name": fname, "open": bool(f.get("open", True)),
                                "items": items})
        return {"folders": folders, "hidden": hidden}

    def save_proc_org(self, folders: list, hidden: list) -> None:
        seen_names = set()
        norm_folders = []
        used_items = set()
        for f in folders:
            if not isinstance(f, dict):
                raise ValueError("フォルダの形式が不正です")
            fname = str(f.get("name", "")).strip()
            if not fname:
                raise ValueError("フォルダ名を入力してください")
            if fname in seen_names:
                raise ValueError(f"フォルダ「{fname}」が重複しています")
            seen_names.add(fname)
            items = []
            for n in (f.get("items") or []):
                n = str(n)
                if n in used_items:
                    continue    # 1手順は最大1フォルダ(先に出た方を優先)
                used_items.add(n)
                items.append(n)
            norm_folders.append({"name": fname, "open": bool(f.get("open", True)),
                                 "items": items})
        d = self._load_order()
        d["folders"] = norm_folders
        d["hidden"] = [str(n) for n in hidden]
        self._order_path().write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    def _update_org_refs(self, old: str, new: str | None,
                         kind: str = "procedures") -> None:
        """order.json の中の名前を、改名・削除に追従させる。

        rename_*/delete_* から呼ぶ。
        並び順(procedures/parts)も名前で位置を覚えているので、追従しないと
        改名した途端に「載っていない名前」と見なされ、D&D で決めた場所を
        失って一覧の末尾へ飛ぶ。フォルダ所属・非表示(手順のみ)は名前が
        そのまま意味を持つため、放置すると別物として消えてしまう。
        new が None なら削除(参照を取り除く)、それ以外は改名(名前を差し替え)。
        """
        d = self._load_order()
        changed = False

        def follow(lst) -> None:
            nonlocal changed
            if not isinstance(lst, list) or old not in lst:
                return
            i = lst.index(old)
            if new is None:
                lst.pop(i)
            else:
                lst[i] = new
            changed = True

        follow(d.get(kind))
        if kind == "procedures":
            follow(d.get("hidden"))
            folders = d.get("folders")
            if isinstance(folders, list):
                for f in folders:
                    if isinstance(f, dict):
                        follow(f.get("items"))
        if changed:
            self._order_path().write_text(
                json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 手順 ----

    def procedure_names(self) -> list[str]:
        d = self.root / "procedures"
        if not d.is_dir():
            return []
        return self._apply_order(
            [p.name[: -len(".flow.json")] for p in d.glob("*.flow.json")],
            "procedures")

    def part_names(self) -> list[str]:
        d = self.root / "parts"
        if not d.is_dir():
            return []
        return self._apply_order([p.stem for p in d.glob("*.csv")], "parts")

    # ---- 編成(連結実行の盤面スナップショット。sets/<名前>.json) ----
    # 利用者の資産(手順・部品と同格)なのでプロジェクト直下に置く。
    # 保存キーは装置の個体ID(改名しても編成が切れないように。計画 D6)

    def formation_names(self) -> list[str]:
        d = self.root / "sets"
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def _formation_path(self, name: str) -> Path:
        return self.root / "sets" / f"{validate_name(name)}.json"

    def save_formation(self, name: str, data: dict) -> None:
        d = self.root / "sets"
        d.mkdir(exist_ok=True)
        payload = {"schema": 1, "name": validate_name(name),
                   "linked": bool(data.get("linked", True)),
                   "auto_join": bool(data.get("auto_join", True)),
                   "arm": int(data.get("arm", 0)),
                   "devices": [
                       {"id": str(x.get("id", "")),
                        # 名前は ID が空のときの解決の綱(練習の mock は設計上
                        # ID を学習しないため、ID だけだと装置を引けない)
                        "name": str(x.get("name", "")),
                        "proc": str(x.get("proc", "")),
                        "loops": max(0, int(x.get("loops", 0))),
                        "resume": str(x.get("resume", ""))}
                       for x in data.get("devices", [])]}
        self._formation_path(name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def load_formation(self, name: str) -> dict:
        return json.loads(
            self._formation_path(name).read_text(encoding="utf-8"))

    def delete_formation(self, name: str) -> None:
        self._formation_path(name).unlink(missing_ok=True)

    def rename_formation(self, old: str, new: str) -> None:
        """プリセットの名前を変える(ファイル改名+中身の name も書き換え)。"""
        old, new = validate_name(old), validate_name(new)
        if old == new:
            return
        if not self._formation_path(old).is_file():
            raise ValueError(f"プリセット「{old}」がありません")
        if self._formation_path(new).exists():
            raise ValueError(f"「{new}」は既にあります")
        data = self.load_formation(old)
        data["name"] = new
        self._formation_path(new).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._formation_path(old).unlink(missing_ok=True)

    def build(self, name: str) -> BuildResult:
        c = compile_flow(self.root, name)
        blob = binfmt.encode(c.name, c.events, c.total_frames)
        out = self.root / "build"
        out.mkdir(exist_ok=True)
        (out / f"{name}.bin").write_bytes(blob)
        return BuildResult(
            name=name, blob=blob, total_frames=c.total_frames,
            events=len(c.events),
            warnings=[{"line": w.line, "msg": w.msg} for w in c.warnings],
            labels=[{"frame": f, "text": t} for f, t in c.labels],
            pre=c.pre,
            resume_points=c.resume_points,
            wait_branch_arms=c.wait_branch_arms,
        )

    def _friendly(self, name: str, e: Exception) -> str:
        import json as _json
        if isinstance(e, _json.JSONDecodeError):
            return (f"手順「{name}」のファイルが壊れています"
                    f"({e.lineno}行目{e.colno}文字目から読めません)")
        if isinstance(e, UnicodeDecodeError):
            return f"手順「{name}」の文字コードが UTF-8 ではありません"
        return str(e)

    def build_safe(self, name: str) -> tuple[BuildResult | None, str]:
        """コンパイルし、失敗したらエラーメッセージを返す(GUI 用)。"""
        try:
            return self.build(name), ""
        except (FlowError, ValueError, UnicodeDecodeError, OSError) as e:
            return None, self._friendly(name, e)

    # ---- 編集(GUI から使う読み書き)----

    def flow_path(self, name: str) -> Path:
        return self.root / "procedures" / f"{validate_name(name)}.flow.json"

    def part_path(self, name: str) -> Path:
        return self.root / "parts" / f"{validate_name(name)}.csv"

    def load_flow_doc(self, name: str) -> dict:
        return json.loads(self.flow_path(name).read_text(encoding="utf-8"))

    def save_flow_doc(self, name: str, doc: dict) -> None:
        if doc.get("name") != name:
            raise ValueError("手順名とファイル名が一致していません")
        self.flow_path(name).parent.mkdir(parents=True, exist_ok=True)
        self.flow_path(name).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_part_table(self, name: str) -> dict:
        """CSV を編集しやすい形(ヘッダ+行の二次元配列)で返す。"""
        import csv
        import io
        text = self.part_path(name).read_text(encoding="utf-8").lstrip("﻿")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            return {"name": name, "header": ["A"], "rows": [[""]]}
        header = [h.strip() for h in rows[0] if h.strip() != ""]
        body = []
        for r in rows[1:]:
            if not r or (all(c.strip() == "" for c in r) and len(r) < len(header)):
                continue
            cells = [c.strip() for c in r][:len(header)]
            cells += [""] * (len(header) - len(cells))
            body.append(cells)
        return {"name": name, "header": header, "rows": body}

    def save_part_table(self, name: str, header: list, rows: list) -> None:
        """編集結果を CSV へ書く。書く前に必ず検証する。"""
        import csv
        import io

        from .matrix import load_part
        header = [str(h).strip() for h in header if str(h).strip()]
        if not header:
            raise ValueError("列がありません")
        buf = io.StringIO(newline="")
        # csv.writer を使う(1列だけの空行が空行と区別できなくなるのを避けるため。
        # 単一の空セルは "" と引用して書かれる)
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            cells = [str(c).strip() for c in r][:len(header)]
            cells += [""] * (len(header) - len(cells))
            w.writerow(cells)
        text = buf.getvalue()
        load_part(name, text)   # 不正なら例外(保存しない)
        self.part_path(name).parent.mkdir(parents=True, exist_ok=True)
        self.part_path(name).write_text(text, encoding="utf-8")

    def _rewrite_refs(self, kind: str, old: str, new: str) -> int:
        """全手順の中の参照(call / part)を書き換える。

        名前を変えたのに参照が古いままだと、変換に失敗するか
        別のものを指してしまう。改名と同時に必ず直す。
        """
        changed = 0
        for pname in self.procedure_names():
            try:
                doc = self.load_flow_doc(pname)
            except (OSError, ValueError):
                continue      # 壊れた手順は触らない

            def walk(nodes) -> bool:
                hit = False
                for nd in nodes or []:
                    if not isinstance(nd, dict):
                        continue
                    if nd.get("type") == kind and nd.get("ref") == old:
                        nd["ref"] = new
                        hit = True
                    if isinstance(nd.get("body"), list):
                        hit = walk(nd["body"]) or hit
                    arms = nd.get("arms")
                    if isinstance(arms, list):
                        for arm in arms:
                            hit = walk(arm) or hit
                    elif isinstance(arms, dict):
                        for arm in arms.values():
                            hit = walk(arm) or hit
                return hit

            if walk(doc.get("body")):
                self.save_flow_doc(pname, doc)
                changed += 1
        return changed

    def rename_procedure(self, old: str, new: str) -> int:
        """手順の名前を変える。呼んでいる側の参照も書き換える。"""
        old, new = validate_name(old), validate_name(new)
        if old == new:
            return 0
        if not self.flow_path(old).is_file():
            raise ValueError(f"手順「{old}」がありません")
        if self.flow_path(new).exists():
            raise ValueError(f"「{new}」は既にあります")
        doc = self.load_flow_doc(old)
        doc["name"] = new
        self.save_flow_doc(new, doc)
        self.flow_path(old).unlink(missing_ok=True)
        (self.root / "build" / f"{old}.bin").unlink(missing_ok=True)
        self._update_org_refs(old, new)
        return self._rewrite_refs("call", old, new)

    def rename_part(self, old: str, new: str) -> int:
        """部品の名前を変える。使っている手順の参照も書き換える。"""
        old, new = validate_name(old), validate_name(new)
        if old == new:
            return 0
        if not self.part_path(old).is_file():
            raise ValueError(f"部品「{old}」がありません")
        if self.part_path(new).exists():
            raise ValueError(f"「{new}」は既にあります")
        self.part_path(new).write_text(
            self.part_path(old).read_text(encoding="utf-8"), encoding="utf-8")
        self.part_path(old).unlink(missing_ok=True)
        self._update_org_refs(old, new, "parts")
        return self._rewrite_refs("part", old, new)

    def copy_procedure(self, src: str, new: str) -> None:
        """手順をコピーして別名で作る(中身の参照はそのまま)。"""
        src, new = validate_name(src), validate_name(new)
        if self.flow_path(new).exists():
            raise ValueError(f"「{new}」は既にあります")
        doc = self.load_flow_doc(src)
        doc["name"] = new
        self.save_flow_doc(new, doc)

    def copy_part(self, src: str, new: str) -> None:
        src, new = validate_name(src), validate_name(new)
        if self.part_path(new).exists():
            raise ValueError(f"「{new}」は既にあります")
        self.part_path(new).write_text(
            self.part_path(src).read_text(encoding="utf-8"), encoding="utf-8")

    def delete_procedure(self, name: str) -> None:
        self.flow_path(name).unlink(missing_ok=True)
        (self.root / "build" / f"{name}.bin").unlink(missing_ok=True)
        self._update_org_refs(name, None)

    def delete_part(self, name: str) -> None:
        self.part_path(name).unlink(missing_ok=True)
        self._update_org_refs(name, None, "parts")

    def init_sample(self) -> None:
        """はじめて使うときの雛形を作る。"""
        (self.root / "procedures").mkdir(parents=True, exist_ok=True)
        (self.root / "parts").mkdir(parents=True, exist_ok=True)
        flow = self.root / "procedures" / "サンプル.flow.json"
        if not flow.exists():
            flow.write_text(json.dumps({
                "schema": 1,
                "name": "サンプル",
                "pre": "ホーム画面を開いた状態",
                "body": [
                    {"type": "label", "text": "開始"},
                    {"type": "press", "buttons": ["A"], "frames": 5},
                    {"type": "wait", "frames": 60},
                    {"type": "loop", "count": 3, "body": [
                        {"type": "part", "ref": "サンプル部品"},
                        {"type": "wait", "frames": 30},
                    ]},
                    {"type": "label", "text": "終了"},
                    {"type": "wait", "frames": 60},
                ],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        part = self.root / "parts" / "サンプル部品.csv"
        if not part.exists():
            part.write_text("F,A,B,LX\n1,1,,\n2,1,,\n3,1,1,-1200\n"
                            "4,,1,-1200\n5,,,\n", encoding="utf-8")
