"""协议冒烟测试：模拟客户端走完 登录 → 选角 → EnterGame → 用经验符升级。

同时充当新服务端的回归测试（替代手工点客户端）。
用法：python tools/smoke_client.py [host] [port]
"""

import base64
import json
import struct
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0] + "/..")
sys.path.insert(0, __file__.rsplit("/", 1)[0] + "/../..")

from sghy_server.proto import ps_protobuf_pb2 as pb

MAGIC = 10008


def encode_request(opcode: int, msg) -> bytes:
    body = msg.SerializeToString()
    payload = struct.pack("<i", opcode) + struct.pack("<i", 1) + body
    return struct.pack("<i", MAGIC) + struct.pack("<i", 8 + len(body)) + payload


def read_frame(sock, buf: bytearray):
    """解析一条响应帧 [4B len][4B opCode][pb]（len = 4 + len(pb)）。

    返回 (opcode, pb_bytes) 或 None；不完整的部分留在 buf。
    """
    while True:
        if len(buf) < 8:
            return None
        length = struct.unpack_from("<i", buf, 0)[0]
        total = 4 + length
        if len(buf) < total:
            return None
        opcode = struct.unpack_from("<i", buf, 4)[0]
        pb = bytes(buf[8:total])
        del buf[:total]
        return opcode, pb


def recv_until(sock, buf, want_opcode, max_frames=50):
    for _ in range(max_frames):
        got = read_frame(sock, buf)
        if got is None:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
            continue
        opcode, body = got
        if opcode == want_opcode:
            return body
    raise TimeoutError("opCode %d not seen" % want_opcode)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2724

    import socket
    sock = socket.create_connection((host, port), timeout=10)
    buf = bytearray()

    # 1) 登录首包（账号隔离：smoke 账号自带独立角色，不碰真实存档）
    #    可选 argv[3] 指定账号（默认 smoke）——换账号可强制走"建新号"链路验证开局包
    acct = sys.argv[3] if len(sys.argv) > 3 else "smoke"
    login = json.dumps({"Command": "Login", "Account": acct, "Password": "x",
                        "ServerID": "10001", "Platform": "youguTV",
                        "IsVisitor": "False", "Device": "smoke-runner"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    body = recv_until(sock, buf, 100)
    ok = pb.LoginOK()
    ok.ParseFromString(body)
    print("[1] LoginOK characters=%d" % len(ok.characters))

    # 1b) 0 角色 -> 建号（名字全局唯一，带随机尾巴；客户端此时也会走这条路）
    if len(ok.characters) == 0:
        import random
        cc = pb.CreateCharacter()
        cc.nickName = "烟测%04d" % random.randint(0, 9999)
        cc.gender = 1
        cc.type = 1
        sock.sendall(encode_request(10000, cc))
        body = recv_until(sock, buf, 101)
        cko = pb.CreateCharacterOK()
        cko.ParseFromString(body)
        print("[1b] CreateCharacter id=%d name=%s" % (cko.id, cko.name))
        my_id = cko.id
    else:
        my_id = ok.characters[0].id
        print("[1b] 已有角色 id=%d name=%s" % (my_id, ok.characters[0].name))

    # 2) 选角 -> EnterGame
    sel = pb.IDMessage()
    sel.id = my_id
    sock.sendall(encode_request(10001, sel))
    body = recv_until(sock, buf, 102)
    eg = pb.EnterGame()
    eg.ParseFromString(body)
    print("[2] EnterGame heroes=%d equips=%d items=%d battleGameplays=%d levelRecords=%d"
          % (len(eg.heros), len(eg.equips), len(eg.items), len(eg.battleGameplays), len(eg.levelRecords)))

    # 3) 找一只没满级的武将（lv<maxLv 且不是 6★40 那种），用天灵符
    target = None
    for h in eg.heros:
        if h.lv < h.maxLv and h.exp == 0 and h.lv >= 1:
            target = h
            break
    if target is None:
        # 新开局（2026-10-30）：只有固定等级圣兽，用符段跳过，战斗链照跑
        print("[3] 没有可升级武将（新开局=固定等级圣兽+未开卡），跳过用符")
    else:
        print("[3] target hero id=%d type=%d lv=%d exp=%d maxLv=%d power=%d"
              % (target.id, target.type, target.lv, target.exp, target.maxLv, target.power))

        # 4) 用符（FunctionData.param 是 string 字段 —— 客户端 pbc 会把数字 ASCII 化）
        fn = pb.FunctionData()
        fn.id = 20007          # 天灵符 +1000
        fn.name = "AddProperty"
        fn.count = 1
        fn.param = str(target.id)
        sock.sendall(encode_request(5008, fn))
        print("[4] sent UseFunction(20007 x1, hero=%d)" % target.id)

        # 5) 收齐响应：451 道具 / 200 角色 / 453 武将 / 5008 ack
        seen = {}
        for _ in range(60):
            got = read_frame(sock, buf)
            if got is None:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                continue
            opcode, body = got
            seen[opcode] = body
            if opcode == 5008:
                break
        for op in sorted(seen):
            print("[5] resp opCode=%d (%d bytes)" % (op, len(seen[op])))

        if 453 in seen:
            h = pb.HeroData()
            h.ParseFromString(seen[453])
            print("[5] 453 hero id=%d lv=%d exp=%d maxLv=%d power=%d"
                  % (h.id, h.lv, h.exp, h.maxLv, h.power))
            if h.lv > target.lv or (h.lv == target.lv and h.exp > target.exp):
                print("PASS: 武将数据已更新")
            else:
                print("FAIL: 武将数据没变")
        else:
            print("FAIL: 没收到 453")

    # 6) 战斗：LevelBegin + 合法胜利
    lb = pb.LevelBegin()
    lb.id = 1001
    sock.sendall(encode_request(5030, lb))
    body = recv_until(sock, buf, 5030)
    lb2 = pb.LevelBegin()
    lb2.ParseFromString(body)
    print("[6] LevelBegin groups=%d (关卡存在)" % len(lb2.groups))

    rec = pb.LevelBattleRecord()
    rec.id = 1001
    rec.result = 1
    rec.star = 1
    rec.totalDamage = 200        # 1001 敌方总血≈200，合法区间内
    rec.useTime = 30
    sock.sendall(encode_request(5031, rec))
    got = {}
    for _ in range(60):
        g = read_frame(sock, buf)
        if g is None:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            continue
        op, b = g
        got[op] = b
        if op == 5031:
            break
    lr = pb.LevelResult()
    if 5031 in got:
        lr.ParseFromString(got[5031])
        print("[7] 合法胜利 LevelResult: coin=%d exp=%d teamExp=%d" % (lr.coin, lr.exp, lr.teamExp))
        print("PASS: 合法胜利有奖励" if lr.coin > 0 else "FAIL: 合法胜利没奖励")
    else:
        print("FAIL: 合法胜利没回 5031")

    # 8) 战斗：伪造星级（客户端只可能报 0/1）
    rec2 = pb.LevelBattleRecord()
    rec2.id = 1001
    rec2.result = 1
    rec2.star = 3
    rec2.totalDamage = 200
    rec2.useTime = 30
    sock.sendall(encode_request(5031, rec2))
    got2 = {}
    for _ in range(60):
        g = read_frame(sock, buf)
        if g is None:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            continue
        op, b = g
        got2[op] = b
        if op == 5031:
            break
    lr2 = pb.LevelResult()
    if 5031 in got2:
        lr2.ParseFromString(got2[5031])
        rejected = 5002 in got2 and lr2.coin == 0
        print("[8] 伪造星级 LevelResult: coin=%d (%s)" % (
            lr2.coin, "带5002提示" if 5002 in got2 else "无提示"))
        print("PASS: 伪造星级被拒且零奖励" if rejected else "FAIL: 伪造星级未被拦")
    else:
        print("FAIL: 伪造星级没回 5031")

    sock.close()


if __name__ == "__main__":
    main()
