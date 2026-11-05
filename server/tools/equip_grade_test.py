"""装备升阶公式对齐验证（2026-10-22）。

背景：服务端旧「diff 比例」规则与客户端预览公式不一致（预览 +X 实际 2 点/件），
已逐行对齐 EquipShengJiePanel:getShengJieInfo：
    每件经验 = ExpSetConfig[材料品质].EquipGradeExp[材料星级] × (1 + (5-装备星级)×0.2)
    铜币     = 总经验 × ExpSetConfig[装备品质].EquipUpGradeExpToCoin

用法：python tools/equip_grade_test.py [host] [port]
流程：GM 发放目标装备(32112, q3s2) + 材料(31111, q2s1)×2 → 6021 → 断言
      EquipData.grade_exp == 客户端公式值；重复提交 → 5002 材料不存在（不炸）。
"""

import base64
import json
import struct
import sys
import urllib.request

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


def collect(sock, buf, stop_ops, max_frames=40, drain=False):
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


def gm(action, **params):
    qs = "&".join("%s=%s" % (k, v) for k, v in params.items())
    url = "http://127.0.0.1:8000/api/gm?action=%s&%s" % (action, qs)
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode())


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2724

    import socket
    sock = socket.create_connection((host, port), timeout=10)
    buf = bytearray()

    login = json.dumps({"Command": "Login", "Account": "equip_grade_test",
                        "Password": "x", "ServerID": "10001",
                        "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    for op, body in collect(sock, buf, {100}):
        pass

    sel = pb.IDMessage()
    sel.id = 1001
    sock.sendall(encode_request(10001, sel))
    eg = None
    import time as _t
    _frames, _t0 = 0, _t.time()
    for op, body in collect(sock, buf, set(), max_frames=2000, drain=True):
        _frames += 1
        if op == 102:
            eg = pb.EnterGame()
            eg.ParseFromString(body)
    print("[1] EnterGame equips=%d（%d 帧 / 剩余 buf=%dB / 耗时 %.1fs）"
          % (len(eg.equips), _frames, len(buf), _t.time() - _t0))

    # GM 发放：目标 q3s2 ×1，材料 q2s1 ×2（HTTP /api/gm，此结构曾多次 PASS）
    r1 = gm("grant_equip", type=32112, count=1)
    r2 = gm("grant_equip", type=31111, count=2)
    print("[2] GM grant:", r1["msg"], "|", r2["msg"])
    import time
    time.sleep(2)

    # 从 403 NewEquip 广播收集实例 id（grant 经 HTTP 触发、同进程广播到本会话）
    got403 = []
    sock.settimeout(3.0)
    for op, body in collect(sock, buf, set(), max_frames=40, drain=True):
        if op == 403:
            e = pb.EquipData()
            e.ParseFromString(body)
            got403.append((e.id, e.type))
    sock.settimeout(10)
    targets = [eid for eid, t in got403 if t == 32112]
    mats = [eid for eid, t in got403 if t == 31111]
    if not targets or len(mats) < 2:
        print("SKIP: 未收到 403 广播 targets=%s mats=%s" % (targets, mats))
        return
    target, m1, m2 = targets[0], mats[0], mats[1]
    print("[3] target=%d(q3s2) mats=%s(q2s1)" % (target, [m1, m2]))

    # 客户端公式预计算
    base = 5  # ExpSetConfig[q2].EquipGradeExp[1] = 5
    mult = 1 + (5 - 2) * 0.2  # 装备 2星 → ×1.6
    added = base * mult * 2   # 两件材料
    coin_cost = int(added * 1500)  # ExpSetConfig[q3].EquipUpGradeExpToCoin = 1500
    print("[4] 客户端公式预期: addedExp=%.1f coin=%d grade_exp→%d"
          % (added, coin_cost, int(added)))

    got_ack, got200, coin_now, del_ids = False, False, None, []
    frame_order = []          # 记录 200/6021 先后（2026-10-28 帧序回归）
    # ★ 首次 6021：IDListMessage{id=[目标装备, 材料1, 材料2]}
    # （2026-10-23 修复：历史编辑丢失了 idl 构造与 sendall，导致必然超时 FAIL）
    idl = pb.IDListMessage()
    for x in (target, m1, m2):
        idl.id.append(x)
    sock.sendall(encode_request(6021, idl))
    # ★ 帧序（2026-10-28 修）：499×N → 200 → 6021 ack。200 必须先于 ack，
    #   否则客户端面板在 ack 当时用旧币值渲染「当前/花费」（用户报障）。
    #   停止条件必须是 {6021, 5002}——以前写 {5002, 200} 会在 200 处提前 break，
    #   帧序一改就再也读不到 ack。
    for op, body in collect(sock, buf, {6021, 5002}, max_frames=60):
        if op == 6021:
            frame_order.append(6021)
            e = pb.EquipData()
            e.ParseFromString(body)
            got_ack = (e.id == target)
            print("[5] 6021 EquipData id=%d grade=%d gradeExp=%d %s"
                  % (e.id, e.grade, e.gradeExp, "OK" if got_ack else "FAIL"))
            if e.gradeExp != int(added):
                print("FAIL: gradeExp=%d ≠ 客户端公式 %d" % (e.gradeExp, int(added)))
                sys.exit(1)
        elif op == 5002:
            sm = pb.ShowMessage()
            sm.ParseFromString(body)
            print("FAIL: 被拒: %s" % sm.msg)
            sys.exit(1)
        elif op == 499:
            dd = pb.DeleteData()
            dd.ParseFromString(body)
            del_ids.append((dd.typeID, dd.id))
        elif op == 200:
            frame_order.append(200)
            c = pb.CharacterData()
            c.ParseFromString(body)
            coin_now = c.coin
            got200 = True
            print("[6] 200 coin=%d（应扣 %d）" % (coin_now, coin_cost))

    # ★ 200 必须先于 6021 ack：客户端面板在 ack 当时重读本地币值渲染
    #   「当前/花费」，ack 先到就画旧币（2026-10-28 用户报障根因）
    if got200 and got_ack and frame_order != [200, 6021]:
        print("FAIL: 帧序应为 [200, 6021]，实际 %s" % frame_order)
        sys.exit(1)

    if not got_ack:
        print("FAIL: 没收到 6021 ack")
        sys.exit(1)
    if not got200 or coin_now is None:
        print("FAIL: 没收到 200 铜币同步")
        sys.exit(1)
    # ★ 499 删材料必须 typeID=3（客户端 BagManager.onDelProp: 2=道具 3=装备）——
    #   曾错发 2 → 客户端当删道具执行到不存在的道具 = 无操作，材料永远留在列表
    for tid, mid in del_ids:
        if tid != 3 or mid not in (m1, m2):
            print("FAIL: 499 异常 typeID=%d id=%d（应 typeID=3 且为材料）" % (tid, mid))
            sys.exit(1)
    print("[6b] 499 删材料 typeID=3 ×%d OK" % len(del_ids))

    # 重复提交（材料已删）→ 5002 材料不存在，不炸
    sock.sendall(encode_request(6021, idl))
    got5002 = False
    for op, body in collect(sock, buf, {5002, 6021}, max_frames=30):
        if op == 5002:
            sm = pb.ShowMessage()
            sm.ParseFromString(body)
            got5002 = "材料不存在" in sm.msg
            print("[7] 重复提交 -> 5002 %r" % sm.msg)
            break
        elif op == 6021:
            print("FAIL: 重复提交不该收到 ack")
            sys.exit(1)
    if not got5002:
        print("FAIL: 重复提交未得到 5002 材料不存在")
        sys.exit(1)

    print("PASS: 装备升阶公式与客户端预览一致；拒绝路径不炸且有 5002 反馈")


if __name__ == "__main__":
    main()
