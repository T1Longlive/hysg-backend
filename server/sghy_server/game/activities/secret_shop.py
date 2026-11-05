"""神秘商店（MysteryStore / SecretShop）—— 原版多轮制对齐版（2026-10-29）。

原版机制（客户端 RuleMysteryCell + MysteryStore:update + proto 实证）：
  · **一天内多个时间轮次**：proto timeRegion 下发 "HH:MM" 字符串表，
    客户端规则弹窗渲染"每轮刷新时间表"（开启轮次/开启时间/结束时间）；
  · 每轮服务端下发一批商品，**轮内每件限购一次**（status 0→1，售空章）；
  · 轮间空档：props 为空 + nextTime=下轮开启秒数（客户端显示商人图 +
    "下轮开启倒计时"）；轮内 nextTime=本轮关闭倒计时；
  · 5314 BuySecretProp(id=props[i].index) → 空回 + 5317 整包推送。

配置（activities.json secret_shop）：
  rounds:   [{"start":"HH:MM","end":"HH:MM","slots":8,"pool"?:[...]}, ...]
            start<end（不跨零点）；pool 缺省用全局 pool；slots 缺省全量；
  pool:     全局商品池 [{type,count,base_price,group}]
  discounts 折扣档（默认 3~8 折）；free_slots 前 N 格免费（默认 0）。
每轮商品 = 以 round_key("YYYYMMDD#N") 为种子的**确定性随机**：
  全服所有玩家同一轮看到的商品一致、进程重启不变、重复读取不变。
购买状态按轮次存档（activity.secret_shop{round_key, items[]}），
轮次切换自动重建 → 天然实现"每轮限购一次"。
"""

import random
import time

from ...proto import ps_protobuf_pb2 as pb
from . import common


def _cfg():
    return common.activity_cfg("secret_shop")


# ---------------------------------------------------------------- 轮次解析

def _hhmm_to_min(s):
    """'HH:MM' → 当天分钟数；非法返回 None。"""
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return None


def _min_to_hhmm(m):
    return "%02d:%02d" % (m // 60, m % 60)


def _rounds(cfg):
    """解析 rounds 配置 → [{idx,s,e,rcfg}]（按开始时间排序）。"""
    out = []
    for i, r in enumerate(cfg.get("rounds") or []):
        s, e = _hhmm_to_min(r.get("start")), _hhmm_to_min(r.get("end"))
        if s is None or e is None or s >= e:
            continue                      # 非法/跨零点轮次直接忽略
        out.append({"idx": i, "s": s, "e": e, "rcfg": r})
    out.sort(key=lambda r: r["s"])
    return out


def _day0(now):
    t = time.localtime(now)
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0,
                            t.tm_wday, t.tm_yday, t.tm_isdst)))


def _locate(cfg, now):
    """当前时刻 → {"phase":"in"/"gap", key, end_ts/next_ts, round}。

    in  : round_key = "YYYYMMDD#<当日第几个轮次>"（按开始时间排序后的序号）
    gap : key = "gap"，next_ts = 下一个轮次开始时间（今天更晚 / 明天最早）
    """
    rs = _rounds(cfg)
    if not rs:
        return None
    t = time.localtime(now)
    m = t.tm_hour * 60 + t.tm_min
    day0 = _day0(now)
    day_tag = time.strftime("%Y%m%d", t)
    for seq, r in enumerate(rs):
        if r["s"] <= m < r["e"]:
            return {"phase": "in", "round": r,
                    "key": "%s#%d" % (day_tag, seq),
                    "end_ts": day0 + r["e"] * 60}
    for r in rs:
        if r["s"] > m:
            return {"phase": "gap", "key": "gap",
                    "next_ts": day0 + r["s"] * 60}
    return {"phase": "gap", "key": "gap",
            "next_ts": day0 + 86400 + rs[0]["s"] * 60}


# ---------------------------------------------------------------- 商品生成

def _gen_items(cfg, r, round_key):
    """按轮次生成商品（确定性随机，全服一致）。"""
    pool = r["rcfg"].get("pool") or cfg.get("pool") or []
    if not pool:
        return []
    slots = int(r["rcfg"].get("slots") or 0)
    rng = random.Random("secret_shop|%s" % round_key)
    if slots <= 0 or slots >= len(pool):
        picks = list(pool)
    else:
        picks = rng.sample(pool, slots)
    free_n = int(cfg.get("free_slots") or 0)
    discounts = cfg.get("discounts") or [3, 4, 5, 6, 7, 8]
    items = []
    for i, item in enumerate(picks):
        base = int(item.get("base_price") or 100)
        if i < free_n:
            price = 0                                   # 免费格
        else:
            d = int(rng.choice(discounts))
            price = max(1, base * d // 10)              # 3~8 折
        items.append({
            "type": int(item.get("type") or 0),
            "count": int(item.get("count") or 1),
            "price": price, "status": 0, "index": i + 1,
            "showGroupName": item.get("group") or "特惠",
            "originalPrice": base,
        })
    return items


def _st():
    return common.act_state().setdefault("secret_shop", {"round_key": "", "items": []})


def _ensure_state(cfg, loc):
    """轮次不变复用现有商品（保留已售状态）；换轮重建（每轮限购一次的根基）。"""
    st = _st()
    if st.get("round_key") != loc["key"] or not st.get("items"):
        st["round_key"] = loc["key"]
        st["items"] = _gen_items(cfg, loc["round"], loc["key"])
    return st


# ---------------------------------------------------------------- 对外接口

def snapshot(now=None):
    """SecretShopActivityData（5311 字段 / 5317 整包）。

    轮内：props=本轮商品 + nextTime=本轮关闭倒计时；
    空档：props 空   + nextTime=下轮开启倒计时（客户端显示商人图）。
    timeRegion 恒填全轮次表（规则弹窗"每轮刷新时间表"）。
    """
    now = int(now if now is not None else time.time())
    cfg = _cfg()
    m = pb.SecretShopActivityData()
    if not cfg:
        m.isStart = -1
        return m
    for r in _rounds(cfg):
        tr = m.timeRegion.add()
        tr.startTime = _min_to_hhmm(r["s"])
        tr.endTime = _min_to_hhmm(r["e"])
    _start, _end, is_open = common.time_window(cfg)
    if not is_open:
        m.isStart = -1
        return m
    loc = _locate(cfg, now)
    if loc is None:
        m.isStart = -1
        return m
    m.isStart = 1
    m.activityTime.startTime = _day0(now)               # 活动时间=当天（天级）
    m.activityTime.endTime = _day0(now) + 86400
    m.nextTime = max(0, int(loc["end_ts" if loc["phase"] == "in" else "next_ts"]) - now)
    if loc["phase"] == "in":
        st = _ensure_state(cfg, loc)
        for p in (st.get("items") or []):
            d = m.record.props.add()
            d.type = int(p["type"])
            d.count = int(p["count"])
            d.price = int(p["price"])
            d.status = int(p["status"])
            d.index = int(p["index"])
            d.showGroupName = p.get("showGroupName") or ""
            d.originalPrice = int(p.get("originalPrice") or 0)
    # 空档：props 留空 → 客户端走"下轮开启倒计时"分支
    return m


def buy(index, now=None):
    """购买（5314，id=商品 index，仅限当前轮）。返回 (ok, why, awards, prop)。"""
    cfg = _cfg()
    if not cfg:
        return False, "活动未开启", [], None
    now = int(now if now is not None else time.time())
    loc = _locate(cfg, now)
    if loc is None:
        return False, "未配置轮次", [], None
    if loc["phase"] != "in":
        return False, "商人已离开，请等下一轮开启", [], None
    st = _ensure_state(cfg, loc)
    hit = next((p for p in (st.get("items") or [])
                if int(p["index"]) == int(index)), None)
    if hit is None:
        return False, "商品不存在", [], None
    if int(hit["status"]) == 1:
        return False, "该商品本轮已售空（每轮限购一次）", [], None
    price = int(hit["price"])
    from .. import core
    p = core.PLAYER_DATA.setdefault("player", {})
    if price > 0:
        if int(p.get("diamond") or 0) < price:
            return False, "元宝不足（需 %d）" % price, [], None
        p["diamond"] = int(p.get("diamond") or 0) - price
    hit["status"] = 1
    award = {"kind": "object", "type": int(hit["type"]), "count": int(hit["count"])}
    return True, "购买成功", [award], hit


# ---------------------------------------------------------------- 轮次巡检

_LAST_SIG = None


def tick():
    """轮次巡检（_game_ticker 每 10s 调一次）：进入新一轮 / 进入空档 → True。

    调用方收到 True 后应广播 5317 整包（客户端有现成 handler，
    面板开着时倒计时走完立刻换商品/显示商人图，与原版行为一致）。
    进程首调只记录基线不触发。
    """
    global _LAST_SIG
    cfg = _cfg()
    if not cfg:
        return False
    loc = _locate(cfg, time.time())
    sig = loc["key"] if loc else None
    if _LAST_SIG is None:
        _LAST_SIG = sig
        return False
    if sig != _LAST_SIG:
        _LAST_SIG = sig
        return True
    return False
