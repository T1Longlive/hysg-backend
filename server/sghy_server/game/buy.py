"""元宝购买体力 / 铜钱（10051 BuyEp、10050 BuyCoin）。

客户端事实（UserManager.lua / PlayerInfoPanel.lua / User.lua 实读）：

  · 入口：主城点击玩家资源区 → PlayerInfoPanel，两个按钮分别发
    `socket:send(nil, Protocol.BuyEp)` → 10051、`Protocol.BuyCoin` → 10050（**无 body**）
  · 回包必须是**裸 CharacterData**：`ackBuyEp` / `ackBuyCoin` 都走
    `UserManager:onCharacterValue(msg)` 解析后再回调（包错会解成垃圾）。
  · 能否购买由 `CharacterData.actionTime`（字段 27）判定：
        buyEpTimes < VIPSetConfig[vipLv].BuyEpTimes  或 freeBuyEpTimes > 0
        buyCoinTimes < VIPSetConfig[vipLv].BuyCoinTimes 或 freeBuyCoinTimes > 0
    服务端之前**完全没下发 actionTime** → `me:buyEpTimes()` 是 nil，比较直接报错。
  · 价格：`ExpSetConfig[已买次数+1].BuyEp` / `.BuyCoin`（元宝，随次数递增）
  · 体力：`GameSetConfig.BuyEpOneTimes`（=50）
  · 铜钱：`ExpSetConfig[玩家等级].LevelBuyCoin`
  · 免费次数：通关每章 +1 免费体力；三星 BOSS 关 +1 免费铜钱（不占 VIP 次数）
  · 暴击：服务端推 10053 `CoinCritRateInfo{rate=倍率, coin=基础, rateCoin=暴击后}`，
    客户端 `onCoinBaoJiHandler` → `PlayerInfoPanel:coinBaoJiEffect(rateCoin, rate)` 播特效。

存档：`PLAYER_DATA["buy_power"] = {day, ep_times, coin_times, free_ep, free_coin}`
跨天（本地日期变化）自动重置四个计数。
"""

import time

BUY_KEY = "buy_power"

# 暴击：配置表里**没有**概率与倍率字段（VIP 描述只写"购买铜钱开放2倍暴击"）。
# 倍率取 2（与描述一致）；概率按 VIP 阶梯，⚠️ 待核实（原版服务端无代码可对照）。
COIN_CRIT_MULT = 2
COIN_CRIT_PROB = {0: 0.10, 1: 0.10, 2: 0.12, 3: 0.12, 4: 0.12,
                  5: 0.15, 6: 0.15, 7: 0.15, 8: 0.20, 9: 0.20,
                  10: 0.20, 11: 0.25, 12: 0.25, 13: 0.25, 14: 0.30, 15: 0.30}


def _data():
    from . import core
    return core.PLAYER_DATA


def _today():
    return time.strftime("%Y-%m-%d")


def state(data=None):
    """当日购买状态；跨天自动清零。"""
    d = data or _data()
    st = d.setdefault(BUY_KEY, {})
    today = _today()
    if st.get("day") != today:
        st["day"] = today
        st["ep_times"] = 0
        st["coin_times"] = 0
        st["free_ep"] = 0
        st["free_coin"] = 0
    for k in ("ep_times", "coin_times", "free_ep", "free_coin"):
        st[k] = int(st.get(k) or 0)
    return st


def snapshot(data=None):
    """给 CharacterData.actionTime 用的四个值。"""
    st = state(data)
    return {
        "buyEpTimes": st["ep_times"],
        "buyCoinTimes": st["coin_times"],
        "freeBuyEpTimes": st["free_ep"],
        "freeBuyCoinTimes": st["free_coin"],
    }


def add_free_ep(n=1, data=None):
    st = state(data)
    st["free_ep"] = int(st.get("free_ep") or 0) + int(n)
    return st["free_ep"]


def add_free_coin(n=1, data=None):
    st = state(data)
    st["free_coin"] = int(st.get("free_coin") or 0) + int(n)
    return st["free_coin"]


def _vip_limit(vip_lv, field):
    """VIP 购买次数上限。★ VIPSetConfig 的 key 是 ID(1..16)，VIP 等级在 `Lv` 字段里
    （VIP0 那条的 ID=1、Lv=0），所以必须按 Lv 匹配，不能直接拿 vip_lv 当 key。"""
    from . import growth
    growth._load_vip()
    want = int(vip_lv)
    row = {}
    # _load_vip 可能返回 list（lua_full 数组）或 dict（旧 JSON），两种都要认
    _cfg = growth._vip_cfg or {}
    for r in (_cfg.values() if isinstance(_cfg, dict) else _cfg):
        try:
            if int(float(r.get("Lv") or 0)) == want:
                row = r
                break
        except (TypeError, ValueError):
            continue
    try:
        return int(float(row.get(field) or 0))
    except (TypeError, ValueError):
        return 0


def _price(times, field):
    """第 times+1 次购买的价格（元宝）。"""
    from . import growth
    row = growth._row_at(int(times) + 1)
    if not row:
        return 0
    return int(growth._num(row.get(field)) or 0)


def _cost_and_gain(kind, data=None):
    """返回 (ok, reason, cost_diamond, is_free, times_before)。

    kind = "ep" | "coin"
    优先用免费次数；否则校验 VIP 次数上限，返回第 (times+1) 次的价格。
    """
    from . import core
    st = state(data)
    p = core.PLAYER_DATA.get("player", {})
    vip = int(p.get("vip") or 0)
    if kind == "ep":
        if st["free_ep"] > 0:
            return True, "", 0, True, st["ep_times"]
        limit = _vip_limit(vip, "BuyEpTimes")
        if st["ep_times"] >= limit:
            return False, "今日购买体力次数已用完（VIP%d 上限 %d 次），提升VIP可增加次数" % (vip, limit), 0, False, st["ep_times"]
        return True, "", _price(st["ep_times"], "BuyEp"), False, st["ep_times"]
    else:
        if st["free_coin"] > 0:
            return True, "", 0, True, st["coin_times"]
        limit = _vip_limit(vip, "BuyCoinTimes")
        if st["coin_times"] >= limit:
            return False, "今日购买铜钱次数已用完（VIP%d 上限 %d 次），提升VIP可增加次数" % (vip, limit), 0, False, st["coin_times"]
        return True, "", _price(st["coin_times"], "BuyCoin"), False, st["coin_times"]


def buy_ep(data=None, rng=None):
    """买体力。返回 (ok, reason, cost_diamond, gained_ep, is_free)。"""
    import random
    from . import core, game_config
    rng = rng or random
    st = state(data)
    p = core.PLAYER_DATA.setdefault("player", {})

    ok, reason, cost, is_free, _tb = _cost_and_gain("ep", data)
    if not ok:
        return False, reason, 0, 0, False
    if cost > int(p.get("diamond") or 0):
        return False, "元宝不足（需要 %d）" % cost, cost, 0, is_free

    gain = int(game_config.ensign_game_set("BuyEpOneTimes", 0) or 0)
    if gain <= 0:
        gain = 50     # GameSetConfig.BuyEpOneTimes 实测 50，兜底
    p["diamond"] = int(p.get("diamond") or 0) - cost
    p["ep"] = int(p.get("ep") or 0) + gain
    if is_free:
        st["free_ep"] = max(0, st["free_ep"] - 1)
    else:
        st["ep_times"] += 1
    return True, "", cost, gain, is_free


def buy_coin(data=None, rng=None):
    """买铜钱（含暴击）。返回 (ok, reason, cost_diamond, gained_coin, is_free, crit)。

    crit = None 或 {"rate": 倍率, "coin": 基础铜钱, "rateCoin": 暴击后铜钱}
    """
    import random
    from . import core, growth
    rng = rng or random
    st = state(data)
    p = core.PLAYER_DATA.setdefault("player", {})

    ok, reason, cost, is_free, _tb = _cost_and_gain("coin", data)
    if not ok:
        return False, reason, 0, 0, False, None
    if cost > int(p.get("diamond") or 0):
        return False, "元宝不足（需要 %d）" % cost, cost, 0, is_free, None

    lv = int(p.get("lv") or 1)
    row = growth._row_at(lv) or {}
    base = int(growth._num(row.get("LevelBuyCoin")) or 0)
    if base <= 0:
        base = 10000

    vip = int(p.get("vip") or 0)
    crit = None
    gained = base
    prob = COIN_CRIT_PROB.get(vip, 0.10)
    if rng.random() < prob:
        gained = base * COIN_CRIT_MULT
        crit = {"rate": COIN_CRIT_MULT, "coin": base, "rateCoin": gained}

    p["diamond"] = int(p.get("diamond") or 0) - cost
    p["coin"] = int(p.get("coin") or 0) + gained
    if is_free:
        st["free_coin"] = max(0, st["free_coin"] - 1)
    else:
        st["coin_times"] += 1
    return True, "", cost, gained, is_free, crit
