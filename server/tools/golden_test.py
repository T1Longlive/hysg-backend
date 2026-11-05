"""黄金测试：proto_map 助手的「typed 路径」与「字节级路径」必须等价。

做法：用 protoc 生成的类构造已知取值的请求消息 -> 编码成线上字节 ->
两条路各解一次 -> 断言结果相等。这正是在证明「迁到真 protobuf 不改行为」。

跑法：python tools/golden_test.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sghy_server.game import core
from sghy_server.game.proto_map import (OPCODE_TO_MSG, parse_typed, req_equip_hero,
                                        req_function, req_id, req_id_list,
                                        req_id_value, req_level_begin,
                                        req_level_end, req_prop_action)
from sghy_server.proto import ps_protobuf_pb2 as pb

_r = core._read_varint_field
_rs = core._read_string_field
_rl = core._read_varint_fields

PASS = 0
FAIL = 0


def check(name, typed_result, byte_result):
    global PASS, FAIL
    if typed_result == byte_result:
        PASS += 1
        print("  PASS %-34s -> %r" % (name, typed_result))
    else:
        FAIL += 1
        print("  FAIL %-34s typed=%r byte=%r" % (name, typed_result, byte_result))


def both(opcode, msg):
    """返回 (typed解析结果, 字节级解析结果) 的构造器闭包"""
    data = msg.SerializeToString()
    tmsg = parse_typed(opcode, data)
    assert tmsg is not None, "parse_typed 失败: opcode=%d" % opcode
    return data, tmsg


print("== 映射表健全性 ==")
for op, name in OPCODE_TO_MSG.items():
    cls = getattr(pb, name, None)
    ok = cls is not None
    print("  %s opCode=%-6d -> %s" % ("PASS" if ok else "FAIL", op, name))
    PASS += 1 if ok else 0
    FAIL += 0 if ok else 1

print("== req_id (IDMessage) ==")
m = pb.IDMessage(); m.id = 1001
data, t = both(10001, m)
check("id=1001", req_id(t, None), req_id(None, data, _r))

print("== req_id_value (IDInt32Message) ==")
m = pb.IDInt32Message(); m.id = 5; m.value = 3
data, t = both(5016, m)
check("id=5 value=3", req_id_value(t, None), req_id_value(None, data, _r))

print("== req_prop_action (PropActionMessage) ==")
m = pb.PropActionMessage(); m.id = 7; m.targetID = 9
data, t = both(5024, m)
check("id=7 target=9", req_prop_action(t, None), req_prop_action(None, data, _r))

print("== req_id_list (IDListMessage) ==")
m = pb.IDListMessage(); m.id.extend([1, 2, 3])
data, t = both(5028, m)
check("ids=[1,2,3]", req_id_list(t, None), req_id_list(None, data, _rl))
m2 = pb.IDListMessage()
data2, t2 = both(5028, m2)
check("ids=[]", req_id_list(t2, None), req_id_list(None, data2, _rl))

print("== req_function (FunctionData) —— 历史 bug 的主战场 ==")
m = pb.FunctionData(); m.id = 20007; m.name = "AddProperty"; m.count = 1; m.param = "52"
data, t = both(5008, m)
check("param='52' -> 52", req_function(t, None), req_function(None, data, _r, _rs))
m = pb.FunctionData(); m.id = 20006; m.name = "AddProperty"; m.count = 33; m.param = "3"
data, t = both(5008, m)
check("param='3' -> 3", req_function(t, None), req_function(None, data, _r, _rs))
m = pb.FunctionData(); m.id = 20005; m.name = "AddProperty"; m.count = 2; m.param = ""
data, t = both(5008, m)
check("param='' -> 0", req_function(t, None), req_function(None, data, _r, _rs))
m = pb.FunctionData(); m.id = 20005; m.name = "Synthetic"; m.count = 2; m.param = "abc"
data, t = both(5008, m)
check("param='abc' -> 0", req_function(t, None), req_function(None, data, _r, _rs))

print("== req_equip_hero (EquipHeroToBattleGameplayIndexRequest) ==")
m = pb.EquipHeroToBattleGameplayIndexRequest()
m.category = 0; m.index = 2; m.id = 23; m.position = 3; m.categoryPlayMode = 1
data, t = both(20412, m)
check("cat0 idx2 id23 pos3", req_equip_hero(t, None), req_equip_hero(None, data, _r))

print("== req_level_begin (LevelBegin) ==")
m = pb.LevelBegin(); m.id = 1002
data, t = both(5030, m)
check("id=1002", req_level_begin(t, None), req_level_begin(None, data, _r))

print("== req_level_end (LevelBattleRecord) ==")
m = pb.LevelBattleRecord(); m.id = 1002; m.result = 1; m.star = 3
data, t = both(5031, m)
check("id1002 res1 star3", req_level_end(t, None), req_level_end(None, data, _r))

print("== parse_typed 兜底 ==")
check("未知 opcode -> None", parse_typed(99999, b"\x08\x01"), None)
check("垃圾字节 -> None", parse_typed(5008, b"\xff\xff\xff\xff"), None)
# 空 body：protobuf 解析出「全默认值」消息（不抛错），与字节级缺失字段取
# 默认值等价 —— 断言的是取值等价（id=0）而不是 None。
t = parse_typed(10001, b"")
check("空 body -> id=0", req_id(t, None) if t is not None else "None",
      req_id(None, b"", _r))

print()
print("TOTAL: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
