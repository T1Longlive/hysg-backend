"""穿戴/卸下回包类型回归测试（5012/5013）。

背景（2026-10-21 bug）：5012 回包曾用 EquipData，客户端 ackEquip 按
PropActionMessage 解码 —— EquipData.type（装备配置 id 33115）被当成 targetID
（武将 id）-> getWarriorData(33115)=nil -> showFatesTip ipairs(nil) 崩。
5013 同理应回 IDMessage。

本测试跑真实服务端往返：登录 -> 穿戴 -> 按客户端的解码类型解析回包并断言语义。

跑法：先起服务端，再 python tools/equip_ack_test.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from smoke_client import encode_request, read_frame, recv_until
from sghy_server.proto import ps_protobuf_pb2 as pb

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %-44s %s" % (name, detail))
    else:
        FAIL += 1
        print("  FAIL %-44s %s" % (name, detail))


def main():
    import socket
    sock = socket.create_connection(("127.0.0.1", 2724), timeout=10)
    buf = bytearray()

    # 登录
    import base64, json
    login = json.dumps({"Command": "Login", "Account": "smoke", "Password": "x",
                        "ServerID": "10001", "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    recv_until(sock, buf, 100)
    sel = pb.IDMessage(); sel.id = 1001
    sock.sendall(encode_request(10001, sel))
    eg_body = recv_until(sock, buf, 102)
    print("[login ok]")

    # 动态选：从 EnterGame 挑一个武将 + 一件未穿戴装备（存档会漂移，勿硬编码）
    eg = pb.EnterGame(); eg.ParseFromString(eg_body)
    hero_id = None
    for h in eg.heros:
        hero_id = h.id
        break
    eq_id = None
    for e in eg.equips:
        if e.host == 0:
            eq_id = e.id
            break
    assert hero_id and eq_id, "存档里没有可用武将/未穿戴装备"
    print("[pick] hero=%s equip=%s" % (hero_id, eq_id))

    # 1) 穿戴 5012：客户端 ackEquip 按 PropActionMessage 解码
    req = pb.PropActionMessage()
    req.id = eq_id
    req.targetID = hero_id
    sock.sendall(encode_request(5012, req))
    body = recv_until(sock, buf, 5012)
    ack = pb.PropActionMessage()
    try:
        ack.ParseFromString(body)
        check("5012 回包是 PropActionMessage", True)
        check("5012 id=装备实例", ack.id == eq_id, "id=%s 期望 %s" % (ack.id, eq_id))
        check("5012 targetID=武将 id", ack.targetID == hero_id,
              "targetID=%s 期望 %s（错时会是 33115 这类配置 id）" % (ack.targetID, hero_id))
    except Exception as e:
        check("5012 回包是 PropActionMessage", False, repr(e))

    # 2) 卸下 5013：客户端 ackUnEquip 按 IDMessage 解码
    req2 = pb.IDMessage()
    req2.id = eq_id
    sock.sendall(encode_request(5013, req2))
    body2 = recv_until(sock, buf, 5013)
    ack2 = pb.IDMessage()
    try:
        ack2.ParseFromString(body2)
        check("5013 回包是 IDMessage", True)
        check("5013 id=装备实例", ack2.id == eq_id, "id=%s 期望 %s" % (ack2.id, eq_id))
    except Exception as e:
        check("5013 回包是 IDMessage", False, repr(e))

    sock.close()
    print()
    print("TOTAL: %d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
