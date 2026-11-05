"""图鉴/收集配置。

客户端 CollectManager 的口径（已核对 env/_work/rt_all/app.model.CollectManager.lua）：
  · 武将图鉴：HeroConfig.ShowInCollect == 1 的条目；按 Origin（1~6，Awards 只配了 1~4）
    分组，SubType = Origin ID；进度 = 该 Origin 下已点亮的**不同 type 数**。
  · 装备图鉴：EquipConfig.ShowInCollect == 1 的条目；按 Part（1 武器/2 头盔/3 铠甲/4 宝物）
    分组，SubType = Part；进度 = 该部位下已点亮的不同 type 数。
  · 里程碑奖励：CollectionAwardConfig{Type=1武将/2装备, SubType, MilestoneCount, Awards}；
    Awards 两类：PropertyAward{PropertyName, Value} / ObjectAward{Type, Count}。

配置直接从 Lua 读（与 lottery_config 同一个解析器/缓存）。
"""
from . import lottery_config as _lc

_CACHE = {}


def collection_awards():
    """{id: {Type, SubType, MilestoneCount, Awards}}"""
    return _lc._load("CollectionAwardConfig")


def hero_collect_map():
    """{hero_type: {"origin": int, "quality": int, "profession": int}}（仅 ShowInCollect=1）"""
    if "hero" not in _CACHE:
        out = {}
        for hid, row in (_lc._load("HeroConfig") or {}).items():
            if _toi(row.get("ShowInCollect")) != 1:
                continue
            try:
                out[int(hid)] = {
                    "origin": _toi(row.get("Origin")),
                    "quality": _toi(row.get("Quality")),
                    "profession": _toi(row.get("Profession")),
                }
            except (TypeError, ValueError):
                continue
        _CACHE["hero"] = out
    return _CACHE["hero"]


def equip_collect_map():
    """{equip_type: {"part": int, "quality": int}}（仅 ShowInCollect=1）"""
    if "equip" not in _CACHE:
        out = {}
        for eid, row in (_lc._load("EquipConfig") or {}).items():
            if _toi(row.get("ShowInCollect")) != 1:
                continue
            try:
                out[int(eid)] = {
                    "part": _toi(row.get("Part")),
                    "quality": _toi(row.get("Quality")),
                }
            except (TypeError, ValueError):
                continue
        _CACHE["equip"] = out
    return _CACHE["equip"]


def _toi(v, d=0):
    try:
        if v is None or v == "":
            return d
        return int(float(v))
    except (TypeError, ValueError):
        return d


def award_rows(collection_type, sub_type):
    """[(milestone, awards_raw), ...] 按 milestone 升序。"""
    ctype, subtype = int(collection_type), int(sub_type)
    rows = []
    for row in (collection_awards() or {}).values():
        if _toi(row.get("Type")) != ctype or _toi(row.get("SubType")) != subtype:
            continue
        rows.append((_toi(row.get("MilestoneCount")), row.get("Awards") or []))
    rows.sort(key=lambda x: x[0])
    return rows


def award_row(collection_type, sub_type, milestone):
    """精确找一条配置；没有返回 None。"""
    ctype, subtype, ms = int(collection_type), int(sub_type), int(milestone)
    for row in (collection_awards() or {}).values():
        if (_toi(row.get("Type")) == ctype and _toi(row.get("SubType")) == subtype
                and _toi(row.get("MilestoneCount")) == ms):
            return row
    return None


def clear_cache():
    _CACHE.clear()
