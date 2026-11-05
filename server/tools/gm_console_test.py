"""GM 命令台端到端验证（2026-10-22）。

背景：设置界面"激活码"输入框改造为 GM 命令台 —— 客户端原版流程把输入文本以
StringMessage{msg} 发到 8000（UseActivationCode），服务端解析执行 gm_* 并用
5002 ShowMessage 回显结果（不回 8000，避免客户端按 AwardResult 解错位）。

用法：python tools/gm_console_test.py [host] [port]
断言：add_item 命令 → 5002 回显含"发放道具"；未知命令 → 回显含"未知命令"。
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


def send_gm(sock, buf, text):
    sm = pb.StringMessage()
    sm.msg = text
    sock.sendall(encode_request(8000, sm))
    for op, body in collect(sock, buf, {5002}, max_frames=30):
        if op == 5002:
            sm2 = pb.ShowMessage()
            sm2.ParseFromString(body)
            return sm2.msg
    return None


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

    login = json.dumps({"Command": "Login", "Account": "gm_console_test",
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
    for op, body in collect(sock, buf, set(), max_frames=200, drain=True):
        pass
    print("[2] EnterGame ok")

    # 用符前记录道具数
    def item_count(items, item_id):
        for it in items:
            if it.type == item_id:
                return it.count
        return 0

    # EnterGame 里带 items —— 需要再拿一次；直接靠 5002 文本 + 451 推送验证
    msg = send_gm(sock, buf, "add_item 20007 3")
    print("[3] add_item 20007 3 -> %r" % msg)
    if not msg or "发放道具" not in msg:
        print("FAIL: add_item 回显不符")
        sys.exit(1)

    msg = send_gm(sock, buf, "nonsense_cmd")
    print("[4] nonsense_cmd -> %r" % (msg or "")[:30])
    if not msg or "未知命令" not in msg:
        print("FAIL: 未知命令回显不符")
        sys.exit(1)

    msg = send_gm(sock, buf, "help")
    print("[5] help -> %d 字帮助文本" % len(msg or ""))
    if not msg or "GM命令台" not in msg:
        print("FAIL: help 回显不符")
        sys.exit(1)

    print("PASS: GM 命令台链路（8000 StringMessage → 执行 → 5002 回显）")


if __name__ == "__main__":
    main()
