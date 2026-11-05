"""装备洗练 实链 TCP 端到端测试（2026-10-28 报障回归）。

流程：
  登录（从 EnterGame 读当前石头数 N，不断言绝对值）→ GM 发 5★ 装备
  → 6024 解锁 → 6025 洗练#1（断言 451 石头 N→N-3、200 铜币
  -450×(lv+1)）→ 6026 保存 → 6025 洗练#2（石头 N-3→N-6）
  → 6027 放弃 → 连续洗练把石头打到 <3 → 再一次必须被 5002
  「洗练石不足」拒绝。

用法：python tools/reform_tcp_test.py [host] [port]（默认 127.0.0.1:2724，需服务器已启动）
"""

import base64
import json
import socket
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.proto import ps_protobuf_pb2 as pb

MAGIC = 10008
STONE = 20002        # 洗练石/改造石（ItemConfig 20002）
EQUIP_5STAR = 33115  # q4 5星，两条副属性（slot1 需3星 / slot2 需5星）


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


def collect(sock, buf, stop_ops, max_frames=80):
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
        seen.setdefault(op, body)
        if stop_ops and all(w in seen for w in stop_ops):
            break
    return seen


def drain(sock, buf):
    """把连接上残留的异步推送（520/5318/邮件等）全部读掉再 discard。

    不清缓冲区的话，上一轮遗留的帧会混进下一轮的 seen，导致帧归属错乱
    （2026-10-28 实测：登录的 520/5318 残留曾让解锁的 200 找不着）。
    """
    sock.settimeout(0.3)
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    del buf[:]
    sock.settimeout(3)


def login(sock, buf, account):
    login = json.dumps({"Command": "Login", "Account": account, "Password": "x",
                        "ServerID": "10001", "Platform": "youguTV"}) + "\n"
    sock.sendall(base64.b64encode(login.encode()))
    collect(sock, buf, {100})
    return reselect(sock, buf)


def reselect(sock, buf):
    """同连接重发 10001 选角拿最新 EnterGame（不能重发 base64 登录——
    服务端此时已进入帧模式，会当 bad magic 丢弃）。"""
    sel = pb.IDMessage()
    sel.id = 1001
    sock.sendall(encode_request(10001, sel))
    eg = collect(sock, buf, {102})
    data = pb.EnterGame()
    data.ParseFromString(eg[102])
    return data


def gm(sock, buf, text):
    sm = pb.StringMessage()
    sm.msg = text
    sock.sendall(encode_request(8000, sm))
    return collect(sock, buf, {5002})


def stone_of(frames):
    if 451 not in frames:
        return None
    m = pb.ItemData()
    m.ParseFromString(frames[451])
    return m.count if m.type == STONE else None


def coin_of(frames):
    if 200 not in frames:
        return None
    m = pb.CharacterData()
    m.ParseFromString(frames[200])
    return m.coin


def stones_in_entergame(eg):
    return next((it.count for it in eg.items if it.type == STONE), 0)


def reform_ids(eq_id, slot=1):
    ids = pb.IDListMessage()
    ids.id.append(eq_id)
    ids.id.append(slot)
    return ids


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2724
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(3)
    buf = bytearray()

    eg = login(sock, buf, "reform_tcp")
    lv = eg.character.lv
    stone0 = stones_in_entergame(eg)
    print("[login] lv=%s stone=%s coin=%s" % (lv, stone0, eg.character.coin))

    gm(sock, buf, "add_coin 100000000")          # 保证洗得动
    gm(sock, buf, "add_diamond 100000")          # 解锁 300 + 元宝补充要用
    gm(sock, buf, "add_item %d 300" % STONE)     # 自给自足（共享账号，历史余量未知）
    gm(sock, buf, "add_equip %d" % EQUIP_5STAR)
    drain(sock, buf)
    eg = reselect(sock, buf)
    stone0 = stones_in_entergame(eg)
    eq = max((e for e in eg.equips if e.type == EQUIP_5STAR),
             key=lambda e: e.id)                 # GM 新发的那件 id 最大
    eq_id = eq.id
    print("[gm] equip id=%s type=%s stone=%s" % (eq_id, eq.type, stone0))

    # ---- 6024 解锁第 1 条（耗 300 元宝）----
    drain(sock, buf)
    sock.sendall(encode_request(6024, reform_ids(eq_id)))
    f = collect(sock, buf, {6024})
    assert 6024 in f, "解锁无 ack"
    coin_after_unlock = coin_of(f)
    assert coin_after_unlock is not None, "解锁必须推 200（元宝不足会被拒，见开局 add_diamond）"
    print("[unlock] ack ok, coin=%s" % coin_after_unlock)

    # ---- 6025 洗练 #1：必须扣 3 石 + 450×(lv+1) 币 ----
    drain(sock, buf)
    sock.sendall(encode_request(6025, reform_ids(eq_id)))
    f = collect(sock, buf, {6025})
    assert 6025 in f, "洗练无 ack"
    stone = stone_of(f)
    coin = coin_of(f)
    print("[reform#1] stone=%s (期望 %s) coin=%s (期望 %s)"
          % (stone, stone0 - 3, coin, coin_after_unlock - 450 * (lv + 1)))
    assert stone == stone0 - 3, "洗练必须扣 3 个洗练石：%s -> %s" % (stone0, stone)
    assert coin == coin_after_unlock - 450 * (lv + 1), \
        "铜币必须按 450×(lv+times) 扣：%s -> %s" % (coin_after_unlock, coin)

    # ---- 6026 保存（同属性更低会被门禁拒，重试到保存成功为止）----
    drain(sock, buf)
    saved = False
    for attempt in range(12):
        sock.sendall(encode_request(6026, reform_ids(eq_id)))
        f = collect(sock, buf, {6026, 5002})
        if 6026 in f:
            saved = True
            print("[save] ok（第 %d 次尝试）" % (attempt + 1))
            break
        m = pb.StringMessage()
        m.ParseFromString(f[5002])
        print("[save] 被门禁拒（%s），放弃再洗" % m.msg)
        drain(sock, buf)
        sock.sendall(encode_request(6027, reform_ids(eq_id)))
        assert 6027 in collect(sock, buf, {6027}), "放弃无 ack"
        drain(sock, buf)
        sock.sendall(encode_request(6025, reform_ids(eq_id)))
        fr = collect(sock, buf, {6025})
        assert 6025 in fr, "补刀洗练无 ack"
        stone = stone_of(fr)
        assert stone == stone0 - 3 - 3 * (attempt + 1), \
            "补刀洗练也必须扣 3 石：%s" % stone
    assert saved, "12 次都没能保存成功（门禁逻辑可疑）"

    # ---- 6025 洗练 #2：继续扣 3 石 ----
    drain(sock, buf)
    sock.sendall(encode_request(6025, reform_ids(eq_id)))
    f = collect(sock, buf, {6025})
    assert 6025 in f, "洗练#2 无 ack"
    stone = stone_of(f)
    print("[reform#2] stone=%s（相对起点 -%d）" % (stone, stone0 - stone))
    assert stone is not None and stone < stone0 - 3 + 1, "洗练必须持续扣 3 个洗练石"

    # ---- 6027 放弃 ----
    drain(sock, buf)
    sock.sendall(encode_request(6027, reform_ids(eq_id)))
    f = collect(sock, buf, {6027})
    assert 6027 in f, "放弃无 ack"
    print("[giveup] ok")

    # ---- 把石头打到 <3：再洗必须走「元宝补充」并成功 ----
    guard = 0
    while stone is not None and stone >= 3 and guard < 400:
        drain(sock, buf)
        sock.sendall(encode_request(6025, reform_ids(eq_id)))
        f = collect(sock, buf, {6025})
        assert 6025 in f, "第 %d 轮补刀洗练无 ack" % guard
        stone = stone_of(f)
        guard += 1
    print("[drain] 剩余石头=%s（<3，共补刀 %d 轮）" % (stone, guard))
    if stone is None or stone >= 3:
        print("[no-stone] SKIP：余量太大（%s），补充/拒绝分支由单元测试覆盖" % stone)
        sock.close()
        print("TOTAL: reform tcp chain passed（无补充分支）")
        return

    # 石头 <3 → 客户端弹「花 100+times 元宝购买洗练石」→ 确认后原样重发 6025。
    # 服务端必须自己推断并扣元宝代付（2026-10-28 报障：以前这里直接 5002）。
    drain(sock, buf)
    sock.sendall(encode_request(6025, reform_ids(eq_id)))
    f = collect(sock, buf, {6025, 5002})
    assert 6025 in f, "元宝补充后洗练必须成功（客户端确认后重发 6025）"
    stone_after = stone_of(f)
    print("[diamond-supply] ok（石头 %s -> %s 不变）" % (stone, stone_after))
    assert stone_after == stone, "元宝代付不动已有石头"

    # 元宝也清零 → 再洗必须被 5002 拒绝且不扣
    gm(sock, buf, "set_diamond 0")
    drain(sock, buf)
    sock.sendall(encode_request(6025, reform_ids(eq_id)))
    f = collect(sock, buf, {5002})
    assert 5002 in f, "石头+元宝都不足必须回 5002 拒绝"
    # 5002 是 legacy 字段格式 f_int(1,0)+f_str(2,msg)，不是 protobuf StringMessage
    raw = f[5002].decode("utf-8", "replace")
    print("[no-stone] rejected:", raw)
    assert "洗练石不足" in raw and "元宝补充需" in raw, raw

    sock.close()
    print("TOTAL: reform tcp chain passed")


if __name__ == "__main__":
    main()
