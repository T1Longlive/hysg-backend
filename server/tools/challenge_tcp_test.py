"""挑战副本 TCP 冒烟（真服务端链路）：5327 / 5034 / 5037 / 6007 / 6008。

复用 smoke_client 的登录与帧收发；只验证「线上真实管道」能走通协议，
奖励数量级与业务规则由 tools/challenge_test.py 覆盖。

用法： python tools/challenge_tcp_test.py

★ 会**真实消耗**当天次数（圣兽/阵营各 1 次）。跑完想复原：
   1) 关掉服务端窗口（内存状态会覆盖库）
   2) python tools/reset_challenge_state.py
   3) 重新启动服务端
"""
import base64
import json
import os
import socket
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import smoke_client as sc                                    # noqa: E402
from sghy_server.proto import ps_protobuf_pb2 as pb          # noqa: E402
from sghy_server.game import challenge                       # noqa: E402

def ask(sock, buf, opcode, msg=None):
    """发请求 → 等指定 opcode 回包（跳过服务端主动推送的帧）。

    msg=None 时发**空包**：客户端 `socket:send(nil, 5327)` 就是 4B opcode + 4B
    protoIndex、body 零字节（原版空请求），用任何 pb 消息都会因 required 字段报错。
    """
    if msg is None:
        payload = struct.pack("<i", opcode) + struct.pack("<i", 1)
        sock.sendall(struct.pack("<i", sc.MAGIC) +
                     struct.pack("<i", 8 + 0) + payload)
    else:
        sock.sendall(sc.encode_request(opcode, msg))
    # 登录阶段服务端会推 600+ 条 520（关卡记录），默认 max_frames=50 不够
    return sc.recv_until(sock, buf, opcode, max_frames=3000)


def ask_any(sock, buf, opcodes, msg=None, max_frames=3000):
    """发请求 → 返回第一个命中 opcodes 集合的回包 (opcode, body)。

    用于「可能成功也可能被拒」的请求（次数用尽返回 5002）。"""
    if msg is None:
        payload = struct.pack("<i", 0) + struct.pack("<i", 1)
        sock.sendall(struct.pack("<i", sc.MAGIC) + struct.pack("<i", 8) + payload)
    else:
        sock.sendall(sc.encode_request(opcodes[0], msg))
    want = set(opcodes)
    for _ in range(max_frames):
        got = sc.read_frame(sock, buf)
        if got is None:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
            continue
        if got[0] in want:
            return got
    raise TimeoutError("opCodes %s not seen" % opcodes)


PASS = [0, 0]


def check(cond, label):
    PASS[0] += 1
    if cond:
        PASS[1] += 1
        print("  ok   %s" % label)
    else:
        print("  FAIL %s" % label)
    return cond


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2724
    sock = socket.create_connection((host, port), timeout=10)
    buf = bytearray()

    # ---- 登录（与客户端同格式：base64(JSON)+换行）----
    login = json.dumps({"Command": "Login", "Account": "smoke", "Password": "x",
                        "ServerID": "10001", "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    body = sc.recv_until(sock, buf, 100, max_frames=3000)
    ok = pb.LoginOK()
    ok.ParseFromString(body)
    cid = ok.characters[0].id if ok.characters else 1001

    sel = pb.IDMessage()
    sel.id = cid
    sock.sendall(sc.encode_request(10001, sel))
    body = sc.recv_until(sock, buf, 102, max_frames=3000)
    eg = pb.EnterGame()
    eg.ParseFromString(body)
    print("[login] EnterGame lv=%d coin=%d ActivityTherionTimes=%d/Coin=%d"
          % (eg.character.lv, eg.character.coin,
             eg.ActivityTherionTimes, eg.ActivityCoinLevelTimes))
    print("[login] actionTime.leftTherionTimes=%d"
          % eg.character.actionTime.leftTherionTimes)

    # ---- 5327 活动关卡组 ----
    body = ask(sock, buf, 5327)
    check(bool(body), "5327 回包 opcode 正确")
    grp = pb.ActivityLevelGroupData()
    grp.ParseFromString(body)
    check([d.Type for d in grp.levelGroup] == [challenge.camp_act_today()],
          "levelGroup 只发今天那一组阵营（%s）" % [d.Type for d in grp.levelGroup])
    base = {b.type: b for b in grp.baseInfo}
    _open_acts = [a for a, _n in challenge.THERION_TYPES
                  if challenge.open_today(challenge.group_by_act(a))]
    check(sorted(base) == sorted(_open_acts + [5]),
          "baseInfo = 今天开放的圣兽岛 %s + 金银岛" % _open_acts)
    check(base.get(5) and list(base[5].levelTypes) == [80001, 80002, 80003, 80004, 80005],
          "金银岛难度链 80001~80005")

    # ---- 5034 圣兽信息 ----
    body = ask(sock, buf, 5034)
    check(bool(body), "5034 回包 opcode 正确")
    ti = pb.NewTherionInfo()
    ti.ParseFromString(body)
    check(len(ti.baseInfo) == 4, "5034 四座圣兽岛")

    # ---- 5037 挑战面板信息 ----
    body = ask(sock, buf, 5037)
    check(bool(body), "5037 回包 opcode 正确")
    ci = pb.ChallengeInfo()
    ci.ParseFromString(body)
    check(ci.coinIslandInfo.times == 2, "金银岛次数 = 2")
    check(ci.worldBoss.IsWorldBossOpen == 2, "世界BOSS = close")

    # ---- 6007 / 6008 阵营副本：开战 + 结算 ----
    # 只打今天开放的那一组（每天一组，4 天轮换）
    _act = challenge.camp_act_today()
    body, cand = None, None
    for _cand in [challenge.group_by_act(_act)["levels"][0]]:
        _m = pb.IDMessage(); _m.id = _cand
        _op, _b = ask_any(sock, buf, (6007, 5002), _m)
        if _op == 6007:
            body, cand = _b, _cand
            break
    check(body is not None, "6007 有回包（今日阵营=%s，关卡 %s）"
          % (challenge.CAMP_NAMES[_act], cand))
    if body is None:
        print("  (本日阵营次数已用完，跳过战斗结算；"
              "先停服再跑 tools/reset_challenge_state.py 可复跑)")
        sock.close()
        print("\nTOTAL: %d/%d" % (PASS[1], PASS[0]))
        return 0 if PASS[0] == PASS[1] else 1
    lb = pb.LevelBegin()
    lb.ParseFromString(body)
    lid = int(lb.id)          # 以服务端回包为准（回包 opcode/关卡必须自洽）
    # 注：这一轮可能读到上一条请求的回包（TCP 缓冲里残留的 5002/6007 交错），
    # 所以只校验「是四组阵营之一且带敌方阵容」，不强制 == 本次请求的 cand。
    check(lid in challenge.group_by_act(_act)["levels"] and len(lb.groups) > 0,
          "6007 LevelBegin id=%s 敌方 %d 组（请求 %s）" % (lid, len(lb.groups), cand))

    rec = pb.LevelBattleRecord()
    rec.id = lid
    rec.result = 1
    rec.star = 1
    rec.totalDamage = 30000      # 90030 敌血 28070，落在合法区间
    rec.useTime = 30
    sock.sendall(sc.encode_request(6008, rec))
    body = sc.recv_until(sock, buf, 6008, max_frames=3000)
    check(bool(body), "6008 回包 opcode 正确")
    lr = pb.LevelResult()
    lr.ParseFromString(body)
    check(lr.id == lid, "LevelResult id = %s" % lr.id)
    check(len(lr.props) == 1, "随机掉 1 只武将（%d）" % len(lr.props))
    if lr.props:
        expect_pool = set(challenge._award_types(challenge.level_row(lid)))
        check(lr.props[0].type in expect_pool,
              "掉落来自该组 ShowAward 池：%s ∈ %s" % (lr.props[0].type, sorted(expect_pool)))
    print("[6008] coin=%d battleID=%s" % (lr.coin, lr.battleID))

    # 次数已用完 → 再开战应被 5002 拒绝（服务端不回 6007，客户端不切场景）
    _idm2 = pb.IDMessage(); _idm2.id = lid
    _op3, _b3 = ask_any(sock, buf, (6007, 5002), _idm2)
    check(_op3 == 5002, "次数用完 → 5002（opcode=%s）" % _op3)

    sock.close()
    print("\nTOTAL: %d/%d" % (PASS[1], PASS[0]))
    return 0 if PASS[0] == PASS[1] else 1


if __name__ == "__main__":
    sys.exit(main())
