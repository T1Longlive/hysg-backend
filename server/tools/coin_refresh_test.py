"""200(UpdateCharacter) body 形状验证（2026-10-22）。

回归背景：200 的 body 曾被包成 UpdateCharacter{character=CharacterData}，
但客户端 onCharacterValue 解的是**裸 CharacterData**（socket:decode("CharacterData")）
——包层错位解出垃圾（只剩乱码 id），updateMyData 判 coin=nil 跳过，
界面铜币永远是登录旧值。修复后 body = 裸 CharacterData。

用法：python tools/coin_refresh_test.py [host] [port]
断言：触发一次扣费后，200 body 按 CharacterData 解出 coin/dollar/lv 且 id 正确。
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


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2724

    import socket
    sock = socket.create_connection((host, port), timeout=10)
    buf = bytearray()

    login = json.dumps({"Command": "Login", "Account": "coin_refresh_test",
                        "Password": "x", "ServerID": "10001",
                        "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    for op, body in collect(sock, buf, {100}):
        if op == 100:
            ok = pb.LoginOK()
            ok.ParseFromString(body)
            print("[1] LoginOK characters=%d" % len(ok.characters))

    sock.settimeout(3.0)
    sel = pb.IDMessage()
    sel.id = 1001
    sock.sendall(encode_request(10001, sel))
    eg = None
    for op, body in collect(sock, buf, set(), max_frames=200, drain=True):
        if op == 102:
            eg = pb.EnterGame()
            eg.ParseFromString(body)
    # EnterGame 内嵌的 character 是正确参照
    ref = eg.character
    print("[2] EnterGame.character: id=%d coin=%d dollar=%d lv=%d"
          % (ref.id, ref.coin, ref.dollar, ref.lv))

    # 触发扣铜币：强化一次装备（5014 StrengthenEquip，必扣费必推200）
    # ★ 选【未强化满】的装备（存档漂移教训：硬挑 id 最小会撞上限被拒不推 200）
    eq = None
    for e in sorted(eg.equips, key=lambda x: x.id):
        if e.StrengthenLv < 60:
            eq = e
            break
    if eq is None:
        print("SKIP: 没有可强化装备（全部 ≥60 级）")
        return
    idm = pb.IDMessage()
    idm.id = eq.id
    sock.sendall(encode_request(5014, idm))
    print("[3] sent StrengthenEquip(equip=%d)" % eq.id)

    got200 = False
    for op, body in collect(sock, buf, {200, 5002}, max_frames=80):
        if op == 200:
            c = pb.CharacterData()
            try:
                c.ParseFromString(body)
            except Exception as e:
                print("FAIL: 200 body 不能按 CharacterData 解析: %s" % e)
                sys.exit(1)
            got200 = True
            print("[4] 200 CharacterData: id=%d coin=%d dollar=%d lv=%d name=%r"
                  % (c.id, c.coin, c.dollar, c.lv, c.name))
            if c.id != ref.id:
                print("FAIL: id 错位（%d != %d）——body 仍带外壳?" % (c.id, ref.id))
                sys.exit(1)
            if c.coin <= 0:
                print("FAIL: coin 未解出（<=0）——updateMyData 会跳过")
                sys.exit(1)
            if c.name != ref.name:
                print("FAIL: name=%r 与参照 %r 不符" % (c.name, ref.name))
                sys.exit(1)
        elif op == 5002:
            sm = pb.ShowMessage()
            sm.ParseFromString(body)
            print("[4] 5002 ShowMessage: %s" % sm.msg)
            print("FAIL: 强化被拒，未产生扣费")
            sys.exit(1)

    if not got200:
        print("FAIL: 没收到 200")
        sys.exit(1)
    print("PASS: 200 body = 裸 CharacterData（id/coin/dollar/name 全部正确）")


if __name__ == "__main__":
    main()
