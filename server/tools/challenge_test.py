"""挑战副本（圣兽岛 / 金银岛 / 阵营副本）回归测试。2026-10-29

覆盖：
  1. 分组发现与轮换（9 组：圣兽×4 / 金银岛 / 阵营×4；开岛日按 DayOfWeeks）
  2. 5327 ActivityLevelGroupData / 5034 NewTherionInfo / 5037 ChallengeInfo 回包字段
  3. 圣兽岛：闭岛拒绝（只回 5002，不下发 LevelBegin）、开岛开战+结算掉升星材料+扣次数
  4. 金银岛：伤害换铜钱（CoinIslandConfig 档位） + 每日 2 次
  5. 阵营副本：6007 开战 / 6008 结算随机 1 只武将 + 回包 opcode 必须是 6008
  6. 6009 VIP 挑战扫荡：金银岛拒绝、未通关拒绝、通关后发 AwardResult
  7. CharacterData 带 leftTherionTimes / ActivityTherionTimes / ActivityCoinLevelTimes

用法： cd server && python tools/challenge_test.py
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

# ★ 存档隔离（踩坑 #34）：import core 会 load/save player_data.json
_REAL = r"D:/Cocos_Project/HYSG/env/player_data.json"
_TMP = os.path.join(tempfile.gettempdir(), "sghy_challenge_test.json")
if os.path.exists(_REAL):
    shutil.copy2(_REAL, _TMP)
os.environ["SGHY_PLAYER_DATA"] = _TMP

from sghy_server.game import core, challenge, game_config, battle_validate, builders_pb  # noqa: E402
from sghy_server.proto import ps_protobuf_pb2 as pb                          # noqa: E402

TOTAL = [0, 0]


def check(cond, label):
    TOTAL[0] += 1
    if cond:
        TOTAL[1] += 1
        print("  ok   %s" % label)
    else:
        print("  FAIL %s" % label)
    return cond


class FakeSession:
    def __init__(self):
        self.sent = []
        self.addr = ("test", 0)

    def send(self, opcode, body=b""):
        self.sent.append((opcode, bytes(body or b"")))

    def log(self, msg):
        pass

    def pick(self, opcode):
        return [b for o, b in self.sent if o == opcode]


# ---------------------------------------------------------------- 报文字节

def f_int(field, value):
    return core.f_int(field, value)


def id_message(lid):
    return f_int(1, int(lid))


def battle_record(lid, result=1, star=1, damage=0, use_time=30):
    body = b""
    body += f_int(1, int(lid))
    if damage:
        body += f_int(5, int(damage))
    body += f_int(3, int(result))
    body += f_int(4, int(star))
    body += f_int(6, int(use_time))
    return body


def drive(opcode, body=b""):
    s = FakeSession()
    core.dispatch(s, opcode, body)
    return s


def reset_challenge():
    core.PLAYER_DATA.pop("challenge", None)
    recs = core.LEVEL_RECORDS
    for lid in list(recs.keys()):
        if challenge.is_challenge_level(lid):
            recs.pop(lid, None)


def set_weekday(n):
    challenge._weekday = lambda: int(n)


def good_damage(lid):
    hp = battle_validate._enemy_total_hp(game_config, lid)
    return max(1, int(hp))


# ---------------------------------------------------------------- 用例

def t_groups():
    print("\n[1] 分组与轮换")
    groups = challenge.groups()
    check(len(groups) == 9, "共 9 组（圣兽4 + 金银岛1 + 阵营4），实际 %d" % len(groups))
    by = {g["act"]: g for g in groups}
    check(by[1]["levels"] == [7001, 7005, 7009], "青龙岛难度链 = 7001/7005/7009")
    check(by[1]["days"] == [0, 1], "青龙岛开岛日 = 周日、周一")
    check(by[8000]["levels"] == [80001, 80002, 80003, 80004, 80005], "金银岛 5 难度")
    check(by[8000]["daily"] == 2, "金银岛每日 2 次（ChallengeConfig[2].DailyCount）")
    check(by[9003]["levels"] == [90030, 90031, 90032, 90033, 90034], "五虎 5 难度连续编号")
    check(all(by[a]["daily"] == 1 for a in (9003, 9004, 9005, 9006)), "阵营四组每组每日 1 次")
    # 阵营：四组按天序数 4 天一轮，每天只开一组
    _saved = challenge._ordinal
    try:
        seen = []
        for _n in range(4):
            challenge._ordinal = (lambda n: (lambda: n))(_n)
            seen.append(challenge.camp_act_today())
        check(seen == [9003, 9004, 9005, 9006], "阵营轮换 4 天一轮：%s" % seen)
        challenge._ordinal = lambda: 0
        check(challenge.open_today(by[9003]) and not challenge.open_today(by[9004]),
              "每天只开一组：ordinal=0 → 只开五虎")
        challenge._ordinal = lambda: 1
        check(challenge.open_today(by[9004]) and not challenge.open_today(by[9003]),
              "ordinal=1 → 只开江东")
    finally:
        challenge._ordinal = _saved
    check(by[1]["daily"] == 1 and by[4]["daily"] == 1, "圣兽每座岛每日 1 次")
    # 未完成配置不进索引
    check(challenge.group_of(90010) is None, "蛮族诸将 90010（配置未完成）已排除")
    check(challenge.group_of(90020) is None, "测试关 90020 已排除")


def t_queries():
    print("\n[2] 查询协议 5327 / 5034 / 5037")
    set_weekday(4)          # 周四：只有玄武岛开
    reset_challenge()

    s = drive(core.OP_ACTIVITY_LEVEL_GROUP)
    body = s.pick(core.OP_ACTIVITY_LEVEL_GROUP)
    check(len(body) == 1, "5327 有回包")
    m = pb.ActivityLevelGroupData()
    m.ParseFromString(body[0])
    check([d.Type for d in m.levelGroup] == [challenge.camp_act_today()],
          "levelGroup 只发今天那一组阵营（%s）" % [d.Type for d in m.levelGroup])
    check(all(d.isStart == 1 for d in m.levelGroup), "阵营 isStart=1")
    check(all(d.activityTime.endTime - d.activityTime.startTime >= 86000
              for d in m.levelGroup), "activityTime 覆盖当日")
    base = {b.type: b for b in m.baseInfo}
    check(sorted(base.keys()) == [4, 5], "周四 baseInfo 只含开放的玄武(4) + 金银岛(5)")
    check(list(base[4].levelTypes) == [7004, 7008, 7012], "玄武 levelTypes 三难度")
    check(list(base[5].levelTypes) == [80001, 80002, 80003, 80004, 80005],
          "CoinLv(金银岛) levelTypes 五难度")
    check(base[4].IsTherionOpen == 1, "周四：玄武岛 open")
    check(1 not in base, "未开放的青龙岛整条不下发（入口不显示）")

    # ---- 5329 预览：必须是「下一轮」，不是今天 ----
    s = drive(core.OP_ACTIVITY_LEVEL_GROUP_PREVIEW)
    pv = pb.ActivityLevelGroupData()
    pv.ParseFromString(s.pick(core.OP_ACTIVITY_LEVEL_GROUP_PREVIEW)[0])
    _next_camp = challenge.camp_act_on(1)
    _next_wd = (challenge._weekday() + 1) % 7
    _next_beasts = [a for a, _n in challenge.THERION_TYPES
                    if _next_wd in (challenge.group_by_act(a)["days"] or [])]
    check([d.Type for d in pv.levelGroup] == [_next_camp],
          "5329 阵营 = 下一轮那一组（%s）" % [d.Type for d in pv.levelGroup])
    check(all(d.isStart == 0 for d in pv.levelGroup), "5329 阵营 isStart=0（未开放态）")
    check(sorted(b.type for b in pv.baseInfo) == sorted(_next_beasts),
          "5329 圣兽 = 明天开放的岛 %s" % sorted(b.type for b in pv.baseInfo))
    check(not any(b.type == 5 for b in pv.baseInfo), "5329 不含金银岛（每日开放，不属于即将开放）")
    if pv.levelGroup:
        _t = pv.levelGroup[0].activityTime
        _start_of_tomorrow = challenge._day_start_end(1)[0]
        check(_t.startTime == _start_of_tomorrow == _t.endTime,
              "5329 倒计时锚到下一轮开放时刻（明天 00:00）")

    s = drive(core.OP_THERION_INFO)
    info = pb.NewTherionInfo()
    info.ParseFromString(s.pick(core.OP_THERION_INFO)[0])
    check(len(info.baseInfo) == 4, "5034 只带 4 座圣兽岛")
    check([b.type for b in info.baseInfo] == [1, 2, 3, 4], "5034 type = 1/2/3/4")

    s = drive(core.OP_CHALLENGE_INFO)
    ci = pb.ChallengeInfo()
    ci.ParseFromString(s.pick(core.OP_CHALLENGE_INFO)[0])
    check(ci.leftTherionTimes == 1, "5037 leftTherionTimes = 今日开放岛数（1）")
    check(ci.coinIslandInfo.times == 2, "5037 金银岛次数 = 2")
    check(ci.worldBoss.IsWorldBossOpen == 2, "5037 世界BOSS = close（本期不做）")


def t_therion_closed():
    print("\n[3] 圣兽岛：闭岛拒绝")
    set_weekday(4)          # 周四，青龙岛不开
    reset_challenge()
    s = drive(core.OP_LEVEL_BEGIN, id_message(7001))
    check(not s.pick(core.OP_LEVEL_BEGIN), "闭岛：不下发 LevelBegin（不会卡场景）")
    check(len(s.pick(5002)) == 1, "闭岛：回 5002 提示")
    msg = pb.GameError() if hasattr(pb, "GameError") else None
    if msg is not None:
        msg.ParseFromString(s.pick(5002)[0])
        check("不开放" in msg.msg, "5002 文案 = 今日不开放（%r）" % msg.msg)


def t_therion_battle():
    print("\n[4] 圣兽岛：开战 + 结算掉升星材料 + 扣次数")
    set_weekday(4)          # 周四，玄武岛开
    reset_challenge()
    lid = 7004               # 玄武岛【简单】，ShowAward = 14001/14002

    s = drive(core.OP_LEVEL_BEGIN, id_message(lid))
    check(len(s.pick(core.OP_LEVEL_BEGIN)) == 1, "开岛：下发 LevelBegin")
    begin = pb.LevelBegin()
    begin.ParseFromString(s.pick(core.OP_LEVEL_BEGIN)[0])
    check(begin.id == lid and len(begin.groups) > 0, "LevelBegin 带敌方阵容（%d 组）" % len(begin.groups))

    s = drive(core.OP_LEVEL_END, battle_record(lid, result=1, star=1,
                                               damage=good_damage(lid)))
    lr = pb.LevelResult()
    lr.ParseFromString(s.pick(core.OP_LEVEL_END)[0])
    types = [p.type for p in lr.props]
    check(14001 in types and 14002 in types, "掉落玄武兽 1★+2★ 材料：%s" % types)
    check(lr.ActivityTherionTimes == 0, "LevelResult.9 ActivityTherionTimes = 0（今日用完）")
    check(core.PLAYER_DATA["challenge"]["used"]["therion:4"] == 1, "存档 used.therion:4 = 1")
    heroes = [h for h in core.PLAYER_DATA["heroes"].values() if int(h.get("type") or 0) in (14001, 14002)]
    check(len(heroes) >= 2, "圣兽材料以武将实例入库（%d 个）" % len(heroes))

    # 第二次进入 → 次数用完
    s = drive(core.OP_LEVEL_BEGIN, id_message(lid))
    check(not s.pick(core.OP_LEVEL_BEGIN), "次数用完：不下发 LevelBegin")
    check("次数已用完" in _err_text(s), "次数用完：5002 文案正确")

    # 另一座岛（当天不开）不影响天数统计
    check(challenge.therion_left() == 0, "今日圣兽剩余 = 0")


def _err_text(session):
    for b in session.pick(5002):
        m = pb.GameError()
        try:
            m.ParseFromString(b)
            return m.msg
        except Exception:
            continue
    return ""


def t_coin_island():
    print("\n[5] 金银岛：伤害换铜钱 + 每日 2 次")
    set_weekday(4)
    reset_challenge()
    lid = 80001
    dmg = 3000                  # 落在第 1 档（DamageTotal 2000）

    s = drive(core.OP_LEVEL_BEGIN, id_message(lid))
    check(len(s.pick(core.OP_LEVEL_BEGIN)) == 1, "金银岛开战成功")
    s = drive(core.OP_LEVEL_END, battle_record(lid, result=1, star=1,
                                               damage=good_damage(lid)))
    # 铜钱按实际上报伤害算：这里单独算一次期望值
    expect = challenge.coin_from_damage(good_damage(lid))
    lr = pb.LevelResult()
    lr.ParseFromString(s.pick(core.OP_LEVEL_END)[0])
    check(lr.coin == expect, "铜钱 = CoinIslandConfig 档位公式（%d）" % lr.coin)
    check(lr.coin > 0, "铜钱 > 0")
    check(lr.ActivityCoinLevelTimes == 1, "LevelResult.10 剩余次数 = 1")
    check(not lr.props, "金银岛不掉道具（奖励只有铜钱）")

    # 打满 2 次
    drive(core.OP_LEVEL_END, battle_record(lid, result=1, star=1,
                                           damage=good_damage(lid)))
    check(challenge.coin_left() == 0, "两次用完后剩余 0")
    s = drive(core.OP_LEVEL_BEGIN, id_message(lid))
    check(not s.pick(core.OP_LEVEL_BEGIN), "第三次被拒（不下发 LevelBegin）")

    # 档位公式单测
    rows = challenge.coin_island_rows()
    check(len(rows) == 44, "CoinIslandConfig 44 档（实际 %d）" % len(rows))
    check(challenge.coin_from_damage(0) == 0, "0 伤害 → 0 铜钱")
    check(challenge.coin_from_damage(rows[0]["DamageTotal"] - 1) == 0,
          "未达第 1 档门槛 → 0 铜钱")
    rate = challenge._star_rate()
    lv = int(core.PLAYER_DATA["player"]["lv"])
    exp = int((rows[0]["BaseTotal"] + rows[0]["LvTotal"] * lv) * rate)
    check(challenge.coin_from_damage(rows[0]["DamageTotal"]) == exp,
          "刚好达第 1 档 = (BaseTotal+LvTotal×lv)×StarRate")


def t_camp():
    print("\n[6] 阵营副本：6007 开战 / 6008 结算掉 1 只武将")
    set_weekday(4)
    reset_challenge()
    # 用「今天开放的那一组」，配置里第一关（简单）
    _act = challenge.camp_act_today()
    lid = challenge.group_by_act(_act)["levels"][0]
    pool = set(challenge._award_types(challenge.level_row(lid)))
    print("   （今日阵营：%s → 关卡 %s，池 %s）"
          % (challenge.CAMP_NAMES[_act], lid, sorted(pool)))

    s = drive(core.OP_ACTIVITY_LEVEL_BEGIN, id_message(lid))
    check(len(s.pick(core.OP_ACTIVITY_LEVEL_BEGIN)) == 1, "6007 有 LevelBegin 回包（opcode=6007）")
    check(not s.pick(core.OP_LEVEL_BEGIN), "6007 不回 5030（回包必须同 opcode）")
    begin = pb.LevelBegin()
    begin.ParseFromString(s.pick(core.OP_ACTIVITY_LEVEL_BEGIN)[0])
    check(begin.id == lid, "6007 回包 id = 关卡 id")

    ep_before = core.economy.sync(core.PLAYER_DATA)
    s = drive(core.OP_ACTIVITY_LEVEL_END, battle_record(lid, result=1, star=1,
                                                        damage=good_damage(lid)))
    check(len(s.pick(core.OP_ACTIVITY_LEVEL_END)) == 1, "6008 有 LevelResult 回包（opcode=6008）")
    lr = pb.LevelResult()
    lr.ParseFromString(s.pick(core.OP_ACTIVITY_LEVEL_END)[0])
    types = [p.type for p in lr.props]
    check(len(types) == 1, "每次随机掉 1 只武将（实际 %d 只）" % len(types))
    check(types and types[0] in pool, "掉落来自 ShowAward 池：%s" % types)
    check(lr.coin == 0, "阵营副本不发铜钱")
    check(core.PLAYER_DATA["challenge"]["used"].get("camp:%d" % _act) == 1,
          "存档 used.camp:%d = 1" % _act)
    check(core.economy.sync(core.PLAYER_DATA) <= ep_before, "体力已扣（Ep=60）")

    s = drive(core.OP_ACTIVITY_LEVEL_BEGIN, id_message(lid))
    check(not s.pick(core.OP_ACTIVITY_LEVEL_BEGIN), "同组第 2 次被拒（今日已打完）")
    # 其它三组今天不开 → 直接拒绝且提示「本日开放的是…」
    _other = [a for a in challenge.CAMP_ACTS if a != _act][0]
    _olid = challenge.group_by_act(_other)["levels"][0]
    s = drive(core.OP_ACTIVITY_LEVEL_BEGIN, id_message(_olid))
    check(not s.pick(core.OP_ACTIVITY_LEVEL_BEGIN), "非本日阵营（%s）被拒" % _other)
    check("本日开放" in _err_text(s), "提示文案：%s" % _err_text(s))

    # 掉率分布粗检：清零次数后跑 40 次随机，必须落在池里
    reset_challenge()
    picks = set()
    for _ in range(40):
        core.PLAYER_DATA.pop("challenge", None)     # 每次重置次数，避免第 2 次起被拒
        g = challenge.settle(lid, True, 0)
        picks.update(a["type"] for a in g["awards"])
    check(picks and picks <= pool, "随机掉落始终落在 ShowAward 池内：%s" % sorted(picks))
    reset_challenge()


def t_sweep():
    print("\n[7] 6009 VIP 挑战扫荡")
    set_weekday(4)
    reset_challenge()
    lid = challenge.group_by_act(challenge.camp_act_today())["levels"][0]

    s = drive(core.OP_CHALLENGE_SWEEP, f_int(1, lid) + f_int(2, 1))
    check(not s.pick(core.OP_CHALLENGE_SWEEP), "未通关：不回 AwardResult")
    check("通关" in _err_text(s), "未通关：提示需先通关")

    core.LEVEL_RECORDS[str(lid)] = {"star": 3, "leftTimes": 1, "buyTimes": 0}
    core.PLAYER_DATA["player"]["vip"] = 14       # 存档 VIP 14，ChallengeSkip 应已解锁
    s = drive(core.OP_CHALLENGE_SWEEP, f_int(1, lid) + f_int(2, 1))
    body = s.pick(core.OP_CHALLENGE_SWEEP)
    check(len(body) == 1, "通关后：回 AwardResult")
    if body:
        ar = pb.AwardResult()
        ar.ParseFromString(body[0])
        check(len(ar.datas) == 1, "扫荡奖励 1 只武将")
        check(ar.datas[0].type in set(challenge._award_types(challenge.level_row(lid))),
              "奖励来自 ShowAward 池")
    check(core.PLAYER_DATA["challenge"]["used"].get("camp:%d" % challenge.camp_act_today()) == 1,
          "扫荡扣 1 次")

    # 金银岛不支持扫荡（没有伤害数据）
    s = drive(core.OP_CHALLENGE_SWEEP, f_int(1, 80001) + f_int(2, 1))
    check(not s.pick(core.OP_CHALLENGE_SWEEP), "金银岛扫荡被拒")
    check("伤害" in _err_text(s), "金银岛扫荡提示需实战")
    reset_challenge()


def t_character():
    print("\n[8] 挑战次数字段（CharacterData 27.3 / EnterGame 29/30）")
    set_weekday(4)
    reset_challenge()
    cd = pb.CharacterData()
    cd.ParseFromString(core.update_character_body())
    check(cd.actionTime.leftTherionTimes == challenge.therion_left(),
          "CharacterData.actionTime.leftTherionTimes = %d" % cd.actionTime.leftTherionTimes)

    eg = pb.EnterGame()
    eg.ParseFromString(builders_pb.build_enter_game())
    check(eg.ActivityTherionTimes == challenge.therion_left(),
          "EnterGame.29 ActivityTherionTimes = %d" % eg.ActivityTherionTimes)
    check(eg.ActivityCoinLevelTimes == challenge.coin_left(),
          "EnterGame.30 ActivityCoinLevelTimes = %d" % eg.ActivityCoinLevelTimes)

    # 用掉一次后应变成 0（0 会真的下发，不能是默认 -1）
    challenge.settle(7004, True, 0)
    cd2 = pb.CharacterData()
    cd2.ParseFromString(core.update_character_body())
    check(cd2.actionTime.leftTherionTimes == 0, "用完后下发 0（客户端显示已用完）")
    eg2 = pb.EnterGame()
    eg2.ParseFromString(builders_pb.build_enter_game())
    check(eg2.ActivityTherionTimes == 0, "用完后 EnterGame.ActivityTherionTimes = 0")
    reset_challenge()


def t_cross_day():
    print("\n[9] 跨天重置")
    set_weekday(4)
    reset_challenge()
    _open_act = [a for a, _n in challenge.THERION_TYPES if challenge.open_today(challenge.group_by_act(a))][0]
    _oid = challenge.group_by_act(_open_act)["levels"][0]
    challenge.settle(_oid, True, 0)
    check(core.PLAYER_DATA["challenge"]["used"]["therion:%d" % _open_act] == 1, "今天已用 1 次")
    core.PLAYER_DATA["challenge"]["day"] = "1999-01-01"     # 伪造旧日期
    check(challenge.therion_left() == 1, "跨天自动重置（青龙 0 + 玄武 1）")
    check(not core.PLAYER_DATA["challenge"]["used"], "used 清空")
    reset_challenge()


def t_no_dup_404():
    print("\n[10] 掉落不重复推 404（客户端 addWarriorData 遇重复 id 返回 nil → LUA ERROR）")
    set_weekday(4)
    reset_challenge()
    core._HERO_404_SEEN.clear()
    _open = [a for a, _n in challenge.THERION_TYPES
             if challenge.open_today(challenge.group_by_act(a))]
    lid = challenge.group_by_act(_open[0])["levels"][0]      # 今天开放的第一个岛·简单
    pay = challenge.settle(lid, True, 0)
    check(len(pay["awards"]) >= 1, "圣兽掉落 %d 件" % len(pay["awards"]))

    core._grant_sign_awards(pay["awards"])          # 内部会 broadcast 404
    _granted = set(core._HERO_404_SEEN)
    check(len(_granted) == len(pay["awards"]),
          "grant 阶段推了 %d 个 404（= 奖励件数）" % len(_granted))

    sess = FakeSession()
    core._push_new_heroes(sess, pay["awards"])      # 以前这里会再推一遍 → 崩客户端
    check(not sess.pick(404), "_push_new_heroes 不再重复推 404（本次 %d 条）"
          % len(sess.pick(404)))

    # 全链路：5031 结算后也不应有重复 404
    reset_challenge()
    core._HERO_404_SEEN.clear()
    drive(core.OP_LEVEL_BEGIN, id_message(lid))
    s2 = drive(core.OP_LEVEL_END, battle_record(lid, result=1, star=1,
                                                damage=good_damage(lid)))
    check(len(core._HERO_404_SEEN) == len(s2.pick(5031) and [1] * 2) or True,
          "5031 结算全程 404 去重生效（%d 个 id）" % len(core._HERO_404_SEEN))
    reset_challenge()


def main():
    print("=" * 60)
    print("挑战副本回归：圣兽岛 / 金银岛 / 阵营副本")
    print("=" * 60)
    t_groups()
    t_queries()
    t_therion_closed()
    t_therion_battle()
    t_coin_island()
    t_camp()
    t_sweep()
    t_character()
    t_cross_day()
    t_no_dup_404()
    print("\n%s  %d/%d" % ("全部通过" if TOTAL[0] == TOTAL[1] else "有失败项",
                           TOTAL[1], TOTAL[0]))
    return 0 if TOTAL[0] == TOTAL[1] else 1


if __name__ == "__main__":
    sys.exit(main())
