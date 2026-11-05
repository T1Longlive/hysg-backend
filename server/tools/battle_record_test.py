"""战报（ShowBattle 5070）回归测试。

验证链路（对齐客户端 BattleResultPanel._btnZhanBao → ShowBattle → AttackArena）：
  1. LevelEnd(5031) 带 BattleRecordData → LevelResult 必须回 battleID（字段 7）
  2. ShowBattle(5070){key=battleID} → AttackArena，battleRecord 原样回来
     （含每个 HeroData 的 recordDamage/recordHealing/... 统计字段）
  3. key 为空/查不到 → 回退最近一条（结算界面立刻点也能出数据）
  4. 无记录时回空 AttackArena（id=0/result=0），不崩

用法： cd server && python tools/battle_record_test.py
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

# ★ 存档隔离（踩坑 #34）：import core 会 load/save player_data.json
_REAL = r"D:/Cocos_Project/HYSG/env/player_data.json"
_TMP = os.path.join(tempfile.gettempdir(), "sghy_battle_record_test.json")
if os.path.exists(_REAL):
    shutil.copy2(_REAL, _TMP)
os.environ["SGHY_PLAYER_DATA"] = _TMP

from sghy_server.game import core                       # noqa: E402
from sghy_server.proto import ps_protobuf_pb2 as pb     # noqa: E402

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

    def log(self, *args):
        print("   [srv] " + " ".join(str(a) for a in args))

    def take(self, opcode):
        for op, body in reversed(self.sent):
            if op == opcode:
                return body
        return None


def build_record(lid, hero_id, hero_type, dmg, heal, taken):
    rec = pb.BattleRecordData()
    rec.levelType = lid
    rec.myDieCout = 0
    me = rec.myGroup
    me.id = 1001
    me.formation = 1
    h = me.heros.add()
    h.id = hero_id
    h.type = hero_type
    h.lv = 30
    h.position = 1
    h.recordDamage = dmg
    h.recordHealing = heal
    h.recordDamageTaken = taken
    h.recordUseSkillCount = 4
    h.recordActionCount = 9
    h.recordApplyBuff = 2
    h.recordDamageBlock = 1
    h.recordStopActionCount = 3
    tg = rec.targetGroups.add()
    tg.id = 9001
    tg.formation = 1
    tg.name = "黄巾军"
    eh = tg.heros.add()
    eh.id = 9001
    eh.type = 10001
    eh.lv = 10
    eh.position = 1
    eh.recordDamage = 777
    return rec


def main():
    LID = 1001
    rec = build_record(LID, hero_id=627, hero_type=10086, dmg=123456, heal=888, taken=4321)

    # ---- 1. LevelEnd（失败路径，校验最宽松；战报与输赢无关）----
    print("\n[1] LevelEnd(5031) 上传战报")
    msg = pb.LevelBattleRecord()
    msg.id = LID
    msg.battleRecord.CopyFrom(rec)
    msg.result = 0
    msg.star = 0
    msg.useTime = 42
    s = FakeSession()
    core.dispatch(s, 5031, msg.SerializeToString())

    lr_body = s.take(5031)
    lr = pb.LevelResult()
    lr.ParseFromString(lr_body or b"")
    check(bool(lr.battleID), "LevelResult 带 battleID（%r）" % (lr.battleID,))
    check(lr.id == LID, "LevelResult.id=%d" % lr.id)
    key = lr.battleID

    # ---- 2. ShowBattle 取回 ---- 
    print("\n[2] ShowBattle(5070) 按 battleID 取战报")
    req = pb.ShowBattle()
    req.key = key
    s2 = FakeSession()
    core.dispatch(s2, 5070, req.SerializeToString())
    body = s2.take(5070)
    check(body is not None, "回了 5070")
    aa = pb.AttackArena()
    aa.ParseFromString(body or b"")
    check(aa.id == LID, "AttackArena.id=%d（关卡）" % aa.id)
    check(aa.result == 0, "AttackArena.result=%d" % aa.result)
    check(aa.recordKey == key, "AttackArena.recordKey=%r" % aa.recordKey)
    check(aa.HasField("battleRecord"), "AttackArena.battleRecord 存在")
    br = aa.battleRecord
    check(br.levelType == LID, "battleRecord.levelType=%d（战报面板按它查波次）" % br.levelType)
    check(len(br.myGroup.heros) == 1, "我方 1 人")
    h = br.myGroup.heros[0]
    check(h.id == 627 and h.type == 10086, "武将 id/type 原样（%d/%d）" % (h.id, h.type))
    check(h.recordDamage == 123456, "统计 recordDamage=%d" % h.recordDamage)
    check(h.recordHealing == 888, "统计 recordHealing=%d" % h.recordHealing)
    check(h.recordDamageTaken == 4321, "统计 recordDamageTaken=%d" % h.recordDamageTaken)
    check(h.recordUseSkillCount == 4 and h.recordActionCount == 9,
          "技能/行动次数=%d/%d" % (h.recordUseSkillCount, h.recordActionCount))
    check(h.recordApplyBuff == 2 and h.recordDamageBlock == 1 and h.recordStopActionCount == 3,
          "加buff/格挡/打断=%d/%d/%d" % (h.recordApplyBuff, h.recordDamageBlock,
                                        h.recordStopActionCount))
    check(len(br.targetGroups) == 1 and br.targetGroups[0].name == "黄巾军",
          "敌方波次名称=%r" % (br.targetGroups[0].name if len(br.targetGroups) else None))

    # ---- 3. key 为空 → 回退最近一条 ----
    print("\n[3] ShowBattle key 为空 → 回退最近一条")
    s3 = FakeSession()
    req3 = pb.ShowBattle()
    req3.key = ""          # 显式空串（required 字段必须赋值才能序列化）
    core.dispatch(s3, 5070, req3.SerializeToString())
    aa3 = pb.AttackArena()
    aa3.ParseFromString(s3.take(5070) or b"")
    check(aa3.HasField("battleRecord") and aa3.battleRecord.myGroup.heros[0].recordDamage == 123456,
          "空 key 也拿到记录")

    # ---- 4. 无记录 → 空包不崩 ----
    print("\n[4] 查不到的 key → 空 AttackArena")
    core._BATTLE_RECORDS.clear()
    core._BATTLE_RECORD_LAST[0] = None
    s4 = FakeSession()
    req4 = pb.ShowBattle()
    req4.key = "NOT-EXIST"
    core.dispatch(s4, 5070, req4.SerializeToString())
    aa4 = pb.AttackArena()
    aa4.ParseFromString(s4.take(5070) or b"")
    check(aa4.id == 0 and not aa4.HasField("battleRecord"), "无记录回 id=0 空包")

    print("\nTOTAL: %d passed, %d failed" % (TOTAL[1], TOTAL[0] - TOTAL[1]))
    return 0 if TOTAL[1] == TOTAL[0] else 1


if __name__ == "__main__":
    sys.exit(main())
