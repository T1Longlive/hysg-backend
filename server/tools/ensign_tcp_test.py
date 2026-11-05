"""军旗系统 实链 TCP 端到端测试（2026-10-23）。

流程：登录 → GM 发材料 → 5346 制作 → 5347 升级 → 5351 装备 → 5353 上阵
→ 5030 LevelBegin（myGroup.flagParts）→ 5354 下阵 → 5349 分解 → 5356 解锁旗帐。

用法：python tools/ensign_tcp_test.py [host] [port]（默认 127.0.0.1:2724，需服务器已启动）
"""

import base64
import json
import socket
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.proto import ps_protobuf_pb2 as pb

MAGIC = 10008


def encode_request(opcode: int, msg) -> bytes:
    body = msg.SerializeToString() if hasattr(msg, "SerializeToString") else b""
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


def collect(sock, buf, stop_ops, max_frames=60):
    seen = {}
    for _ in range(max_frames):
        got = read_frame(sock, buf)
        if got is None:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            continue
        op, body = got
        seen[op] = body
        if op in stop_ops and all(w in seen for w in stop_ops):
            break
    return seen


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2724
    sock = socket.create_connection((host, port), timeout=10)
    buf = bytearray()

    login = json.dumps({"Command": "Login", "Account": "ensign_tcp", "Password": "x",
                        "ServerID": "10001", "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    collect(sock, buf, {100})
    sel = pb.IDMessage()
    sel.id = 1001
    sock.sendall(encode_request(10001, sel))
    collect(sock, buf, {102})

    # GM 发材料：图纸 22201 ×3 + 绿布料 21001 ×60 + 绿皮革 21006 ×20
    for text in ("add_item 22201 3", "add_item 21001 60", "add_item 21006 20"):
        sm = pb.StringMessage()
        sm.msg = text
        sock.sendall(encode_request(8000, sm))
        collect(sock, buf, {5002})

    # 1) 制作 5346（42001 校旗冠）
    req = pb.MultIDCounteListByIDMessage()
    req.id = 42001
    sock.sendall(encode_request(5346, req))
    seen = collect(sock, buf, {5346}, max_frames=40)
    assert 5346 in seen, "5346 无 ack: %s" % sorted(seen)
    part = pb.FlagPartData()
    part.ParseFromString(seen[5346])
    assert part.type == 42001 and part.host == 0 and len(part.unlockAssistants) == 1
    a0 = part.unlockAssistants[0]
    assert a0.unlock is True and 44 <= a0.value <= 134, (a0.value, a0.maxValue)
    print("PASS 5346 制作: part=%s value=%s/%s" % (part.id, a0.value, a0.maxValue))

    # 2) 升级 5347
    req2 = pb.MultIDCounteListByIDMessage()
    req2.id = part.id
    sock.sendall(encode_request(5347, req2))
    seen = collect(sock, buf, {5347}, max_frames=40)
    assert 5347 in seen, "5347 无 ack"
    up = pb.FlagPartData()
    up.ParseFromString(seen[5347])
    assert up.upCount == 1
    assert up.unlockAssistants[0].value == min(448, a0.value + 89)
    print("PASS 5347 升级: value=%s upCount=%s" % (
        up.unlockAssistants[0].value, up.upCount))

    # 3) 装备到旗帐 1（5351）
    pa = pb.PropActionMessage()
    pa.id, pa.targetID = part.id, 1
    sock.sendall(encode_request(5351, pa))
    seen = collect(sock, buf, {5351}, max_frames=40)
    assert 5351 in seen
    eq = pb.FlagPartData()
    eq.ParseFromString(seen[5351])
    assert eq.host == 1
    print("PASS 5351 装备: host=%s" % eq.host)

    # 4) 上阵 5353（category=0 index=1）
    ef = pb.EquipFlagToBattleGameplayIndexRequest()
    ef.category, ef.index, ef.id, ef.categoryPlayMode = 0, 1, 1, 0
    sock.sendall(encode_request(5353, ef))
    seen = collect(sock, buf, {5353}, max_frames=40)
    assert 5353 in seen, "5353 无 ack: %s" % sorted(seen)
    ud = pb.UpdateBattleGameplaySquadDatas()
    ud.ParseFromString(seen[5353])
    assert ud.gameplayDataSquadFlags[0].squad.flag == 1
    print("PASS 5353 上阵: squad.flag=1")

    # 5) LevelBegin：myGroup.flagParts 应含 42001
    lb = pb.LevelBegin()
    lb.id = 1001
    sock.sendall(encode_request(5030, lb))
    seen = collect(sock, buf, {5030}, max_frames=40)
    assert 5030 in seen, "5030 无 ack"
    lbr = pb.LevelBegin()
    lbr.ParseFromString(seen[5030])
    # myGroup = field 4（HeroGroupData），flagParts = field 3
    assert lbr.HasField("myGroup"), "无 myGroup"
    fparts = list(lbr.myGroup.flagParts)
    assert 42001 in fparts, "myGroup.flagParts=%s" % fparts
    print("PASS 5030 LevelBegin: myGroup.flagParts=%s" % fparts)

    # 6) 下阵 5354
    uf = pb.UnequipFlagFromBattleGameplayIndexRequest()
    uf.category, uf.index, uf.id = 0, 1, 1
    sock.sendall(encode_request(5354, uf))
    seen = collect(sock, buf, {5354}, max_frames=40)
    assert 5354 in seen, "5354 无 ack: %s" % sorted(seen)
    ud1 = pb.UpdateBattleGameplaySquadData()
    ud1.ParseFromString(seen[5354])
    assert ud1.squad.flag == 0
    print("PASS 5354 下阵: squad.flag=0")

    # 7) 分解 5349（返还 451/200）
    im = pb.IDMessage()
    im.id = part.id
    sock.sendall(encode_request(5349, im))
    seen = collect(sock, buf, {5349}, max_frames=40)
    assert 5349 in seen, "5349 无 ack: %s" % sorted(seen)
    dl = pb.IDListMessage()
    dl.ParseFromString(seen[5349])
    assert part.id in list(dl.id)
    assert 451 in seen and 200 in seen, "分解应有返还同步: %s" % sorted(seen)
    print("PASS 5349 分解: removed=%s + 451/200 返还" % list(dl.id))

    # 8) 解锁旗帐 5356（无 body）
    sock.sendall(encode_request(5356, None))   # 客户端 socket:send(nil, 5356) → 空 body
    seen = collect(sock, buf, {5356}, max_frames=40)
    assert 5356 in seen, "5356 无 ack: %s" % sorted(seen)
    nf = pb.FlagData()
    nf.ParseFromString(seen[5356])
    assert nf.index >= 2, nf.index   # 可重复跑：index 随存档递增
    # 9) 跑商全流程：先在免费新手图（map1）跑 5 轮把马车升到 Lv1，再跑 1001
    def warbook_step(ok_ops, ack_op, ack_cls):
        sock.sendall(encode_request(ack_op, None))
        seen = collect(sock, buf, {ack_op}, max_frames=40)
        assert ack_op in seen, "%s 无 ack: %s" % (ack_op, sorted(seen))
        msg = ack_cls()
        msg.ParseFromString(seen[ack_op])
        return msg

    # 选新手图 1
    im = pb.IDListMessage()
    im.id.append(1)
    sock.sendall(encode_request(31204, im))
    seen = collect(sock, buf, {31204}, max_frames=40)
    assert 31204 in seen, "31204 无 ack: %s" % sorted(seen)
    print("PASS 31204 选图 map1")

    lv_reached = 0
    for rnd in range(5):
        sock.sendall(encode_request(31207, None))
        seen = collect(sock, buf, {31207}, max_frames=40)
        assert 31207 in seen, "31207 无 ack"
        wr = pb.WarBookRecordData()
        wr.ParseFromString(seen[31207])
        assert wr.startDrawState == 1
        # 背包容量 3（lv0）→ 3 步
        for step in range(3):
            sock.sendall(encode_request(31203, None))
            seen = collect(sock, buf, {31203}, max_frames=40)
            assert 31203 in seen, "31203 无 ack"
            dv = pb.DivinationData()
            dv.ParseFromString(seen[31203])
            assert dv.event == 1 and len(dv.baguaStatus.remainWarBookTypes) >= 1
            sock.sendall(encode_request(31208, None))
            seen = collect(sock, buf, {31208}, max_frames=40)
            assert 31208 in seen, "31208 无 ack"
        # 结算
        sock.sendall(encode_request(31206, None))
        seen = collect(sock, buf, {31206}, max_frames=40)
        assert 31206 in seen, "31206 无 ack"
        wr = pb.WarBookRecordData()
        wr.ParseFromString(seen[31206])
        assert wr.startDrawState == 0
        lv_reached = wr.lv
    print("PASS 31207/31203/31208/31206 新手图 5 轮（马车lv=%s）" % lv_reached)
    assert lv_reached >= 1, "5 轮结算后马车应到 Lv1"

    # 1001 图（NeedLv=1，门票 20020）
    im = pb.IDListMessage()
    im.id.append(1001)
    sock.sendall(encode_request(31204, im))
    collect(sock, buf, {31204}, max_frames=40)
    sm = pb.StringMessage()
    sm.msg = "add_item 20020 3"
    sock.sendall(encode_request(8000, sm))
    collect(sock, buf, {5002}, max_frames=30)
    sock.sendall(encode_request(31207, None))
    seen = collect(sock, buf, {31207}, max_frames=40)
    assert 31207 in seen, "1001 开跑失败"
    wr = pb.WarBookRecordData()
    wr.ParseFromString(seen[31207])
    assert wr.mapID == 1001
    # 掷一步（DivinationNeedCoin=20000）→ 200 扣币推送
    coin_before = None
    sock.sendall(encode_request(31203, None))
    seen = collect(sock, buf, {31203, 200}, max_frames=40)
    assert 31203 in seen and 200 in seen, "31203 应有 200 扣币同步"
    dv = pb.DivinationData()
    dv.ParseFromString(seen[31203])
    assert dv.event == 1
    cd = pb.CharacterData()
    cd.ParseFromString(seen[200])
    print("PASS 1001 图：开跑+掷骰（耗币后 coin=%s）" % cd.coin)
    # 领事件 + 放弃（保留一半）
    sock.sendall(encode_request(31208, None))
    seen = collect(sock, buf, {31208}, max_frames=40)
    assert 31208 in seen
    sock.sendall(encode_request(31205, None))
    seen = collect(sock, buf, {31205}, max_frames=40)
    assert 31205 in seen, "31205 无 ack"
    wr = pb.WarBookRecordData()
    wr.ParseFromString(seen[31205])
    assert wr.startDrawState == 0
    print("PASS 31205 放弃：轮次重置")

    # 10) 图纸合成 5355：4001 = 6 张品质 3 图纸（21044）→ 池 5025 随机图纸
    sm = pb.StringMessage()
    sm.msg = "add_item 21044 6"
    sock.sendall(encode_request(8000, sm))
    collect(sock, buf, {5002}, max_frames=30)
    req = pb.IDCounteListByIDMessage()
    req.id = 4001
    e = req.idCount.add()
    e.id, e.count = 21044, 6
    sock.sendall(encode_request(5355, req))
    seen = collect(sock, buf, {5355}, max_frames=40)
    assert 5355 in seen, "5355 无 ack: %s" % sorted(seen)
    ar = pb.AwardResult()
    ar.ParseFromString(seen[5355])
    assert len(ar.datas) >= 1, "合成无产出"
    assert 451 in seen, "应有图纸扣减同步"
    print("PASS 5355 图纸合成: 产出=%s" % [(d.type, d.value) for d in ar.datas])
    print("\n军旗实链端到端（含跑商+图纸合成）全部通过 ✔")


if __name__ == "__main__":
    main()
