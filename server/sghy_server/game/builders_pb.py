"""builders_pb —— 用 protoc 生成类构造回包（替代 mock 的字节级 builder）。

与 legacy_builders.py（黄金测试对照）逐字段对齐，四处字段错位修正见
legacy_builders 模块头的 DOCUMENTED_DIFFS（FateInfo/honors/guideIndex 挪到
EnterGame 的正确槽位、LoginCharacterData 的未知字段丢弃）。

存在语义的细节都保留：
  · AdditionData 的 index/times/param* 只在非零时写（与 legacy 的 `if x:` 一致，
    proto2 下显式赋 0 会带上字段，消息相等性比较会不等）。
  · HonorData.newTip 显式写 0（legacy 也显式写）。
  · 字段书写顺序与 legacy 不同（protobuf 按字段号序列化）——客户端按 tag 解析，
    语义等价；黄金测试按「解析后字段相等」判定。
"""

from ..proto import ps_protobuf_pb2 as pb
from . import core as _ctx


# ---------------------------------------------------------------- 叶子消息

def build_prop_action(prop_id, target_id) -> bytes:
    """PropActionMessage{id=装备, targetID=武将} —— 穿戴(5012)的回包。

    ★ 客户端 ackEquip 按 PropActionMessage 解码并把 targetID 当武将 id 用
    （getWarriorData(targetID)），回错类型（EquipData）会把装备配置 id 当成
    武将 id -> 拿到 nil -> showFatesTip ipairs(nil) 崩（2026-10-21 实证）。
    """
    m = pb.PropActionMessage()
    m.id = int(prop_id)
    m.targetID = int(target_id)
    return m.SerializeToString()


def build_id_message(mid) -> bytes:
    """IDMessage{id} —— 卸下(5013)的回包（客户端 ackUnEquip 按 IDMessage 解码）。"""
    m = pb.IDMessage()
    m.id = int(mid)
    return m.SerializeToString()

def build_item_data(item_id, count, type_id=None) -> bytes:
    """ItemData{id, type, count}。

    普通堆叠道具：id = type（一个 type 只有一条）；
    武将卡/定缘卡：id = 唯一实例 id、type = 卡配置 id、count = 1
    （客户端 canStack=false，按 `_itemList` 每一条画一个卡格）。
    """
    m = pb.ItemData()
    m.id = int(item_id)
    m.type = int(item_id if type_id is None else type_id)
    m.count = int(count or 0)
    return m.SerializeToString()


def build_formation_data(fid=1, ftype=1, lv=1) -> bytes:
    m = pb.FormationData()
    m.id = int(fid)
    m.type = int(ftype)
    m.lv = int(lv)
    return m.SerializeToString()


def build_level_record_data(lid, rec) -> bytes:
    m = pb.LevelRecordData()
    m.id = int(lid)
    m.type = int(lid)              # type 存关卡 id，客户端查 LevelData 用
    m.star = int((rec or {}).get("star", 0))
    m.leftTimes = int((rec or {}).get("leftTimes", 0))
    m.buyTimes = int((rec or {}).get("buyTimes", 0))
    return m.SerializeToString()


def build_addition_data(typ, value, mode=0, index=0, times=0,
                        param=0, param2=0, param3=0, param4=0) -> bytes:
    return addition_msg(typ, value, mode, index, times,
                        param, param2, param3, param4).SerializeToString()


def addition_msg(typ, value, mode=0, index=0, times=0,
                 param=0, param2=0, param3=0, param4=0) -> pb.AdditionData:
    m = pb.AdditionData()
    m.type = int(typ)
    m.value = int(value)
    m.mode = int(mode)
    # 与 legacy 的 `if x:` 一致：非零才写（否则消息相等性比较会因字段存在性不等）
    if index:
        m.index = int(index)
    if times:
        m.times = int(times)
    if param:
        m.param = int(param)
    if param2:
        m.param2 = int(param2)
    if param3:
        m.param3 = int(param3)
    if param4:
        m.param4 = int(param4)
    return m


def build_head_type() -> bytes:
    return head_type_msg().SerializeToString()


def head_type_msg() -> pb.HeadType:
    """当前佩戴的头像/头像框（CharacterData.characterHead）。

    客户端读法：`honorManager:getUserHead()` = head().type、
    `getUserBorder()` = head().honorIconFrame。所以换完头像必须让它跟着存档走，
    否则界面永远显示第一个。
    """
    heads, frames = _ctx.game_config.honor_ids()
    p = _ctx.PLAYER_DATA.get("player") or {}
    m = pb.HeadType()
    m.type = int(p.get("head_id") or (heads[0] if heads else 50014))
    m.border = int(p.get("head_border") or 1)
    m.honorIconFrame = int(p.get("head_frame_id") or (frames[0] if frames else 50000))
    return m


def build_honors():
    """返回 (HonorData 消息列表, heads, frames)。由 EnterGame.honors 挂载。"""
    heads, frames = _ctx.game_config.honor_ids()
    msgs = []
    for hid in heads + frames:
        h = pb.HonorData()
        h.id = hid
        h.type = hid
        h.count = 1
        h.canUse = 1
        h.newTip = 0
        msgs.append(h)
    return msgs, heads, frames


def build_award_spec(type_id, value=1, name="") -> bytes:
    m = pb.AwardSpecData()
    m.type = int(type_id)
    m.name = name or ""
    # ★ 2026-10-28 修：value 超 int32（如邮件后台发 100 亿铜币）时
    #   ParseFromString/赋值直接 ValueError，把 EnterGame 整个炸掉、客户端
    #   卡死登录（用户实测）。钳制到 int32 上限，超限打警告。
    _v = int(value or 1)
    if _v > 2147483647:
        print("[builders] !! award value %d 超 int32，钳制到 2147483647 (%s)" % (_v, name))
        _v = 2147483647
    m.value = max(1, _v)
    return m.SerializeToString()


# ---------------------------------------------------------------- 抽卡

def lottery_record_data(rec, now=None) -> pb.LotteryRecordData:
    """LotteryRecordData：nextFreeTime 是**剩余秒数**（客户端 CountDown:start(seconds)）。"""
    import time as _time
    if now is None:
        now = _time.time()
    m = pb.LotteryRecordData()
    m.id = int(rec.get("id") or rec.get("type") or 0)
    m.type = int(rec.get("type") or 0)
    nf = int(rec.get("next_free") or 0)
    m.nextFreeTime = max(0, int(round(nf - now))) if nf > now else 0
    m.totalTimes = int(rec.get("total_times") or 0)
    m.discount = int(rec.get("discount") or 100)
    m.discountEndTime = int(rec.get("discount_end") or 0)
    m.discountLeftTimes = int(rec.get("discount_left") or 0)
    return m


def build_lottery_records(records) -> bytes:
    """LotteryRecordDatas —— 5041 响应 / EnterGame.lotteryRecords 共用。"""
    m = pb.LotteryRecordDatas()
    for rec in (records or []):
        m.lotteryRecords.add().CopyFrom(lottery_record_data(rec))
    return m.SerializeToString()


def build_all_hero_message(items) -> bytes:
    """AllHeroMessage{list:[{key=404, heroData}]} —— 招募结果。

    ★ key 必须是 404(NewHero)：客户端 applyAllHeroMessageItem 只有 key==404
      才走 handleNewWarriorData（入列表 + 触发 newWarrior 回调）；用 453 会
      导致 newWarriors 为空、结果面板崩。
    """
    m = pb.AllHeroMessage()
    for key, hero_bytes in (items or []):
        it = m.list.add()
        it.key = int(key)
        it.heroData.ParseFromString(hero_bytes)
    return m.SerializeToString()


def build_lottery_activity_info() -> bytes:
    """LotteryActivityInfo —— 5318 推送（折扣活动）。"""
    info = _ctx.lottery_discount_info() or {}
    m = pb.LotteryActivityInfo()
    m.discount = int(info.get("discount") or 100)
    m.discountTimes = int(info.get("times") or 0)
    m.isStart = 1 if int(info.get("is_start") or 0) else 0
    m.type = int(info.get("type") or 0)
    m.leftTimes = int(info.get("left") or 0)
    return m.SerializeToString()


# ---------------------------------------------------------------- 图鉴 / 收集

def hero_record_msg(hero_type, record_id=None) -> pb.HeroRecordData:
    m = pb.HeroRecordData()
    m.id = int(record_id if record_id is not None else hero_type)
    m.type = int(hero_type)
    return m


def build_hero_record(hero_type, record_id=None) -> bytes:
    """560 NewHeroRecordRt —— 武将图鉴点亮。"""
    return hero_record_msg(hero_type, record_id).SerializeToString()


def equip_record_msg(equip_type, record_id=None) -> pb.EquipRecordData:
    m = pb.EquipRecordData()
    m.id = int(record_id if record_id is not None else equip_type)
    m.type = int(equip_type)
    return m


def build_equip_record(equip_type, record_id=None) -> bytes:
    """565 NewEquipRecordRt —— 装备图鉴点亮。"""
    return equip_record_msg(equip_type, record_id).SerializeToString()


def collection_award_record_msg(collection_type, sub_type, milestones) -> pb.CollectionAwardRecordData:
    m = pb.CollectionAwardRecordData()
    m.type = int(collection_type)
    m.subType = int(sub_type)
    for ms in sorted(set(int(x) for x in (milestones or []))):
        m.receivedMilestoneCounts.append(ms)
    return m


def build_collection_award_record(collection_type, sub_type, milestones) -> bytes:
    """10115 领取回包 / EnterGame.collectionAwardRecords 共用。"""
    return collection_award_record_msg(collection_type, sub_type, milestones).SerializeToString()



def mail_data_msg(m) -> pb.MailData:
    """MailData（300 新邮件 / EnterGame.mails）。

    客户端 `MailData:setSvrMail` 把 `award` 读成 bonusItems（领取弹窗显示的奖励），
    所以道具类必须 `{type=道具id, name="", value=数量}`、
    属性类必须 `{type=0, name="Dollar"/"Coin", value=数量}` —— name 写错界面就是空白格。
    """
    d = pb.MailData()
    d.id = int(m["id"])
    d.senderId = 0
    d.senderName = str(m.get("senderName") or "系统")
    d.status = int(m.get("status") or 0)
    # 类型只由有无附件决定（唯一出包口，300 推送和 EnterGame 都过这里）：
    #   无附件 → Notice(0)，客户端显示「查看详情」/ 详情按钮「确定」
    #   有附件 → Award(1)，客户端显示「领取奖励」
    # 客户端 MailEntry 对 Award 恒画「领取奖励」角标、不看附件内容，
    # 所以"纯文本 + Award"必然被渲染成可领取，必须在这里兜住。
    # ★ 唯一例外：月卡/周卡每日奖励按 ShopConfig.MailType 存成 Card1(2)/Card2(3)
    #   （客户端对这两种同样画「领取奖励」）；无附件时仍回落 Notice。
    _awards = m.get("award") or []
    _mt = int(m.get("type") or 0)
    if _awards and _mt in (2, 3):
        d.type = _mt
    else:
        d.type = (pb.MailData.MailType.Value("Award") if _awards
                  else pb.MailData.MailType.Value("Notice"))
    d.sendTime = int(m.get("sendTime") or 0)
    d.title = str(m.get("title") or "")
    d.content = str(m.get("content") or "")
    for a in (m.get("award") or []):
        if a.get("kind") == "property":
            d.award.add().ParseFromString(build_award_spec(
                0, int(a.get("value") or 0), str(a.get("prop") or "")))
        else:
            d.award.add().ParseFromString(build_award_spec(
                int(a.get("type") or 0), int(a.get("count") or 0), ""))
    return d


def buy_prop_record_msg(rec) -> pb.BuyPropRecordData:
    """BuyPropRecordData：590 NewBuyPropRecord / 591 UpdateBuyPropRecord /
    EnterGame.buyPropRecordDatas(20) 共用。

    客户端 UserManager 用 type=ShopConfig.ID 匹配记录，endDate 是**剩余天数**
    （PayView 直接当时长显示，LevelManager 用 endDate>0 判月卡特权），
    totalCount 是累计购买次数。四个字段都是 required，必须全部赋值。
    """
    m = pb.BuyPropRecordData()
    m.id = int(rec.get("id") or 0)
    m.type = int(rec.get("type") or 0)
    m.totalCount = int(rec.get("totalCount") or 0)
    m.endDate = int(rec.get("endDate") or 0)
    return m


def mission_data_msg(m) -> pb.MissionData:
    """MissionData：501 NewMission / 502 UpdateMission 的 body。

    客户端 `MissionData:setData(needValue, completeValue, acceptDate, missionStatus)`，
    `isCompleted()` 对非七日任务判 `needValue <= completeValue`。
    主线 needValue=1（TargetValue），cleared 时 completeValue=1。
    """
    d = pb.MissionData()
    d.id = int(m["id"])            # 实例 id（主线直接用配置 id）
    d.type = int(m["type"])        # 配置 id
    d.needValue = int(m.get("need") or 1)
    d.completeValue = int(m.get("complete") or 0)
    d.acceptDate = int(m.get("accept") or 0)
    # missionStatus 不写（proto default -1）：客户端只把它存起来，判定用 needValue/completeValue，
    # 显式写 0 会造成与 legacy 字节对照的假差异。
    return d


def build_mission_data(m) -> bytes:
    return mission_data_msg(m).SerializeToString()


def build_mission_success(mid, mtype, award_body) -> bytes:
    """504 MissionSuccess{id, type, result} —— 领奖结果，客户端弹奖励窗。

    award_body 是 AwardResult 的序列化（build_award_result 产出）。
    """
    msg = pb.MissionSuccess()
    msg.id = int(mid)
    msg.type = int(mtype)
    msg.result.ParseFromString(award_body)
    return msg.SerializeToString()


def build_award_result(type_name, specs) -> bytes:
    """specs 元素 (type_id, value) 或 (type_id, value, name)——name 供
    type=0 的属性类显示（"Dollar"/"Coin"）。"""
    m = pb.AwardResult()
    m.type = type_name or ""
    for spec in (specs or []):
        tid, val = spec[0], spec[1]
        name = spec[2] if len(spec) > 2 else ""
        m.datas.add().ParseFromString(build_award_spec(tid, val, name))
    return m.SerializeToString()


# ---------------------------------------------------------------- 武将 / 装备

def hero_msg(hid, htype, lv=1, pos=-1, stats=None, exp_override=None,
             extra_bonuses=None) -> pb.HeroData:
    """HeroData：属性按成长曲线实时计算（等级成长 + 阶成长），战力同源。

    extra_bonuses：编队级加成（军旗词条）[(AdditionType, value, mode)]，
    只在服务端自算属性时（stats=None）叠加；敌方/显式 stats 不叠。
    """
    h = _ctx.PLAYER_DATA.get("heroes", {}).get(str(hid)) or {}
    grade = int(h.get("grade") or 0)
    awaken = int(h.get("awaken") or 0)
    if stats is None:
        stats = _ctx.hero_attrs(htype, lv or h.get("lv") or 1, grade)
        # 编队军旗加成（词条固定值 + 套装百分比）只给**已上阵**武将（存档 position>0）。
        # 用存档 position 判，不用入参 pos —— 有些调用传 0/-1 只表示"不在编队"。
        # ★ 2026-10-27 改按武将取：部件词条全体生效；套装（Additions+AddPowerPer）
        #   带势力限定（蜀汉/曹魏/东吴/群雄）只对该势力武将生效。
        # ★ 全部加成必须**一次性**交给 apply_fate_to_stats：分两次算百分比会变复利。
        _bon = []
        if int(h.get("position") or 0) > 0:
            _bon.extend(_ctx.ensign.squad_flag_bonuses_for_hero(htype) or [])
        if extra_bonuses:
            _bon.extend(extra_bonuses)
        _ctx.apply_fate_to_stats(stats, h or {"type": htype}, _bon or None)
    power = stats.get("power")
    if power is None:
        power = _ctx.growth.calc_power(stats)
    quality = stats.get("quality", 6)
    # ★ 2026-10-28：can_break 以配置为准（客户端 canBreakLv() 也是读配置）。
    #   以前优先信存档里的标志，存档过期时 maxLv 就算错（无双吕布突破后仍显示 40）。
    can_break = _ctx.growth.hero_can_break(htype)
    if can_break is None:
        can_break = h.get("can_break")
    max_lv = _ctx.growth.level_cap(quality, awaken, can_break)

    m = pb.HeroData()
    m.id = int(hid)
    m.type = int(htype)
    m.maxHP = int(stats.get("maxHP", 295))
    m.attack = int(stats.get("attack", 99))
    m.defence = int(stats.get("defence", 51))
    m.speed = int(stats.get("speed", 12))
    m.comboRate = int(stats.get("comboRate", 0))
    m.blockRate = int(stats.get("blockRate", 0))
    m.counterRate = int(stats.get("counterRate", 10))
    m.critRate = int(stats.get("critRate", 0))
    m.critDamage = int(stats.get("critDamage", 150))
    m.position = int(pos)
    m.grade = grade
    m.gradeExp = int(h.get("grade_exp") or 0)
    m.lv = int(lv)
    m.maxLv = int(max_lv)
    m.exp = int(exp_override if exp_override is not None else (h.get("exp") or 0))
    m.power = int(power)
    m.name = _ctx.game_config.get_hero_name(htype) or ""
    m.awakenTimes = awaken
    m.miss = int(stats.get("miss", 0))
    m.hit = int(stats.get("hit", 0))
    # ★ 2026-10-28 补齐特殊属性（HeroData 22-29/32/35/36）：
    #   客户端 WarriorManager 会读这些字段（damageIncrease→warrior:damageIncrease…），
    #   以前服务端从不赋值 → 装备副属性洗出的「增伤/减伤/治疗/范围/技能伤害/韧性」
    #   在武将面板不显示、战斗入场也拿不到（用户报障：穿戴后增伤不进面板）。
    m.damageIncrease = int(stats.get("finalDamage", 0))          # 增伤
    m.damageDecrease = int(stats.get("damageReduce", 0))         # 减伤
    m.cureIncrease = int(stats.get("cureIncrease", 0))           # 治疗加成
    m.cureDecrease = int(stats.get("cureDecrease", 0))           # 治疗削弱
    m.AOEIncrease = int(stats.get("aoeIncrease", 0))             # 范围伤害加成
    m.AOEDecrease = int(stats.get("aoeDecrease", 0))             # 范围伤害减免
    m.skillDamageIncrease = int(stats.get("skillIncrease", 0))   # 技能伤害加成
    m.skillDamageDecrease = int(stats.get("skillDecrease", 0))   # 技能伤害减免
    m.tenacity = int(stats.get("tenacity", 0))                   # 韧性
    m.effectResist = int(stats.get("effectResist", 0))           # 效果抵抗
    m.effectHit = int(stats.get("effectHit", 0))                 # 效果命中
    return m


def build_hero_data(hid, htype, lv=1, pos=-1, stats=None, exp_override=None,
                    extra_bonuses=None) -> bytes:
    return hero_msg(hid, htype, lv, pos, stats, exp_override,
                    extra_bonuses).SerializeToString()


def equip_msg(eq) -> pb.EquipData:
    if not eq:
        # ★ 兜底必须给合法消息：EquipData.id 是 required，空消息序列化直接
        #   EncodeError 炸掉 dispatch → 客户端收不到任何回包（2026-10-22
        #   装备升阶拒绝路径事故）。id=0 显式赋值满足 required 语义。
        m = pb.EquipData()
        m.id = 0
        return m
    etype = int(eq.get("type") or 0)
    m = pb.EquipData()
    m.id = int(eq.get("id") or 0)
    m.type = etype
    # host 必须总是显式下发（0 = 未穿戴）：客户端 `e:host() == 0` 判未穿戴
    m.host = int(eq.get("host") or 0)
    m.StrengthenLv = int(eq.get("strengthen") or 0)
    adds = _ctx.growth.equip_calc_additions(
        etype, eq.get("strengthen", 0), eq.get("grade", 0))
    # ★ 副属性(unlockAssistant)绝不能并入 finalAdditions：客户端装备详情/图标
    #   的“攻击 +N”由 finalAdditions 累加而来 —— 曾把 assist 也append 进来，
    #   洗练一次副属性，装备【本身属性】的显示值就跟着变（2026-10-22 用户报障：
    #   “洗练副属性把装备本身的属性值改了”，实际存档 type/strengthen 未动，
    #   纯显示层聚合错误）。副属性只走 unlockAssistant / lastUnlockAssistant。
    for typ, val, mode in adds:
        m.finalAdditions.add().ParseFromString(build_addition_data(typ, val, mode))
    for slot, a in sorted((eq.get("assist") or {}).items(), key=lambda kv: int(kv[0])):
        a = a or {}
        if int(a.get("attr") or 0) <= 0:
            continue
        m.unlockAssistant.add().ParseFromString(build_addition_data(
            int(a["attr"]), int(a.get("value") or 0), 0,
            index=int(slot), times=int(a.get("times") or 1),
            param=int(a.get("cost_coin") or 0),
            param2=int(a.get("min") or 0), param3=int(a.get("max") or 0),
            param4=int(a.get("cost_stone") or 0)))
    for slot, a in sorted((eq.get("pending_assist") or {}).items(), key=lambda kv: int(kv[0])):
        a = a or {}
        if int(a.get("attr") or 0) <= 0:
            continue
        m.lastUnlockAssistant.add().ParseFromString(build_addition_data(
            int(a["attr"]), int(a.get("value") or 0), 0,
            index=int(slot), times=int(a.get("times") or 1),
            param=int(a.get("cost_coin") or 0),
            param2=int(a.get("min") or 0), param3=int(a.get("max") or 0),
            param4=int(a.get("cost_stone") or 0)))
    m.EnCritCount = int(eq.get("en_crit") or 0)
    m.grade = int(eq.get("grade") or 0)
    m.gradeExp = int(eq.get("grade_exp") or 0)
    return m


def build_equip_data(eq) -> bytes:
    return equip_msg(eq).SerializeToString()


def enemy_hero_msg(hid, etype, pos, name=None, stats=None, scale=100, grade=0,
                   lv=1) -> pb.HeroData:
    """敌方 HeroData：数值来自配置 Real* 再乘 Scale。

    lv = LevelSetConfig.ShowLevel（敌人头顶显示等级；配置 1~80 随关卡递增）。
    之前写死 1，导致所有关卡敌人都显示 1 级（2026-10-24 用户报障）。
    """
    if stats is None:
        stats = _ctx.game_config.get_hero_stats(etype)
    if name is None:
        name = _ctx.game_config.get_hero_name(etype)
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
    power = _ctx.growth.calc_power({
        "maxHP": hp, "attack": atk, "defence": dfn, "speed": spd,
        "critRate": crit, "critDamage": crit_dmg, "comboRate": combo,
        "blockRate": block, "counterRate": counter,
        "hit": scaled("hit", default=0), "miss": scaled("miss", default=0),
    })
    m = pb.HeroData()
    m.id = int(hid)
    m.type = int(etype)
    m.maxHP = hp
    m.attack = atk
    m.defence = dfn
    m.speed = spd
    m.comboRate = combo
    m.blockRate = block
    m.counterRate = counter
    m.critRate = crit
    m.critDamage = crit_dmg
    m.position = int(pos)
    m.grade = int(grade)
    m.lv = max(1, int(lv or 1))
    m.maxLv = 80          # ExpSetConfig 的 Exp 只配了 Lv 1~80
    m.power = power
    m.name = name or ""
    # 增减伤 / 抗性 / 命中：LevelSetConfig 的 Real* 列，proto2 下只在非零时写
    for field, key in (("damageIncrease", "damage_increase"),
                       ("damageDecrease", "damage_decrease"),
                       ("tenacity", "tenacity"),
                       ("hit", "hit"),
                       ("miss", "miss")):
        val = int(stats.get(key) or 0)
        if val:
            setattr(m, field, val)
    if int(stats.get("is_devil") or 0):
        m.IsDevil = True
    return m


def build_enemy_hero_data(hid, etype, pos, name=None, stats=None, scale=100, grade=0,
                          lv=1) -> bytes:
    return enemy_hero_msg(hid, etype, pos, name, stats, scale, grade,
                          lv).SerializeToString()


# ---------------------------------------------------------------- 缘分 / 头像

def fate_info_msg() -> pb.FateFastenInfo:
    m = pb.FateFastenInfo()
    for fid, rec in (_ctx.fate_store() or {}).items():
        groups = (rec or {}).get("groups") or {}
        if not groups:
            continue
        rd = m.fateInfo.add()
        rd.type = int(fid)
        for g, st in groups.items():
            gd = rd.groups.add()
            gd.group = int(g)
            gd.exp = int((st or {}).get("exp") or 0)
            gd.Complete = int((st or {}).get("complete") or 0)
    return m


def build_fate_info() -> bytes:
    return fate_info_msg().SerializeToString()


# ---------------------------------------------------------------- 角色 / 登录

def login_character_msg(info: dict | None = None) -> pb.LoginCharacterData:
    """LoginCharacterData：登录时角色列表的一项。

    info: {"id","name","lv","power"}——账号隔离后按库中真实角色下发；
    不传（黄金测试/旧行为）→ 用当前全局 CHAR_ID/名字/lv=60 的旧口径。
    """
    m = pb.LoginCharacterData()
    if info:
        m.id = int(info.get("id") or 0)
        m.name = str(info.get("name") or "")
        m.lv = int(info.get("lv") or 1)
        m.power = int(info.get("power") or 0)
    else:
        m.id = _ctx.CHAR_ID
        m.name = _ctx.char_name()
        m.lv = 60
        m.power = _ctx.team_power()
    m.HeadBorder = 0
    m.Gender = 1
    m.BirthServer = _ctx.SERVER_ID
    return m


def build_login_character_data(info: dict | None = None) -> bytes:
    return login_character_msg(info).SerializeToString()


def build_login_ok(characters: list | None = None) -> bytes:
    """LoginOK：characters 为角色列表（账号隔离）；None = 旧行为单角色（黄金测试用）。

    ★ 空列表 [] 是合法输入（新账号 0 角色 → 客户端进建号场景），
      必须 `is not None` 判断——`if characters:` 会把空表吞成旧行为（实测踩过）。
    """
    m = pb.LoginOK()
    if characters is None:
        m.characters.add().ParseFromString(build_login_character_data())
    else:
        for info in characters:
            m.characters.add().ParseFromString(build_login_character_data(info))
    return m.SerializeToString()


def build_create_character_ok(info: dict) -> bytes:
    """CreateCharacterOK{id,name,gender,type,lv,teamExp}。

    客户端 onCreateCharacterOK 实读：只取 result.id → userManager:userLogin(id)
    → 自己发 10001 SelectCharacter。其余字段按 proto required 补齐。
    """
    m = pb.CreateCharacterOK()
    m.id = int(info.get("id") or 0)
    m.name = str(info.get("name") or "")
    m.gender = int(info.get("gender") or 1)
    m.type = int(info.get("type") or 1)
    m.lv = int(info.get("lv") or 1)
    m.teamExp = int(info.get("exp") or 0)
    return m.SerializeToString()


def character_msg() -> pb.CharacterData:
    """CharacterData：玩家自身数据。

    注意三处字段位置修正（见 legacy_builders.DOCUMENTED_DIFFS）：
    guideIndex / FateInfo / honors 不在此处下发，改由 EnterGame 的
    21 / 28 / 39 槽位下发 —— 那才是客户端的真实读取路径。
    """
    p = _ctx.PLAYER_DATA.get("player", {})
    tp = _ctx.team_power()
    m = pb.CharacterData()
    m.id = _ctx.CHAR_ID
    m.name = _ctx.char_name()
    m.gender = 1
    m.type = 1
    m.lv = int(p.get("lv") or 60)
    m.teamExp = int(p.get("exp") or 0)
    m.dollar = int(p.get("diamond") or 0)
    m.coin = int(p.get("coin") or 0)
    m.adminType = 0
    m.vipLv = int(p.get("vip") or 0)
    m.selectedFormation = 1
    m.Vigour = int(p.get("vigour") or 100)
    m.HeroCoin = 0
    # 体力上限/下次恢复时间（2026-10-29）：客户端 User:getMaxEp 读 maxEp(29)，
    # 主界面体力条与倒计时读 nextGetEpTime(30)。不下发时上限取不到 → 显示 0/30。
    # ★ 18/29/30 统一从 ep_display() 取：它内部会做惰性恢复 + 上限钳制，
    #   直接读 player["ep"] 可能超上限（老存档 850 > 154），与 legacy 侧不一致。
    _epv = _ctx.ep_display()
    m.ep = int(_epv["ep"])
    m.maxEp = int(_epv["max"])
    if _epv["next"]:
        m.nextGetEpTime = int(_epv["next"])
    m.prestige = 0
    m.pvePower = tp
    m.pvpPower = tp
    # 充值字段（25/26/31/40）：VIP 进度条区间端点，缺失会致 setScaleX 负数反向
    py = _ctx.pay_display()
    m.payDollar = int(py["payDollar"])
    m.payTimes = int(py["payTimes"])
    m.payMoney = int(py["payMoney"])
    m.payMoneyOfDay = int(py["payMoneyOfDay"])
    # 签到字段（CharacterData 11/12/13/21/22）：客户端 SignInPanel/updateMyData 读
    sd = _ctx.sign_display()
    m.signTimes = int(sd.get("sign_times") or 0)
    m.missSignTimes = int(sd.get("miss_sign_times") or 0)
    m.todaySignTimes = int(sd.get("today_sign_times") or 0)
    m.bugSignTimes = int(sd.get("bug_sign_times") or 0)
    m.signId = int(sd.get("sign_id") or 0)
    m.characterHead.CopyFrom(head_type_msg())
    # 购买体力/铜钱次数（CharacterData.actionTime 字段 27，2026-10-24）：
    # 客户端 PlayerInfoPanel 用 `buyEpTimes < VIPSetConfig[vip].BuyEpTimes or freeBuyEpTimes > 0`
    # 判断能否买，服务端不下发时这两个值是 nil → 比较直接报错。
    # 只发这 4 个字段；其余（leftTherionTimes 等）留默认 -1，客户端 `0 <= x` 判断会跳过。
    _bt = _ctx.buy_snapshot()
    m.actionTime.buyEpTimes = int(_bt["buyEpTimes"])
    m.actionTime.buyCoinTimes = int(_bt["buyCoinTimes"])
    m.actionTime.FreeBuyEpTimes = int(_bt["freeBuyEpTimes"])
    m.actionTime.FreeBuyCoinTimes = int(_bt["freeBuyCoinTimes"])
    # 改名卡数量（CharacterData 字段 35）：客户端 PlayerInfoPanel 的「改名」按钮
    # 点下去先判 `me:changeNameCard() > 0`，为 0 直接提示"改名卡道具不足"不进改名面板。
    m.changeNameCard = int(_ctx.name_card_count())
    # 挑战次数（2026-10-29）：CharacterData 只有 actionTime.leftTherionTimes(27.3)，
    # 客户端 User.lua:510 `if 0 <= val.leftTherionTimes` 才会刷新挑战面板剩余次数。
    # 默认 -1 会被跳过，所以必须真发 0（= 今日已用完）。
    # ★ ActivityTherionTimes / ActivityCoinLevelTimes 在 **EnterGame**（29/30），
    #   不在 CharacterData —— 见 build_enter_game。
    try:
        from . import challenge as _ch
        _cs = _ch.snapshot()
        m.actionTime.leftTherionTimes = int(_cs["therion_left"])
        m.actionTime.buyTherionTimes = 0
    except Exception:
        pass
    return m


def build_character_data() -> bytes:
    return character_msg().SerializeToString()


def update_character_body() -> bytes:
    # 累计消耗活动：200 推送是元宝余额变化的必经点，先做余额差值同步（2026-10-23）
    try:
        from .activities import grade_pay as _gp
        _gp.sync_dollar_consume()
    except Exception:
        pass
    # ★ 回包类型以客户端 decode 为准（同 5012 教训）：客户端 onCharacterValue
    #   解的是**裸 CharacterData**（socket:decode("CharacterData", msg)），
    #   全客户端零处解 UpdateCharacter 外壳。曾经包一层
    #   UpdateCharacter{character=...} 导致解出垃圾（只剩乱码 id、coin=nil
    #   被 updateMyData 跳过）——「铜币用后界面不刷新」的根因（2026-10-22）。
    return build_character_data()


# ---------------------------------------------------------------- 战斗编队

def build_battle_hero_data(hid, pos) -> bytes:
    m = pb.BattleHeroData()
    m.id = int(hid)
    m.position = int(pos)
    return m.SerializeToString()


def build_battle_squad_data(index, heros, power=None, flag=0,
                            always_flag=False) -> bytes:
    """always_flag=False 时 flag=0 不序列化（与 legacy 黄金等价）；
    5353/5354 的编队回包传 True（下阵时 flag=0 也必须显式下发）。"""
    if power is None:
        power = _ctx.team_power()
    m = pb.BattleGameplaySquadData()
    m.index = int(index)
    for hid, pos in heros:
        m.heros.add().ParseFromString(build_battle_hero_data(hid, pos))
    m.power = int(power)
    if always_flag or int(flag or 0):
        m.flag = int(flag or 0)   # 上阵军旗 id（军旗系统 2026-10-23）
    return m.SerializeToString()


def build_battle_gameplay_data(category, squads, flags=None) -> bytes:
    """squads: [(index, heros)]；flags: {index: flag_id}（缺省 0）。"""
    m = pb.BattleGameplayData()
    m.category = int(category)
    for idx, heros in squads:
        m.squads.add().ParseFromString(build_battle_squad_data(
            idx, heros, power=_ctx.team_power(),
            flag=int((flags or {}).get(idx, 0))))
    return m.SerializeToString()


def build_battle_group_data(gid, formation=1, heros=None) -> bytes:
    if heros is None:
        heros = []
    m = pb.BattleGroupData()
    m.id = int(gid)
    m.formation = int(formation)
    for h in heros:
        m.heros.add().ParseFromString(h)
    return m.SerializeToString()


def build_squad_power_change(power=None) -> bytes:
    if power is None:
        power = _ctx.team_power()
    m = pb.BattleGameplayPowerChange()
    m.category = 0
    m.index = 1
    m.power = int(power)
    return m.SerializeToString()


def build_coin_crit_info(rate, coin, rate_coin) -> bytes:
    """10053 CoinCritRateInfo —— 买铜钱暴击通知。

    客户端 `UserManager:onCoinBaoJiHandler` 解 rateCoin / rate，
    交给 `PlayerInfoPanel:coinBaoJiEffect(rateCoin, rate)` 播暴击特效。
    · rate     = 暴击倍率（2 = 双倍）
    · coin     = 基础铜钱（未暴击时给多少）
    · rateCoin = 暴击后实际到账铜钱
    """
    m = pb.CoinCritRateInfo()
    m.rate = int(rate)
    m.coin = int(coin)
    m.rateCoin = int(rate_coin)
    return m.SerializeToString()


def build_buy_character_body() -> bytes:
    """10050/10051 的回包 —— **裸 CharacterData**。

    ★ 客户端 ackBuyCoin/ackBuyEp 都走 `UserManager:onCharacterValue(msg)`，
    解的就是 CharacterData；包成 UpdateCharacter{character=...} 会解出垃圾。
    """
    return character_msg().SerializeToString()


# ---------------------------------------------------------------- 军旗
def flag_msg(flag) -> pb.FlagData:
    m = pb.FlagData()
    m.id = int((flag or {}).get("id") or 0)
    m.index = int((flag or {}).get("index") or 0)
    return m


def build_flag_data(flag) -> bytes:
    return flag_msg(flag).SerializeToString()


def flag_part_addition_msg(a) -> pb.FlagPartAdditionData:
    m = pb.FlagPartAdditionData()
    m.type = int((a or {}).get("type") or 0)
    m.value = int((a or {}).get("value") or 0)
    m.mode = int((a or {}).get("mode") or 0)
    m.Index = int((a or {}).get("index") or 0)
    m.maxValue = int((a or {}).get("maxValue") or 0)
    m.unlock = bool((a or {}).get("unlock"))
    m.baseValue = int((a or {}).get("baseValue") or 0)
    return m


def flag_part_msg(part) -> pb.FlagPartData:
    m = pb.FlagPartData()
    m.id = int((part or {}).get("id") or 0)
    m.type = int((part or {}).get("type") or 0)
    m.host = int((part or {}).get("host") or 0)      # 0=未装备，恒下发
    m.unlockCount = int((part or {}).get("unlock_count") or 0)
    m.upCount = int((part or {}).get("up_count") or 0)
    for a in ((part or {}).get("additions") or []):
        m.unlockAssistants.add().CopyFrom(flag_part_addition_msg(a))
    return m


def build_flag_part_data(part) -> bytes:
    return flag_part_msg(part).SerializeToString()


def warbook_record_msg(data=None) -> pb.WarBookRecordData:
    wb = _ctx.ensign_warbook(data)
    m = pb.WarBookRecordData()
    m.mapID = int(wb.get("map_id") or 0)
    m.lv = int(wb.get("lv") or 0)
    m.startDrawState = int(wb.get("start_draw_state") or 0)
    m.lvExp = int(wb.get("lv_exp") or 0)
    m.addBagFlagEffect = int(wb.get("add_bag") or 0)
    m.addPerFlagEffect = int(wb.get("add_per") or 0)
    m.reduceCostFlagEffect = int(wb.get("reduce_cost") or 0)
    m.currentevent = int(wb.get("current_event") or 0)
    m.divinationCount = int(wb.get("divination_count") or 0)
    for t in (wb.get("war_map_tips") or []):
        m.warMapNewTips.append(int(t))
    # baguaStatus 恒下发（客户端多处直接读 WarBookRecordData().baguaStatus.xxx）
    bag = wb.get("bagua") or {}
    bs = m.baguaStatus
    bs.currentResult = int(bag.get("current_result") or 0)
    bs.isSuccess = bool(bag.get("is_success"))
    for entry in (bag.get("remain") or []):
        spec = bs.remainWarBookTypes.add()
        spec.type = int(entry.get("type") or 0)
        spec.name = ""                     # required 字段（2026-10-23 教训）
        spec.value = int(entry.get("value") or 0)
    return m


def build_warbook_record_data(data=None) -> bytes:
    return warbook_record_msg(data).SerializeToString()


def build_divination_data(event, bagua) -> bytes:
    """DivinationData{event, baguaStatus} —— 31203 前进回包。

    ★ event 是 FlagDrawEvent 枚举（0 Null / 1 DrawReward）；客户端 pbc 解成
    名字字符串后走 getEventID(event) 判 "DrawReward" —— 数值线正确即可。
    """
    m = pb.DivinationData()
    m.event = int(event or 0)
    bs = m.baguaStatus
    bs.currentResult = int(bagua.get("current_result") or 0)
    bs.isSuccess = bool(bagua.get("is_success"))
    for entry in (bagua.get("remain") or []):
        spec = bs.remainWarBookTypes.add()
        spec.type = int(entry.get("type") or 0)
        spec.name = ""
        spec.value = int(entry.get("value") or 0)
    return m.SerializeToString()


def build_divination_result(event, divination_count) -> bytes:
    """DivinationResultData{event, divinationCount} —— 31208 领取事件回包。"""
    m = pb.DivinationResultData()
    m.event = int(event or 0)
    m.divinationCount = int(divination_count or 0)
    return m.SerializeToString()


def update_squad_flag_msgs(entries) -> pb.UpdateBattleGameplaySquadDatas:
    """entries: [(category, categoryPlayMode, index, heros, power, flag)]"""
    m = pb.UpdateBattleGameplaySquadDatas()
    for category, cpm, index, heros, power, flag in entries:
        u = m.gameplayDataSquadFlags.add()
        u.category = int(category)
        u.categoryPlayMode = int(cpm)
        u.squad.index = int(index)
        for hid, pos in (heros or []):
            u.squad.heros.add().ParseFromString(build_battle_hero_data(hid, pos))
        u.squad.power = int(power)
        u.squad.flag = int(flag or 0)     # 上/下阵都要显式回 flag（0=已下阵）
    return m


def build_update_squad_flag_datas(entries) -> bytes:
    return update_squad_flag_msgs(entries).SerializeToString()


def build_update_squad_flag_data(category, cpm, index, heros, power, flag) -> bytes:
    m = pb.UpdateBattleGameplaySquadData()
    m.category = int(category)
    m.categoryPlayMode = int(cpm)
    m.squad.index = int(index)
    for hid, pos in (heros or []):
        m.squad.heros.add().ParseFromString(build_battle_hero_data(hid, pos))
    m.squad.power = int(power)
    m.squad.flag = int(flag or 0)         # 显式（0=已下阵）
    return m.SerializeToString()


# ---------------------------------------------------------------- EnterGame

def build_enter_game() -> bytes:
    """EnterGame：登录后一次性下发全部玩家数据。

    字段位置修正（mock 时代错位，客户端读取路径已核实）：
      · guideIndex(21) = 9999 —— 跳过所有新手引导（mock 错写进 CharacterData）
      · FateInfo(28)    —— 缘分进度（UserManager -> fateManager）
      · honors(39)      —— 头像/头像框解锁（UserManager -> honorManager）
    """
    e = pb.EnterGame()
    e.character.ParseFromString(build_character_data())
    for _hk, h in _ctx.PLAYER_DATA.get("heroes", {}).items():
        e.heros.add().ParseFromString(build_hero_data(
            h["id"], h["type"], h.get("lv", 1), h.get("position", -1)))
    for eid, eq in (_ctx.equip_store() or {}).items():
        if not _ctx.growth.is_equip_ownable(eq.get("type")):
            continue
        e.equips.add().ParseFromString(build_equip_data(eq))
    for iid, cnt in (_ctx.PLAYER_DATA.get("items") or {}).items():
        if int(cnt or 0) > 0:
            e.items.add().ParseFromString(build_item_data(iid, cnt))
    # 武将卡/定缘卡（2026-10-27 实例模型）：每张一个唯一 id、count=1，
    # 客户端 getRDCardList 按条目一卡一格（原版口径，不再靠 card_expand 展开）。
    for _uid, _ctype in sorted((_ctx.PLAYER_DATA.get("item_cards") or {}).items(),
                               key=lambda kv: int(kv[0])):
        e.items.add().ParseFromString(build_item_data(int(_uid), 1, int(_ctype)))
    # 月卡/周卡有效期（字段 20）：客户端 UserManager:initializePreloadData
    # 只从这里取 buyPropRecordDatas，缺了 PayView 会显示未购买、月卡特权失效。
    for _bp in (_ctx.buy_prop_records_snapshot() or []):
        e.buyPropRecordDatas.add().CopyFrom(buy_prop_record_msg(_bp))
    e.formations.add().ParseFromString(build_formation_data(1, 1, 1))
    on_queue = [(h["id"], h["position"]) for h in _ctx.PLAYER_DATA.get("heroes", {}).values()
                if h.get("position", -1) > 0]
    on_queue.sort(key=lambda x: x[1])
    # 军旗（2026-10-23）：编队 squad.flag + 旗帐/部件/跑商记录下发
    _sq_flag = _ctx.ensign_get_squad_flag(0, 1)
    e.battleGameplays.add().ParseFromString(
        build_battle_gameplay_data(0, [(1, on_queue[:5])], flags={1: _sq_flag}))
    for _f in _ctx.ensign_flags_snapshot():
        e.flags.add().CopyFrom(flag_msg(_f))
    for _p in _ctx.ensign_parts_snapshot():
        e.flagParts.add().CopyFrom(flag_part_msg(_p))
    e.warBookRecordData.CopyFrom(warbook_record_msg())
    for lid, rec in _ctx.LEVEL_RECORDS.items():
        e.levelRecords.add().ParseFromString(build_level_record_data(lid, rec))
    e.systemTime = int(__import__("time").time())
    e.guideIndex = 9999
    e.FateInfo.ParseFromString(build_fate_info())
    honor_msgs, _heads, _frames = build_honors()
    for h in honor_msgs:
        e.honors.add().CopyFrom(h)
    # 抽卡记录（字段 8）：招募/寻宝面板的价格、免费倒计时、保底计数、首次角标
    # 全来自这里；缺了客户端 getEmployeeByType() 返回 nil，点招募直接崩。
    for rec in (_ctx.lottery_records_snapshot() or []):
        e.lotteryRecords.add().CopyFrom(lottery_record_data(rec))
    # 折扣活动（ActivityData.lotteryActivityInfo）：客户端
    # UserManager:initializePreloadData 会从登录结果里取它，覆盖 5318 推送；
    # 不带就会在登录后被空的惰性表盖掉（折扣 UI 不显示）。
    _act = pb.ActivityData()
    _act.lotteryActivityInfo.ParseFromString(build_lottery_activity_info())
    # 在线奖励（M4）：activityData.onlineAwardRecord——客户端本地计时判定+红点
    from .activities import online_award as _oa
    _act.onlineAwardRecord.CopyFrom(_oa.snapshot())
    e.activityData.CopyFrom(_act)
    # 主线任务（2026-10-25）：missions(7) 下发当前节点、completedMission(23) 下发已领奖节点。
    # 客户端 initDataOnLogin 会据此建 _missionList；进度全靠服务端推 501/502。
    # 邮件（字段 3）：客户端 MailManager 只从 EnterGame 初始化（GetMails 10002 是死接口）
    for _mail in _ctx.mail_snapshot():
        e.mails.append(mail_data_msg(_mail))
    for _m in _ctx.mission_snapshot()[0]:
        e.missions.add().ParseFromString(build_mission_data(_m))
    for _t in _ctx.mission_snapshot()[1]:
        e.completedMission.append(int(_t))
    # 图鉴/收集（字段 11/45/46）：点亮的 type + 已领取里程碑
    _hero_types, _equip_types, _award_map = _ctx.collect_snapshot()
    for _t in _hero_types:
        e.heroRecords.add().CopyFrom(hero_record_msg(_t))
    for _t in _equip_types:
        e.equipRecords.add().CopyFrom(equip_record_msg(_t))
    for _key, _ms in (_award_map or {}).items():
        try:
            _ctype, _sub = str(_key).split("_", 1)
            e.collectionAwardRecords.add().CopyFrom(
                collection_award_record_msg(int(_ctype), int(_sub), _ms))
        except (ValueError, TypeError):
            continue
    # 挑战次数（EnterGame 字段 29/30，2026-10-29）：客户端 MyApp.lua:325
    # `levelManager:setShengShouTimes(loginResult.ActivityTherionTimes)` 登录即读，
    # 缺省 -1 → 挑战面板显示"次数未知"。默认 -1 会被客户端 `0 <= x` 跳过，
    # 所以用完了要真发 0。
    try:
        from . import challenge as _ch
        _cs = _ch.snapshot()
        e.ActivityTherionTimes = int(_cs["therion_left"])
        e.ActivityCoinLevelTimes = int(_cs["coin_left"])
    except Exception:
        pass
    return e.SerializeToString()
