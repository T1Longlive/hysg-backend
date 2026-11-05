"""武将成长 / 属性合成 / 战力计算 / 升级 / 突破（mock 服务端用）

口径全部来自**原包客户端代码**（不是推测），逐条对应：

  1. 属性合成 —— app.model.data.WarriorData.lua:367-381 `implement()`
        levelGrowth = ExpSetConfig[self._lv].LvGrowth[self:quality()]
        GradeGrowth = ExpSetConfig[self._grade].GradeGrowth[heroConfig.Quality]
        maxHP   = floor(BaseHP   * (1 + (levelGrowth + GradeGrowth)/100) + HPMod)
        attack  = floor(BaseAttack* (1 + (...)/100) + AttackMod)
        defence = floor(BaseDefence*(1 + (...)/100) + DefenceMod)
        speed   = BaseSpeed                       -- 不吃成长，透传
     ★ 注意 Mod 是**在乘完之后加**，且用 floor 不是 round。

  2. 星级等级上限 —— WarriorData.lua:1233 `starMaxLv()`
        = ExpSetConfig[quality].StarMaxLv       -- 1★15 2★20 3★25 4★30 5★35 6★40

  3. 突破（tupo / awaken）—— AwakenConfig 表 + WarriorData.lua:995-1060
        getTuPoMaxLv()   = starMaxLv() + awakenTimes() * 5
        upgradeMaxLv()   = canBreakLv() and max(starMaxLv, getTuPoMaxLv()) or starMaxLv
        每次突破 +5 级上限；6★ 从 40 一路到 80，共 8 次
        材料 = 任意等级的**同名同星级卡牌**（TupoInfo.lua 文案原文）
        金币 = AwakenConfig.Coin
        ★ 能否突破由 HeroConfig.CanBreakLevel 决定，**只有 6★ 那一行为 1**
          （曹操 10014/10015 为 0，只有 10016 为 1）→ 就是"6 星以后才靠突破"
        ★ 真实入口是 WarriorSysNaviPanel:setUpKeyGideState()（升级键满级后变"突破"），
          门槛仅 canBreakLv() + lv < WARRIOR_MAX_BREAKEN_LV(=80)。
          注意 WarriorData:isTupoMax() 里的 `maxLv() >= 50` 是**死代码**：它只被
          app.view_tv.warrior.WarriorPanel（未注册进 PanelDefine）引用，实际面板是
          app.view_tv.warriorPanel.WarriorPanel，不走这条路。

  4. 升级经验 —— WarriorCell.lua:31  &  WarriorData.lua:1122
        ExpSetConfig[当前等级].Exp[quality] = 升到下一级所需经验

  5. 战力权重 —— Hysg_Refactor battle/core/Warrior.lua COMBAT_POWER_WEIGHTS
        单武将战力 = floor(Σ 属性 × 权重)

  6. 关卡给上阵武将发经验 —— 服务端下发 `param.exp` / `param.expChanges`
        （WarriorContainer.lua:53-55）。原包内 LevelConfig.EPAward **全表为 0**
        （已废弃），唯一可用的候选是 ExpSetConfig.TeamExp（12@Lv1 → 4700@Lv80）。
        → 见 team_exp()，标注为【待核实】

老项目里属性其实客户端自己会算（implement()），服务端只要给 lv/grade/quality
就够；但战力 power 是纯服务端字段，所以这里补齐。
"""

import json
import math
import os
import random

_HERE = os.path.dirname(os.path.abspath(__file__))
# 兜底 JSON 已并入后端：server/sghy_server/config/legacy_json/（9 张表）
_JSON_DIR = os.path.join(os.path.dirname(_HERE), "config", "legacy_json")

_exp_cfg = None       # {lv: {LvGrowth:{q:pct}, Exp:{q:exp}, StarMaxLv, TeamExp, ...}}
_hero_cfg = None      # {id: row}
_hero_by_star = None  # {maxStarType: {quality: row}}
_awaken_cfg = None    # {(quality, lv): {"Coin":x, "HeroMaxLv":y}}

# 战力权重（Hysg_Refactor battle/core/Warrior.lua:331-343）
POWER_WEIGHTS = {
    "hp": 0.3, "attack": 0.6, "defence": 1.0, "speed": 20.0,
    "critRate": 20.0, "critDamage": 5.0, "comboRate": 20.0,
    "blockRate": 20.0, "counterRate": 20.0, "hit": 20.0, "miss": 20.0,
}

MAX_QUALITY = 6          # WARRIOR_MAX_QUALITY
MAX_GRADE = 9            # WARRIOR_MAX_GRADE
MAX_BREAKEN_LV = 80      # WARRIOR_MAX_BREAKEN_LV = OPEN_NEW_LEVEL_CONFIG and 80 or 60（本包开关=true）
MAX_HERO_LV = MAX_BREAKEN_LV
DEFAULT_CRIT_DAMAGE = 150

# 兜底表（配置缺失时用，值已与原包核对一致）
_STAR_MAX_LV_FALLBACK = {1: 15, 2: 20, 3: 25, 4: 30, 5: 35, 6: 40}


def _load_json(name):
    p = os.path.join(_JSON_DIR, "app.config.%s.json" % name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _num(v, d=0.0):
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _mapf(d):
    """{"1": 3.0, "2": 4.0} -> {1: 3.0}"""
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        try:
            out[int(float(k))] = _num(v)
        except (TypeError, ValueError):
            continue
    return out


def _mapf_any(v):
    """{"1":3} 或 [3,4] -> {1:3, 2:4}（1 基下标）。

    rt_all .new lua 解析出来数组字段是 list，旧 JSON 是 {"1":v} dict，
    两种形状都要吃。
    """
    if isinstance(v, dict):
        return _mapf(v)
    if isinstance(v, list):
        return {i + 1: _num(x) for i, x in enumerate(v)}
    return {}


def _seq(v):
    """lua 数组(list) 或 json {"1":x} 顺序 dict -> 按序 value 列表。

    迁移到 .new lua 后，表内嵌套数组一律是 list；兜底旧 JSON 时是
    {"1":..,"2":..} 顺序 dict。两种形状统一拉平。
    """
    if isinstance(v, list):
        return list(v)
    if isinstance(v, dict):
        def _k(x):
            s = str(x)
            return (0, int(s), "") if s.lstrip("-").isdigit() else (1, 0, s)
        return [v[k] for k in sorted(v, key=_k)]
    return []


def _load_rows(name):
    """配置行加载统一入口：优先 rt_all 的 app.config.new.X.lua（与客户端
    ConfigManager 同源），兜底 legacy_json（早期有损副本——缺字段/旧行/
    CanRelation 被压扁，2026-10-28 无双吕布缘分报障根因）。

    返回 {key: row_dict}。new lua 顶层 [10011]=.. 稀疏整数键保持 dict，
    连续 1..n 键会被 _lua_table 折叠成 list（用 enumerate 回填键）。
    """
    try:
        from . import game_config as _gc
        data = _gc._load_lua_full(os.path.join(
            _gc._CONFIG_DIR, "app.config.new.%s.lua" % name))
        if data:
            if isinstance(data, list):
                return {i: r for i, r in enumerate(data, 1) if isinstance(r, dict)}
            if isinstance(data, dict):
                rows = {k: v for k, v in data.items() if isinstance(v, dict)}
                if rows:
                    return rows
    except Exception:
        pass
    return _load_json(name) or {}


def _load_exp_rows():
    """ExpSetConfig 行列表：优先 rt_all 的 .new lua（与客户端同源），兜底旧 JSON。

    ★ 2026-10-28 修（用户报障「洗练装备副属性不消耗道具」的根因）：
      legacy_json 是早期反编译产物，缺了大量字段——
      · 第 5 行整组装备字段丢失：EquipReformCoin/Stone、EquipStarUpCost、
        EquipUpGradeExpToCoin、SubAdditionUnlock、*EvolveCoin 全为空
        → 5★ 装备洗练消耗 (0,0)，道具/铜币一分不扣（报障直接原因）；
      · 所有 *StrengthenCost 数组只有 4 个元素（真配置 5 个）
        → 5★ 装备强化也是零消耗；
      · BuyEp 17 行起、BuyCoin 121 行起、ResetLevelDollar 12 行起为空
        → 买体力/买铜钱/重置关卡价格全错。
      与 ProbabilitySetConfig（2026-10-28 已修）同类问题，统一改读 .new lua。
    """
    try:
        from . import game_config as _gc
        parsed = _gc._load_lua_full(os.path.join(
            _gc._CONFIG_DIR, "app.config.new.ExpSetConfig.lua"))
        if parsed:
            rows = [r for r in parsed if isinstance(r, dict)]
            if len(rows) >= 160:
                return rows
    except Exception:
        pass
    return list((_load_json("ExpSetConfig") or {}).values())


def reset():
    """清空懒加载配置缓存（配置热重载用，见 game_config.reload_all）。"""
    global _exp_cfg, _hero_cfg, _hero_by_star, _awaken_cfg, _exp_items
    global _fate_cfg, _fate_items, _equip_cfg, _vip_cfg, _prob_cfg, _synth_cfg
    _exp_cfg = None
    _hero_cfg = None
    _hero_by_star = None
    _awaken_cfg = None
    _exp_items = None
    _fate_cfg = None
    _fate_items = None
    _equip_cfg = None
    _vip_cfg = None
    _prob_cfg = None
    _synth_cfg = None
    return init()


def init():
    """懒加载配置表。返回 True 表示成长表可用。"""
    global _exp_cfg, _hero_cfg, _hero_by_star, _awaken_cfg
    if _exp_cfg is not None:
        return bool(_exp_cfg)
    _exp_cfg = {}
    for idx, row in enumerate(_load_exp_rows(), start=1):
        try:
            key = int(float(row.get("Lv") or idx))
        except (TypeError, ValueError):
            key = idx
        if key <= 0:
            continue
        _exp_cfg[key] = {
            "LvGrowth": _mapf_any(row.get("LvGrowth")),
            "GradeGrowth": _mapf_any(row.get("GradeGrowth")),
            "Exp": _mapf_any(row.get("Exp")),
            # 升阶进度基准：[材料星级] -> 进度点（同星 116，星级越低越小，高于自己为 0）
            "GradeExp": _mapf_any(row.get("GradeExp")),
            "StarMaxLv": _num(row.get("StarMaxLv")),
            "TeamExp": _num(row.get("TeamExp")),
            "LevelExp": _num(row.get("LevelExp")),
            "HeroMaxGrade": _num(row.get("HeroMaxGrade")),
            "EvolveCoin": _num(row.get("EvolveCoin")),
            "StarBC": _num(row.get("StarBC")),
            # 武将升阶每点进度的铜币价（客户端 WarriorData:getShengjieCost 同源）
            "UpGradeExpToCoin": (None if row.get("UpGradeExpToCoin") in (None, "")
                                 else _num(row.get("UpGradeExpToCoin"))),
            "FastenFateExp": (None if row.get("FastenFateExp") in (None, "") else _num(row.get("FastenFateExp"))),
            # 装备相关：强化上限/消耗/升星/升阶/改造 —— 不再白名单，整行留着
            "EquipMaxLv": (None if row.get("EquipMaxLv") in (None, "") else _num(row.get("EquipMaxLv"))),
            "WeaponStrengthenCost": _mapf_any(row.get("WeaponStrengthenCost")),
            "HelmetStrengthenCost": _mapf_any(row.get("HelmetStrengthenCost")),
            "ArmorStrengthenCost": _mapf_any(row.get("ArmorStrengthenCost")),
            "AccessoryStrengthenCost": _mapf_any(row.get("AccessoryStrengthenCost")),
            "EquipReformCoin": (None if row.get("EquipReformCoin") in (None, "")
                                else _num(row.get("EquipReformCoin"))),
            "EquipReformStone": (None if row.get("EquipReformStone") in (None, "")
                                 else _num(row.get("EquipReformStone"))),
            "EquipStarUpCost": (None if row.get("EquipStarUpCost") in (None, "")
                                else _num(row.get("EquipStarUpCost"))),
            "EquipSubAddCount": (None if row.get("EquipSubAddCount") in (None, "")
                                 else _num(row.get("EquipSubAddCount"))),
            "EquipGradeExp": _mapf_any(row.get("EquipGradeExp")),
            # 升阶铜币消耗：按材料星级取值（1:0 / 2:300 / 3:1500 / 4:3000 / 5:0）
            "EquipUpGradeExpToCoin": (None if row.get("EquipUpGradeExpToCoin") in (None, "")
                                      else _num(row.get("EquipUpGradeExpToCoin"))),
            "_raw": row,
            # 买体力/买铜钱（2026-10-24）：客户端 User:getBuyEpCost() 按
            # ExpSetConfig[已买次数+1].BuyEp 取价，getBuyCoinCost() 同理用 BuyCoin，
            # getBuyCoinGet() 按玩家等级行取 LevelBuyCoin。之前白名单里没有这三个字段。
            "BuyEp": _num(row.get("BuyEp")),
            "BuyCoin": _num(row.get("BuyCoin")),
            "LevelBuyCoin": _num(row.get("LevelBuyCoin")),
        }

    _hero_cfg = {}
    _hero_by_star = {}
    for _, row in _load_rows("HeroConfig").items():
        hid = int(_num(row.get("ID")))
        if hid <= 0:
            continue
        _hero_cfg[hid] = row
        base = int(_num(row.get("maxStarType"), hid))
        _hero_by_star.setdefault(base, {})[int(_num(row.get("Quality")))] = row

    _awaken_cfg = {}
    for _, row in _load_rows("AwakenConfig").items():
        q = int(_num(row.get("Quality")))
        lv = int(_num(row.get("Lv")))
        if q <= 0 or lv <= 0:
            continue
        _awaken_cfg[(q, lv)] = {
            "coin": int(_num(row.get("Coin"))),
            "heroMaxLv": int(_num(row.get("HeroMaxLv"))),
        }
    return bool(_exp_cfg and _hero_cfg)


# ---------------------------------------------------------------- 品质/成长

def growth_quality(q):
    """星级列 1..6 全部存在（LvGrowth/Exp 都是 6 列），不要折叠到 4★。

    历史教训：早期把 1~3★ 压到 4★ 列是错的。原包 1★ 的成长百分比**高于** 6★
    （低星靠更高的成长率补偿基础值劣势），例如 Lv10：1★=54% / 4★=27%。
    """
    q = int(q or MAX_QUALITY)
    return min(max(q, 1), MAX_QUALITY)


def _row_at(level):
    """按行号取 ExpSetConfig 行（行号被三处复用：等级 / 星级 / 阶）。"""
    if not init():
        return None
    lv = int(level)
    row = _exp_cfg.get(lv)
    if row is None:                     # 越界兜底到最大行
        row = _exp_cfg.get(max(_exp_cfg))
    return row


def lv_growth_pct(level, quality):
    """等级成长百分点。level = 武将等级，quality = 1..6。"""
    row = _row_at(level)
    if not row:
        return 0.0
    return row["LvGrowth"].get(growth_quality(quality), 0.0)


def grade_growth_pct(quality, grade):
    """阶成长百分点 = ExpSetConfig[阶].GradeGrowth[星级] = 阶 × (4 + 星级)。

    grade 上限 9（WARRIOR_MAX_GRADE）；>9 时原包查不到行 → 0。
    """
    grade = max(0, min(int(grade or 0), MAX_GRADE))
    row = _row_at(grade) if grade >= 1 else None
    if row is None:
        return 0.0
    return row["GradeGrowth"].get(growth_quality(quality), 0.0)


def star_max_lv(quality):
    """星级决定的等级上限：1★15 / 2★20 / 3★25 / 4★30 / 5★35 / 6★40。"""
    q = growth_quality(quality)
    row = _row_at(q)
    if row and row["StarMaxLv"] > 0:
        return int(row["StarMaxLv"])
    return _STAR_MAX_LV_FALLBACK[q]


def level_cap(quality, awaken=0, can_break=None):
    """当前等级上限 —— 对齐客户端 `WarriorData:upgradeMaxLv()`（WarriorData.lua:1221）。

        upgradeMaxLv = canBreakLv() and max(starMaxLv, starMaxLv + awakenTimes*5)
                                    or  starMaxLv

    can_break = HeroConfig.CanBreakLevel > 0 **仅 6★ 那一行是 1**
                （曹操 10014/10015 都是 0，只有 10016 是 1）
    can_break 为 None 时退化为"6★ 才可突破"的推断。
    """
    q = growth_quality(quality)
    base = star_max_lv(q)
    if can_break is None:
        can_break = (q >= MAX_QUALITY)
    if not can_break:
        return base                      # 不能突破：死在上限（5★=35 / 4★=30 …）
    return min(MAX_HERO_LV, base + 5 * max(0, int(awaken or 0)))


def can_break_lv(hero_row):
    """该配置行是否允许突破等级上限（HeroConfig.CanBreakLevel）。"""
    if not hero_row:
        return False
    return _num(hero_row.get("CanBreakLevel")) > 0


def hero_can_break(hero_id, quality=None):
    return can_break_lv(find_hero_row(hero_id, quality))


def awaken_info(quality, level):
    """查 AwakenConfig：该等级能否突破、花多少钱、突破后上限多少。"""
    init()
    q = growth_quality(quality)
    return (_awaken_cfg or {}).get((q, int(level)))


def can_awaken(quality, level, awaken, can_break=None, current_cap=None):
    """是否可突破 —— 对齐真实入口 `WarriorSysNaviPanel:setUpKeyGideState()`(:319-345)。

    真实门槛只有两条：
      ① canBreakLv()                    否则提示"该武将不能突破等级上限"
      ② lv() < WARRIOR_MAX_BREAKEN_LV   否则提示"已经到达最大等级，无法突破"
    另有隐含前提：升级键只有在 `upgradeMaxLv() <= lv()`（已满级）时才会变成"突破"。
    材料 = 任意等级的同名同星级卡牌；金币 = AwakenConfig.Coin。
    """
    q = growth_quality(quality)
    if can_break is None:
        can_break = (q >= MAX_QUALITY)
    if not can_break:
        return False, "该武将不能突破等级上限"
    if int(level or 0) >= MAX_BREAKEN_LV:
        return False, "已经到达最大等级，无法突破"
    cap = current_cap if current_cap is not None else level_cap(q, awaken, can_break)
    if int(level or 0) < cap:
        return False, "武将未满级"
    if not awaken_info(q, level):
        return False, "该等级无可用的突破配置"
    return True, ""


# ---------------------------------------------------------------- 升星 / 圣兽

# 职业 → 圣兽。客户端 WarriorShengxingPanel.lua 里就是这么写的：
#   local animalNames = { 玄武, 青龙, 朱雀, 朱雀, 白虎 }
#   animalName = animalNames[self._data:profession() + 1]
# 与 HeroConfig.UpgradeMaterial 的分组完全一致（1400X=玄武 / 1401X=青龙 /
# 1402X=朱雀 / 1403X=白虎），谋士与刺客共用朱雀。
PROFESSION_BEASTS = {0: "玄武", 1: "青龙", 2: "朱雀", 3: "朱雀", 4: "白虎"}
BEAST_BASE_ID = {0: 14001, 1: 14011, 2: 14021, 4: 14031}   # + (星级-1)
BEAST_ID_MIN, BEAST_ID_MAX = 14001, 14035


def profession(hero_id):
    row = find_hero_row(hero_id)
    return int(_num(row.get("Profession"))) if row else 0


def profession_beast(prof):
    return PROFESSION_BEASTS.get(int(prof or 0), "玄武")


def upgrade_material(hero_id):
    """升星需要消耗的道具/武将 type = 当前星级对应的同星圣兽 ID。"""
    row = find_hero_row(hero_id)
    return int(_num(row.get("UpgradeMaterial"))) if row else 0


def next_type(hero_id):
    """升星后的武将 type（0 = 已是最终形态）。"""
    row = find_hero_row(hero_id)
    return int(_num(row.get("NextType"))) if row else 0


def can_upgrade_star(hero_id):
    row = find_hero_row(hero_id)
    return bool(row) and _num(row.get("CanUpgrade")) > 0


def evolve_coin(quality):
    """升星金币 = ExpSetConfig[当前星级].EvolveCoin（4★10万 / 5★20万）。"""
    row = _row_at(quality)
    return int(row["EvolveCoin"]) if row else 0


def is_beast(hero_id):
    return BEAST_ID_MIN <= int(hero_id or 0) <= BEAST_ID_MAX


def beast_lv(star):
    """圣兽不练级：等级永远等于该星级的等级上限（1★15 … 5★35）。"""
    return star_max_lv(star)


def beast_type(prof, star):
    """职业 + 星级 → 圣兽 type。"""
    p = int(prof or 0)
    base = BEAST_BASE_ID.get(p)
    if base is None:                       # 刺客(2)/谋士(3) 共用朱雀
        base = BEAST_BASE_ID[2] if p in (2, 3) else BEAST_BASE_ID[0]
    return base + max(0, int(star) - 1)


def check_star_up(hero_id, level, grade, quality=None):
    """升星条件 —— 对齐客户端 WarriorListCell.lua:85-90 的高亮判定：
           nextType ~= 0 and isMaxGrade() and isMaxLv() and not isMaxQuality() and 有材料
    返回 (ok, reason, material_type, coin)
    """
    row = find_hero_row(hero_id, quality)
    if not row:
        return False, "配置缺失", 0, 0
    q = int(_num(row.get("Quality")))
    nt = int(_num(row.get("NextType")))
    mat = int(_num(row.get("UpgradeMaterial")))
    coin = evolve_coin(q)
    if _num(row.get("CanUpgrade")) <= 0:
        return False, "该武将无法升星", mat, coin
    if nt <= 0:
        return False, "已达最高星级", mat, coin
    if q >= MAX_QUALITY:
        return False, "已达最高星级", mat, coin
    if int(level or 0) < star_max_lv(q):
        return False, "武将未满级", mat, coin
    if int(grade or 0) < MAX_GRADE:
        return False, "武将未满阶", mat, coin
    return True, "", mat, coin


# ---------------------------------------------------------------- 升阶

GRADE_EXP_PER_GRADE = 100     # 一阶 = 100 进度点（getTotalGradeExp = grade*100 + gradeExp）

# 升级用的经验符：从 ItemConfig 动态读（Functions[1].PropertyName == "Exp"）
#   20005 训练符 = 10 / 20006 鬼才符 = 100 / 20007 天灵符 = 1000
#   （20008 dummy = 10000，未投放，但若背包里有也照样能用）
EXP_ITEMS_FALLBACK = ((20007, 1000), (20006, 100), (20005, 10))
_exp_items = None


def exp_items():
    """{itemId: 经验值}，按数值从大到小返回 list[(id, value)]。"""
    global _exp_items
    if _exp_items is None:
        found = {}
        for _, row in _load_rows("ItemConfig").items():
            iid = int(_num(row.get("ID")))
            if iid <= 0:
                continue
            funcs = row.get("Functions")
            if not isinstance(funcs, (dict, list)):
                continue
            for f in _seq(funcs):
                if isinstance(f, dict) and f.get("PropertyName") == "Exp":
                    v = int(_num(f.get("Value")))
                    if v > 0:
                        found[iid] = v
                    break
        _exp_items = found or dict(EXP_ITEMS_FALLBACK)
    return _exp_items


def exp_item_tiers():
    """[(itemId, value)]，按面额从大到小。plan_exp_items 依赖这个顺序。"""
    return sorted(exp_items().items(), key=lambda kv: -kv[1])


def plan_exp_items(need, inv):
    """按最大面额优先凑够 need 点经验，返回 ({物品ID: 数量}, 实际获得点数)。

    与客户端 getTufeiCost()（WarriorData.lua:1117）的贪心口径一致：
      aCnt = need // 1000；bCnt = 余 // 100；cCnt = ceil(余 / 10)
    库存不足时按现有数量退化；最小面额向上取整，避免差几点凑不满。
    """
    need = max(0, int(need or 0))
    tiers = exp_item_tiers()
    if not tiers:
        return {}, 0
    smallest = tiers[-1][1]
    used = {}
    gained = 0
    rem = need
    for iid, val in tiers:
        if rem <= 0:
            break
        avail = int((inv or {}).get(str(iid), (inv or {}).get(iid, 0)) or 0)
        if avail <= 0:
            continue
        cnt = rem // val
        if val == smallest:
            cnt = (rem + val - 1) // val     # 最小面额向上取整
        cnt = min(cnt, avail)
        if cnt <= 0:
            continue
        used[iid] = cnt
        gained += cnt * val
        rem -= cnt * val
    return used, gained


def exp_to_cap(level, cur_exp, cap, quality):
    """从当前等级/经验升到 cap 级还差多少经验（用于一键提炼）。"""
    q = growth_quality(quality)
    lv = max(1, int(level or 1))
    cap = max(lv, int(cap or lv))
    total = 0
    for l in range(lv, cap):
        total += exp_need(l, q)
    return max(0, total - max(0, int(cur_exp or 0)))


def grade_exp_base(hero_star, mat_star):
    """GradeExp[主卡星级][材料星级]。材料星级 > 主卡星级 时原包返回 0（不可消耗）。"""
    q = growth_quality(hero_star)
    m = growth_quality(mat_star)
    if m > q:
        return 0
    row = _row_at(q)
    if not row:
        return 0
    return int(row["GradeExp"].get(m, 0))


def grade_exp_gain(hero_star, mat_star, mat_grade, mat_grade_exp):
    """一颗材料能提供的升阶进度 —— 逐行对齐 WarriorData:getGainGradeExp():
           floor(GradeExp[自己星级][材料星级] * (材料阶*100 + 材料阶进度 + 200) / 230)
    """
    base = grade_exp_base(hero_star, mat_star)
    if base <= 0:
        return 0
    total = int(mat_grade or 0) * GRADE_EXP_PER_GRADE + int(mat_grade_exp or 0) + 200
    return int(math.floor(base * total / 230.0))


def hero_grade_coin_per(quality):
    """武将升阶每点进度的铜币价 = ExpSetConfig[品质].UpGradeExpToCoin。

    客户端 WarriorData:getShengjieCost() 同源同式（WarriorShengjiePanel:
    caculateCoinCost = getShengjieCost() × 本次材料总进度）。
    1★32 / 2★32 / 3★63 / 4★156 / 5★468 / 6★1670。
    """
    row = _row_at(int(quality or 0)) or {}
    return _num(row.get("UpGradeExpToCoin"))


def hero_grade_coin_cost(quality, gain):
    """一次武将升阶的总铜币 = UpGradeExpToCoin[品质] × 本次材料总进度。"""
    return int(_num(hero_grade_coin_per(quality)) * max(0, int(gain or 0)))


def add_grade_exp(grade, grade_exp, gain):
    """累加升阶进度，返回 (new_grade, new_grade_exp)。上限 9 阶。

    对齐 ShengjieInfo.lua:  expAfter = gradeExp + gain
                            gradeGrowth = floor(expAfter / 100)
                            showGrade = min(9, grade + gradeGrowth)
    """
    grade = max(0, min(int(grade or 0), MAX_GRADE))
    ge = max(0, int(grade_exp or 0))
    if grade >= MAX_GRADE:
        return MAX_GRADE, 0
    total = ge + max(0, int(gain or 0))
    g = grade + total // GRADE_EXP_PER_GRADE
    if g >= MAX_GRADE:
        return MAX_GRADE, 0
    return g, total % GRADE_EXP_PER_GRADE


# ---------------------------------------------------------------- 配缘（缘分）

# Additions.Type → 属性字段（从 FateConfig.Description 文本反推，全表核对过）
FATE_TYPE_ATTR = {
    1: "attack", 2: "defence", 3: "speed", 4: "comboRate", 5: "blockRate",
    6: "counterRate", 7: "critRate", 8: "critDamage", 9: "maxHP",
    12: "finalDamage", 13: "damageReduce",
}

# 缘分卡（替身卡）：25000~25227，每武将 3 张，HeroConfig.ID 存在 ItemConfig.HeroType
FATE_ITEM_MIN, FATE_ITEM_MAX = 25000, 25227
# 一种星级提供的进度：4★ +3 / 5★ +10 / 6★ +30
FATE_STAR_PROGRESS = {4: 3, 5: 10, 6: 30}
# 万能定缘替身卡（开出替身卡，不直接当材料）
FATE_BOX_ITEMS = {29962: 4, 29963: 5, 29964: 6}

_fate_cfg = None
_fate_items = None


def _load_fate():
    global _fate_cfg
    if _fate_cfg is not None:
        return
    _fate_cfg = {}
    for _, row in _load_rows("FateConfig").items():
        fid = int(_num(row.get("ID")))
        if fid > 0:
            _fate_cfg[fid] = row


def _load_fate_items():
    """{替身卡ID: (武将Group, 星级)}"""
    global _fate_items
    if _fate_items is not None:
        return
    _fate_items = {}
    for _, row in _load_rows("ItemConfig").items():
        iid = int(_num(row.get("ID")))
        if not (FATE_ITEM_MIN <= iid <= FATE_ITEM_MAX):
            continue
        ht = int(_num(row.get("HeroType")))
        star = int(_num(row.get("Star")))
        if ht <= 0 or star <= 0:
            continue
        hrow = find_hero_row(ht)
        if not hrow:
            continue
        g = int(_num(hrow.get("Group")))
        if g > 0:
            _fate_items[iid] = (g, star)


def fate_item_info(item_id):
    """替身卡 -> (目标武将 Group, 星级)；非替身卡返回 None。"""
    init()
    _load_fate_items()
    return _fate_items.get(int(item_id or 0))


def hero_group(hero_id):
    row = find_hero_row(hero_id)
    return int(_num(row.get("Group"))) if row else -1


def hero_origin(hero_type):
    """武将势力（HeroConfig.Origin，OriginType: 魏1/蜀2/吴3/群4/杂兵6）。

    军旗带势力套装过滤用；查不到返回 None（视为无势力，吃所有无势力限定的加成）。
    """
    row = find_hero_row(hero_type)
    if not row or row.get("Origin") in (None, ""):
        return None
    try:
        return int(float(_num(row.get("Origin"))))
    except (TypeError, ValueError):
        return None


def hero_fate_ids(hero_id):
    """武将的 4 条缘分 ID（来自 HeroConfig.CanRelation，顺序固定）。

    形状兼容：new.lua 是 [{FateID=..},..] list，旧 JSON 是 {"1":{..}} dict。
    """
    row = find_hero_row(hero_id)
    cr = (row or {}).get("CanRelation")
    if not isinstance(cr, (dict, list)):
        return []
    out = []
    for e in _seq(cr):
        if isinstance(e, dict):
            fid = int(_num(e.get("FateID")))
            if fid > 0:
                out.append(fid)
    return out


def fate_row(fate_id):
    init()
    _load_fate()
    return (_fate_cfg or {}).get(int(fate_id or 0))


def fate_targets(fate_id):
    """该缘分要求的条件武将 Group 列表（1~2 个）。"""
    row = fate_row(fate_id) or {}
    return [int(_num(v)) for v in _seq(row.get("ClientTargets"))]


def fate_target_exp(fate_id):
    """每个条件的进度需求，与 fate_targets 一一对应。"""
    row = fate_row(fate_id) or {}
    return [int(_num(v)) for v in _seq(row.get("TargetExp"))]


def fate_kind(fate_id):
    """0 = 上阵缘（与人），1 = 装备缘（武器）。"""
    return int(_num((fate_row(fate_id) or {}).get("Group")))


def fate_additions(fate_id, upgraded=False):
    """缘分加成 [(Type, Value, Mode)]。upgraded=True 取永久激活后的强化值。"""
    row = fate_row(fate_id) or {}
    key = "UpgradeAdditions" if upgraded else "Additions"
    out = []
    for v in _seq(row.get(key)):
        if isinstance(v, dict):
            out.append((int(_num(v.get("Type"))), _num(v.get("Value")), int(_num(v.get("Mode")))))
    return out


def fate_progress_for_star(star):
    """一颗材料（替身卡或武将实例）提供的进度。

    来源：`ExpSetConfig[星级].FastenFateExp`（客户端 FateInforLabel 也读这个），
    实测值：1★=0 / 2★=0 / 3★=1 / 4★=3 / 5★=10 / 6★=30
    """
    s = int(star or 0)
    if s <= 0:
        return 0
    row = _row_at(s)
    if row:
        v = row.get("FastenFateExp")
        if v is not None:
            return int(v)
    return FATE_STAR_PROGRESS.get(s, 0)


def fate_active_state(fate_id, progress, on_field_groups, equipped=None):
    """判定缘分状态，返回 (state, upgraded)。

    state: 2 = 永久激活，1 = 临时生效，0 = 未生效
    progress: {targetGroup: {"exp": x, "complete": 0/1}}
    equipped: 该武将身上已装备的武器/装备 ID 集合（用于装备缘判定）

    规则（用户 17:40 + 17:44 确认）：
      · 上阵缘：所有条件 Complete → 永久激活，用 UpgradeAdditions（**替换** Additions）
                否则只要「未完成」的条件武将都在场 → 临时生效，用 Additions
                （已完成的那些不需要在场）
      · 装备缘：**没有进度/永久激活概念**，装上对应武器就生效（Additions），
                卸下就失效，没有额外加成。
    """
    targets = fate_targets(fate_id)
    if not targets:
        return 0, False

    if fate_kind(fate_id) == 1:
        return (1, False) if (equipped and targets[0] in equipped) else (0, False)

    complete = []
    for g in targets:
        p = (progress or {}).get(str(g)) or (progress or {}).get(g) or {}
        complete.append(1 if int(_num(p.get("complete"))) == 1 else 0)
    if all(complete):
        return 2, True
    pending = [g for g, c in zip(targets, complete) if c == 0]
    if pending and all(g in (on_field_groups or ()) for g in pending):
        return 1, False
    return 0, False


def apply_addition_bonus(attrs, bonuses, mapping=None):
    """把加成叠到属性上，**结果与加成条目顺序无关**。

    口径（用户 2026-10-24 拍板）：
        最终 = (武将成长 + Σ固定值) × (1 + Σ百分比/100)

    · 固定值来源：武将自身成长（attrs 的初值）、装备、军旗词条、缘分
      —— 全部先加完，得到「固定值总和」
    · 百分比来源：装备百分比词条、缘分百分比词条、军旗套装效果
      —— 全部相加（不连乘），最后统一乘一次
    · 所以"先算哪个加成"完全不影响结果（旧实现按列表顺序复利，顺序不同数值就不同）

    bonuses = [(AdditionType, Value, Mode)]，Mode 1=百分比 0=固定值。
    mapping 决定枚举 → 属性字段；**军旗/装备必须传 EQUIP_TYPE_ATTR**
    （比缘分表多 tenacity/cure/aoe/skill 等枚举，用缘分表会静默丢加成）。
    """
    if not attrs or not bonuses:
        return attrs
    table = mapping or FATE_TYPE_ATTR
    base = {k: (attrs.get(k) or 0) for k in attrs}
    fixed = {}
    pct = {}
    for typ, val, mode in bonuses:
        key = table.get(int(typ))
        if not key:
            continue
        # ★ 2026-10-28 修：增伤/减伤/治疗/范围/技能伤害/韧性这类属性 calc_attrs
        #   不含基础值，旧实现 `key not in attrs` 直接丢弃 → 装备副属性洗出的
        #   「增伤 +X」永远不生效、面板也不显示（用户报障）。缺键时就地建 0。
        if key not in attrs:
            attrs[key] = 0
        if int(mode) == 1:
            pct[key] = pct.get(key, 0.0) + float(val)
        else:
            fixed[key] = fixed.get(key, 0.0) + float(val)
    for key in set(fixed) | set(pct):
        total_fixed = float(base.get(key) or 0) + fixed.get(key, 0.0)
        attrs[key] = total_fixed * (1.0 + pct.get(key, 0.0) / 100.0)
    for k in ("maxHP", "hp", "attack", "defence", "speed", "comboRate", "blockRate",
              "counterRate", "critRate", "critDamage", "hit", "miss",
              "finalDamage", "damageReduce", "cureIncrease", "cureDecrease",
              "aoeIncrease", "aoeDecrease", "skillIncrease", "skillDecrease",
              "tenacity", "effectResist", "effectHit"):
        if k in attrs:
            attrs[k] = int(round(attrs[k]))
    return attrs


def apply_fate_bonus(attrs, bonuses):
    """把缘分加成叠加到属性上。bonuses = [(Type, Value, Mode)]，Mode 1=百分比 0=固定值。"""
    return apply_addition_bonus(attrs, bonuses, FATE_TYPE_ATTR)


# ---------------------------------------------------------------- 装备（EquipConfig）

_equip_cfg = None


def _load_equip():
    global _equip_cfg
    if _equip_cfg is not None:
        return
    _equip_cfg = {}
    for _, row in _load_rows("EquipConfig").items():
        eid = int(_num(row.get("ID")))
        if eid > 0:
            _equip_cfg[eid] = row


# AdditionType 枚举（proto）-> 属性字段，与缘分同一套
EQUIP_TYPE_ATTR = dict(FATE_TYPE_ATTR)
EQUIP_TYPE_ATTR.update({10: "cureIncrease", 11: "cureDecrease",
                        14: "aoeIncrease", 15: "aoeDecrease",
                        16: "skillIncrease", 17: "skillDecrease",
                        20: "tenacity", 21: "miss", 22: "hit"})
# ValueMode: 0 = Number(固定值) / 1 = Percent(百分比) —— 与缘分一致

EQUIP_MAX_STRENGTHEN = 120      # 强化等级上限（暴击可突破）
# 实际上限不是常量，见 equip_max_strengthen_lv()：= 玩家等级 × 2

# 强化暴击概率（按 VIP）——⚠️ 待核实
# 用户口述锚点：VIP8 = 20%、VIP11 = 25%；其余档位为推断
EQUIP_STRENGTHEN_CRIT_RATE = {
    0: 0.15, 1: 0.15, 2: 0.15,
    3: 0.17, 4: 0.18, 5: 0.18, 6: 0.19, 7: 0.19,
    8: 0.20, 9: 0.21, 10: 0.23,
    11: 0.25,
    12: 0.26, 13: 0.27, 14: 0.28, 15: 0.30,
}


def equip_max_strengthen_lv(player_lv):
    """强化等级上限 = ExpSetConfig[玩家等级].EquipMaxLv ≈ 玩家等级 × 2。

    玩家 1 级 -> 2，10 级 -> 20，60 级 -> 120（即常说的"满级 120"），
    70 级 -> 140，80 级 -> 160。**强化暴击可以突破此上限**。
    """
    row = _row_at(int(player_lv or 0))
    if row and row.get("EquipMaxLv") is not None:
        return int(row["EquipMaxLv"])
    return int(player_lv or 0) * 2


def equip_strength_cost(equip_type, strength_lv, is_five_times=False, player_lv=1):
    """强化一次/五次的铜币消耗。

    取 ExpSetConfig[当前强化等级 + 1] 的 `XxxStrengthenCost[Quality]`，
    部位决定字段名（武器/头盔/铠甲/宝物），品质决定数组下标。
    is_five_times=True 时返回 ×5 的批量价。
    """
    row = _row_at(int(strength_lv or 0) + 1)
    if not row:
        return 0
    part = equip_part(equip_type)
    key = {1: "WeaponStrengthenCost", 2: "HelmetStrengthenCost",
           3: "ArmorStrengthenCost", 4: "AccessoryStrengthenCost"}.get(part)
    cost_list = row.get(key) or {}
    q = equip_quality(equip_type)
    cost = _num(cost_list.get(q) if q in cost_list else cost_list.get(str(q)))
    return cost * (5 if is_five_times else 1)


def equip_crit_rate(vip_lv):
    """强化暴击概率（按 VIP）——⚠️ 待核实，锚点 VIP8=20% / VIP11=25%"""
    return EQUIP_STRENGTHEN_CRIT_RATE.get(int(vip_lv or 0), 0.15)


_vip_cfg = None


def _load_vip():
    """VIPSetConfig（含 EquipEnCritCount = 强化暴击倍率上限）。

    ★ 2026-10-28：与 _load_prob 同步，改读 rt_all 运行时真配置（旧 JSON 只作兜底）。
      当前两处数据一致（VIP0-2=2 / 3-7=3 / 8-11=4 / 12+=5），但旧产物不可靠，
      以后配置改动必须以 rt_all 为准。
    """
    global _vip_cfg
    if _vip_cfg is None:
        rows = None
        try:
            from . import game_config as _gc
            parsed = _gc._load_lua_full(os.path.join(
                _gc._CONFIG_DIR, "app.config.new.VIPSetConfig.lua"))
            if parsed:
                rows = parsed
        except Exception:
            rows = None
        _vip_cfg = rows if rows else (_load_json("VIPSetConfig") or {})
    return _vip_cfg


def equip_crit_max(vip_lv):
    """强化暴击倍率上限 = VIPSetConfig[该VIP].EquipEnCritCount

    实测：VIP0-2=2 / VIP3-7=3 / VIP8-11=4 / VIP12-15=5
    """
    want = int(vip_lv or 0)
    cfg = _load_vip() or {}
    rows = cfg.values() if isinstance(cfg, dict) else cfg   # lua 解析可能是数组
    for r in rows:
        if isinstance(r, dict) and int(_num(r.get("Lv"))) == want:
            v = _num(r.get("EquipEnCritCount"), 2)
            return max(1, int(v)) if v else 2
    return 2


def equip_strengthen_apply(strength_lv, times, crit_rate, crit_max,
                           _randint=None, _rand=None):
    """执行 times 次强化点击 -> (new_lv, total_add, crit_hits)

    规则（用户 10:49 口述）：
      · 每次点击算 1 次强化，**有概率暴击**
      · 暴击时本次强化次数 = random(1, 倍率上限)，倍率上限 = VIPSetConfig.EquipEnCritCount
      · 批量"强化5次"= 连续 5 次点击，**总次数一次性累加**，中途不截断上限
        （例：113 级点强化5次暴击出 20 次 -> 直接到 133 级）
      · 上限只用于判断"还能不能强化"，不截断数值（所以能超过 120）
    """
    if _randint is None:
        _randint = random.randint
    if _rand is None:
        _rand = random.random
    total_add = 0
    crit_hits = 0
    for _ in range(max(0, int(times))):
        add = 1
        if _rand() < crit_rate:
            add = _randint(1, max(1, int(crit_max)))
            crit_hits += 1
        total_add += add
    return strength_lv + total_add, total_add, crit_hits


def equip_row(equip_type):
    init()
    _load_equip()
    return (_equip_cfg or {}).get(int(equip_type or 0))


def equip_group(equip_type):
    return int(_num((equip_row(equip_type) or {}).get("Group")))


def equip_part(equip_type):
    """部位：1=武器 2=头盔 3=铠甲 4=宝物"""
    return int(_num((equip_row(equip_type) or {}).get("Part")))


def equip_star(equip_type):
    return int(_num((equip_row(equip_type) or {}).get("Star")))


def equip_quality(equip_type):
    return int(_num((equip_row(equip_type) or {}).get("Quality")))


# ---- 有效装备过滤（来源：Hysg_Refactor/scripts/player_data.lua:629 OWNABLE_STAR_RANGE）----
# 「配置表里有」不等于「玩家能拥有」：每个品质只允许一段星级区间。
# ⚠️ Quality 编号 -> 颜色（客户端 getEquipCellBgOnTVU 为准，别叫错）：
#   1=灰 / 2=绿 / 3=蓝 / 4=紫 / 5=黄(神)
#   2(绿) -> 1~3 星
#   3(蓝) -> 2~4 星
#   4(紫) -> 3~5 星
#   5(黄) -> 3~5 星（配置里暂无 Q5 装备，预留）
EQUIP_QUALITY_COLOR = {1: "灰", 2: "绿", 3: "蓝", 4: "紫", 5: "黄"}
EQUIP_OWNABLE_STAR_RANGE = {
    2: (1, 3),
    3: (2, 4),
    4: (3, 5),
    5: (3, 5),
}


def equip_star_range(quality):
    """该品质允许的星级区间 -> (min, max)；不可拥有返回 (0, 0)"""
    return EQUIP_OWNABLE_STAR_RANGE.get(int(quality or 0), (0, 0))


def is_equip_ownable(equip_type):
    """该配置行是否是玩家可拥有的有效装备（Hysg_Refactor 的 isEquipOwnable）"""
    row = equip_row(equip_type)
    if not row:
        return False
    lo, hi = equip_star_range(equip_quality(equip_type))
    if lo <= 0:
        return False
    return lo <= equip_star(equip_type) <= hi


def equip_max_star(equip_type):
    """该装备的**最高星级** = EquipConfig.MaxQuality。

    注意与 equip_star_range 的区别：
      · MaxQuality 是「配置上限」（蓝3/紫4/神5）
      · equip_star_range 是「可拥有区间」（蓝1~3/紫2~4/神3~5）
    升星上限判定用本函数（当前星级 < MaxQuality 才能升）。
    """
    return int(_num((equip_row(equip_type) or {}).get("MaxQuality")))


def equip_next_type(equip_type):
    return int(_num((equip_row(equip_type) or {}).get("NextType")))


def equip_last_type(equip_type):
    return int(_num((equip_row(equip_type) or {}).get("LastType")))


def equip_main_addition(equip_type):
    """主属性 -> (Type, Value, StrengthenGrowth, UpGradeGrowth)"""
    d = (equip_row(equip_type) or {}).get("MainAddition") or {}
    return (int(_num(d.get("Type"))), _num(d.get("Value")),
            _num(d.get("StrengthenGrowth")), _num(d.get("UpGradeGrowth")))


def equip_assistant_cfg(equip_type):
    """副属性配置 -> [{IndexID, Condition(解锁所需星级), Type(1攻/2防)}]"""
    d = (equip_row(equip_type) or {}).get("AssistantAddition") or {}
    out = []
    for v in _seq(d):
        if isinstance(v, dict):
            out.append({"index": int(_num(v.get("IndexID"))),
                        "need_star": int(_num(v.get("Condition"))),
                        "kind": int(_num(v.get("Type")))})
    return out


def equip_calc_additions(equip_type, strength_lv=0, grade=0):
    """算一件装备当前的属性加成 -> [(Type, Value, Mode)]

    主属性 = Base + StrengthenGrowth × 强化等级 + UpGradeGrowth × 阶数
    Mode：1=百分比（与缘分一致），装备 MainAddition 没配 Mode，默认按固定值(0)。
    """
    typ, base, sg, ug = equip_main_addition(equip_type)
    if typ <= 0:
        return []
    val = base + sg * int(strength_lv or 0) + ug * int(grade or 0)
    return [(typ, val, 0)]


# ---- 升阶（grade）----
EQUIP_MAX_GRADE = 9          # 每个星级最多升阶 9 次
EQUIP_GRADE_EXP_PER_LV = 100  # 每阶需要 100 经验（100%）

# 升阶进度比例：diff = (自己档位品质+星级) - (材料品质+星级)
# 每差 1 减半，>=4 封底 7%（用户 12:55 确认）
EQUIP_GRADE_RATIO = {0: 1.12, 1: 0.56, 2: 0.28, 3: 0.14}
EQUIP_GRADE_RATIO_MIN = 0.07


def equip_grade_base_exp(mat_quality, mat_star):
    """升阶基准进度 = EquipGradeExp[材料品质][材料星级]

    配置规律（15/15 验证）：= 5 × 2^(品质+星级-3)，只由两者之和决定。
    """
    row = _row_at(int(mat_quality or 0))
    if not row:
        return 0
    tbl = row.get("EquipGradeExp") or {}
    return _num(tbl.get(mat_star) if mat_star in tbl else tbl.get(str(mat_star)))


def _equip_tier_quality(q):
    """神装(Q4)自己升阶时按紫装(Q3)算 —— 即「自己侧减一」。

    这样神5 与紫5 的档位都是 8，吃同样材料给的进度完全相同，
    「同星级下神品质和紫色品质升阶的要求一样」才严格成立。
    """
    q = int(q or 0)
    return q - 1 if q == 4 else q


def equip_grade_ratio(self_quality, self_star, mat_quality, mat_star):
    """升阶进度比例（112% / 56% / 28% / 14% / 7% 封底）"""
    s_self = _equip_tier_quality(self_quality) + int(self_star or 0)
    s_mat = int(mat_quality or 0) + int(mat_star or 0)
    diff = max(0, s_self - s_mat)
    return EQUIP_GRADE_RATIO.get(diff, EQUIP_GRADE_RATIO_MIN)


def equip_grade_exp_gain(self_quality, self_star, mat_quality, mat_star):
    """一件材料提供的升阶经验 = 基准 × 比例"""
    base = equip_grade_base_exp(mat_quality, mat_star)
    return base * equip_grade_ratio(self_quality, self_star, mat_quality, mat_star)


def equip_can_grade_material(self_quality, self_star, mat_quality, mat_star):
    """材料是否可用于升阶：同品质只能吃 ≤自己星级；低品质任意星级可吃。"""
    mq, ms = int(mat_quality or 0), int(mat_star or 0)
    sq, ss = int(self_quality or 0), int(self_star or 0)
    if mq > sq:
        return False        # 材料品质不能高于自己
    if mq == sq and ms > ss:
        return False        # 同品质时星级不能高于自己
    return True


def equip_grade_apply(self_quality, self_star, cur_grade, cur_exp, mats):
    """执行升阶：mats = [(mat_quality, mat_star), ...]
    返回 (new_grade, new_exp, gained, consumed)
    经验满 100 升一阶，阶数上限 9，多余经验保留（不溢出到下一星级）。
    """
    gained = 0.0
    for mq, ms in mats:
        if equip_can_grade_material(self_quality, self_star, mq, ms):
            gained += equip_grade_exp_gain(self_quality, self_star, mq, ms)
    total = cur_exp + gained
    new_grade = cur_grade
    while new_grade < EQUIP_MAX_GRADE and total >= EQUIP_GRADE_EXP_PER_LV:
        total -= EQUIP_GRADE_EXP_PER_LV
        new_grade += 1
    if new_grade >= EQUIP_MAX_GRADE:
        total = min(total, EQUIP_GRADE_EXP_PER_LV - 1)   # 满阶后经验封顶
    return new_grade, int(total), gained, len(mats)


# ---- 升星（star up）----
# 消耗 2 件同星同品质同名装备实例 + 铜币，升星后阶数清零、强化等级保留

def equip_star_up_cost(cur_star):
    """升星铜币消耗 = ExpSetConfig[当前星级].EquipStarUpCost
    1→2=1万 / 2→3=3万 / 3→4=10万 / 4→5=30万
    """
    row = _row_at(int(cur_star or 0))
    return int(_num((row or {}).get("EquipStarUpCost")))


def equip_can_star_up(equip_type, grade, mat_types):
    """升星前置校验 -> (ok, reason)
    · 满 9 阶才能升星
    · 星级 < MaxQuality（蓝3 / 紫4 / 神5）
    · 材料 2 件，同 Group（同名）、同 Quality、同 Star
    """
    if int(grade or 0) < EQUIP_MAX_GRADE:
        return False, "需先升满 %d 阶（当前 %d 阶）" % (EQUIP_MAX_GRADE, int(grade or 0))
    row = equip_row(equip_type)
    if not row:
        return False, "装备配置不存在"
    cur_star = equip_star(equip_type)
    max_star = equip_max_star(equip_type)
    if cur_star >= max_star:
        return False, "已满星（%d星，上限 %d 星）" % (cur_star, max_star)
    if not equip_next_type(equip_type):
        return False, "没有下一星级形态"
    if len(mat_types) != 2:
        return False, "需要 2 件材料"
    g = equip_group(equip_type)
    for mt in mat_types:
        mrow = equip_row(mt)
        if not mrow:
            return False, "材料配置不存在(%s)" % mt
        if equip_group(mt) != g:
            return False, "材料必须是同名装备"
        if equip_quality(mt) != equip_quality(equip_type):
            return False, "材料必须同品质"
        if equip_star(mt) != cur_star:
            return False, "材料必须同星级（%d星）" % cur_star
    return True, "ok"


def equip_star_up_apply(equip_type):
    """返回升星后的 EquipConfig.ID（走 NextType 链）"""
    return equip_next_type(equip_type)


# ---- 改造（副属性洗练）----
# ProbabilitySetConfig：Group=1攻击类 / 2防御类，各 8 个属性档位
# 攻击类只刷 攻击(1)/连击(4)/暴击(7)/暴伤(8)；防御类只刷 防御(2)/格挡(5)/反击(6)/生命(9)

_prob_cfg = None


def _load_prob():
    """ProbabilitySetConfig（属性池）。

    ★ 2026-10-28 修：以前读 04_config_tables/json 的旧反编译 JSON —— 只有 32 行、
      仅 Group 1/2；而运行时真配置有 Group 3/4（客户端 ClientConfig.
      equipAssistantAddition：Type 1/4=攻击、2/3=防御）。七星龙渊剑 34075 两条
      副属性 Type=4 → Group 4 的行全丢 → 解锁/洗练一直报
      「属性池为空（玩家等级不够）」。改为读 rt_all 的 .new lua（与客户端同源）。
    """
    global _prob_cfg
    if _prob_cfg is not None:
        return
    rows = None
    try:
        from . import game_config as _gc
        parsed = _gc._load_lua_full(os.path.join(
            _gc._CONFIG_DIR, "app.config.new.ProbabilitySetConfig.lua"))
        if parsed:
            rows = list(parsed.values()) if isinstance(parsed, dict) else list(parsed)
    except Exception:
        rows = None
    if not rows:
        rows = list((_load_json("ProbabilitySetConfig") or {}).values())  # 兜底（旧 JSON）
    _prob_cfg = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        _prob_cfg.append({
            "group": int(_num(row.get("Group"))),            # 1攻 2防
            "attr": int(_num(row.get("AdditionGroup"))),     # AdditionType 枚举
            "open_lv": int(_num(row.get("OpenLv"))),
            "min": _num(row.get("MinValue")),
            "max": _num(row.get("MaxValue")),
            "max_lv": int(_num(row.get("MaxLv"))),
        })


def equip_reform_pool(kind, player_lv):
    """可刷出的属性池 -> [{attr, min, max, open_lv, max_lv}]
    kind: 1=攻击类 2=防御类（来自 EquipConfig.AssistantAddition.Type）
    每个 AdditionGroup 取玩家等级已达标的最高档。
    """
    _load_prob()
    best = {}
    for r in (_prob_cfg or []):
        if r["group"] != int(kind or 0):
            continue
        if int(player_lv or 0) < r["open_lv"]:
            continue
        cur = best.get(r["attr"])
        if cur is None or cur["open_lv"] < r["open_lv"]:
            best[r["attr"]] = r
    return [best[k] for k in sorted(best)]


def equip_reform_roll(kind, player_lv, _randint=None):
    """洗练一次 -> (attr_type, value, lo, hi)；池空返回 (0, 0, 0, 0)

    lo/hi 是该属性本次可刷出的区间——客户端用它做颜色分档
    （AdditionBtn:getColor: <40% 蓝 / <70% 紫 / <max 紫更高档 / ==max 金），
    必须随 pending/assist 下发（AdditionData.param2/param3）。
    """
    if _randint is None:
        _randint = random.randint
    pool = equip_reform_pool(kind, player_lv)
    if not pool:
        return 0, 0, 0, 0
    pick = pool[_randint(0, len(pool) - 1)]
    lo, hi = int(pick["min"]), int(pick["max"])
    return pick["attr"], (_randint(lo, hi) if hi > lo else lo), lo, hi


def equip_reform_cost(cur_star):
    """改造消耗 -> (铜币, 洗练石)，按星级取 ExpSetConfig"""
    row = _row_at(int(cur_star or 0))
    if not row:
        return 0, 0
    return int(_num(row.get("EquipReformCoin"))), int(_num(row.get("EquipReformStone")))


def equip_assistant_slots(cur_star):
    """该星级可解锁的副属性条数 = EquipSubAddCount（1★0 / 2★0 / 3★1 / 4★1 / 5★2）"""
    row = _row_at(int(cur_star or 0))
    return int(_num((row or {}).get("EquipSubAddCount")))


# ---- 分解返还 ----
# 以客户端 EquipDecomposePanel:caculateCoin 为准（用户 14:18 确认）：
#   强化累计 / 2、升阶累计 / 2、洗练币 × 0.5、洗练石 × 0.4
#   另加基础返还：品质×500 + 星级×200
EQUIP_REFUND_COIN_RATE = 0.5     # 铜币类返还比例
EQUIP_REFUND_STONE_RATE = 0.4    # 洗练石返还比例


def equip_decompose_refund(equip_type, strengthen_lv, grade, grade_exp,
                           assistant_list):
    """分解返还 -> (铜币, 洗练石)

    客户端公式（EquipDecomposePanel:caculateCoin）：
      coin  = 品质×500 + 星级×200
      coin += 强化累计(Σ StrengthenCost[品质]) / 2
      coin += EquipUpGradeExpToCoin[品质] × (grade×100 + gradeExp) / 2
      coin += Σ(reformCostCoin × 0.5)          （每条已洗练的副属性）
      stone += Σ floor(reformCostStone × 0.4)
    assistant_list = [{"times": n, "cost_coin": x, "cost_stone": y}, ...]
    """
    q = equip_quality(equip_type)
    s = equip_star(equip_type)
    coin = q * 500 + s * 200

    # 强化累计消耗：Σ(i=1..strengthLv) StrengthenCost[品质]
    total = 0
    for i in range(1, int(strengthen_lv or 0) + 1):
        row = _row_at(i)
        if not row:
            continue
        key = {1: "WeaponStrengthenCost", 2: "HelmetStrengthenCost",
               3: "ArmorStrengthenCost", 4: "AccessoryStrengthenCost"}.get(
            equip_part(equip_type))
        cl = row.get(key) or {}
        total += _num(cl.get(q) if q in cl else cl.get(str(q)))
    coin += total * EQUIP_REFUND_COIN_RATE

    # 升阶累计消耗：EquipUpGradeExpToCoin[品质] × (grade×100 + gradeExp)
    row_q = _row_at(q)
    per = _num((row_q or {}).get("EquipUpGradeExpToCoin"))
    spent = per * (int(grade or 0) * EQUIP_GRADE_EXP_PER_LV + int(grade_exp or 0))
    coin += spent * EQUIP_REFUND_COIN_RATE

    # 洗练返还：币 ×0.5、石 ×0.4（仅统计洗练过的副属性）
    stone = 0
    for a in (assistant_list or []):
        times = int((a or {}).get("times") or 0)
        if times > 0:
            coin += _num((a or {}).get("cost_coin")) * EQUIP_REFUND_COIN_RATE
            stone += int(_num((a or {}).get("cost_stone")) * EQUIP_REFUND_STONE_RATE)
    return int(coin), int(stone)


# ---- 碎片合成（SyntheticConfig）----
# 碎片道具 ItemConfig.SubType=5，ID 24036 起，名字即装备名（古锭刀/赤兔/青龙偃月刀…）
# SyntheticConfig：Condition{Type:碎片ID, Value:需求量} + Products(产出装备) + Coin(手续费)
# 需求量分布：10 / 50 / 80；手续费 5万 / 20万 / 50万
#
# 规则（用户 14:15）：**一次性批量合成，能合几件给几件**
#   110/30 → 3 件（余 20）    80/80 → 1 件    59/50 → 1 件（余 9）
#   即 floor(拥有 / 需求) 件，余下碎片保留

_synth_cfg = None          # {碎片ID: {"need": n, "product": equipID, "coin": x}}


def _load_synth():
    global _synth_cfg
    if _synth_cfg is not None:
        return
    _synth_cfg = {}
    for _, row in _load_rows("SyntheticConfig").items():
        cond = row.get("Condition")
        if not isinstance(cond, (dict, list)):
            continue
        prod = int(_num(row.get("Products")))
        if prod <= 0:
            continue
        for v in _seq(cond):
            if not isinstance(v, dict):
                continue
            frag = int(_num(v.get("Type")))
            need = int(_num(v.get("Value")))
            if frag > 0 and need > 0 and prod not in _synth_cfg:
                _synth_cfg[frag] = {"need": need, "product": prod,
                                    "coin": int(_num(row.get("Coin"))),
                                    "name": row.get("Name")}


def synth_entry(frag_id):
    init()
    _load_synth()
    return (_synth_cfg or {}).get(int(frag_id or 0))


def synth_result(frag_id, have):
    """能合出几件 -> (件数, 消耗碎片数, 手续费总额)；不合返回 (0,0,0)"""
    e = synth_entry(frag_id)
    if not e:
        return 0, 0, 0
    need = e["need"]
    n = int(have or 0) // need
    if n <= 0:
        return 0, 0, 0
    return n, n * need, n * e["coin"]


# ---------------------------------------------------------------- 升级经验

def exp_need(level, quality):
    """当前等级升到下一级所需经验 = ExpSetConfig[level].Exp[quality]。"""
    row = _row_at(level)
    if not row:
        return 0
    return int(row["Exp"].get(growth_quality(quality), 0))


def add_exp(quality, level, cur_exp, gain, cap, can_break=None):
    """加经验并自动升级，止于等级上限。

    返回 (new_level, new_exp, levels_gained, wasted)
    wasted = 满级后溢出丢弃的经验（原包规则：满级不加经验）
    """
    q = growth_quality(quality)
    lv = max(1, int(level or 1))
    exp = max(0, int(cur_exp or 0))
    cap = int(cap or level_cap(q, 0, can_break))
    gain = int(gain or 0)
    if gain <= 0:
        return lv, exp, 0, 0
    if lv >= cap:
        return lv, exp, 0, gain          # 已满级：整笔丢弃

    start = lv
    while gain > 0 and lv < cap:
        need = exp_need(lv, q)
        if need <= 0:
            break
        lack = need - exp
        if gain < lack:
            exp += gain
            gain = 0
            break
        gain -= lack
        lv += 1
        exp = 0
    if lv >= cap:                        # 冲到上限：余量作废
        wasted = gain
        gain = 0
        exp = min(exp, max(0, exp_need(lv, q)))
    else:
        wasted = 0
    return lv, exp, lv - start, wasted


STAGE_HERO_EXP = 80     # 每次通关每个上阵武将获得的经验


def team_exp(player_level=None):
    """通关一关发给**每个上阵武将**的经验。

    【当前口径】用户 2026-10-20 17:09 指定：每次通关固定 +80。
    【留档】原包内没有任何关卡经验配置：
      · LevelConfig.EPAward 812 行全为 0（废弃列）
      · MissionConfig.TeamExp 692 行全为 0（且是任务表）
      · 全库 PropertyName=="Exp" 只出现在 4 个经验符道具上
    唯一形状匹配的曲线是 ExpSetConfig.TeamExp（Lv1~9=12 / Lv40=750 / Lv80=4700），
    用 team_exp_cfg() 可取到，等抓到真服结算包再切回来。
    """
    return STAGE_HERO_EXP


def team_exp_cfg(player_level):
    """ExpSetConfig.TeamExp 曲线（【待核实】原包唯一候选，暂未启用）。"""
    row = _row_at(player_level)
    if not row or row["TeamExp"] <= 0:
        return 12
    return int(row["TeamExp"])


# ---------------------------------------------------------------- 属性/战力

def find_hero_row(hero_id, quality=None):
    """按武将 ID 取配置行；给了 quality 时按 maxStarType 归一到该星级记录。"""
    if not init():
        return None
    row = _hero_cfg.get(int(hero_id))
    if row is None:
        return None
    if quality is None:
        return row
    base = int(_num(row.get("maxStarType"), int(hero_id)))
    return (_hero_by_star.get(base) or {}).get(int(quality)) or row


def max_quality_of(hero_id):
    """该武将能升到的最高星级（maxStarType 分组里最大的 Quality）。"""
    if not init():
        return MAX_QUALITY
    row = _hero_cfg.get(int(hero_id))
    if row is None:
        return MAX_QUALITY
    base = int(_num(row.get("maxStarType"), int(hero_id)))
    qs = list((_hero_by_star.get(base) or {}).keys())
    return max(qs) if qs else MAX_QUALITY


def calc_attrs(hero_row, level=1, grade=0):
    """属性合成：floor(Base × (1 + (等级成长% + 阶成长%)) + Mod)，speed 透传。

    与 WarriorData:implement() 逐行一致（WarriorData.lua:367-381）。
    """
    if not hero_row:
        return {}
    q = int(_num(hero_row.get("Quality")))
    level = max(1, int(level or 1))
    pct = lv_growth_pct(level, q) + grade_growth_pct(q, grade)
    factor = 1.0 + pct / 100.0

    def grow(base_key, mod_key):
        base = _num(hero_row.get(base_key))
        mod = _num(hero_row.get(mod_key))
        return int(math.floor(base * factor + mod))

    return {
        "maxHP": grow("BaseHP", "HPMod"),
        "attack": grow("BaseAttack", "AttackMod"),
        "defence": grow("BaseDefence", "DefenceMod"),
        "speed": int(_num(hero_row.get("BaseSpeed"))),
        "critRate": int(_num(hero_row.get("CritMod"))),
        "counterRate": int(_num(hero_row.get("CounterMod"))),
        "comboRate": int(_num(hero_row.get("ComboMod"))),
        "blockRate": int(_num(hero_row.get("BlockMod"))),
        "hit": int(_num(hero_row.get("Hit"))),
        "miss": int(_num(hero_row.get("Miss"))),
        # 2026-10-28：这三项 HeroConfig 有基础字段（当前全 0），以前没进 stats
        # → 装备/缘分/军旗给它们的加成会被 apply_addition_bonus 丢掉，且 hero_msg
        # 无从下发。纳入统计后固定值/百分比都能正确叠加。
        "tenacity": int(_num(hero_row.get("Tenacity"))),
        "effectResist": int(_num(hero_row.get("EffectResist"))),
        "effectHit": int(_num(hero_row.get("EffectHit"))),
        "critDamage": DEFAULT_CRIT_DAMAGE,
        "quality": q,
        "factorPct": int(round(pct)),
    }


def calc_power(attrs):
    """单武将战力 = floor(Σ 属性 × 权重)。

    注意：属性字典里生命字段叫 `maxHP`，而权重表里叫 `hp` —— 必须做一次映射，
    否则血量那 0.3 的权重会被静默丢掉（曾经的真 bug：曹操6★Lv40 少算 713 战力）。
    """
    if not attrs:
        return 0
    total = 0.0
    for key, w in POWER_WEIGHTS.items():
        v = attrs.get(key)
        if v is None and key == "hp":
            v = attrs.get("maxHP")
        total += (v or 0) * w
    return int(total)


def hero_attrs(hero_id, level=1, grade=0, quality=None):
    """便捷入口：按武将 ID 算属性（含战力）。"""
    row = find_hero_row(hero_id, quality)
    a = calc_attrs(row, level, grade)
    if a:
        a["power"] = calc_power(a)
    return a


def hero_power(hero_id, level=1, grade=0, quality=None):
    return hero_attrs(hero_id, level, grade, quality).get("power", 0)


if __name__ == "__main__":
    ok = init()
    print("配置加载:", ok, " 等级行:", len(_exp_cfg or {}),
          " 武将行:", len(_hero_cfg or {}), " 突破行:", len(_awaken_cfg or {}))
    print("\n[星级上限 / 突破阶梯]")
    for q in range(1, 7):
        print("  %d★ 上限 %2d 级" % (q, star_max_lv(q)))
    print("  6★ 逐次突破:", [level_cap(6, n, True) for n in range(9)])
    for lv in (40, 45, 50, 75):
        print("   6★ Lv%-2d 突破:" % lv, awaken_info(6, lv))

    print("\n[CanBreakLevel 实测] 曹操三行")
    for hid in (10014, 10015, 10016):
        row = find_hero_row(hid)
        print("  id=%d %d★ CanBreakLevel=%s -> 上限 %d"
              % (hid, int(_num(row.get("Quality"))),
                 int(_num(row.get("CanBreakLevel"))),
                 level_cap(int(_num(row.get("Quality"))), 0, can_break_lv(row))))

    print("\n[成长曲线]")
    for lv in (1, 10, 20, 40, 50, 59, 60, 61, 70, 80):
        g1 = lv_growth_pct(lv, 1)
        g4 = lv_growth_pct(lv, 4)
        g6 = lv_growth_pct(lv, 6)
        print("  Lv%-2d 1★=%6.0f%% 4★=%6.0f%% 6★=%6.0f%%  升下一级需经验(6★)=%d"
              % (lv, g1, g4, g6, exp_need(lv, 6)))

    print("\n[升级模拟] 6★曹操 Lv1 exp0，每次 +50 经验")
    lv, exp = 1, 0
    for i in range(3):
        lv, exp, up, waste = add_exp(6, lv, exp, 50, level_cap(6, 0, True))
        print("   第%d次 +50 -> Lv%d exp%d 升%d级 溢出%d" % (i + 1, lv, exp, up, waste))

    print("\n[属性/战力]")
    cases = [
        ("曹操 4★ Lv10 阶0", 10014, 10, 0),
        ("曹操 6★ Lv40 阶0", 10016, 40, 0),
        ("曹操 6★ Lv40 阶9", 10016, 40, 9),
        ("曹操 6★ Lv80 阶9", 10016, 80, 9),
        ("刘备 6★ Lv60 阶0", 10196, 60, 0),
    ]
    for label, hid, lv, g in cases:
        a = hero_attrs(hid, lv, g)
        if not a:
            print("  %-18s 配置缺失 id=%s" % (label, hid))
            continue
        cb = hero_can_break(hid)
        print("  %-18s +%4d%%  HP=%-6d 攻=%-5d 防=%-5d 速=%-3d 战力=%d (上限Lv%d,可突破=%s)"
              % (label, a["factorPct"], a["maxHP"], a["attack"], a["defence"],
                 a["speed"], a["power"], level_cap(a["quality"], 0, cb), cb))
