"""招募 / 寻宝 TCP 端到端测试（真服务器 + 真帧格式）。

覆盖：登录 → 选角 → EnterGame(含 lotteryRecords) → 5041 → 5040(101/104/201)。
需要服务器已在 host:port 上运行（端口由参数给，默认 12725，避开用户实例 2724）。

用法：python tools/lottery_tcp_test.py [host] [port]
"""
import base64
import json
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.proto import ps_protobuf_pb2 as pb

MAGIC = 10008


def encode_request(opcode, msg):
    body = msg.SerializeToString()
    payload = struct.pack("<i", opcode) + struct.pack("<i", 1) + body
    return struct.pack("<i", MAGIC) + struct.pack("<i", 8 + len(body)) + payload


def encode_empty(opcode):
    """客户端 socket:send(nil, op) —— 空 protobuf body（5041 就是这样发的）。"""
    payload = struct.pack("<i", opcode) + struct.pack("<i", 1)
    return struct.pack("<i", MAGIC) + struct.pack("<i", 8) + payload


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


def main():
    import socket
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 12725

    sock = socket.create_connection((host, port), timeout=15)
    buf = bytearray()

    login = json.dumps({"Command": "Login", "Account": "lottery", "Password": "x",
                        "ServerID": "10001", "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    ok = pb.LoginOK()
    ok.ParseFromString(recv_until(sock, buf, 100))
    print("[1] LoginOK characters=%d" % len(ok.characters))

    sel = pb.IDMessage()
    sel.id = 1001
    sock.sendall(encode_request(10001, sel))
    eg = pb.EnterGame()
    eg.ParseFromString(recv_until(sock, buf, 102))
    print("[2] EnterGame heroes=%d lotteryRecords=%d activity=%s" % (
        len(eg.heros), len(eg.lotteryRecords), eg.activityData.HasField("lotteryActivityInfo")))
    assert len(eg.lotteryRecords) == 10, "EnterGame 应带 10 条抽卡记录"
    assert eg.activityData.HasField("lotteryActivityInfo"), "EnterGame 应带折扣活动信息"

    sock.sendall(encode_empty(5041))
    lr = pb.LotteryRecordDatas()
    lr.ParseFromString(recv_until(sock, buf, 5041))
    print("[3] 5041 records=%d types=%s" % (
        len(lr.lotteryRecords), sorted(r.type for r in lr.lotteryRecords)))
    assert len(lr.lotteryRecords) == 10

    # 101 首次免费单抽
    ul = pb.UseLotteryData()
    ul.id = 101
    sock.sendall(encode_request(5040, ul))
    ahm = pb.AllHeroMessage()
    ahm.ParseFromString(recv_until(sock, buf, 476))
    print("[4] 5040(101) heroes=%d key=%s type=%s" % (
        len(ahm.list), ahm.list[0].key, ahm.list[0].heroData.type))
    assert len(ahm.list) == 1 and ahm.list[0].key == 404

    # 104 首次十连（元宝够）
    ul2 = pb.UseLotteryData()
    ul2.id = 104
    sock.sendall(encode_request(5040, ul2))
    ahm2 = pb.AllHeroMessage()
    ahm2.ParseFromString(recv_until(sock, buf, 476))
    print("[5] 5040(104) heroes=%d lastType=%s" % (len(ahm2.list), ahm2.list[-1].heroData.type))
    assert len(ahm2.list) == 10

    # 201 首次免费寻宝 → AwardResult(5040) + 403
    ul3 = pb.UseLotteryData()
    ul3.id = 201
    sock.sendall(encode_request(5040, ul3))
    ar = pb.AwardResult()
    ar.ParseFromString(recv_until(sock, buf, 5040))
    print("[6] 5040(201) datas=%d firstType=%s" % (len(ar.datas), ar.datas[0].type))
    assert len(ar.datas) == 1 and ar.datas[0].type == 31111

    sock.close()
    print("TOTAL: 6 passed")


if __name__ == "__main__":
    main()
