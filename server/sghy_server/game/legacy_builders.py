"""legacy_builders —— mock 时代字节级 builder 的**只读快照**（黄金测试对照用）。

这些函数用手写 varint/length-delimited 编码直接拼 protobuf 线字节。
它们已被 game/builders_pb.py 的原生 protobuf 实现取代（core.build_* 已委托），
保留在此仅作为 tools/builder_golden_test.py 的参考实现 —— 每个新 builder 的
输出都必须与这里的语义等价（差异仅限下方 DOCUMENTED_DIFFS 里列明的字段错位修正）。

DOCUMENTED_DIFFS（迁移时修正的 mock 时代错位，客户端读取路径已核实）：
  · FateFastenInfo —— mock 写 CharacterData.28（该槽位是 arenaShop，
    客户端把缘分数据当商店数据解析成垃圾）；正确位置是 EnterGame.28
    （UserManager.lua:587 读 result.FateInfo -> fateManager）。
  · honors —— mock 写 CharacterData.39（未知字段，客户端跳过）；
    正确位置是 EnterGame.39（UserManager.lua:599 读 result.honors -> honorManager）。
  · guideIndex —— mock 写 CharacterData.21（该槽位是 bugSignTimes）；
    正确位置是 EnterGame.21（UserManager 读 result.guideIndex -> guideManager）。
  · LoginCharacterData.28 —— 未知字段，直接丢弃（客户端跳过，无影响）。
"""

import time

from . import core as _ctx
from .core import (CHAR_ID, CHAR_NAME, LEVEL_RECORDS, PLAYER_DATA, SERVER_ID, sign_display, pay_display,
                   ep_display,
                   f_bytes, f_int, f_msg, f_str, flog, _tag, _varint, _read_varint)
from .core import (apply_fate_to_stats, equip_additions_for, equip_store,
                   fate_store, find_equip, game_config, growth, hero_attrs,
                   hero_equip_list, team_power)

def _varint(n):
    if n < 0:                       # int64 负数用 64 位补码
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def f_int(field, v):
    """变长整型（int32/int64/bool/enum）"""
    return _tag(field, 0) + _varint(int(v))


def f_bytes(field, b):
    return _tag(field, 2) + _varint(len(b)) + b


def f_str(field, s):
    return f_bytes(field, s.encode("utf-8"))


def f_msg(field, m):
    return f_bytes(field, m)


def build_honors():
    """HonorData 列表：把全部头像(75)与头像框(47)都解锁给这个账号。

    HonorData { required id=1; type=2; count=3; canUse=4; newTip=5 }
    HonorConfig.HonorPart: 0=头像(userHead/*.jpg) 1=头像框(headFrame/border_*)
    """
    heads, frames = game_config.honor_ids()
    body = b""
    for hid in heads + frames:
        h = b""
        h += f_int(1, hid)      # required id
        h += f_int(2, hid)      # type = HonorConfig.ID
        h += f_int(3, 1)        # count
        h += f_int(4, 1)        # canUse = true
        h += f_int(5, 0)        # newTip = false
        body += f_msg(39, h)    # repeated honors（挂在 CharacterData 上）
    return body, heads, frames


def build_head_type():
    """HeadType { type=1; border=2; honorIconFrame=3 } —— 当前佩戴的头像/头像框"""
    heads, frames = game_config.honor_ids()
    p = PLAYER_DATA.get("player") or {}
    m = b""
    m += f_int(1, int(p.get("head_id") or (heads[0] if heads else 50014)))      # type = 头像
    m += f_int(2, int(p.get("head_border") or 1))                                # border
    m += f_int(3, int(p.get("head_frame_id") or (frames[0] if frames else 50000)))  # 头像框
    return m


def build_login_character_data():
    m = b""
    m += f_int(1, CHAR_ID)          # required id
    m += f_str(2, str((PLAYER_DATA.get("player") or {}).get("name") or CHAR_NAME))        # name
    m += f_int(3, 60)               # lv（选角界面显示的玩家等级）
    m += f_int(4, 0)                # HeadBorder
    m += f_int(5, 1)                # Gender
    m += f_int(6, SERVER_ID)        # BirthServer
    m += f_int(7, team_power())     # power（上阵武将战力之和）
    m += f_msg(28, build_fate_info())   # FateInfo（缘分进度）
    return m


def build_login_ok():
    body = b""
    body += f_msg(1, build_login_character_data())   # repeated characters
    return body


def build_character_data():
    """CharacterData：玩家自身数据。除 id 外都可选，但默认 -1 会显示异常，故显式给值。

    关键：`pvePower`(32) / `pvpPower`(33) = **上阵武将战力之和**，
    升星/升阶/升级/缘分/换人后要让客户端同步（推 UpdateCharacter=200）。
    """
    p = PLAYER_DATA.get("player", {})
    tp = team_power()
    m = b""
    m += f_int(1, CHAR_ID)          # required id
    m += f_str(2, str((PLAYER_DATA.get("player") or {}).get("name") or CHAR_NAME))        # name
    m += f_int(3, 1)                # gender
    m += f_int(4, 1)                # type
    m += f_int(5, int(p.get("lv") or 60))       # lv（主公等级）
    m += f_int(6, int(p.get("exp") or 0))       # teamExp
    m += f_int(7, int(p.get("diamond") or 0))   # dollar（元宝）
    m += f_int(8, int(p.get("coin") or 0))      # coin（银币）
    m += f_int(9, 0)                # adminType
    m += f_int(10, int(p.get("vip") or 0))  # vipLv（VIP 权益：强化暴击倍率/概率都看它）
    m += f_int(14, 1)               # selectedFormation
    m += f_int(16, int(p.get("vigour") or 100))  # Vigour（体力）
    m += f_int(17, 0)               # HeroCoin
    # ★ 2026-10-29：体力三件套统一从 ep_display() 取（内部做惰性恢复+上限钳制），
    #   直接读 player["ep"] 可能超上限（老存档 850 > 154），与 pb 侧不一致。
    _epv = ep_display()
    m += f_int(18, int(_epv["ep"]))              # ep
    m += f_int(29, int(_epv["max"]))             # maxEp
    if _epv["next"]:
        m += f_int(30, int(_epv["next"]))        # nextGetEpTime
    m += f_int(19, 0)               # prestige
    # ★ 签到字段（对齐 CharacterData proto：11/12/13/21/22）。
    #   21 是 bugSignTimes（曾误当 guideIndex 发 9999——guideIndex 实在
    #   EnterGame 的 21 槽位，character 里发会污染补签价格基数）
    _py = pay_display()
    m += f_int(25, int(_py["payDollar"]))    # payDollar（VIP 进度条）
    m += f_int(26, int(_py["payTimes"]))     # payTimes
    m += f_int(31, int(_py["payMoney"]))     # payMoney
    m += f_int(40, int(_py["payMoneyOfDay"]))  # payMoneyOfDay
    _sd = sign_display()
    m += f_int(11, int(_sd.get("sign_times") or 0))         # signTimes
    m += f_int(12, int(_sd.get("miss_sign_times") or 0))    # missSignTimes
    m += f_int(13, int(_sd.get("today_sign_times") or 0))   # todaySignTimes
    m += f_int(21, int(_sd.get("bug_sign_times") or 0))     # bugSignTimes
    m += f_int(22, int(_sd.get("sign_id") or 0))            # signId
    m += f_msg(28, build_fate_info())   # FateInfo（缘分进度）
    m += f_int(32, tp)              # pvePower（主界面战力 = 上阵武将战力之和）
    m += f_int(33, tp)              # pvpPower
    m += f_msg(34, build_head_type())   # characterHead：当前佩戴的头像/头像框
    honors, _heads, _frames = build_honors()
    m += honors                         # repeated honors（全部解锁）
    # 购买次数（2026-10-24）：CharacterData.actionTime(27)
    #   {buyCoinTimes=1, buyEpTimes=2, FreeBuyCoinTimes=8, FreeBuyEpTimes=9}
    #   只写这 4 个字段，其余留 proto default -1（客户端 `0 <= x` 判断会跳过）。
    from . import buy as _buy
    _bt = _buy.snapshot()
    _at = b""
    _at += f_int(1, _bt["buyCoinTimes"])
    _at += f_int(2, _bt["buyEpTimes"])
    _at += f_int(8, _bt["freeBuyCoinTimes"])
    _at += f_int(9, _bt["freeBuyEpTimes"])
    m += f_msg(27, _at)
    # 改名卡数量（CharacterData 字段 35）：与 builders_pb 保持一致
    m += f_int(35, int((PLAYER_DATA.get("player") or {}).get("change_name_card") or 0))
    return m


def update_character_body():
    """200 body = **裸 CharacterData**（客户端 socket:decode("CharacterData")）。
    曾包装成 UpdateCharacter{character=...} 导致客户端解出垃圾、coin 不更新。"""
    return build_character_data()


def build_hero_data(hid, htype, lv=1, pos=-1, stats=None, exp_override=None):
    """HeroData：属性按成长曲线实时计算（等级成长 + 阶成长），战力同源。"""
    h = PLAYER_DATA.get("heroes", {}).get(str(hid)) or {}
    grade = int(h.get("grade") or 0)
    awaken = int(h.get("awaken") or 0)
    if stats is None:
        # 统一走 growth：成长曲线吃 exp_set 的 LvGrowth，阶成长吃 (4+品质)×阶
        stats = hero_attrs(htype, lv or h.get("lv") or 1, grade)
        # 缘分加成（永久激活用强化值替换，临时生效用基础值）
        apply_fate_to_stats(stats, h or {"type": htype})
    power = stats.get("power")
    if power is None:
        power = growth.calc_power(stats)
    quality = stats.get("quality", 6)
    can_break = h.get("can_break")
    if can_break is None:
        can_break = growth.hero_can_break(htype, quality)
    max_lv = growth.level_cap(quality, awaken, can_break)
    m = b""
    m += f_int(1, hid)              # required id
    m += f_int(2, htype)            # type（对应 HeroConfig.ID）
    m += f_int(3, stats.get("maxHP", 295))    # maxHP
    m += f_int(4, stats.get("attack", 99))    # attack
    m += f_int(5, stats.get("defence", 51))   # defence
    m += f_int(6, stats.get("speed", 12))     # speed
    m += f_int(7, stats.get("comboRate", 0))  # comboRate
    m += f_int(8, stats.get("blockRate", 0))  # blockRate
    m += f_int(9, stats.get("counterRate", 10))  # counterRate
    m += f_int(10, stats.get("critRate", 0)) # critRate
    m += f_int(11, stats.get("critDamage", 150))  # critDamage
    m += f_int(12, pos)             # position
    m += f_int(13, grade)           # grade（阶）
    m += f_int(14, int(h.get("grade_exp") or 0))   # gradeExp（升阶进度）
    m += f_int(15, lv)              # lv
    m += f_int(16, max_lv)          # maxLv（星级上限 + 5×突破次数，封顶 80）
    m += f_int(17, int(exp_override if exp_override is not None else (h.get("exp") or 0)))  # exp（当前等级已积累经验）
    m += f_int(30, awaken)          # awakenTimes（突破次数）
    m += f_int(33, stats.get("miss", 0))     # miss
    m += f_int(34, stats.get("hit", 0))      # hit
    m += f_int(20, power)           # power（与客户端 WarriorData:power() 同口径）
    m += f_str(21, game_config.get_hero_name(htype))  # name
    return m


def build_addition_data(typ, value, mode=0, index=0, times=0,
                        param=0, param2=0, param3=0, param4=0):
    """AdditionData{type=1, value=2, mode=3, index=4, times=5,
                    param=6, param2=7, param3=8, param4=9}"""
    m = b""
    m += f_int(1, int(typ))
    m += f_int(2, int(value))
    m += f_int(3, int(mode))
    if index:
        m += f_int(4, int(index))
    if times:
        m += f_int(5, int(times))
    if param:
        m += f_int(6, int(param))
    if param2:
        m += f_int(7, int(param2))
    if param3:
        m += f_int(8, int(param3))
    if param4:
        m += f_int(9, int(param4))
    return m


def build_equip_data(eq):
    """EquipData（proto 字段见 reverse_out/07_full_teardown/protocol）"""
    if not eq:
        return b""
    etype = int(eq.get("type") or 0)
    m = b""
    m += f_int(1, int(eq.get("id") or 0))          # required id
    m += f_int(2, etype)                            # type
    # ★ host 必须**总是显式下发**，未穿戴时发 0。
    #   proto 里 `optional int64 host = 3 [default = -1]`，不发就是 -1；
    #   而客户端 getEquipByPart() 用 `e:host() == 0` 判"未穿戴"，
    #   -1 不等于 0 → 武将装备格永远不显示"可装备"提示（真 bug）。
    m += f_int(3, int(eq.get("host") or 0))        # host（0 = 未穿戴）
    m += f_int(4, int(eq.get("strengthen") or 0))   # StrengthenLv
    # finalAdditions：服务端算好的最终加成，客户端直接铺到属性槽
    adds = growth.equip_calc_additions(
        etype, eq.get("strengthen", 0), eq.get("grade", 0))
    # ★ 副属性不并入 finalAdditions（只走 unlockAssistant）：曾 append 进来导致
    #   洗练副属性时装备“本身属性”显示值跟着变（2026-10-22）
    for typ, val, mode in adds:
        m += f_msg(6, build_addition_data(typ, val, mode))
    # unlockAssistant(11)：已解锁/已洗练的副属性（UI 显示用）
    for slot, a in sorted((eq.get("assist") or {}).items(), key=lambda kv: int(kv[0])):
        a = a or {}
        if int(a.get("attr") or 0) <= 0:
            continue
        m += f_msg(11, build_addition_data(
            int(a["attr"]), int(a.get("value") or 0), 0,
            index=int(slot), times=int(a.get("times") or 1),
            param=int(a.get("cost_coin") or 0),
            param2=int(a.get("min") or 0), param3=int(a.get("max") or 0),
            param4=int(a.get("cost_stone") or 0)))
    # lastUnlockAssistant(12)：玩家还没保存的洗练结果
    for slot, a in sorted((eq.get("pending_assist") or {}).items(), key=lambda kv: int(kv[0])):
        a = a or {}
        if int(a.get("attr") or 0) <= 0:
            continue
        m += f_msg(12, build_addition_data(
            int(a["attr"]), int(a.get("value") or 0), 0,
            index=int(slot), times=int(a.get("times") or 1),
            param=int(a.get("cost_coin") or 0),
            param2=int(a.get("min") or 0), param3=int(a.get("max") or 0),
            param4=int(a.get("cost_stone") or 0)))
    m += f_int(8, int(eq.get("en_crit") or 0))      # EnCritCount（本次暴击倍率记录）
    m += f_int(9, int(eq.get("grade") or 0))        # grade（阶数 0~9）
    m += f_int(10, int(eq.get("grade_exp") or 0))   # gradeExp
    return m


def build_fate_info():
    """FateFastenInfo：登录/结算时下发的缘分进度"""
    body = b""
    for fid, rec in (fate_store() or {}).items():
        groups = (rec or {}).get("groups") or {}
        if not groups:
            continue
        rd = b""
        rd += f_int(1, int(fid))            # type = FateConfig.ID
        for g, st in groups.items():
            gd = b""
            gd += f_int(1, int(g))          # group = 条件武将 Group
            gd += f_int(2, int((st or {}).get("exp") or 0))
            gd += f_int(3, int((st or {}).get("complete") or 0))
            rd += f_msg(2, gd)              # repeated groups
        body += f_msg(1, rd)                # repeated fateInfo
    return body


def build_formation_data(fid=1, ftype=1, lv=1):
    m = b""
    m += f_int(1, fid)              # required id
    m += f_int(2, ftype)            # type
    m += f_int(3, lv)               # lv
    return m


def build_enemy_hero_data(hid, etype, pos, name=None, stats=None, scale=100, grade=0):
    """敌方 HeroData：数值来自 LevelSetConfig 的 Real*，再乘 Scale（强度倍率）。"""
    if stats is None:
        stats = game_config.get_hero_stats(etype)
    if name is None:
        name = game_config.get_hero_name(etype)
    f = (scale or 100) / 100.0

    def scaled(*keys, **kw):
        default = kw.get("default", 0)
        for k in keys:
            if k in stats and stats[k] is not None:
                return int(round(float(stats[k]) * f))
        return default

    hp = scaled("maxHP", "hp", default=110)
    atk = scaled("attack", "atk", default=90)
    dfn = scaled("defence", "def", default=10)
    spd = scaled("speed", default=8)
    combo = scaled("comboRate", "combo", default=0)
    block = scaled("blockRate", "block", default=0)
    counter = scaled("counterRate", "counter", default=5)
    crit = scaled("critRate", "crit", default=0)
    crit_dmg = int(stats.get("critDamage", stats.get("crit_damage", 120)) or 120)
    power = growth.calc_power({
        "maxHP": hp, "attack": atk, "defence": dfn, "speed": spd,
        "critRate": crit, "critDamage": crit_dmg, "comboRate": combo,
        "blockRate": block, "counterRate": counter,
        "hit": scaled("hit", default=0), "miss": scaled("miss", default=0),
    })
    m = b""
    m += f_int(1, hid)              # required id
    m += f_int(2, etype)            # type（对应 HeroConfig.ID）
    m += f_int(3, hp)               # maxHP
    m += f_int(4, atk)              # attack
    m += f_int(5, dfn)              # defence
    m += f_int(6, spd)              # speed
    m += f_int(7, combo)            # comboRate
    m += f_int(8, block)            # blockRate
    m += f_int(9, counter)          # counterRate
    m += f_int(10, crit)            # critRate
    m += f_int(11, crit_dmg)        # critDamage
    m += f_int(12, pos)             # position
    m += f_int(13, grade)           # grade
    m += f_int(15, 1)               # lv
    m += f_int(16, 80)              # maxLv（ExpSetConfig 的 Exp 只配了 Lv 1~80，超了会取到空表）
    m += f_int(20, power)           # power
    m += f_str(21, name)            # name
    return m


def build_battle_group_data(gid, formation=1, heros=None):
    """BattleGroupData：LevelBegin.groups 的一个波次/敌方阵容"""
    if heros is None:
        heros = []
    m = b""
    m += f_int(1, gid)              # required id
    m += f_int(2, formation)        # required formation
    for h in heros:
        m += f_msg(3, h)            # repeated heros (HeroData)
    return m


def build_battle_hero_data(hid, pos):
    m = b""
    m += f_int(1, hid)              # required id
    m += f_int(2, pos)              # position
    return m


def build_battle_squad_data(index, heros, power=None):
    if power is None:
        power = team_power()
    m = b""
    m += f_int(1, index)            # index
    for hid, pos in heros:
        m += f_msg(2, build_battle_hero_data(hid, pos))   # repeated heros
    m += f_int(3, power)            # power
    # field4(flag) 不发：python protobuf 省略默认值 0，legacy 也省略——
    # 解码语义等价（flag 默认 0=无军旗），golden 测试以 ignore={4} 记录差异
    return m


def build_battle_gameplay_data(category, squads):
    """BattleGameplayData: category=0 是 levelTeam（关卡队伍）"""
    m = b""
    m += f_int(1, category)
    for idx, heros in squads:
        # ★ squadron.power 必须给真实值：主界面/布阵界面的战力读的就是它
        m += f_msg(2, build_battle_squad_data(idx, heros, power=team_power()))
    return m


def build_squad_power_change(power=None):
    """BattleGameplayPowerChange{category=1, index=2, power=3}

    客户端 `onUpdateSquadPowerHandler` 收到后 `squad.power = result.power` 并
    `EventManager.Send(RefreshSquadPower)` —— 这是主界面/布阵界面战力刷新的唯一通道。
    """
    if power is None:
        power = team_power()
    m = b""
    m += f_int(1, 0)        # category（0 = levelTeam）
    m += f_int(2, 1)        # index
    m += f_int(3, power)    # power
    return m


def build_level_record_data(lid, rec):
    """LevelRecordData：id=1, type=2, star=3, leftTimes=4, buyTimes=5
    type 存关卡 id，客户端 levelManager:updateLevelData 用它查 LevelData"""
    m = b""
    m += f_int(1, lid)
    m += f_int(2, lid)
    m += f_int(3, rec.get("star", 0))
    m += f_int(4, rec.get("leftTimes", 0))
    m += f_int(5, rec.get("buyTimes", 0))
    return m


def build_award_spec(type_id, value=1, name=""):
    """AwardSpecData{required type=1, required name=2, value=3, param=4, extra=5}

    客户端 AwardItem.lua: `data.type > 0` → ObjectAward（物品/装备），
    所以装备 ID 放 type，数量放 value。
    """
    m = b""
    m += f_int(1, int(type_id))
    m += f_str(2, name or "")
    m += f_int(3, int(value or 1))
    return m


def build_award_result(type_name, specs):
    """AwardResult{required type=1(string), repeated datas=2(AwardSpecData)}

    推 301 会让客户端弹 AwardShow 面板（AwardMessageManager:awardHandler）。
    specs = [(type_id, value), ...]
    """
    m = b""
    m += f_str(1, type_name or "")
    for spec in (specs or []):
        tid, val = spec[0], spec[1]
        name = spec[2] if len(spec) > 2 else ""
        m += f_msg(2, build_award_spec(tid, val, name))
    return m


def build_item_data(item_id, count):
    """ItemData{required id=1, type=2, count=3}
    堆叠道具用 item_id 同时当实例 id 和配置 type（背包按 type 合并显示）。"""
    m = b""
    m += f_int(1, int(item_id))     # required id
    m += f_int(2, int(item_id))     # type = ItemConfig.ID
    m += f_int(3, int(count or 0))  # count
    return m


def _activity_data_with_online():
    """ActivityData：lotteryActivityInfo + onlineAwardRecord（M4）。"""
    from .activities import online_award as _oa
    from ..proto import ps_protobuf_pb2 as _pb
    from . import builders_pb as _bpb
    a = _pb.ActivityData()
    a.lotteryActivityInfo.ParseFromString(_bpb.build_lottery_activity_info())
    a.onlineAwardRecord.CopyFrom(_oa.snapshot())
    return a.SerializeToString()


def build_enter_game():
    body = b""
    body += f_msg(1, build_character_data())        # required character
    # 全部武将（从 PLAYER_DATA 读）
    for hero_key, h in PLAYER_DATA.get("heroes", {}).items():
        body += f_msg(2, build_hero_data(h["id"], h["type"], h.get("lv", 1), h.get("position", -1)))
    flog("[EnterGame] 下发武将 %d 个 / 装备 %d 件 / 道具 %d 种" % (
        len(PLAYER_DATA.get("heroes") or {}),
        len(equip_store() or {}),
        len([1 for v in (PLAYER_DATA.get("items") or {}).values() if int(v or 0) > 0])))
    # ★ 装备列表：EnterGame.equips = 字段 5（repeated EquipData）
    #   只下发「可拥有」的装备：品质决定星级区间（Hysg_Refactor OWNABLE_STAR_RANGE）
    for eid, eq in (equip_store() or {}).items():
        if not growth.is_equip_ownable(eq.get("type")):
            continue                       # 配置里有但玩家拿不到的行，不下发
        body += f_msg(5, build_equip_data(eq))
    # ★ 背包道具：EnterGame.items = 字段 4（repeated ItemData）
    for iid, cnt in (PLAYER_DATA.get("items") or {}).items():
        if int(cnt or 0) > 0:
            body += f_msg(4, build_item_data(iid, cnt))
    body += f_msg(6, build_formation_data(1, 1, 1))  # FormationData：阵型1
    # ★ 关键：阵容面板读的是 battleGameplays（EnterGame 字段 37）
    # 取前 5 个 position>0 的武将作为上阵队伍
    on_queue = [(h["id"], h["position"]) for h in PLAYER_DATA.get("heroes", {}).values()
                if h.get("position", -1) > 0]
    on_queue.sort(key=lambda x: x[1])
    body += f_msg(37, build_battle_gameplay_data(0, [(1, on_queue[:5])]))
    # ★ 关卡进度：levelRecords(字段9)
    for lid, rec in LEVEL_RECORDS.items():
        body += f_msg(9, build_level_record_data(lid, rec))
    body += f_int(12, int(time.time()))             # systemTime
    # 邮件（字段 3）：同 builders_pb，从 mail.snapshot 下发
    from . import mail as _mail
    from . import game_config as _gc
    for _m in _mail.to_snapshot():
        _mb = b""
        _mb += f_int(1, int(_m["id"]))                      # id
        _mb += f_str(3, str(_m.get("senderName") or ""))     # senderName
        _mb += f_int(4, int(_m.get("status") or 0))          # status
        _mb += f_int(5, int(_m.get("type") or 1))            # type
        _mb += f_int(6, int(_m.get("sendTime") or 0))        # sendTime
        for _a in (_m.get("award") or []):
            if _a.get("kind") == "property":
                _ab = f_int(1, 0) + f_str(2, str(_a.get("prop") or "")) + f_int(3, int(_a.get("value") or 0))
            else:
                _ab = f_int(1, int(_a.get("type") or 0)) + f_str(2, "") + f_int(3, int(_a.get("count") or 0))
            _mb += f_msg(7, _ab)
        _mb += f_str(8, str(_m.get("title") or ""))
        _mb += f_str(9, str(_m.get("content") or ""))
        body += f_msg(3, _mb)
    # 主线任务（2026-10-25）：missions(7) 当前节点 / completedMission(23) 已领奖节点
    # ★ 2026-10-29：改走 core.mission_snapshot()（它会额外并入每日扫荡令牌任务），
    #   与 builders_pb 同源，否则 golden 测试的 EnterGame 会被判字段不一致。
    _ms, _done = _ctx.mission_snapshot()
    for _m in _ms:
        _mb = b""
        _mb += f_int(1, _m["id"])          # required id
        _mb += f_int(2, _m["type"])
        _mb += f_int(3, _m["need"])
        _mb += f_int(4, _m["complete"])
        _mb += f_int(5, _m["accept"])
        body += f_msg(7, _mb)
    for _t in _done:
        body += f_int(23, _t)
    # 注：activityData(25) 按 DOCUMENTED_DIFFS 设计仅 builders_pb 下发
    # （lottery 折扣 + 在线奖励），legacy 不模仿——黄金测试白名单 {8,11,21,25,28,39,45}
    return body
