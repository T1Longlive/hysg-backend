"""图鉴/收集 TCP 端到端测试（真服务器 + 真帧格式）。

覆盖：登录 → EnterGame(heroRecords/equipRecords) → 10115 领取 → 10115 回包 + 301 弹窗。
需要服务器已在 host:port 上运行（默认 12726，避开用户实例）。

用法：python tools/collect_tcp_test.py [host] [port]
"""
import base64
import json
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")
sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/../../sghy/work")

from sghy_server.proto import ps_protobuf_pb2 as pb

MAGIC = 10008


def encode_request(opcode, msg):
    body = msg.SerializeToString()
    payload = struct.pack("<i", opcode) + struct.pack("<i", 1) + body
    return struct.pack("<i", MAGIC) + struct.pack("<i", 8 + len(body)) + payload


def read_frame(sock, buf):
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


def recv_until(sock, buf, want, max_frames=200):
    for _ in range(max_frames):
        got = read_frame(sock, buf)
        if got is None:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
            continue
        op, body = got
        if op == want:
            return body
    raise TimeoutError("opCode %s not seen" % want)


def recv_ops(sock, buf, wants, max_frames=120):
    seen = {}
    for _ in range(max_frames):
        got = read_frame(sock, buf)
        if got is None:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            continue
        op, body = got
        seen[op] = body
        if op in wants and all(w in seen for w in wants):
            break
    return seen


def main():
    import socket
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 12726

    sock = socket.create_connection((host, port), timeout=15)
    buf = bytearray()

    login = json.dumps({"Command": "Login", "Account": "collect", "Password": "x",
                        "ServerID": "10001", "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    pb.LoginOK().ParseFromString(recv_until(sock, buf, 100))

    sel = pb.IDMessage()
    sel.id = 1001
    sock.sendall(encode_request(10001, sel))
    eg = pb.EnterGame()
    eg.ParseFromString(recv_until(sock, buf, 102))
    print("[1] EnterGame heroRecords=%d equipRecords=%d awardRecords=%d" % (
        len(eg.heroRecords), len(eg.equipRecords), len(eg.collectionAwardRecords)))
    assert len(eg.heroRecords) >= 15, "应有 ≥15 条武将图鉴"
    assert len(eg.equipRecords) >= 20, "应有 ≥20 条装备图鉴"

    # 领取武将 Origin1 的 15 里程碑
    req = pb.ReceiveCollectionAwardRequestData()
    req.type, req.subType, req.milestoneCount = 1, 1, 15
    sock.sendall(encode_request(10115, req))
    seen = recv_ops(sock, buf, {10115, 301, 200})
    print("[2] 领取武将15: ops=%s" % sorted(seen))
    assert 10115 in seen and 301 in seen, "应回 10115 + 301"
    rec = pb.CollectionAwardRecordData()
    rec.ParseFromString(seen[10115])
    print("[3] record type=%d sub=%d milestones=%s" % (
        rec.type, rec.subType, list(rec.receivedMilestoneCounts)))
    assert 15 in list(rec.receivedMilestoneCounts)

    # 重复领取 → 10115 + 5002
    sock.sendall(encode_request(10115, req))
    seen2 = recv_ops(sock, buf, {10115, 5002})
    print("[4] 重复领取: ops=%s" % sorted(seen2))
    assert 5002 in seen2 and 301 not in seen2, "重复领取应 5002 且不弹奖"

    # 装备 Part1 的 20 里程碑
    req2 = pb.ReceiveCollectionAwardRequestData()
    req2.type, req2.subType, req2.milestoneCount = 2, 1, 20
    sock.sendall(encode_request(10115, req2))
    seen3 = recv_ops(sock, buf, {10115, 301})
    print("[5] 领取装备20: ops=%s" % sorted(seen3))
    assert 10115 in seen3 and 301 in seen3

    sock.close()
    print("TOTAL: 5 passed")


if __name__ == "__main__":
    main()
