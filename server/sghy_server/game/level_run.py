"""关卡运行：扫荡、每日次数、重置、概率掉落、队伍经验（2026-10-29）。

客户端事实（MopUpPanel / LevelDetailPanel / LevelManager / AwardContainer 实读）：

  扫荡请求   `socket:send("IDCountMessage", ReapLevel=5032, {id=关卡, count=N})`
  扫荡回包   `socket:decode("LevelResults", msg)`：
               results[]        repeated LevelResult（**AwardContainer 只读 results[1]**
                                               的 props/coin；次数由面板自己显示）
               extraProps       repeated AwardSpecData（客户端不读）
               characterChange  ExpChange（队伍经验条：oldLv/oldExp/newLv/newExp）
  重置次数   `socket:send("IDMessage", BuyLevelTimes=6004, {id=关卡})`
             回包 `IDCountMessage{id=关卡, count=新的 leftTimes}`
  频率显示   关卡详情 `leftTimes/maxTimes`，maxTimes = **LevelConfig.MaxTimes**
             （普通关=1 / 困难 BOSS 关=10），leftTimes 由 520 NewLevelRecord 下发。
  体力      每关消耗 `LevelConfig.Ep`（普通 6）；扫荡同样每次扣、每次 1 个扫荡令牌
             （道具 20004）。客户端进关前自检体力，扫荡前自检体力+令牌+leftTimes。
  开启条件   客户端按钮：`getDifficultyNums() <= getMaxGettedStarOfOneLevel()` 且
             面板里 `isFullStar` 才真正发请求 —— 即**该关满星（3 星）通关**。
  重置门槛   VIP>0 且 `buyTimes < VIPSetConfig[vip].BuyLevelTimes`，
             价格 `ExpSetConfig[buyTimes+1].ResetLevelDollar`（元宝 50/100/200/200...）。
             客户端点重置会先弹「需要月卡」的提示（原版付费设计），服务端不拦月卡。

掉落：LevelConfig 只有 `ShowAward`（展示用的 type 列表，混着武将/道具/装备），
**没有概率列**。概率放在 `sghy_server/config/level_drop.json`（可调），
命中后按 type 归属走 items / 装备实例 / 武将实例。
"""

import json
import os
import random
import time

from . import game_config, growth, economy, month_card

SIDE_TOKEN_ITEM = 20004          # 扫荡令牌
LEVEL_COIN = 5000                # 每次通关铜币（与 LevelEnd 保持一致）
# 队伍（主公）等级上限：设计值 60。ExpSetConfig 其实配到了 80（TeamExp 81+ 为空），
# 之前用 `max(rows)` 当上限直接放到 160，玩家被扫荡经验顶到了 61 —— 收敛到设计值。
TEAM_LV_CAP = 60
_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "level_drop.json")
_CFG_CACHE = {"mtime": None, "data": None}

DEFAULT_DROP_CFG = {
    "enabled": True,
    "rates": {"normal": 0.18, "hard": 0.32, "boss": 0.50},
    "hero_weight": 0.15,                 # 武将在候选池里的相对权重（压低）
    "item_count": {"normal": [1, 2], "hard": [1, 3], "boss": [2, 4]},
}


# ---------------------------------------------------------------- 基础数据

def _data():
    from . import core
    return core.PLAYER_DATA


def _records():
    from . import core
    return core.LEVEL_RECORDS


def _today():
    return time.strftime("%Y-%m-%d")


def level_row(lid):
    rows = game_config.load_level_config() or {}
    return rows.get(int(lid)) or rows.get(str(lid)) or {}


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def ep_cost(lid):
    return _int(level_row(lid).get("Ep"), 0)


def max_times(lid):
    return _int(level_row(lid).get("MaxTimes"), 0)


def daily_limit(lid):
    """该关每日可扫荡次数 = **LevelConfig.MaxTimes**（2026-10-29 核对过分布）：

        困难关（Difficulty=3，非章节末关）…… 10 次   ×113
        困难关（Difficulty=3，章节最后一关）… 5 次    ×29
        普通/精英关（Difficulty=1/2）……… 1 次（等于不能刷）
        MaxTimes=0 ……………………… 不可扫荡（活动/剧情关）×23

    即用户说的「章节最后一关 5 次，其它 10 次」在配置里**本来就写着**，
    客户端关卡详情显示的 `leftTimes/maxTimes` 分母也是这一列 —— 直接用它，
    两侧同源，不会出现 "10/1" 这种显示。
    要整体改成别的数值：config/level_drop.json 的 `sweep_daily_limit`（>0 时覆盖）。
    """
    mt = max_times(lid)
    if mt <= 0:
        return 0
    try:
        ov = int((drop_cfg() or {}).get("sweep_daily_limit") or 0)
    except (TypeError, ValueError):
        ov = 0
    return ov if ov > 0 else mt


def token_count(data=None):
    inv = (data or _data()).get("items") or {}
    return _int(inv.get(str(SIDE_TOKEN_ITEM)), 0)


def drop_cfg():
    """读 config/level_drop.json（带 mtime 缓存）；缺失或坏了用默认值。"""
    try:
        mt = os.path.getmtime(_CFG_PATH)
    except OSError:
        return dict(DEFAULT_DROP_CFG)
    if _CFG_CACHE["mtime"] == mt and _CFG_CACHE["data"] is not None:
        return _CFG_CACHE["data"]
    try:
        with open(_CFG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
    except Exception as e:
        print("[level_run] level_drop.json 读取失败（%r），用默认配置" % e, flush=True)
        data = dict(DEFAULT_DROP_CFG)
    _CFG_CACHE["mtime"] = mt
    _CFG_CACHE["data"] = data
    return data


# ---------------------------------------------------------------- 每日次数

def daily_state(data=None):
    """每日扫荡状态：{day, levels:{lid:{left,buy}}}；跨天整体重置。"""
    d = data or _data()
    st = d.setdefault("level_daily", {})
    today = _today()
    if st.get("day") != today:
        st["day"] = today
        st["levels"] = {}
    st.setdefault("levels", {})
    return st


def level_state(lid, data=None):
    """某关当日状态（惰性初始化 left=MaxTimes/buy=0）。

    ★ 2026-10-29 修「关卡详情显示 10/5」：存量存档里可能存着旧上限（改规则前
      按 10 写进去的），读的时候必须按**当前** daily_limit 钳一次，
      否则 leftTimes 永远大于 maxTimes，客户端就显示成 10/5。
    """
    lid = int(lid)
    st = daily_state(data)
    rec = st["levels"].get(str(lid))
    lim = daily_limit(lid)
    if rec is None:
        rec = {"left": lim, "buy": 0}
        st["levels"][str(lid)] = rec
    rec["left"] = _int(rec.get("left"))
    rec["buy"] = _int(rec.get("buy"))
    if lim > 0 and rec["left"] > lim:
        rec["left"] = lim
    return rec


def left_times(lid, data=None):
    return level_state(lid, data)["left"]


def buy_times(lid, data=None):
    return level_state(lid, data)["buy"]


def set_left(lid, value, data=None):
    rec = level_state(lid, data)
    rec["left"] = max(0, _int(value))
    _sync_record(lid, rec, data)
    return rec["left"]


def _sync_record(lid, rec, data=None):
    """把 left/buy 写回 LEVEL_RECORDS（520 / EnterGame 从这里读）。

    ★ 键类型必须与已有条目一致：int/str 混用会让 repo 把同一关写成两行，
      撞 level_records 唯一索引 (player_id, source, level_id) → **全部落库失败**。
    """
    try:
        recs = _records() if data is None else (data.get("levels") or {})
    except Exception:
        return
    key = next((k for k in (str(lid), int(lid)) if k in recs), str(lid))
    old = recs.get(key) or {}
    recs[key] = {
        "star": _int(old.get("star"), 0),
        "leftTimes": _int(rec.get("left")),
        "buyTimes": _int(rec.get("buy")),
    }


def star_of(lid):
    cur = _records().get(str(lid)) or _records().get(int(lid)) or {}
    return _int(cur.get("star"), 0)


def can_sweep(lid):
    """扫荡开关：该关满星（3 星）且 MaxTimes>0。返回 (ok, reason)。"""
    mt = max_times(lid)
    if mt <= 0:
        return False, "该关卡不支持扫荡"
    star = star_of(lid)
    if star < 3:
        return False, "该关卡获得星级不足，不能扫荡"
    return True, ""


# ---------------------------------------------------------------- 队伍经验

def add_team_exp(amount, data=None):
    """加队伍经验，可能连升级。返回 (old_lv, old_exp, new_lv, new_exp)。

    ★ 上限 = TEAM_LV_CAP(60)，不能拿 ExpSetConfig 的最大行号当上限：
      表配到 160 行，但 81+ 行 TeamExp 为空、61~80 是策划没启用的余量，
      按最大行号算会让扫荡经验把玩家顶过 60（2026-10-29 用户实测到 61）。
    """
    d = data or _data()
    p = d.setdefault("player", {})
    old_lv = _int(p.get("lv"), 1)
    old_exp = _int(p.get("exp"), 0)
    exp = old_exp + max(0, _int(amount))
    lv = old_lv
    rows = game_config.load_exp_set_config() or {}
    cap_lv = min(TEAM_LV_CAP, max((k for k, v in rows.items()
                                   if _int(v.get("TeamExp")) > 0), default=TEAM_LV_CAP))
    while lv < cap_lv:
        need = _int((rows.get(lv) or {}).get("TeamExp"), 0)
        if need <= 0 or exp < need:
            break
        exp -= need
        lv += 1
        economy.on_team_level_up(lv, d)
    if lv >= cap_lv:
        # 满级后经验不再累积，钳在"距下一级差 1"以内，避免进度条溢出
        need = _int((rows.get(cap_lv) or {}).get("TeamExp"), 0)
        if need > 0:
            exp = min(exp, need - 1)
    p["lv"] = lv
    p["exp"] = exp
    return old_lv, old_exp, lv, exp


# ---------------------------------------------------------------- 掉落

def _bucket(lid):
    row = level_row(lid)
    is_boss = _int(row.get("IsBoss"))
    if _int(row.get("IsFinalLevel")) == 1 or is_boss == 1:
        return "boss"
    if is_boss >= 2 or _int(row.get("Difficulty")) >= 2:
        return "hard"
    return "normal"


def _roll_pool(lid):
    """候选池：LevelConfig.ShowAward 的 type 列表（按配置归属分类）。

    分类结果：
      item        道具（ItemConfig）
      equip       装备（EquipConfig，可拥有）
      hero        具体武将（HeroConfig 里**真实**武将）
      hero_random 占位武将 —— HeroConfig 里 Group<0 的行，名字就是「随机武将」
                  （如 19001）。它不是能入档的武将，掉落时要**解析成真实武将**，
                  否则会把占位 type 直接建成武将实例，污染存档。
    """
    pool = []
    for a in (level_row(lid).get("ShowAward") or []):
        if not isinstance(a, dict):
            continue
        t = _int(a.get("Type"))
        if t <= 0:
            continue
        kind = None
        if game_config.item_exists(t):
            kind = "item"
        else:
            try:
                if growth.is_equip_ownable(t):
                    kind = "equip"
                else:
                    row = growth.find_hero_row(t)
                    if row:
                        kind = "hero_random" if _is_placeholder_hero(row) else "hero"
            except Exception:
                kind = None
        if kind:
            pool.append((t, kind))
    return pool


def _is_placeholder_hero(row):
    """HeroConfig 里的占位行：不能直接建实例，也不能进掉落池。

    判定口径（2026-11-02 补第 2、3 类）：
      · Group < 0            —— 中文占位行，如 19001「随机武将」
      · Name 为 dummy / 空   —— 19801~19805，**Group=298xx 是正数**，旧逻辑漏判
      · maxStarType <= 0     —— 同上，客户端 WarriorData:type 会用它查
                                HeroConfig[0]，查不到 → 兜底空表 → 崩在 SkillA

    事故：2026-11-02 关卡 1638 掉落把 dummy(19801) 建成武将实例写进存档，
    客户端结算时 `getConfigById(HeroConfig, 0)` → 空表 → `maxQualityCfg.SkillA`
    索引 nil → LUA ERROR，结算面板永不弹出。
    """
    if not row:
        return True
    try:
        if int(str(row.get("maxStarType") or 0).strip('"') or 0) <= 0:
            return True
    except (TypeError, ValueError):
        pass
    name = str(row.get("Name") or "").strip().strip('"').strip().lower()
    if not name or name == "dummy":
        return True
    try:
        return int(str(row.get("Group") or 0).strip('"') or 0) < 0
    except (TypeError, ValueError):
        return False


_HERO_POOL_CACHE = {}


def _real_heroes(q_lo, q_hi):
    """真实武将池（按品质区间过滤），带缓存。"""
    key = (int(q_lo), int(q_hi))
    if key in _HERO_POOL_CACHE:
        return _HERO_POOL_CACHE[key]
    out = []
    for t, row in (game_config.load_hero_config() or {}).items():
        try:
            if _is_placeholder_hero(row):
                continue
            q = int(str(row.get("Quality") or 0).strip('"') or 0)
        except (TypeError, ValueError):
            continue
        if int(q_lo) <= q <= int(q_hi):
            out.append(int(t))
    out.sort()
    _HERO_POOL_CACHE[key] = out
    return out


def _resolve_random_hero(bucket):
    """把「随机武将」解析成一个真实武将 type（品质按关卡档位）。"""
    cfg = drop_cfg() or {}
    rng = (cfg.get("hero_random_quality") or {}).get(bucket, [1, 4])
    try:
        lo, hi = int(rng[0]), int(rng[1])
    except (TypeError, ValueError, IndexError):
        lo, hi = 1, 4
    pool = _real_heroes(lo, hi)
    return random.choice(pool) if pool else 0


def roll_drops(lid, times=1):
    """关卡掉落。返回 awards 列表（内部格式：{kind,type,count}）。

    2026-10-29 修订（用户反馈「扫荡结果页看不到掉落物品」）：
      · **每轮必得 1 件**：优先从道具/装备里抽；该关 ShowAward 只有武将时，
        抽「随机武将」并解析成真实武将（品质按 hero_random_quality 分档）。
      · **概率额外 1 件**：按 config/level_drop.json 的 rates（normal/hard/boss），
        这一槽允许出武将（权重压到 hero_weight）。
    单轮最多 2 件，多次扫荡后按 (kind,type) 合并。
    """
    cfg = drop_cfg()
    if not cfg.get("enabled", True):
        return []
    pool = _roll_pool(lid)
    if not pool:
        return []
    bucket = _bucket(lid)
    rate = float((cfg.get("rates") or {}).get(bucket, 0.2))
    hero_w = float(cfg.get("hero_weight", 0.15))
    rng = (cfg.get("item_count") or {}).get(bucket, [1, 2])
    lo, hi = (int(rng[0]), int(rng[1])) if isinstance(rng, list) and len(rng) >= 2 else (1, 2)

    non_hero = [(t, k) for t, k in pool if k in ("item", "equip")]
    hero_slot = [(t, k) for t, k in pool if k not in ("item", "equip")]

    def pick(p):
        weights = [hero_w if k in ("hero", "hero_random") else 1.0 for _, k in p]
        if sum(weights) <= 0:
            weights = [1.0] * len(p)
        return random.choices(p, weights=weights, k=1)[0]

    def resolve(t, kind):
        """占位武将 → 真实武将。返回 (kind, type) 或 None（解析不出来就跳过）。"""
        if kind != "hero_random":
            return kind, t
        real = _resolve_random_hero(bucket)
        return ("hero", real) if real else None

    out = {}
    for _ in range(max(1, int(times))):
        picks = []
        picks.append(pick(non_hero or hero_slot))     # 必得槽
        if random.random() <= rate:                   # 概率槽
            picks.append(pick(pool))
        for t, kind in picks:
            r = resolve(t, kind)
            if not r:
                continue
            kind, t = r
            cnt = random.randint(lo, hi) if kind == "item" else 1
            out[(kind, t)] = out.get((kind, t), 0) + cnt
    return [{"kind": k, "type": t, "count": c} for (k, t), c in out.items()]


# ---------------------------------------------------------------- 单次通关奖励

def run_rewards(lid, times=1, hero_exp=None):
    """构造 times 次通关的奖励（合并成一份，供 LevelResults.results[1] 显示）。

    返回 {coin, exp, props:[award...], drops:[award...]}
    """
    if hero_exp is None:
        hero_exp = growth.team_exp()
    drops = roll_drops(lid, times)
    return {
        "coin": LEVEL_COIN * max(1, int(times)),
        "exp": _int(hero_exp),
        "props": drops,
    }


# ---------------------------------------------------------------- 扫荡 / 重置

def do_sweep(lid, count):
    """执行扫荡。返回 (ok, reason, payload)。"""
    lid = int(lid)
    count = max(1, min(10, _int(count, 1)))
    row = level_row(lid)
    if not row:
        return False, "关卡不存在", None
    ok, reason = can_sweep(lid)
    if not ok:
        return False, reason, None
    # 客户端「扫荡10次」按 min(令牌, 体力, 剩余次数) 发数，这里再夹一次上限
    count = min(count, daily_limit(lid) or 1)

    left = left_times(lid)
    if left < count:
        return False, "今日可扫荡次数不足（剩 %d 次）" % left, None

    cost_ep = ep_cost(lid)
    need_ep = cost_ep * count
    ep = economy.sync()
    if ep < need_ep:
        return False, "体力不足（需要 %d，当前 %d）" % (need_ep, ep), None

    tokens = token_count()
    if tokens < count:
        return False, "扫荡令牌不足（需要 %d，当前 %d）" % (count, tokens), None

    # 扣资源（顺序：体力 → 令牌 → 次数）
    ok, reason = economy.spend(need_ep)
    if not ok:
        return False, reason, None
    _spend_tokens(count)
    set_left(lid, left - count)

    # ---- 成就计数：扫荡也算征战（击杀/BOSS），2026-10-29 ----
    try:
        from . import mission_achieve, game_config as _gc
        mission_achieve.bump_counter("kill", mission_achieve.level_enemy_count(lid) * count)
        if _int(row.get("IsBoss")) >= 2:      # 带【困难】【大师】难度的 BOSS 关
            mission_achieve.bump_counter("boss", count)
    except Exception as e:
        print("[level_run] 扫荡计数失败: %r" % e)

    hero_exp = growth.team_exp()
    rewards = run_rewards(lid, count, hero_exp)
    old_lv, old_exp, new_lv, new_exp = add_team_exp(100 * count)

    # 武将经验（每个上阵武将）
    exp_changes = []
    for h in sorted([h for h in _data().get("heroes", {}).values()
                     if _int(h.get("position")) > 0],
                    key=lambda x: _int(x.get("position")))[:5]:
        r = growth.find_hero_row(h.get("type"))
        if not r:
            continue
        q = _int(r.get("Quality"))
        cap = growth.level_cap(q, h.get("awaken", 0), h.get("can_break"))
        o_lv, o_exp = _int(h.get("lv"), 1), _int(h.get("exp"), 0)
        n_lv, n_exp, ups, wasted = growth.add_exp(
            q, o_lv, o_exp, hero_exp * count, cap, h.get("can_break"))
        h["lv"], h["exp"] = n_lv, n_exp
        exp_changes.append((h["id"], o_exp, o_lv, n_exp, n_lv,
                            0 if wasted >= hero_exp * count else hero_exp * count))
    exp_changes = [(i, oe, ol, ne, nl, ad) for (i, oe, ol, ne, nl, ad)
                   in exp_changes if nl != ol or ne != oe]

    return True, "", {
        "lid": lid,
        "count": count,
        "rewards": rewards,
        "exp_changes": exp_changes,
        "character_change": (old_lv, old_exp, new_lv, new_exp),
    }


def _spend_tokens(n):
    """扣扫荡令牌（道具 20004）。推送由调用方做 451（这里是纯数据层）。"""
    inv = _data().setdefault("items", {})
    cur = _int(inv.get(str(SIDE_TOKEN_ITEM)))
    inv[str(SIDE_TOKEN_ITEM)] = max(0, cur - int(n))
    return inv[str(SIDE_TOKEN_ITEM)]


def reset_cost(buy_times_):
    """第 buy_times_+1 次重置的价格（ExpSetConfig[buyTimes+1].ResetLevelDollar）。"""
    rows = game_config.load_exp_set_config() or {}
    row = rows.get(int(buy_times_) + 1) or {}
    return _int(row.get("ResetLevelDollar"), 200)


def reset_limit(vip_lv):
    rows = game_config.load_vip_config() or {}
    row = rows.get(int(vip_lv)) or {}
    return _int(row.get("BuyLevelTimes"), 0)


def do_buy_times(lid):
    """重置该关扫荡次数。返回 (ok, reason, left)。

    门槛顺序**照抄客户端 LevelManager:buyLevelTimes**（原版付费设计）：
      ① 该关满星（3 星）
      ② 有效期内月卡（ShopConfig 1016 月卡 / 1017 至尊月卡，endDate>0）
         —— 配置描述就写着「开启扫荡重置功能」，没月卡客户端直接弹购买
      ③ VIP>0 且 buyTimes < VIPSetConfig[vip].BuyLevelTimes
      ④ 元宝 ≥ ExpSetConfig[buyTimes+1].ResetLevelDollar
    """
    lid = int(lid)
    if not level_row(lid):
        return False, "关卡不存在", 0
    ok, reason = can_sweep(lid)
    if not ok:
        return False, reason, 0
    if not month_card.has_active():
        return False, "本功能需要购买月卡才能开启", left_times(lid)
    p = _data().setdefault("player", {})
    vip = _int(p.get("vip"), 0)
    limit = reset_limit(vip)
    if vip <= 0 or limit <= 0:
        return False, "VIP 等级不足，无法重置关卡次数", left_times(lid)
    rec = level_state(lid)
    if rec["buy"] >= limit:
        return False, "今日重置次数已用完（VIP%d 上限 %d 次）" % (vip, limit), rec["left"]
    cost = reset_cost(rec["buy"])
    if _int(p.get("diamond")) < cost:
        return False, "元宝不足（需要 %d）" % cost, rec["left"]
    p["diamond"] = _int(p.get("diamond")) - cost
    rec["buy"] += 1
    rec["left"] = daily_limit(lid)
    _sync_record(lid, rec)
    return True, "", rec["left"]
