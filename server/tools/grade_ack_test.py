"""升阶响应 opcode + 扣费验证（2026-10-21 建，2026-10-28 重写为确定性场景）。

回归背景：客户端 reqShengjie 发 5028（GradeHeroByList），但 ackShengjie 只注册在
5021（GradeHeroRt）。旧服务端回 5028 被 SocketHandler 丢弃（"没有解析的谷歌
协议: opCode = 5028"）→ 界面永不刷新但材料已被消耗。

2026-10-28：GM 发确定性场景（曹操 10013 3★ + 2★材料 ×2）——旧版动态选英雄会选到
CanUpgrade=0 的卡必然 5002（基线即红）；并补武将升阶扣铜币断言
（UpGradeExpToCoin × 材料总进度，客户端 caculateCoinCost 同式）与帧序断言
（200 必须先于 5021 ack）。

用法：python tools/grade_ack_test.py [host] [port]
断言：5028 请求后 HeroData 挂 5021；499 删材料 typeID=1；扣币 63×86=5418；
帧序 [200, 5021]。
"""

import base64
import json
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.proto import ps_protobuf_pb2 as pb

MAGIC = 10008


def encode_request(opcode: int, msg) -> bytes:
    body = msg.SerializeToString()
    payload = struct.pack("<i", opcode) + struct.pack("<i", 1) + body
    return struct.pack("<i", MAGIC) + struct.pack("<i", 8 + len(body)) + payload


def read_frame(sock, buf: bytearray):
    while True:
        if len(buf) < 8:
            return None
        length = struct.unpack_from("<i", buf, 0)[0]
        total = 4 + length
        if len(buf) < total:
            return None
        opcode = struct.unpack_from("<i", buf, 4)[0]
        body = bytes(buf[8:total])
        del buf[:total]
        return opcode, body


def collect(sock, buf, stop_ops, max_frames=30, drain=False):
    seen = []
    for _ in range(max_frames):
        got = read_frame(sock, buf)
        if got is None:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            continue
        seen.append(got)
        if got[0] in stop_ops:
            break
    return seen


def gm(sock, buf, text):
    """GM 命令（8000 → 5002 回显）。"""
    sm = pb.StringMessage()
    sm.msg = text
    sock.sendall(encode_request(8000, sm))
    out = {}
    for op, body in collect(sock, buf, {5002}):
        if op == 5002:
            out[5002] = body
    return out


def drain(sock, buf):
    sock.settimeout(0.3)
    while True:
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    del buf[:]
    sock.settimeout(3.0)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2724

    import socket
    sock = socket.create_connection((host, port), timeout=10)
    buf = bytearray()

    login = json.dumps({"Command": "Login", "Account": "grade_ack_test",
                        "Password": "x", "ServerID": "10001",
                        "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    for op, body in collect(sock, buf, {100}):
        if op == 100:
            ok = pb.LoginOK()
            ok.ParseFromString(body)
            print("[1] LoginOK characters=%d" % len(ok.characters))

    sock.settimeout(3.0)

    def reselect():
        sel = pb.IDMessage()
        sel.id = 1001
        sock.sendall(encode_request(10001, sel))
        eg = None
        for op, body in collect(sock, buf, {102}, max_frames=200):
            if op == 102:
                eg = pb.EnterGame()
                eg.ParseFromString(body)
        return eg

    # ---- GM 发确定性场景（旧版动态选英雄，会选到 CanUpgrade=0 的卡 →
    #      必然 5002「该武将无法升阶」，基线即红）----
    # 目标：曹操 10013（3★，CanUpgrade=1）；材料：10012（2★）×2
    for cmd in ("add_coin 10000000", "add_hero 10013",
                "add_hero 10012", "add_hero 10012"):
        r = gm(sock, buf, cmd)
        print("[gm] %-22s -> %s" % (
            cmd, (r.get(5002) or b"").decode("utf-8", "replace")))
    drain(sock, buf)
    eg = reselect()
    print("[2] EnterGame heroes=%d" % len(eg.heros))

    t_list = [h for h in eg.heros if h.type == 10013]
    m_list = [h for h in eg.heros if h.type == 10012]
    assert t_list and len(m_list) >= 2, "GM 发的武将不在 EnterGame"
    target = max(t_list, key=lambda h: h.id)
    mats = sorted(m_list, key=lambda h: -h.id)[:2]
    print("[3] target=%s(%s 3★) mats=%s(2★)"
          % (target.id, target.name, [m.id for m in mats]))

    # 预期（客户端 WarriorShengjiePanel:caculateCoinCost 同式）：
    #   每件材料进度 = floor(GradeExp[3][2]*(0+0+200)/230) = floor(50*200/230) = 43
    #   总进度 = 86；铜币 = UpGradeExpToCoin[3] * 86 = 63*86 = 5418
    EXP_GAIN, COIN_COST = 86, 63 * 86
    coin0 = eg.character.coin

    idlist = pb.IDListMessage()
    idlist.id.append(target.id)
    for m in mats:
        idlist.id.append(m.id)
    sock.sendall(encode_request(5028, idlist))
    print("[4] sent GradeHeroByList hero=%d mats=%s (grade=%d gradeExp=%d)"
          % (target.id, [m.id for m in mats], target.grade, target.gradeExp))

    ok5021, ok499, leaked5028 = False, [], []
    order = []          # 200/5021 先后（帧序回归）
    coin_now = None
    for op, body in collect(sock, buf, {5021, 5002}, max_frames=80):
        if op == 5021:
            order.append(5021)
            h = pb.HeroData()
            h.ParseFromString(body)
            ok5021 = (h.id == target.id)
            print("[5] 5021 HeroData id=%d grade=%d gradeExp=%d  %s"
                  % (h.id, h.grade, h.gradeExp,
                     "OK" if ok5021 else "FAIL(id不匹配)"))
            if h.gradeExp != EXP_GAIN:
                print("FAIL: gradeExp=%d ≠ 客户端公式 %d" % (h.gradeExp, EXP_GAIN))
                sys.exit(1)
        elif op == 5002:
            sm = pb.ShowMessage()
            sm.ParseFromString(body)
            print("[5] 5002 ShowMessage code=%d msg=%s" % (sm.code, sm.msg))
        elif op == 499:
            dd = pb.DeleteData()
            dd.ParseFromString(body)
            ok499.append((dd.typeID, dd.id))
            print("[5] 499 DeleteData typeID=%d id=%d" % (dd.typeID, dd.id))
        elif op == 200:
            order.append(200)
            c = pb.CharacterData()
            c.ParseFromString(body)
            coin_now = c.coin
            print("[5] 200 coin=%d（应扣 %d）" % (coin_now, COIN_COST))
        elif op == 5028:
            leaked5028.append(len(body))

    print("----")
    if leaked5028:
        print("FAIL: 仍收到5028响应 %d 条（应挂5021）" % len(leaked5028))
        sys.exit(1)
    if not ok5021:
        print("FAIL: 没收到5021 HeroData 响应")
        sys.exit(1)
    if len(ok499) != 2 or any(t != 1 for t, _ in ok499):
        print("FAIL: 499 删材料应 2 条且 typeID=1（武将），实际 %s" % (ok499,))
        sys.exit(1)
    if sorted(i for _, i in ok499) != sorted(m.id for m in mats):
        print("FAIL: 499 删的不是材料 id")
        sys.exit(1)
    # 2026-10-28：武将升阶补扣铜币（UpGradeExpToCoin × 材料总进度）
    if coin_now is None:
        print("FAIL: 没收到 200 货币同步")
        sys.exit(1)
    if coin_now != coin0 - COIN_COST:
        print("FAIL: 铜币应扣 %d（%d -> %d，实际 %d）"
              % (COIN_COST, coin0, coin0 - COIN_COST, coin_now))
        sys.exit(1)
    # 帧序：200 必须先于 5021 ack（面板在 ack 当时重读币值）
    if order != [200, 5021]:
        print("FAIL: 帧序应为 [200, 5021]，实际 %s" % order)
        sys.exit(1)
    print("PASS: 5028→5021；499 删材料；扣币 %d；帧序 [200, 5021]" % COIN_COST)


if __name__ == "__main__":
    main()
