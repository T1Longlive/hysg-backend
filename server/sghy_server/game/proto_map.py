"""opcode → protobuf 消息映射 + 请求字段读取助手（typed-first，字节级回退）。

设计：
  · parse_typed(opcode, pb) 用 protoc 生成的类解析请求；解析失败或 opcode
    未映射时返回 None，助手自动回退到字节级读取 —— 行为与原版一致。
  · 每个助手（req_id / req_function / ...）都可被黄金测试直接单测：
    构造 typed 消息 → 编码 → 分别走 typed 与字节级两条路 → 断言相等。
  · 迁移顺序：助手先上线（带回退），黄金测试通过后，handler 逐个改为只走
    typed；全部迁完再删字节级回退。

param 的教训（2026-10-21 的 bug）：FunctionData.param 是 **string 字段**，
客户端传 Lua number，pbc 的 luaL_checklstring 会把它 ASCII 化成 "52" 上线。
字节级读取若先当 varint 解，会把 '5'(0x35=53) 当 id —— 整个「用符不升级」
bug 的根因。typed 解析下 msg.param 就是 "52"，_to_int() 转回数字，永绝此类。
"""

from ..proto import ps_protobuf_pb2 as pb

# ---------------------------------------------------------------- 映射表

OPCODE_TO_MSG = {
    10001: "IDMessage",                          # SelectCharacter 选角
    10101: "IDMessage",                          # SetGuideIndex 引导进度
    5027: "IDMessage",                           # AdvanceExp 一键提炼
    5013: "IDMessage",                           # DownEquip 卸下装备
    5014: "IDMessage",                           # Strengthen 强化一次
    5020: "IDMessage",                           # Synthetic 碎片合成
    5016: "IDInt32Message",                      # StrengthenAuto 批量强化
    5030: "LevelBegin",                          # LevelBegin 开战
    5031: "LevelBattleRecord",                   # LevelEnd 结算上报
    # 挑战副本（2026-10-29）：阵营副本走 6007/6008，报文与 5030/5031 同构。
    # ★ 必须 typed 解析：battleRecord/useTime/totalDamage 都要读（校验与金银岛结算）。
    6007: "LevelBegin",                          # ActivityLevelBegin（阵营副本开战）
    6008: "LevelBattleRecord",                   # ActivityLevelEnd（阵营副本结算）
    6009: "IDCountMessage",                      # DailyChallengeSweep（VIP 挑战扫荡）
    5070: "ShowBattle",                          # ShowBattle 取战报（key=battleID）
    5032: "IDCountMessage",                      # ReapLevel 扫荡（id=关卡, count=次数）
    6004: "IDMessage",                           # BuyLevelTimes 重置关卡次数（id=关卡）
    20412: "EquipHeroToBattleGameplayIndexRequest",  # 上阵/换位
    20413: "EquipHeroToBattleGameplayIndexRequest",  # 下阵
    5023: "PropActionMessage",                   # AwakenHero 突破
    5024: "PropActionMessage",                   # EvolveHero 升星
    5012: "PropActionMessage",                   # WearEquip 穿戴装备
    5008: "FunctionData",                        # UseFunction 用符/用道具
    8000: "StringMessage",                       # UseActivationCode → GM 命令台
    5028: "IDListMessage",                       # GradeHeroList 升阶
    6021: "IDListMessage",                       # GradeUpEquip 装备升阶
    6023: "IDListMessage",                       # StarUpEquip 装备升星
    6024: "IDListMessage",                       # UnlockAssist 解锁副属性
    6025: "IDListMessage",                       # Reform 洗练
    6026: "IDListMessage",                       # SaveReform 保存洗练
    6027: "IDListMessage",                       # GiveUpReform 放弃洗练
    20210: "IDListMessage",                      # Decompose 分解装备
    5040: "UseLotteryData",                      # UseLottery 招募/寻宝
    10115: "ReceiveCollectionAwardRequestData",  # 图鉴收集奖励领取
    # ---- 军旗系统（2026-10-23）----
    5346: "MultIDCounteListByIDMessage",         # 制作部件
    5347: "MultIDCounteListByIDMessage",         # 升级部件
    5348: "IDMessage",                           # 解锁词条
    5349: "IDMessage",                           # 分解部件
    5351: "PropActionMessage",                   # 装备部件到旗帐
    5352: "IDMessage",                           # 卸下部件
    5353: "EquipFlagToBattleGameplayIndexRequest",       # 军旗上阵
    5354: "UnequipFlagFromBattleGameplayIndexRequest",   # 军旗下阵
    31204: "IDListMessage",                      # 跑商选图
    31209: "IDListMessage",                      # 跑商地图红点移除
    5355: "IDCounteListByIDMessage",             # 图纸合成
}


def parse_typed(opcode: int, pb_bytes: bytes):
    """按映射表用生成的类解析请求；失败返回 None（调用方走字节级回退）。"""
    name = OPCODE_TO_MSG.get(opcode)
    if not name:
        return None
    cls = getattr(pb, name, None)
    if cls is None:
        return None
    try:
        msg = cls()
        msg.ParseFromString(pb_bytes)
        return msg
    except Exception:
        return None


# ---------------------------------------------------------------- 小工具

def _to_int(v, default=0):
    """string 字段里的数字（客户端 pbc ASCII 化的产物）安全转 int。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 请求助手
# 每个助手：tmsg 非 None 走 typed；否则回退字节级（参数 pb_bytes）。

def req_id(tmsg, pb_bytes, _r=None):
    """IDMessage{id}"""
    if tmsg is not None:
        return tmsg.id
    return (_r(pb_bytes, 1) or 0) if _r else 0


def req_id_value(tmsg, pb_bytes, _r=None):
    """IDInt32Message{id, value} -> (id, value)"""
    if tmsg is not None:
        return tmsg.id, tmsg.value
    if _r:
        return (_r(pb_bytes, 1) or 0), (_r(pb_bytes, 2) or 0)
    return 0, 0


def req_id_count(tmsg, pb_bytes, _r=None):
    """IDCountMessage{id, count} -> (id, count)"""
    if tmsg is not None:
        return tmsg.id, (tmsg.count if tmsg.count else 1)
    if _r:
        return (_r(pb_bytes, 1) or 0), (_r(pb_bytes, 2) or 1)
    return 0, 1


def req_prop_action(tmsg, pb_bytes, _r=None):
    """PropActionMessage{id, targetID} -> (id, targetID)"""
    if tmsg is not None:
        return tmsg.id, tmsg.targetID
    if _r:
        return (_r(pb_bytes, 1) or 0), (_r(pb_bytes, 2) or 0)
    return 0, 0


def req_id_list(tmsg, pb_bytes, _rl=None):
    """IDListMessage{repeated id} -> [int]"""
    if tmsg is not None:
        return [int(x) for x in tmsg.id]
    if _rl:
        return [int(x) for x in _rl(pb_bytes, 1)]
    return []


def req_function(tmsg, pb_bytes, _r=None, _rs=None):
    """FunctionData{id, name, count, param} -> (id, name, count, param_int)

    param 是 string 字段：客户端传数字被 pbc ASCII 化（"52"），
    typed 路径下 _to_int(msg.param)；字节级路径按 string 优先读（历史 bug 修复）。
    """
    if tmsg is not None:
        return tmsg.id, tmsg.name, tmsg.count, _to_int(tmsg.param)
    if _r and _rs:
        item_id = _r(pb_bytes, 1) or 0
        fname = _rs(pb_bytes, 2) or ""
        cnt = _r(pb_bytes, 3) or 0
        # param 只按 string 读。**不提供 varint 回退**：客户端 pbc 对 string
        # 字段必走 luaL_checklstring（数字也 ASCII 化），线型永远是
        # length-delimited；varint 回退只会把 'a'(97) 这类字节当 id 解出来
        # —— 2026-10-21 那个 52→53 的 bug 同类，黄金测试已抓住。
        param = _to_int(_rs(pb_bytes, 4), 0)
        return item_id, fname, cnt, param
    return 0, "", 0, 0


def req_string_message(tmsg, pb_bytes, _rs=None):
    """StringMessage{msg = 1} -> msg 文本（8000 GM 命令台用）"""
    if tmsg is not None:
        return tmsg.msg or ""
    return (_rs(pb_bytes, 1) or "") if _rs else ""


def req_equip_hero(tmsg, pb_bytes, _r=None):
    """EquipHeroToBattleGameplayIndexRequest{category, index, id, position}"""
    if tmsg is not None:
        return tmsg.category, tmsg.index, tmsg.id, tmsg.position
    if _r:
        return ((_r(pb_bytes, 1) or 0), (_r(pb_bytes, 2) or 1),
                (_r(pb_bytes, 3) or 0), (_r(pb_bytes, 4) or 0))
    return 0, 1, 0, 0


def req_level_begin(tmsg, pb_bytes, _r=None):
    """LevelBegin{id}"""
    if tmsg is not None:
        return tmsg.id
    return (_r(pb_bytes, 1) or 1002) if _r else 1002


def req_level_end(tmsg, pb_bytes, _r=None):
    """LevelBattleRecord{id, result, star} -> (id, result, star)"""
    if tmsg is not None:
        return tmsg.id, tmsg.result, tmsg.star
    if _r:
        return ((_r(pb_bytes, 1) or 1002), (_r(pb_bytes, 3) or 1),
                (_r(pb_bytes, 4) or 1))


def req_use_lottery(tmsg, pb_bytes, _r=None):
    """UseLotteryData{id=记录 id, useItem=bool} -> (id, use_item)

    id 是 LotteryRecordData.id（服务端记录主键），不是配置 type ——
    客户端 EmployeeManager:reqEmploy 传的是 employRecord:getId()。
    """
    if tmsg is not None:
        return int(tmsg.id), bool(tmsg.useItem)
    if _r:
        return int(_r(pb_bytes, 1) or 0), bool(_r(pb_bytes, 2))
    return 0, False


def req_collection_award(tmsg, pb_bytes, _r=None):
    """ReceiveCollectionAwardRequestData{type, subType, milestoneCount} -> (type, subType, milestone)"""
    if tmsg is not None:
        return int(tmsg.type), int(tmsg.subType), int(tmsg.milestoneCount)
    if _r:
        return (int(_r(pb_bytes, 1) or 0), int(_r(pb_bytes, 2) or 0),
                int(_r(pb_bytes, 3) or 0))
    return 0, 0, 0
    return 1002, 1, 1
