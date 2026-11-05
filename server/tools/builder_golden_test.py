"""builder 黄金测试：legacy 字节级 builder 与 builders_pb 原生构造的语义等价。

判定方式：两份字节都用同一个生成类解析，按「已设置的已知字段」逐一相等。
差异白名单仅限 legacy_builders.DOCUMENTED_DIFFS 记载的字段错位修正
（FateInfo/honors/guideIndex 挪到 EnterGame 正确槽位）。

跑法：python tools/builder_golden_test.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sghy_server.game import builders_pb as pbx
from sghy_server.game import core
from sghy_server.game import legacy_builders as lg
from google.protobuf.message import Message
from sghy_server.proto import ps_protobuf_pb2 as pb

# ★ 军旗加成是 pb 侧新增语义（legacy 基线=原版游戏，无军旗概念）——
#   上阵武将的 maxHP/attack/defence/power 按 spec 必然比 legacy 高。
#   全量比对前摘掉 squad_flags 让两侧同口径；军旗加成的正确性由
#   tools/ensign_unit_test.py 专门覆盖（2026-10-27）。
_SQUAD_FLAGS_BACKUP = core.PLAYER_DATA.get("squad_flags")
core.PLAYER_DATA["squad_flags"] = {}

PASS = 0
FAIL = 0


def _canon(v):
    """递归只保留「已设置的已知字段」——子消息里的 unknown 字段（legacy 的
    错位字段落进去的）不参与比较，与客户端 pbc 解析语义一致。

    注意 repeated 容器是 RepeatedCompositeFieldContainer（不是 list 子类），
    靠「可迭代」兜住。"""
    if isinstance(v, Message):
        return fields(v)
    if isinstance(v, (str, bytes, int, float, bool)):
        return v
    try:
        return [_canon(x) for x in v]
    except TypeError:
        return v


def fields(msg):
    """已设置的已知字段 -> {字段号: 归一化值}（unknown 字段不参与比较）"""
    return {f.number: _canon(v) for f, v in msg.ListFields()}


def eq_fields(a, b, ignore=()):
    fa = {k: v for k, v in fields(a).items() if k not in ignore}
    fb = {k: v for k, v in fields(b).items() if k not in ignore}
    return fa == fb, set(fa) ^ set(fb)


def check(name, legacy_bytes, pb_bytes, cls, ignore=()):
    global PASS, FAIL
    try:
        a = cls(); a.ParseFromString(legacy_bytes)
        b = cls(); b.ParseFromString(pb_bytes)
    except Exception as e:
        FAIL += 1
        print("  FAIL %-38s 解析失败: %r" % (name, e))
        return
    ok, diff = eq_fields(a, b, ignore)
    if ok:
        PASS += 1
        print("  PASS %-38s" % name)
    else:
        FAIL += 1
        print("  FAIL %-38s 字段差异(号): %s" % (name, sorted(diff)))
        for k in sorted(set(fields(a)) | set(fields(b))):
            va, vb = fields(a).get(k), fields(b).get(k)
            if va != vb:
                print("        field%-3d legacy=%r  pb=%r" % (k, _short(va), _short(vb)))


def _short(v):
    s = repr(v)
    return s if len(s) < 90 else s[:87] + "..."


def check_same(name, legacy_bytes, pb_bytes, cls):
    check(name, legacy_bytes, pb_bytes, cls)


print("== 叶子消息 ==")
check("ItemData", lg.build_item_data(20007, 894), pbx.build_item_data(20007, 894), pb.ItemData)
check("FormationData", lg.build_formation_data(1, 1, 1), pbx.build_formation_data(1, 1, 1), pb.FormationData)
check("LevelRecordData", lg.build_level_record_data(1001, {"star": 3, "leftTimes": 10, "buyTimes": 0}),
      pbx.build_level_record_data(1001, {"star": 3, "leftTimes": 10, "buyTimes": 0}), pb.LevelRecordData)
check("AdditionData(全参数)", lg.build_addition_data(3, 40, 1, index=2, times=3, param=100, param2=5, param3=9, param4=7),
      pbx.build_addition_data(3, 40, 1, index=2, times=3, param=100, param2=5, param3=9, param4=7), pb.AdditionData)
check("AdditionData(仅必填)", lg.build_addition_data(3, 40), pbx.build_addition_data(3, 40), pb.AdditionData)
check("HeadType", lg.build_head_type(), pbx.build_head_type(), pb.HeadType)
check("AwardSpecData", lg.build_award_spec(33115, 1, "青龙偃月刀"), pbx.build_award_spec(33115, 1, "青龙偃月刀"), pb.AwardSpecData)
check("AwardResult", lg.build_award_result("Synthetic", [(33115, 1), (33415, 2)]),
      pbx.build_award_result("Synthetic", [(33115, 1), (33415, 2)]), pb.AwardResult)

print("== 战斗编队 ==")
check("BattleHeroData", lg.build_battle_hero_data(5, 3), pbx.build_battle_hero_data(5, 3), pb.BattleHeroData)
# field4(flag)：python protobuf 省略默认值 0、legacy 也不发——解码语义等价（无军旗），
# 但 pb 显式 m.flag=0 时序列化差异——以 ignore={4} 记录（2026-10-23 实测）
check("BattleGameplaySquadData", lg.build_battle_squad_data(1, [(5, 1), (23, 2)], power=2765),
      pbx.build_battle_squad_data(1, [(5, 1), (23, 2)], power=2765), pb.BattleGameplaySquadData,
      ignore={4})
check("BattleGameplayData", lg.build_battle_gameplay_data(0, [(1, [(5, 1)])]),
      pbx.build_battle_gameplay_data(0, [(1, [(5, 1)])]), pb.BattleGameplayData)
check("BattleGameplayPowerChange", lg.build_squad_power_change(2765),
      pbx.build_squad_power_change(2765), pb.BattleGameplayPowerChange)

print("== 缘分 ==")
check("FateFastenInfo", lg.build_fate_info(), pbx.build_fate_info(), pb.FateFastenInfo)

print("== 武将（全量 %d 个）==" % len(core.PLAYER_DATA.get("heroes", {})))
for _k, h in core.PLAYER_DATA.get("heroes", {}).items():
    hid, htype = h["id"], h["type"]
    check("HeroData id=%s type=%s" % (hid, htype),
          lg.build_hero_data(hid, htype, h.get("lv", 1), h.get("position", -1)),
          pbx.build_hero_data(hid, htype, h.get("lv", 1), h.get("position", -1)),
          pb.HeroData,
          # 22-29/32/35/36 = 增伤/减伤/治疗/范围/技能伤害/韧性/效果抵抗/效果命中：
          # pb 侧 2026-10-28 新增下发（legacy 基线不写），属已知演进差异
          ignore={22, 23, 24, 25, 26, 27, 28, 29, 32, 35, 36})
    if FAIL > 3:
        print("  (失败过多，武将全量截断)")
        break

print("== 装备（全量 %d 件）==" % len(core.equip_store() or {}))
_eo = 0
for _k, eq in (core.equip_store() or {}).items():
    check("EquipData id=%s type=%s" % (eq.get("id"), eq.get("type")),
          lg.build_equip_data(eq), pbx.build_equip_data(eq), pb.EquipData)
    _eo += 1
    if FAIL > 3 or _eo >= 30:
        if _eo < 30:
            print("  (失败过多，装备全量截断)")
        break

print("== 敌方武将 ==")
check("EnemyHeroData", lg.build_enemy_hero_data(9001, 10014, 1, scale=120, grade=2),
      pbx.build_enemy_hero_data(9001, 10014, 1, scale=120, grade=2), pb.HeroData)

print("== 登录 / 角色（含 DOCUMENTED_DIFFS 白名单）==")


def _strip_challenge(m):
    """挑战次数（2026-10-29）：legacy 基线（原版报文字节）没有挑战副本概念。

    比对前把 pb 侧新增的字段摘掉，正确性由 tools/challenge_test.py 覆盖：
      · CharacterData.actionTime.leftTherionTimes / buyTherionTimes（27.3 / 27.4）
      · EnterGame.ActivityTherionTimes / ActivityCoinLevelTimes（29 / 30）
    """
    try:
        m.actionTime.ClearField("leftTherionTimes")
        m.actionTime.ClearField("buyTherionTimes")
    except Exception:
        pass
    for _f in ("ActivityTherionTimes", "ActivityCoinLevelTimes"):
        if _f in m.DESCRIPTOR.fields_by_name:
            m.ClearField(_f)
    return m


check("LoginCharacterData", lg.build_login_character_data(), pbx.build_login_character_data(),
      pb.LoginCharacterData)
check("LoginOK", lg.build_login_ok(), pbx.build_login_ok(), pb.LoginOK)
# CharacterData：legacy 多写 bugSignTimes=9999（错当 guideIndex）、arenaShop
# 里塞了 FateFastenInfo、unknown field 39(honors) —— 全部白名单
_cd = pb.CharacterData()
_cd.ParseFromString(pbx.build_character_data())
check("CharacterData", lg.build_character_data(), _strip_challenge(_cd).SerializeToString(),
      pb.CharacterData, ignore={21, 28, 39})
# 200 body = 裸 CharacterData（客户端 socket:decode("CharacterData")），
# 不是 UpdateCharacter 外壳 —— 外壳方案两边一致也会一起错，此断言改按裸解。
a = pb.CharacterData(); a.ParseFromString(lg.update_character_body())
b = pb.CharacterData(); b.ParseFromString(pbx.update_character_body())
ok, _ = eq_fields(a, _strip_challenge(b), ignore={21, 28, 39})
if ok:
    PASS += 1
    print("  PASS %-38s" % "200 body=CharacterData(白名单)")
else:
    FAIL += 1
    print("  FAIL %-38s" % "200 body=CharacterData(白名单)")

print("== EnterGame（含 DOCUMENTED_DIFFS：pb 多 8/21/28/39）==")
_e1 = pb.EnterGame(); _e1.ParseFromString(lg.build_enter_game())
_e2 = pb.EnterGame(); _e2.ParseFromString(pbx.build_enter_game())
_f1, _f2 = fields(_e1), fields(_e2)
_extra = set(_f2) - set(_f1)
# 8=lotteryRecords / 11=heroRecords / 45=equipRecords / 25=activityData
# 36=warBookRecordData / 40=flags / 41=flagParts（军旗系统 2026-10-23 新增）
# 20=buyPropRecordDatas（月卡/周卡有效期，2026-10-27 新增）
# 46=collectionAwardRecords（图鉴收集里程碑，2026-10-27 起常驻）
_extra_expected = {8, 11, 20, 21, 25, 28, 36, 39, 40, 41, 45, 46,
                   # 挑战次数（2026-10-29）：圣兽/金银岛剩余次数，legacy 无此概念
                   29, 30}
if _f2.get(41):            # flagParts 有部件时才出现（空列表不序列化）
    _extra_expected.add(41)
_diff = []
for _k in set(_f1) & set(_f2):
    if _k == 12:            # systemTime 时间戳，忽略
        continue
    if _k in (3, 4):        # mails 类型口径已改（builders_pb 唯一出包口）/
        continue            # items 卡实例模型 pb 独有 —— 已知演进差异（2026-10-27）
    if _k == 2:             # heros：pb 额外下发增伤/减伤等特殊属性（HeroData 单项已覆盖）
        continue
    if _k == 1:             # character：子消息内有 21/28/39 错位修正，白名单比对
        _okc, _ = eq_fields(_e1.character, _strip_challenge(_e2.character),
                            ignore={21, 28, 39})
        if not _okc:
            _diff.append(_k)
        continue
    if _k == 37:            # battleGameplays：pb 侧 squad.flag 为军旗系统新增（内容=存档 squad_flags）
        _flg = pbx._ctx.ensign_get_squad_flag(0, 1)
        _pb_flag = _e2.battleGameplays[0].squads[0].flag
        if _pb_flag != _flg:
            _diff.append(_k)
        continue
    if _f1[_k] != _f2[_k]:
        _diff.append(_k)
ok = (not _diff) and _extra == _extra_expected
if ok:
    PASS += 1
    print("  PASS %-38s  pb 多出字段(8/11/21/25/28/39/45)符合预期" % "EnterGame")
else:
    FAIL += 1
    print("  FAIL %-38s 字段差异=%s 额外字段=%s(期望%s)" % (
        "EnterGame", sorted(_diff), sorted(_extra), sorted(_extra_expected)))
# 附加断言：pb 的新字段内容正确
if (_e2.guideIndex == 9999 and _e2.honors and _e2.FateInfo.fateInfo
        and len(_e2.lotteryRecords) == 10
        and len(_e2.heroRecords) > 0 and len(_e2.equipRecords) > 0):
    PASS += 1
    print("  PASS %-38s" % "EnterGame 新字段内容(guide/honors/fate/lottery/collect)")
else:
    FAIL += 1
    print("  FAIL %-38s guide=%s honors=%d fate=%d lottery=%d heroes=%d equips=%d" % (
        "EnterGame 新字段内容", _e2.guideIndex, len(_e2.honors),
        len(_e2.FateInfo.fateInfo), len(_e2.lotteryRecords),
        len(_e2.heroRecords), len(_e2.equipRecords)))

print("== build_honors（legacy 返回 f_msg(39,..) 流，需拆包对比）==")
_lg_body, _lg_heads, _lg_frames = lg.build_honors()
_pb_msgs, _pb_heads, _pb_frames = pbx.build_honors()
# 拆 legacy 流：每条 = tag varint(314=field39/wire2) + len + HonorData 字节
_honors_lg = []
_buf, _p = _lg_body, 0
while _p < len(_buf):
    _tag, _p = lg._read_varint(_buf, _p)
    _ln, _p = lg._read_varint(_buf, _p)
    _honors_lg.append(_buf[_p:_p + _ln])
    _p += _ln
ok = (_lg_heads == _pb_heads and _lg_frames == _pb_frames
      and len(_honors_lg) == len(_pb_msgs))
if ok:
    for _raw, _h in zip(_honors_lg, _pb_msgs):
        _a = pb.HonorData(); _a.ParseFromString(_raw)
        if fields(_a) != fields(_h):
            ok = False
            print("        honor 不等: legacy=%r pb=%r" % (fields(_a), fields(_h)))
            break
PASS += 1 if ok else 0
FAIL += 0 if ok else 1
print("  %s %-38s  %d 条 honor" % ("PASS" if ok else "FAIL", "build_honors", len(_pb_msgs)))

print()
print("TOTAL: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

# 恢复军旗配置（黄金比对全程关闭军旗加成，见文件头部说明）
if _SQUAD_FLAGS_BACKUP is not None:
    core.PLAYER_DATA["squad_flags"] = _SQUAD_FLAGS_BACKUP
else:
    core.PLAYER_DATA.pop("squad_flags", None)
