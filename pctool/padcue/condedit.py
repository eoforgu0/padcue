"""条件の編集の画面からの要求を受ける、操作画面のサーバーの部分。

いまの映像の取り込みと採点、画像の追加、条件の保存・複製・改名・削除を
担当する。画面の側は web/condition.js である。gui.py の _Handler が
ConditionEditMixin を受け継いで使う。
"""
from __future__ import annotations

import base64
import threading
from typing import ClassVar

from .conditions import (
    AFTER_COUNT,
    COMPARE_H,
    COMPARE_W,
    DEFAULT_THRESHOLD,
    Condition,
    ConditionStore,
    aspect_differs,
    crop_to,
    judge,
    lane_crop,
    lane_file_why,
    png_size,
    resolve_lane_source,
    shrunk_frame,
    suggest_threshold,
    thin_to_view,
)
from .frames import encode_png_fast
from .names import refuse_taken, validate_name
from .words import error_text


class _Superseded(Exception):
    """同じ画面から新しい採点の要求が来たので、この採点をやめる。"""


class ConditionEditMixin:
    """条件の編集の画面の要求を受ける(gui.py の _Handler が受け継ぐ)。

    self.project、self._condwatch()、self._conflict()、self._renaming()、
    self._json()、self._png() は _Handler が持つ。
    """

    def _condition_preview(self, body: dict):
        """映像ソースから 1 枚取って PNG を返す(装置の映像のダイアログで使う)。

        body の source は conditions.make_source() がそのまま受ける形である。
        切り出す前の全体を返す(切り出す範囲は、この絵の上で決める)。判定と同じ
        CondWatcher.take_frame で取り込む。実行中のレーンが同じ
        映像機器を見ていても、2 つから同時には開かない。
        """
        # 取得が例外を投げるのは想定外の不具合で、do_POST の except が理由を返す
        try:
            frame, why = self._condwatch().take_frame(body.get("source") or {})
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        if frame is None:
            return self._json({"error": why}, 200)
        return self._png(encode_png_fast(frame.width, frame.height, frame.rgb))

    def _editing_condition(self, body: dict):
        """いま編集している中身の Condition を組み立てる(保存前も含む)。

        判定する範囲と画像の並びは、画面が持っている途中のものを使う。保存して
        ある範囲で採点すると、動かしている最中の目盛りが実物と合わない。画像の
        並びには、保存する前に追加した画像も入る(追加したときにフォルダーへ置く)。
        """
        store = ConditionStore(self.project.root)
        cond = store.load(body.get("name", ""))
        # 範囲は画面のものをそのまま使う。空は「画面全体」であって、保存して
        # ある範囲に戻すことではない
        regions = body.get("regions")
        if regions is None:
            regions = cond.regions
        refs, negatives = body.get("refs"), body.get("negatives")
        frames, sizes = cond.refs, cond.ref_sizes
        if refs is None:
            refs = cond.ref_files
        else:
            raws = [store.image_bytes(cond.name, k) for k in self._image_keys(refs)]
            frames = [shrunk_frame(raw) for raw in raws]
            sizes = [png_size(raw) for raw in raws]
        if negatives is None:
            negatives = cond.negatives
        return store, Condition(
            name=cond.name, source=cond.source,
            regions=[tuple(r) for r in regions], refs=frames,
            threshold=cond.threshold, ref_files=list(refs),
            negatives=list(self._image_keys(negatives)), ref_sizes=sizes)

    @staticmethod
    def _image_keys(keys) -> list[str]:
        """画面が送った画像の並びを確認する(文字列の並びでなければ拒否する)。"""
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ValueError("画像の並びの形が正しくありません")
        return keys

    # 採点の世代(条件の名前と画面ごと)。範囲を続けて動かすと、採点の要求が
    # 重なる。新しい要求が来たら、前の要求は残りの画像を採点せずに終える(画面は
    # 最後の応答しか使わないので、前の計算は捨てられるだけである)。画面ごとに
    # 数えるのは、別のタブの採点を打ち切らないためである
    _score_gen: ClassVar[dict] = {}
    _score_gen_lock = threading.Lock()

    def _condition_scores(self, body: dict) -> dict:
        """一致しないはずの画面ごとの一致度と、しきい値の目安を返す。

        目安の一致する側は、基準画像を登録した直後に取得した画像の一致度である。
        判定する範囲を動かすとすべての一致度が変わるので、保存せずここで出す。
        同じ画面から新しい要求が来たら、途中でやめて superseded を返す。
        """
        try:
            store, cond = self._editing_condition(body)
        except ValueError as e:
            return {"error": error_text(e)}
        if not cond.refs:
            return {"negatives": [], "suggest": None, "why": "",
                    "hit_min": None, "miss_max": None}
        key = (cond.name, str(body.get("client") or ""))
        with self._score_gen_lock:
            gen = self._score_gen[key] = self._score_gen.get(key, 0) + 1

        def rate(raw: bytes) -> int:
            if self._score_gen.get(key) != gen:
                raise _Superseded
            # 縮めた画像はキャッシュする(範囲を動かすたびにすべてを復号し直さない)
            return judge(shrunk_frame(raw), cond).score

        try:
            negatives = [{"file": k,
                          "score": rate(store.image_bytes(cond.name, k))}
                         for k in cond.negatives]
            hits = []
            for k in cond.ref_files:
                try:
                    afters = [store.image_bytes(cond.name, each)
                              for each in store.after_files(cond.name, k)]
                except ValueError:
                    # 数えた後に消された(条件の削除と重なった)。直後の画像は
                    # 画面に出ない物なので、知らせずに数えない
                    continue
                hits += [rate(each) for each in afters]
        except _Superseded:
            return {"superseded": True}
        except ValueError as e:
            return {"error": error_text(e)}
        misses = [n["score"] for n in negatives]
        suggest, why = suggest_threshold(hits, misses)
        return {"negatives": negatives, "suggest": suggest, "why": why,
                "hit_min": min(hits) if hits else None,
                "miss_max": max(misses) if misses else None}

    def _editing_frame(self, body: dict):
        """編集画面の「いまの映像」を 1 枚取る。(Frame, 取れない理由) を返す。

        いまの映像の表示も、基準画像を登録した直後に続けて取得する画像も、ここを
        通る。同じ処理(レーンへの解決、切り出し、間引き)を通さないと、
        表示した画像と、続けて取得した画像の一致度が比べられない。判定と同じ
        CondWatcher.take_frame で取り込む。実行中のレーンが同じ
        映像機器を見ていても、2 つから同時には開かない。
        """
        source = body.get("source") or {}
        crop = None
        lane = ""
        if (source or {}).get("kind") == "lane":
            lane = (body.get("lane") or "").strip()
            video = next((d.get("video") for d in
                          self.project.load_config().get("devices", [])
                          if d.get("name") == lane), None)
            resolved = resolve_lane_source(source, video)
            if resolved is None:
                return None, f"装置「{lane}」に映像が設定されていません"
            source, crop = resolved, lane_crop(video)
        try:
            frame, why = self._condwatch().take_frame(source)
        except ValueError as e:
            return None, str(e)
        if frame is None:
            # 装置の映像のファイルが読めないときは、修正する場所(装置の「映像」)を示す
            return None, (lane and lane_file_why(lane, source, why)) or why
        # 複数モニターのデスクトップ全体のような巨大な絵は、間引いてから使う。
        # そのままだと縮小と符号化に毎秒何秒もかかり、画面全体が重くなる。
        # 範囲は絵に対する割合で持つので、間引いても範囲の位置は変わらない。
        # 実行中の判定も同じく切り出して間引く(condwatch.pick_index)
        return thin_to_view(crop_to(frame, crop)), ""

    def _put_after(self, name: str, key: str, frame) -> int:
        """frame を基準画像 key の直後の画像として置き、置いた番号を返す。

        画面は、基準画像を追加した後の数回だけ、いまの映像を要求するときに after を
        付ける。取得するかどうかを画面が持つので、画面を離れたり映像を選び直したり
        すれば取得しない。置けないとき(基準画像がもうない、揃っている、書けない)
        は 0 を返し、画面は取得するのをやめる。
        """
        small = frame.shrink(COMPARE_W, COMPARE_H)
        png = encode_png_fast(small.width, small.height, small.rgb)
        try:
            return ConditionStore(self.project.root).add_after(name, key, png)
        except (ValueError, OSError):
            return 0

    def _condition_live(self, body: dict) -> dict:
        """いま見えている映像を 1 枚取り、その絵と一致度を一度に返す。

        絵は _editing_frame が間引いた後の大きさで返し、比較の大きさには
        縮めない(範囲は絵の上で引くので、細かく見えるほど正確に引ける)。
        一致度は同じ 1 枚から出すので、画面の絵と数が矛盾しない。source と
        regions は画面の(保存前も含む)ものを使う。
        返すのは、条件の一致度(score)、基準画像ごとの一致度(refs)、
        最も高い基準画像の位置(best)、その範囲ごとの一致度(regions)と、
        基準画像のどれかといまの映像の縦横の比が違うか(aspect)である。
        映像が取れないときは、その理由を why に返す。after(基準画像の位置)を
        付けると、この 1 枚をその基準画像の直後の画像として置き、置いた番号を
        after に返す(置けなければ 0)。
        """
        frame, why = self._editing_frame(body)
        if frame is None:
            return {"image": None, "score": None, "why": why}
        out = {
            "image": base64.b64encode(encode_png_fast(
                frame.width, frame.height, frame.rgb)).decode("ascii"),
            "score": None, "refs": [], "best": None, "regions": [], "why": "",
            "aspect": False,
        }
        if not body.get("name"):
            return out
        try:
            _store, cond = self._editing_condition(body)
        except ValueError as e:
            out["why"] = str(e)
            # 条件そのものがない(ほかで名前が変わった・消えた)。画面はこれを見て
            # 状態を取得し直し、追従するか、なくなったことを知らせる
            out["missing"] = (body.get("name") not in
                              ConditionStore(self.project.root).names())
            return out
        after = body.get("after")
        if isinstance(after, str) and after:
            out["after"] = self._put_after(cond.name, after, frame)
        if not cond.refs:
            return out
        got = judge(frame, cond)
        out.update(score=got.score, refs=got.refs, best=got.best,
                   regions=got.regions, aspect=aspect_differs(cond, frame))
        return out

    def _act_condition(self, path: str, body: dict) -> dict | None:
        """条件の作成・保存・画像の追加・削除・複製・改名。"""
        # ---- 条件(判定する映像 + 基準画像 + 判定する範囲 + しきい値) ----
        if path == "/api/condition/new":
            # 手順・フレーム表と同じく、名前を付けた時点で作る。既定は
            # 装置に設定している映像を画面全体で比べる、基準画像のない条件である
            name = validate_name(body.get("name"))
            store = ConditionStore(self.project.root)
            # 書き込み先の有無も見る(Windows の短い名前が、別の条件のフォルダーを
            # 指すことがある。そのまま保存すると、その条件の設定を既定に書き換える)
            refuse_taken(store.names(), name, path=store.doc_path(name))
            store.save(name, {"kind": "lane"}, [], DEFAULT_THRESHOLD)
            return {"ok": True, "name": name}
        if path == "/api/condition/save":
            # 設定と画像の並びを一緒に書く。壊れた source / regions / threshold と
            # 画像の並びの誤りは ConditionStore.save が ValueError で拒否する
            # (do_POST が受けて {"error":...} にする)
            conflict = self._conflict("condition", body.get("name", ""), body)
            if conflict:
                return conflict
            name = body.get("name", "")
            dropped = ConditionStore(self.project.root).save(
                name, body.get("source") or {},
                [tuple(r) for r in body.get("regions", [])],
                body.get("threshold", DEFAULT_THRESHOLD),
                refs=self._image_keys(body.get("refs")),
                negatives=self._image_keys(body.get("negatives")))
            # dropped は、ほかで消されていたので並びから外した画像である
            return {"ok": True, "dropped": dropped,
                    "rev": self.project.file_rev("condition", name)}
        if path == "/api/condition/image/add":
            # image は画面に出している「いまの映像」(base64 PNG)。表示した
            # ものをそのまま条件のフォルダーへ置く。条件が使うのは、画面が
            # 並びに入れて保存してからである。after は続けて取得する直後の画像の
            # 枚数で、画面は基準画像に追加したときだけ、この回数ぶん、いまの映像を
            # 要求するときにその画像の位置を付ける
            image = base64.b64decode(body.get("image") or "", validate=True)
            rel = ConditionStore(self.project.root).add_image(
                body.get("name", ""), image,
                "基準画像" if body.get("ref") is True else "画像")
            return {"ok": True, "file": rel, "after": AFTER_COUNT}
        if path == "/api/condition/delete":
            # 実行中の物でも削除できる。開始した実行は、開始した時点の条件のコピーで
            # 判定する(coupling.md D12。condwatch.py の _started_with)
            ConditionStore(self.project.root).delete(body.get("name", ""))
            self.project.follow_order("conditions", body.get("name", ""), None)
            return {"ok": True}
        if path == "/api/condition/copy":
            ConditionStore(self.project.root).copy(
                body.get("src", ""), body.get("dst", ""))
            return {"ok": True}
        if path == "/api/condition/rename":
            # 手順・フレーム表と同じく、選択肢の「選択条件」が指す名前も追従する
            store = ConditionStore(self.project.root)
            with self._renaming("condition", body.get("old"), body.get("new"),
                                store.names):
                store.rename(body.get("old", ""), body.get("new", ""))
                # 並びの位置も付け替える(付け替えないと一覧の末尾へ移る)
                self.project.follow_order("conditions", body.get("old", ""),
                                          body.get("new", ""))
                n = self.project.rename_condition_refs(body.get("old", ""),
                                                       body.get("new", ""))
            return {"ok": True, "updated": n}
        return None
