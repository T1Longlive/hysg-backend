"""成就/收集类任务（MissionConfig 里 Cycle=0 且非主线的部分，2026-10-29）。

模型（用户确认）：任务列表 = **X 组并行**；每组内按 `Next` 顺序一个个解锁——
完成/领掉当前任务后才显示下一个；组与组之间互不干扰。

分组事实（MissionConfig 实测）：
  · 1099 条任务按 `Next` 恰好组成 **500 组**（0 条孤立）
  · 主线 = 3 条 target=7 的长链（含新旧两版，mission.py 已按 MainIndex 归一成 147 节点）
  · 成就链：万人斩(target=0)23、千军破(1)22、未逢敌手(2)22、获得武将(9)21、
    合成武将(11)22、升阶武将(16)21、大师之路(30)15、缘·妙不可言(37)19、缘起缘灭(41)29
  · 单任务组：星星收集狂(target=13)91 条（TargetID=章节、TargetValue=该章累计星数阈值，
    Next 全 0 → 每档自成一组）、等级礼包(target=-1，TriggerValue=等级阈值)
  · Cycle=7 的 326 条是「七日远征」（独立 SevenDayTask 界面），不进任务面板

本模块只接「进度能从现有存档直接推导」的 target（一期）：
  13  章节星星：TargetID=章节号，TargetValue=该章累计星数阈值
  -1  等级礼包：TriggerValue=等级阈值，主公等级达到即完成；未达等级**不下发**
  30  大师之路：上阵武将中 star>=TargetID 的个数 >= TargetValue
  32  升星之路：拥有 star>=TargetID 的圣兽（14001~14035）个数 >= TargetValue
其余 target（0/1/2/9/11/16/28/37/41/45/46/48/50、60xxx、Cycle=7）需要新的计数器
或独立系统，**本期不做** —— 对应的组不下发，任务列表里看不到（明确排除）。

协议：与主线共用 501/502/503/504/10100；文本/图标由客户端按 type 查
MissionConfig（Name/Description/Code），服务端只发 id/need/complete。
存档：`PLAYER_DATA["mission_achieve"] = {"claimed": {配置id: 1}}`
"""

import time

KEY = "mission_achieve"
SUPPORTED_TARGETS = (13, -1, 30, 32, 0, 1, 9, 11, 16)
# 计数器类 target → 存档键（PLAYER_DATA["mission_counters"]）
COUNTER_OF = {0: "kill", 1: "boss", 9: "hero_get", 11: "compose", 16: "grade"}
BEAST_MIN, BEAST_MAX = 14001, 14035
STAR_TARGET = 13           # 星星收集狂
STAR_CHAPTER_MIN = 1       # 章节号从 1 开始（tid=0 是七日任务，排除）


def _data():
    from . import core
    return core.PLAYER_DATA


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 配置

_ROWS = None


def _rows():
    """支持的成就任务行（Cycle=0、非主线、target 受支持），分三类：

    · star   —— target=13 星星收集狂：{章节号: [按阈值升序的行]}（tid=0 的七日任务行排除）
    · lvpack —— target=-1 等级礼包：**一条链**（按 TriggerValue 升序），
                只显示"阈值已达的第一个未领取档"，领掉永久消失、显示下一档。
                ★ 不能进 groups 并行显示：13 个礼包 next 全 0，按并行组算会
                  一屏挂满同款「X级礼包」，领掉一个剩下的看起来像"又出来了"
                  （2026-10-29 用户反馈）。
    · groups —— 其余（30/32）按 Next 链分组（单条 Next=0 自成一组）
    """
    global _ROWS
    if _ROWS is not None:
        return _ROWS
    from . import game_config
    cfg = game_config.load_mission_config() or {}
    star = {}
    lvpack = []
    other = {}
    for mid, row in cfg.items():
        if _int(row.get("cycle")) != 0 or _int(row.get("main_index")) != 0:
            continue
        t = _int(row.get("target"))
        if t == STAR_TARGET:
            ch = _int(row.get("target_id"))
            if ch >= STAR_CHAPTER_MIN:          # tid=0 是七日任务，排除
                star.setdefault(ch, []).append(row)
        elif t == -1:
            lvpack.append(row)
        elif t in SUPPORTED_TARGETS:
            other[_int(row["id"])] = row
    for ch in star:
        star[ch].sort(key=lambda r: _int(r.get("target_value")))
    lvpack.sort(key=lambda r: (_int(r.get("trigger_value")), _int(r["id"])))
    # Next 链分组（非星星类）：组头 = 不被任何 next 指向的 id，沿 next 正向走
    pointed = {_int(r.get("next")) for r in other.values() if _int(r.get("next"))}
    heads = [rid for rid in other if rid not in pointed]
    groups = []
    _seen = set()
    for h in sorted(heads):
        chain, cur = [h], h
        _seen.add(h)
        while True:
            nx = _int(other[cur].get("next"))
            if nx not in other or nx in _seen or len(chain) >= 300:
                break
            cur = nx
            chain.append(cur)
            _seen.add(cur)
        groups.append(chain)
    _ROWS = {"star": star, "lvpack": lvpack,
             "by_id": {**{r["id"]: r for rs in star.values() for r in rs},
                       **{r["id"]: r for r in lvpack},
                       **other},
             "groups": groups}
    return _ROWS


def _lvpack_current():
    """当前应显示的等级礼包：阈值已达的**第一个未领取档**（按阈值升序）。

    链上第一未领取档还没到等级 → 后面的也不显示（礼包按顺序解锁）。
    返回 None = 当前没有可显示的等级礼包。
    """
    lv = _int((_data().get("player") or {}).get("lv"))
    for r in _rows()["lvpack"]:
        if _claimed(r["id"]):
            continue
        if lv >= max(1, _int(r.get("trigger_value"))):
            return r
        return None                     # 第一未领档未达等级，后面更不用看
    return None                         # 全部领完 → 永久消失


def _star_tiers(chapter):
    return _rows()["star"].get(_int(chapter)) or []


def _claimed_count(chapter):
    """该章已领取的档数。"""
    return sum(1 for r in _star_tiers(chapter) if _claimed(r["id"]))


def _star_visible(row):
    """章 c 第 k 档可见 ⟺ c==1 或「前一章对应档（min(k, 前章末档)）已领取」。"""
    c = _int(row.get("target_id"))
    if c <= STAR_CHAPTER_MIN:
        return True
    tiers_prev = _star_tiers(c - 1)
    if not tiers_prev:
        return False
    k = next(i for i, r in enumerate(_star_tiers(c)) if r["id"] == row["id"])
    idx = min(k, len(tiers_prev) - 1)
    return _claimed(tiers_prev[idx]["id"]) == 1


def _state():
    st = _data().setdefault(KEY, {})
    raw = st.get("claimed")
    if not isinstance(raw, dict):
        raw = {}
    # ★ 键必须统一 str（2026-10-30 修复）：存档是 JSON，落库时 int 键会被转成
    #   字符串，加载回来就只剩 "10306"；而旧代码用 `claimed.get(int(mid))` 查
    #   → 永远查不到 → 每次重启/重登后等级礼包和成就任务**全部复活**可再领一遍
    #   （用户反馈"昨天全领完，今天又能全领"）。这里把历史遗留的 int 键归一化。
    if any(not isinstance(k, str) for k in raw):
        raw = {str(k): v for k, v in raw.items()}
        st["claimed"] = raw
    st["claimed"] = raw
    return st


def _claimed(mid):
    return _int(_state()["claimed"].get(str(_int(mid))))


# ---------------------------------------------------------------- 进度

def _chapter_stars(chapter):
    """第 chapter 章的累计星数（主线关卡的 LEVEL_RECORDS 星级之和）。"""
    from . import core, game_config
    lc = game_config.load_level_config() or {}
    recs = core.LEVEL_RECORDS or {}
    total = 0
    for lid, rec in recs.items():
        row = lc.get(_int(lid)) or lc.get(str(lid)) or {}
        if _int(row.get("LevelType")) != 1:
            continue
        if _int(row.get("SceneType")) != _int(chapter):
            continue
        total += _int((rec or {}).get("star"))
    return total


def counters(data=None):
    return _data().setdefault("mission_counters", {})


def bump_counter(key, n=1, data=None):
    """事件计数（击杀/BOSS/获得武将/合成/升阶）。返回累计值。

    同时转发给周期任务：kill/boss → 每日、hero_get → 每周、compose → 每月
    （mission_periodic.py，2026-10-29）。
    """
    c = counters(data)
    c[key] = _int(c.get(key)) + max(0, _int(n))
    try:
        from . import mission_periodic
        mission_periodic.on_event(key, n, data)
    except Exception:
        pass
    return c[key]


def level_enemy_count(lid):
    """关卡敌人总数：优先 LevelSetConfig；【困难】【大师】变体没有
    LevelSetConfig 配置，回退 LevelConfig.LevelEnemy 的行数（2026-10-29）。"""
    from . import game_config, core
    n = len(game_config.get_level_enemies(_int(lid)) or [])
    if n > 0:
        return n
    row = game_config.load_level_config().get(_int(lid)) or \
          game_config.load_level_config().get(str(lid)) or {}
    return len(row.get("LevelEnemy") or [])


def progress(row):
    """返回 (complete, need)。complete 已按 need 封顶（客户端显示 x/y）。"""
    t = _int(row.get("target"))
    need = max(1, _int(row.get("target_value")))
    heroes = (_data().get("heroes") or {}).values()
    if t in COUNTER_OF:
        val = _int(counters().get(COUNTER_OF[t]))
    elif t == STAR_TARGET:
        val = _chapter_stars(_int(row.get("target_id")))
    elif t == -1:
        # 等级礼包：TriggerValue=等级阈值
        lv = _int((_data().get("player") or {}).get("lv"))
        val = 1 if lv >= max(1, _int(row.get("trigger_value"))) else 0
        need = 1
    elif t == 30:
        val = sum(1 for h in heroes
                  if _int(h.get("position")) > 0
                  and _int(h.get("star")) >= _int(row.get("target_id")))
    elif t == 32:
        val = sum(1 for h in heroes
                  if BEAST_MIN <= _int(h.get("type")) <= BEAST_MAX
                  and _int(h.get("star")) >= _int(row.get("target_id")))
    else:
        return 0, need
    return min(need, max(0, val)), need


def _unlocked(row):
    """该任务是否应出现在列表里。"""
    if _claimed(row["id"]):
        return False
    t = _int(row.get("target"))
    if t == STAR_TARGET:
        return _star_visible(row)
    if t == -1:
        # 等级礼包：一条链，只有"当前档"可见（阈值已达的第一个未领取档）
        cur = _lvpack_current()
        return cur is not None and _int(cur["id"]) == _int(row["id"])
    return True


def snapshot_missions(data=None):
    """EnterGame / 全量刷新用：所有解锁组的「当前任务」。

    返回 [{id, type, need, complete, accept}, ...]（与 mission.snapshot 同形状）。
    """
    now = int(time.time())
    out = []
    by_id = _rows()["by_id"]
    # 星星收集狂：按可见规则逐条下发（对角链，一章可同时显示多档）
    for ch in sorted(_rows()["star"]):
        for row in _star_tiers(ch):
            if _claimed(row["id"]) or not _unlocked(row):
                continue
            complete, need = progress(row)
            out.append({"id": int(row["id"]), "type": int(row["id"]),
                        "need": int(need), "complete": int(complete),
                        "accept": now})
    # 其余（30/32）：Next 链，组内第一个未领取的即当前
    for chain in _rows()["groups"]:
        for rid in chain:
            row = by_id[rid]
            if _claimed(rid):
                continue
            if not _unlocked(row):
                break                           # 链被锁住（等级不够）→ 整组先不发
            complete, need = progress(row)
            out.append({"id": int(rid), "type": int(rid),
                        "need": int(need), "complete": int(complete),
                        "accept": now})
            break
    # 等级礼包（-1）：一条链，只发当前档（2026-10-29，修复"领完又出来"）
    cur = _lvpack_current()
    if cur is not None:
        complete, need = progress(cur)
        out.append({"id": int(cur["id"]), "type": int(cur["id"]),
                    "need": int(need), "complete": int(complete),
                    "accept": now})
    return out


def claim(mid, data=None):
    """领奖（10100 兜底分支）。返回 (ok, reason, row, awards)。"""
    by_id = _rows()["by_id"]
    mid = _int(mid)
    row = by_id.get(mid)
    if not row:
        return False, "任务不存在", None, []
    if _claimed(mid):
        return False, "该任务已领取过奖励", None, []
    if not _unlocked(row):
        if _int(row.get("target")) == STAR_TARGET:
            return False, "请先领取前面章节的星星任务", None, []
        return False, "请先完成并领取前一个任务", None, []
    complete, need = progress(row)
    if complete < need:
        return False, "任务尚未完成", None, []
    # ★ 这里**不发奖**，只标记领取：发奖统一由 10100 handler 调
    #   core._grant_sign_awards()（道具/装备/武将/属性分流 + 日志）。
    #   之前 claim 内部还调了一次 mission._grant()，双层发放 → 成就奖励全部双倍
    #   （2026-10-29 用户反馈）。与 daily_token.claim 同口径。
    awards = row.get("awards") or []
    _state()["claimed"][str(mid)] = 1        # ★ str 键，同 _claimed 查询口径
    return True, "", row, awards


# ---------------------------------------------------------------- 推送辅助

def push_updates(session, data=None):
    """重算全部解锁组的当前任务，与**会话内已推送的进度**比对：

    · 新出现的任务 → 501 NewMission
    · 进度变化的任务 → 502 UpdateMission（计数器涨了必须推，否则客户端永远 0）
    · 已推送且进度没变 → 跳过
    会话状态：session._achieve_pushed = {配置id: 上次推送的 complete}；
    EnterGame 时由 core 用快照初始化（客户端已有同份数据）。
    """
    seen = getattr(session, "_achieve_pushed", None)
    if seen is None:
        seen = {}
        session._achieve_pushed = seen
    from . import core
    for m in snapshot_missions(data):
        mid = _int(m["id"])
        comp = _int(m["complete"])
        if seen.get(mid) == comp:
            continue
        seen[mid] = comp
        if comp >= _int(m["need"]):
            session.send(core.OP_UPDATE_MISSION, core.build_mission_data(m))
        else:
            session.send(core.OP_NEW_MISSION, core.build_mission_data(m))
