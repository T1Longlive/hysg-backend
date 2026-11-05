"""挑战副本：圣兽岛 / 金银岛（铜钱副本）/ 阵营副本（2026-10-29）。

规则（用户 2026-10-29 确认）：
  · 圣兽岛：4 座岛 × 3 难度，**每座岛每天 1 次**，且只在 LevelConfig.DayOfWeeks 的开岛日可打
  · 金银岛：5 难度，**每天 2 次**；奖励 = 按总伤害跨档算铜钱（CoinIslandConfig 44 档）
  · 阵营副本：五虎(蜀)/江东(吴)/群雄/魏武 四组 × 5 难度，**四组常开、每组每天 1 次**
  · 阵营掉落：每次胜利**随机 1 只** ShowAward 武将（难度 1~3 出 Q5、4~5 出 Q6，配置如此）

客户端依据（反编译源码实读，见 work/RESEARCH_挑战系统_圣兽金银岛阵营副本.md）：
  · 5327 ActivityLevelGroupData{levelGroup[], baseInfo[]}
      baseInfo   = 圣兽×4 + CoinLv(金银岛)；TherionBaseInfo.type 客户端解码成**枚举名字符串**
                   （"QingLong"/"BaiHu"/"ZhuQue"/"XuanWu"/"CoinLv"），下发时给枚举数字即可
      levelGroup = 阵营 4 组（ActivityLevelData{Type=ActivityType, activityTime, isStart}）
  · 5034 NewTherionInfo{baseInfo[]}（AnimalPanel 按 type + IsTherionOpen 判开岛/次数）
  · 5037 ChallengeInfo{leftTherionTimes, coinIslandInfo{times}, worldBoss}
  · 5030/5031：圣兽岛、金银岛走**通用关卡链**（客户端 ReadyFightPanel → tryEnterLevel）
  · 6007/6008：阵营副本（请求体同样是关卡 ID，LevelData:getType() 实锤）
  · 6009 DailyChallengeSweep：VIP 扫荡 → AwardResult

次数口径：
  · 进入（5030/6007）校验「今日开放 + 剩余次数>0」，不满足 → 5002 提示且**不下发 LevelBegin**
  · 消耗发生在**结算**（胜利；金银岛看伤害>0），失败/掉线不扣次数
  · 剩余次数同时写进 LEVEL_RECORDS[lid].leftTimes —— 客户端 tryEnterLevel 用
    `levelData:getLeftTimes() >= 1` 做入口校验，登录取 520 的即这个值

范围裁剪（本次不做，客户端入口保留但不发数据）：
  · 世界BOSS（5035/5037.worldBoss）→ 只回 IsWorldBossOpen=close
  · 蛮族诸将 9001/9002（ChallengeTpye=2，ShowAward 空，配置未完成）
  · 材质岛 GoodsLv（LevelType 10，本版本无对应关卡）
  · 圣兽 5★ 材料（14005/14015/14025/14035）无副本产出，不补来源
"""

import datetime
import os
import random
import time

from . import game_config
from ..proto import ps_protobuf_pb2 as pb

# ---------------------------------------------------------------- 常量

THERION_TYPES = ((1, "青龙岛"), (2, "白虎岛"), (3, "朱雀岛"), (4, "玄武岛"))
COIN_ACT = 8000                       # 金银岛 ActivityType
CAMP_ACTS = (9003, 9004, 9005, 9006)  # 五虎 / 江东 / 群雄 / 魏武（轮换顺序）
CAMP_NAMES = {9003: "五虎上将", 9004: "江东猛将", 9005: "群雄乱舞", 9006: "魏武铁骑"}

THERION_DAILY = 1                     # 每座岛每天次数
COIN_DAILY = 2                        # 金银岛每天次数（ChallengeConfig[2].DailyCount）
CAMP_DAILY = 1                        # 每组每天次数（每天只开一组 → 实际每天 1 次）

WEEK_TEXT = ("周日", "周一", "周二", "周三", "周四", "周五", "周六")

_INDEX = None


# ---------------------------------------------------------------- 基础工具

def _data():
    from . import core
    return core.PLAYER_DATA


def _records():
    from . import core
    return core.LEVEL_RECORDS


def _today():
    return time.strftime("%Y-%m-%d")


def _weekday():
    """0=周日 … 6=周六（与客户端 dayList 下标一致）。"""
    return int(time.strftime("%w"))


def _ordinal():
    """当天序数（用于阵营副本 4 天一轮，与星期无关，跨周连续轮换）。"""
    return datetime.date.today().toordinal()


def camp_act_on(offset=0):
    """offset=0 今天 / 1 明天 开放的阵营副本 ActivityType（每天一组，4 天一轮）。"""
    return CAMP_ACTS[(_ordinal() + int(offset)) % len(CAMP_ACTS)]


def camp_act_today():
    """今天开放的阵营副本 ActivityType。"""
    return camp_act_on(0)


def _day_start_end(offset=0):
    """offset=0 今天 / 1 明天 的 [00:00:00, 23:59:59] 时间戳。"""
    d = datetime.date.today() + datetime.timedelta(days=int(offset))
    start = int(time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1)))
    return start, start + 24 * 3600 - 1


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def level_row(lid):
    rows = game_config.load_level_config() or {}
    return rows.get(int(lid)) or rows.get(str(lid)) or {}


def _day_list(row):
    """DayOfWeeks → 星期列表（0=周日）。解析器给的是数组，兜底兼容 dict。"""
    v = row.get("DayOfWeeks")
    if isinstance(v, dict):
        return sorted(_int(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return sorted(_int(x) for x in v)
    return []


def _award_types(row):
    """ShowAward → [type]（可能是 list 也可能是 dict）。"""
    v = row.get("ShowAward")
    if isinstance(v, dict):
        v = [v[k] for k in sorted(v, key=lambda x: _int(x))]
    out = []
    for a in (v or []):
        if isinstance(a, dict):
            t = _int(a.get("Type"))
            if t > 0:
                out.append(t)
    return out


def _days_text(days):
    if len(days) >= 7:
        return "每日开放"
    return "、".join(WEEK_TEXT[d] if 0 <= d < 7 else "?" for d in days) + "开放"


# ---------------------------------------------------------------- 分组索引

def _index():
    """扫描 LevelConfig 建分组索引：{level_id: group}。

    group = {kind, act, levels, days, daily, key, name}
      kind: therion / coin / camp
    """
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    rows = game_config.load_level_config() or {}
    buckets = {}
    for _k, row in rows.items():
        if not isinstance(row, dict):
            continue
        lid = _int(row.get("ID"))
        lt = _int(row.get("LevelType"))
        act = _int(row.get("ActivityType"))
        ct = _int(row.get("ChallengeTpye"))
        kind = None
        if lt == 4 and 1 <= act <= 4:
            kind = "therion"
        elif lt == 6 and act == COIN_ACT:
            kind = "coin"
        elif lt == 8 and ct == 1 and act in CAMP_ACTS:
            kind = "camp"
        if not kind or lid <= 0:
            continue
        buckets.setdefault((kind, act), []).append(lid)

    index = {}
    groups = []
    for (kind, act), lids in buckets.items():
        lids = sorted(lids)
        base = level_row(lids[0])
        if kind == "therion":
            daily = THERION_DAILY
            name = dict(THERION_TYPES).get(act, "圣兽岛")
            key = "therion:%d" % act
        elif kind == "coin":
            daily = COIN_DAILY
            name = "金银岛"
            key = "coin"
        else:
            daily = CAMP_DAILY
            name = CAMP_NAMES.get(act, "阵营副本")
            key = "camp:%d" % act
        g = {"kind": kind, "act": act, "levels": lids,
             "days": _day_list(base), "daily": daily, "key": key, "name": name}
        groups.append(g)
        for lid in lids:
            index[lid] = g
    _INDEX = {"by_level": index, "groups": groups}
    return _INDEX


def group_of(lid):
    return _index()["by_level"].get(_int(lid))


def groups():
    return _index()["groups"]


def group_by_act(act):
    for g in groups():
        if g["act"] == _int(act):
            return g
    return None


def is_challenge_level(lid):
    return group_of(lid) is not None


def open_on(g, offset=0):
    """第 offset 天（0=今天，1=明天）是否开放。

    圣兽岛：LevelConfig.DayOfWeeks 里配置的星期（每岛每周 2 天）。
    金银岛：每日。
    阵营：**每天只开一组**，四组按天序数 4 天一轮（与星期无关，跨周连续）。
    """
    if g is None:
        return False
    if g["kind"] == "camp":
        return _int(g["act"]) == camp_act_on(offset)
    wd = (_weekday() + int(offset)) % 7
    return wd in (g.get("days") or [])


def open_today(g):
    """今天是否开放（open_on 的简写）。"""
    return open_on(g, 0)


# ---------------------------------------------------------------- 每日次数

def _state():
    st = _data().setdefault("challenge", {})
    today = _today()
    if st.get("day") != today:
        st["day"] = today
        st["used"] = {}
    st.setdefault("used", {})
    return st


def used(g):
    return _int(_state()["used"].get(g["key"]))


def remaining(g):
    if g is None:
        return 0
    return max(0, _int(g["daily"]) - used(g))


def _consume(g):
    st = _state()
    st["used"][g["key"]] = used(g) + 1
    # 客户端入口校验读 LEVEL_RECORDS[lid].leftTimes（520 / 登录下发）
    for lid in g["levels"]:
        rec = _records().setdefault(str(lid), {})
        rec["leftTimes"] = remaining(g)
        rec.setdefault("buyTimes", 0)
        rec.setdefault("star", 0)
    return remaining(g)


def remaining_for_level(lid):
    g = group_of(lid)
    if g is None:
        return None
    if not open_today(g):
        return 0
    return remaining(g)


def therion_left():
    """今日圣兽总剩余（只算今天开放的岛）。"""
    return sum(remaining(g) for g in groups()
               if g["kind"] == "therion" and open_today(g))


def coin_left():
    g = group_by_act(COIN_ACT)
    return remaining(g) if g else 0


def camp_left(act):
    g = group_by_act(act)
    return remaining(g) if g else 0


def snapshot():
    """CharacterData（actionTime.leftTherionTimes / ActivityTherionTimes / ActivityCoinLevelTimes）。"""
    return {"therion_left": therion_left(), "coin_left": coin_left()}


def sync_records():
    """登录 / 打开面板时把挑战关剩余次数同步进 LEVEL_RECORDS（520 数据源）。"""
    for g in groups():
        left = remaining(g) if open_today(g) else 0
        for lid in g["levels"]:
            rec = _records().setdefault(str(lid), {})
            rec["leftTimes"] = left
            rec.setdefault("buyTimes", 0)
            rec.setdefault("star", 0)


# ---------------------------------------------------------------- 入口校验

def can_enter(lid):
    """返回 (ok, reason)。圣兽岛/金银岛/阵营共用。"""
    g = group_of(lid)
    if g is None:
        return True, ""
    if not open_today(g):
        if g["kind"] == "camp":
            return False, "本日开放的是「%s」" % CAMP_NAMES.get(camp_act_today(), "阵营副本")
        return False, "%s今日不开放（%s）" % (g["name"], _days_text(g["days"]))
    if remaining(g) <= 0:
        return False, "%s今日次数已用完" % g["name"]
    return True, ""


# ---------------------------------------------------------------- 奖励计算

def _star_rate():
    """金银岛铜钱倍率 = ExpSetConfig[Lv=上阵武将星级之和].StarRate / 100（客户端同口径）。"""
    stars = 0
    for h in (_data().get("heroes") or {}).values():
        if _int(h.get("position")) > 0:
            stars += _int(h.get("star"), 1)
    try:
        rows = game_config.load_exp_set_config() or {}
        row = rows.get(stars) or {}
        rate = _int(row.get("StarRate"), 100)
    except Exception:
        rate = 100
    return (rate or 100) / 100.0


_CI_CACHE = {"mtime": None, "rows": None}


def coin_island_rows():
    path = os.path.join(game_config._CONFIG_DIR, "app.config.CoinIslandConfig.lua")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return []
    if _CI_CACHE["mtime"] == mt and _CI_CACHE["rows"] is not None:
        return _CI_CACHE["rows"]
    try:
        rows = game_config._load_lua_full(path) or []
    except Exception as e:
        print("[challenge] CoinIslandConfig 读取失败：%r" % e, flush=True)
        rows = []
    if isinstance(rows, dict):
        rows = [rows[k] for k in sorted(rows, key=lambda x: _int(x))]
    rows = [r for r in rows if isinstance(r, dict)]
    rows.sort(key=lambda r: _int(r.get("DamageTotal")))
    _CI_CACHE["mtime"] = mt
    _CI_CACHE["rows"] = rows
    return rows


def coin_from_damage(damage):
    """伤害 → 铜钱：取「DamageTotal <= 伤害」的最后一档。

    对齐客户端 CoinBossBattleUI:findHurtStepIndex：
      coin = (BaseTotal + LvTotal × 主公等级) × StarRate/100
    """
    dmg = _int(damage)
    rows = coin_island_rows()
    idx = 0
    for i, r in enumerate(rows):
        if _int(r.get("DamageTotal")) <= dmg:
            idx = i + 1
        else:
            break
    if idx == 0:
        return 0
    r = rows[idx - 1]
    lv = _int((_data().get("player") or {}).get("lv"), 1)
    coin = (_int(r.get("BaseTotal")) + _int(r.get("LvTotal")) * lv) * _star_rate()
    return max(0, int(coin))


def settle(lid, win, total_damage=0):
    """结算发奖。返回 dict：
      ok      是否发奖
      reason  不发奖原因
      coin    铜钱
      awards  [{kind,type,count}]（hero=武将实例）
      therion_left / coin_left  本次要回填的 ActivityTherionTimes/ActivityCoinLevelTimes
    """
    g = group_of(lid)
    out = {"ok": False, "reason": "", "coin": 0, "awards": [],
           "therion_left": None, "coin_left": None}
    if g is None:
        out["reason"] = "非挑战关卡"
        return out

    if g["kind"] == "coin":
        # 金银岛：按伤害给铜钱，输了（时间到）也照给
        dmg = _int(total_damage)
        if remaining(g) <= 0:
            out["reason"] = "今日次数已用完"
            out["coin_left"] = 0
            return out
        if dmg <= 0:
            out["reason"] = "未造成伤害"
            out["coin_left"] = remaining(g)
            return out
        out["coin"] = coin_from_damage(dmg)
        _consume(g)
        out["ok"] = True
        out["coin_left"] = remaining(g)
        return out

    # 圣兽岛 / 阵营：必须胜利
    if not win:
        out["reason"] = "战斗失败"
        if g["kind"] == "therion":
            out["therion_left"] = remaining(g) if open_today(g) else 0
        return out
    if not open_today(g):
        out["reason"] = "今日不开放"
        return out
    if remaining(g) <= 0:
        out["reason"] = "今日次数已用完"
        return out

    types = _award_types(level_row(lid))
    if g["kind"] == "therion":
        # 圣兽岛：ShowAward 是升星材料的两档（如 14011+14012），全发
        out["awards"] = [{"kind": "hero", "type": t, "count": 1} for t in types]
    else:
        # 阵营副本：随机 1 只
        if types:
            out["awards"] = [{"kind": "hero", "type": random.choice(types), "count": 1}]
    _consume(g)
    out["ok"] = True
    if g["kind"] == "therion":
        out["therion_left"] = remaining(g)
    return out


# ---------------------------------------------------------------- 扫荡（6009）

def vip_skip_unlocked():
    """VIP ChallengeSkip=1 的最低等级（客户端 ReadyFightPanel 同口径）。"""
    try:
        rows = game_config.load_vip_config() or {}
    except Exception:
        return True
    need = None
    for lv, row in rows.items():
        if not isinstance(row, dict):
            continue
        if _int(row.get("ChallengeSkip")) == 1:
            l = _int(lv)
            need = l if need is None else min(need, l)
    if need is None:
        return True
    vip = _int((_data().get("player") or {}).get("vip"))
    return vip >= need


def sweep(lid, count=1):
    """挑战扫荡。返回 (ok, reason, payload{coin, awards, therion_left, coin_left, left})。"""
    g = group_of(lid)
    if g is None:
        return False, "非挑战关卡", None
    if g["kind"] == "coin":
        return False, "金银岛需要实战打出伤害，不支持扫荡", None
    if not can_enter(lid)[0]:
        return False, can_enter(lid)[1], None
    star = _int((_records().get(str(lid)) or {}).get("star"))
    if star <= 0:
        return False, "通关后才能扫荡", None
    if not vip_skip_unlocked():
        return False, "VIP 等级不足，无法扫荡", None
    if remaining(g) <= 0:
        return False, "今日次数已用完", None

    types = _award_types(level_row(lid))
    if g["kind"] == "therion":
        awards = [{"kind": "hero", "type": t, "count": 1} for t in types]
    else:
        awards = [{"kind": "hero", "type": random.choice(types), "count": 1}] if types else []
    _consume(g)
    return True, "", {
        "coin": 0,
        "awards": awards,
        "therion_left": remaining(g) if g["kind"] == "therion" else None,
        "coin_left": None,
        "left": remaining(g),
    }


# ---------------------------------------------------------------- 回包

def _act_of(therion_type):
    """TherionBaseInfo.type → ActivityType（CoinLv=5 对应金银岛 8000）。"""
    return COIN_ACT if _int(therion_type) == 5 else _int(therion_type)


def _fill_therion(entry, act, preview=False):
    # act 传的是 TherionBaseInfo.type 枚举值（青龙1/白虎2/朱雀3/玄武4/CoinLv5）
    g = group_by_act(_act_of(act))
    entry.type = int(act)
    if g is None:
        entry.IsTherionOpen = 2      # close
        entry.todayLeftTimes = 0
        return
    opened = (not preview) and open_today(g)
    entry.IsTherionOpen = 1 if opened else 2
    entry.todayLeftTimes = remaining(g) if opened else 0
    for lid in g["levels"]:
        entry.levelTypes.append(int(lid))


def build_therion_info():
    """5034 NewTherionInfo：只带 4 座圣兽岛（AnimalPanel 按 type 匹配）。"""
    m = pb.NewTherionInfo()
    for act, _name in THERION_TYPES:
        _fill_therion(m.baseInfo.add(), act)
    return m.SerializeToString()


def build_activity_level_group(preview=False):
    """5327 / 5329 ActivityLevelGroupData。

    5327（preview=False）= **今天**开放的内容；5329（preview=True）= **下一轮**。
    客户端 ActivityLevelPanel 的「即将开放/预览」按钮发 5329，收到后
    `onGetPreviewActivityLevelDataHandler` 把 isPreview=true 传给
    `onGetActivityLevelDataHandler`，所有卡片的 `setOpen(false)` —— 于是全部按
    「未开放」渲染，阵营卡片显示倒计时（activityTime.endTime-now）。

    只发**该轮开放**的入口（原版口径：未开放的副本入口不显示）：
      · levelGroup = 该轮那一组阵营副本（其余三组不下发）
      · baseInfo   = 该轮开放的圣兽岛（闭岛整条不下发）+ 金银岛(CoinLv，每日)
    注：5034 NewTherionInfo 仍然发全 4 座岛 —— AnimalPanel 的四个兽位是写死的，
    少了会 debugLayer:error；那界面用 IsTherionOpen 显示「今日不开放」。
    """
    offset = 1 if preview else 0
    m = pb.ActivityLevelGroupData()
    start, end = _day_start_end(offset)
    if preview:
        # 客户端阵营卡片在「未开放」态显示 `endTime - now` 的倒计时（文案 "…后开放"），
        # 所以预览把 start/end 都锚到**下一轮开放时刻**（明天 00:00），
        # 倒计时才是"还有多久开"，而不是"本轮还剩多久"。
        end = start
    want_camp = camp_act_on(offset)
    for act in CAMP_ACTS:
        g = group_by_act(act)
        if g is None or int(act) != want_camp:
            continue
        d = m.levelGroup.add()
        d.Type = int(act)
        d.isStart = 0 if preview else 1
        d.activityTime.startTime = start
        d.activityTime.endTime = end
    for act, _name in THERION_TYPES:
        g = group_by_act(act)
        if g is None or not open_on(g, offset):
            continue
        _fill_therion(m.baseInfo.add(), act, preview=preview)
    if not preview:
        # 金银岛每日开放，不属于「即将开放」；预览列表只放下一轮的阵营+圣兽
        _fill_therion(m.baseInfo.add(), 5)
    return m.SerializeToString()


def build_challenge_info():
    """5037 ChallengeInfo：圣兽剩余次数 + 金银岛次数；世界BOSS 一律 close（不在本期范围）。"""
    m = pb.ChallengeInfo()
    m.leftTherionTimes = therion_left()
    m.coinIslandInfo.times = coin_left()
    m.worldBoss.IsWorldBossOpen = 2
    m.worldBoss.leftTimes = 0
    return m.SerializeToString()
