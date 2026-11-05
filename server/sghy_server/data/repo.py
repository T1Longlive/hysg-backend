"""玩家仓库：PLAYER_DATA dict ↔ SQLite 行 的双向映射与增量落库。

映射策略：
  · 权威字段（业务会读写的）落真实列，其余进 extra_json —— 与 dict 形状
    无损 round-trip（派生属性、_starup_ready 之类的冗余字段一个不丢）。
  · save() 做快照 diff：只 upsert 变化的行、删除消失的行，单事务提交。
  · levels / level_records 是两个同构 dict（EnterGame 读前者、520 推送读
    后者），用 source 列区分，各自保留。
"""

import copy
import json
import os

from sqlalchemy import delete, select

from .db import Database
from .models import Account, Equip, FateRecord, Hero, ItemStack, LevelRecord, Player

# ---------------------------------------------------------------- 字段映射

HERO_COLS = ["type", "lv", "exp", "grade", "grade_exp", "awaken", "star",
             "position", "can_break", "name", "avatar", "avatar_type",
             "spine", "equips_json"]
HERO_DEFAULTS = {"type": 0, "lv": 1, "exp": 0, "grade": 0, "grade_exp": 0,
                 "awaken": 0, "star": 0, "position": 0, "can_break": False,
                 "name": "", "avatar": "", "avatar_type": 0, "spine": "",
                 "equips_json": []}

EQUIP_COLS = ["type", "host", "strengthen", "grade", "grade_exp", "en_crit",
              "assist_json", "pending_assist_json"]
EQUIP_DEFAULTS = {"type": 0, "host": 0, "strengthen": 0, "grade": 0,
                  "grade_exp": 0, "en_crit": 0, "assist_json": {},
                  "pending_assist_json": {}}

PLAYER_COLS = ["name", "lv", "exp", "coin", "diamond", "ep", "vigour", "vip",
               "guide_index"]
PLAYER_DEFAULTS = {"name": "本地测试", "lv": 1, "exp": 0, "coin": 0,
                   "diamond": 0, "ep": 0, "vigour": 0, "vip": 0,
                   "guide_index": 0}


def _split(d: dict, cols, defaults, drop=()):
    """dict -> (权威列 dict, extra dict)"""
    colset = set(cols) | set(drop)
    columns = {c: d.get(c, defaults.get(c)) for c in cols}
    extra = {k: v for k, v in d.items() if k not in colset}
    return columns, extra


def _merge(row_id, columns, extra):
    """行 -> dict（还原 PLAYER_DATA 里的条目）"""
    out = dict(extra)
    out["id"] = row_id
    out.update(columns)
    return out


def _canon(columns, extra):
    """可比对的规范形式（extra 按键排序稳定序列化）"""
    return (tuple(sorted((k, _freeze(v)) for k, v in columns.items())),
            json.dumps(extra, sort_keys=True, ensure_ascii=False, default=str))


def _freeze(v):
    if isinstance(v, (list, tuple)):
        return tuple(_freeze(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _freeze(x)) for k, x in v.items()))
    return v


# ---------------------------------------------------------------- 仓库

class PlayerRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---- 读 ----
    async def load(self, player_id: int):
        """返回 PLAYER_DATA 形状的 dict；无此玩家返回 None。"""
        async with self.db.sessionmaker() as s:
            p = (await s.execute(
                select(Player).where(Player.id == player_id))).scalar_one_or_none()
            if p is None:
                return None

            player = {c: getattr(p, c) for c in PLAYER_COLS}
            player.update(dict(p.extra_json or {}))
            # ★ 顶层业务键自动持久化（2026-10-22）：save 时收进 extra_json 的
            #   "_top" 命名空间，load 时提升回 PLAYER_DATA 顶层——sign/lottery
            #   等任何新增顶层键自动纳入，无需逐个登记（此前 sign 不落库导致
            #   重启可从头签到）。旧格式（sign 直接混在 player 里）兼容读取。
            top_keys = player.pop("_top", None) or {}
            if "sign" in player:          # 兼容旧格式
                top_keys.setdefault("sign", player.pop("sign"))

            data = {"player": player, "heroes": {}, "items": {}, "equips": {},
                    "fate": {}, "levels": {}, "level_records": {}}
            data.update(top_keys)

            for h in (await s.execute(
                    select(Hero).where(Hero.player_id == player_id))).scalars():
                data["heroes"][str(h.id)] = _merge(
                    h.id, {c: getattr(h, c) for c in HERO_COLS},
                    dict(h.extra_json or {}))

            for it in (await s.execute(
                    select(ItemStack).where(ItemStack.player_id == player_id))).scalars():
                data["items"][str(it.id)] = it.count

            for e in (await s.execute(
                    select(Equip).where(Equip.player_id == player_id))).scalars():
                data["equips"][str(e.id)] = _merge(
                    e.id, {c: getattr(e, c) for c in EQUIP_COLS},
                    dict(e.extra_json or {}))

            for fr in (await s.execute(
                    select(FateRecord).where(FateRecord.player_id == player_id))).scalars():
                data["fate"][str(fr.fate_id)] = dict(fr.groups_json or {})

            for lr in (await s.execute(
                    select(LevelRecord).where(LevelRecord.player_id == player_id))).scalars():
                rec = {"star": lr.star, "leftTimes": lr.left_times,
                       "buyTimes": lr.buy_times}
                bucket = data["levels"] if lr.source == "levels" else data["level_records"]
                bucket[str(lr.level_id)] = rec
            return data

    # ---- 写 ----
    async def save(self, player_id: int, data: dict, dirty: dict | None = None):
        """快照 diff 后单事务落库。data 为调用方的深拷贝（安全）。

        dirty: {"heroes": {id,...}, "equips": {id,...}} —— 调用方先算出变化 id，
        **只查/只写这些行**（512 武将时全表 SELECT+diff 占落库耗时 34.7ms → 只查变更行后 ~5ms）。
        None = 该集合按全量处理（首次落库 / 未提供 diff）。
        空集合 = 没有变化，直接跳过。
        """
        async with self.db.sessionmaker() as s:
            async with s.begin():
                # ★ 顶层业务键自动收进 player.extra_json["_top"]（全量兜底，
                #   新增顶层键无需登记；与 player 自身业务键用命名空间隔离）
                _TABLE_KEYS = {"player", "heroes", "items", "equips",
                               "fate", "levels", "level_records"}
                player = dict(data.get("player") or {})
                top = {k: v for k, v in data.items() if k not in _TABLE_KEYS}
                if top:
                    player["_top"] = top
                await self._save_player(s, player_id, player)
                _d = dirty or {}
                await self._save_collection(
                    s, player_id, data.get("heroes", {}), Hero,
                    HERO_COLS, HERO_DEFAULTS, "id", dirty_ids=_d.get("heroes"))
                await self._save_collection(
                    s, player_id, data.get("equips", {}), Equip,
                    EQUIP_COLS, EQUIP_DEFAULTS, "id", dirty_ids=_d.get("equips"))
                await self._save_items(s, player_id, data.get("items", {}))
                await self._save_fate(s, player_id, data.get("fate", {}))
                await self._save_levels(s, player_id, data)

    async def _save_player(self, s, player_id: int, p: dict):
        columns, extra = _split(p, PLAYER_COLS, PLAYER_DEFAULTS)
        row = (await s.execute(
            select(Player).where(Player.id == player_id))).scalar_one_or_none()
        if row is None:
            s.add(Player(id=player_id, **columns, extra_json=extra))
        else:
            for k, v in columns.items():
                setattr(row, k, v)
            row.extra_json = extra

    async def _save_collection(self, s, player_id, coll: dict, model,
                               cols, defaults, key_field, dirty_ids=None):
        """按 key_field（实例 id）diff upsert。

        dirty_ids=None → 全量（SELECT 整表，逐行 diff）；
        dirty_ids=集合 → 只 SELECT/遍历这些 id（空集直接返回）。
        """
        only = None
        if dirty_ids is not None:
            only = set()
            for _k in dirty_ids:
                try:
                    only.add(int(_k))
                except (TypeError, ValueError):
                    continue
            if not only:
                return
        q = select(model).where(model.player_id == player_id)
        if only is not None:
            q = q.where(model.id.in_(list(only)))
        existing = {r.id: r for r in (await s.execute(q)).scalars()}
        seen = set()
        for k, entry in (coll or {}).items():
            try:
                rid = int(k)
            except (TypeError, ValueError):
                continue
            if only is not None and rid not in only:
                continue
            seen.add(rid)
            columns, extra = _split(entry, cols, defaults)
            row = existing.get(rid)
            if row is None:
                s.add(model(id=rid, player_id=player_id, **columns,
                            extra_json=extra))
            else:
                for kk, vv in columns.items():
                    setattr(row, kk, vv)
                row.extra_json = extra
        for rid, row in existing.items():
            if rid not in seen:
                await s.delete(row)

    async def _save_items(self, s, player_id, items: dict):
        existing = {r.id: r for r in (await s.execute(
            select(ItemStack).where(ItemStack.player_id == player_id))).scalars()}
        seen = set()
        for k, cnt in (items or {}).items():
            try:
                iid = int(k)
            except (TypeError, ValueError):
                continue
            seen.add(iid)
            cnt = int(cnt or 0)
            row = existing.get(iid)
            if row is None:
                s.add(ItemStack(id=iid, player_id=player_id, count=cnt))
            else:
                row.count = cnt
        for iid, row in existing.items():
            if iid not in seen:
                await s.delete(row)

    async def _save_fate(self, s, player_id, fate: dict):
        existing = {r.fate_id: r for r in (await s.execute(
            select(FateRecord).where(FateRecord.player_id == player_id))).scalars()}
        seen = set()
        for k, rec in (fate or {}).items():
            try:
                fid = int(k)
            except (TypeError, ValueError):
                continue
            seen.add(fid)
            groups = dict(rec or {})
            row = existing.get(fid)
            if row is None:
                s.add(FateRecord(player_id=player_id, fate_id=fid,
                                 groups_json=groups))
            else:
                row.groups_json = groups
        for fid, row in existing.items():
            if fid not in seen:
                await s.delete(row)

    async def _save_levels(self, s, player_id, data: dict):
        """levels 与 level_records 分别以 source 落库（互不覆盖）。"""
        for source, key in (("levels", "levels"), ("level_records", "level_records")):
            coll = data.get(key, {}) or {}
            existing = {r.level_id: r for r in (await s.execute(
                select(LevelRecord).where(
                    LevelRecord.player_id == player_id,
                    LevelRecord.source == source))).scalars()}
            seen = set()
            for k, rec in coll.items():
                try:
                    lid = int(k)
                except (TypeError, ValueError):
                    continue
                seen.add(lid)
                star = int((rec or {}).get("star", 0) or 0)
                lt = int((rec or {}).get("leftTimes", 0) or 0)
                bt = int((rec or {}).get("buyTimes", 0) or 0)
                row = existing.get(lid)
                if row is None:
                    s.add(LevelRecord(player_id=player_id, source=source,
                                      level_id=lid, star=star,
                                      left_times=lt, buy_times=bt))
                else:
                    row.star, row.left_times, row.buy_times = star, lt, bt
            for lid, row in existing.items():
                if lid not in seen:
                    await s.delete(row)

    # ---- JSON 迁移 / 导出 ----
    async def import_json(self, player_id: int, json_path: str) -> dict:
        """一次性迁移：读 JSON -> 落库 -> 返回 dict。"""
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        await self.save(player_id, data)
        return data

    # ---- 账号 / 多角色（2026-10-29 账号隔离） ----
    async def accounts_all(self) -> dict:
        """全部账号：{username: {"id":, "hash":, "last_login":}}（启动时载入内存注册表）。"""
        async with self.db.sessionmaker() as s:
            rows = (await s.execute(select(Account))).scalars()
            return {r.username: {"id": r.id, "hash": r.password_hash or "",
                                 "last_login": r.last_login} for r in rows}

    async def account_add(self, username: str, password_hash: str) -> int:
        """新建账号，返回 id。"""
        async with self.db.sessionmaker() as s:
            async with s.begin():
                row = Account(username=username, password_hash=password_hash)
                s.add(row)
                await s.flush()
                return row.id

    async def account_set_hash(self, account_id: int, password_hash: str):
        async with self.db.sessionmaker() as s:
            async with s.begin():
                row = (await s.execute(
                    select(Account).where(Account.id == account_id))).scalar_one_or_none()
                if row is not None:
                    row.password_hash = password_hash

    async def account_touch(self, account_id: int):
        from datetime import datetime as _dt
        async with self.db.sessionmaker() as s:
            async with s.begin():
                row = (await s.execute(
                    select(Account).where(Account.id == account_id))).scalar_one_or_none()
                if row is not None:
                    row.last_login = _dt.utcnow()

    async def account_delete(self, account_id: int) -> int:
        """注销账号：删账号 + 名下全部角色/存档（单事务）。返回删除的角色数。"""
        async with self.db.sessionmaker() as s:
            async with s.begin():
                pids = list((await s.execute(
                    select(Player.id).where(Player.account_id == account_id))).scalars())
                if pids:
                    await s.execute(delete(Hero).where(Hero.player_id.in_(pids)))
                    await s.execute(delete(ItemStack).where(ItemStack.player_id.in_(pids)))
                    await s.execute(delete(Equip).where(Equip.player_id.in_(pids)))
                    await s.execute(delete(FateRecord).where(FateRecord.player_id.in_(pids)))
                    await s.execute(delete(LevelRecord).where(LevelRecord.player_id.in_(pids)))
                    await s.execute(delete(Player).where(Player.id.in_(pids)))
                await s.execute(delete(Account).where(Account.id == account_id))
                return len(pids)

    async def players_brief_by_account(self, account_id: int) -> list[dict]:
        """某账号的角色列表（LoginCharacterData 所需的权威列，不含战力——战力要算英雄，另行 load）。"""
        async with self.db.sessionmaker() as s:
            rows = (await s.execute(
                select(Player).where(Player.account_id == account_id)
                .order_by(Player.id))).scalars()
            return [{"id": r.id, "name": r.name, "lv": r.lv, "vip": r.vip}
                    for r in rows]

    async def players_all(self) -> list[dict]:
        """全部角色（发信台选收件人 / 全服发放用）。"""
        async with self.db.sessionmaker() as s:
            rows = (await s.execute(select(Player).order_by(Player.id))).scalars()
            return [{"id": r.id, "account_id": r.account_id, "name": r.name,
                     "lv": r.lv, "vip": r.vip} for r in rows]

    async def player_bind_account(self, player_id: int, account_id: int):
        """把角色归属到账号（建号 / 旧档迁移用）。幂等。"""
        async with self.db.sessionmaker() as s:
            async with s.begin():
                row = (await s.execute(
                    select(Player).where(Player.id == player_id))).scalar_one_or_none()
                if row is not None:
                    row.account_id = account_id

    async def player_account_id(self, player_id: int) -> int | None:
        """角色归属的账号 id（SelectCharacter 归属校验用）。无此角色返回 None。"""
        async with self.db.sessionmaker() as s:
            row = (await s.execute(
                select(Player.account_id).where(Player.id == player_id))).scalar_one_or_none()
            return row

    async def next_player_id(self, start: int = 2001) -> int:
        """分配新角色 id：取现有最大 id 与 start 的较大者 +1（1001 段留给旧档/测试）。"""
        async with self.db.sessionmaker() as s:
            row = (await s.execute(
                select(Player.id).order_by(Player.id.desc()).limit(1))).scalar_one_or_none()
        base = max(int(row or 0), start - 1)
        return base + 1

    async def export_json(self, player_id: int, json_path: str):
        data = await self.load(player_id)
        if data is None:
            raise RuntimeError("player %s 不在库中" % player_id)
        os.makedirs(os.path.dirname(json_path) or ".", exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data
