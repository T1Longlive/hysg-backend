"""扣币操作「200 必须先于 ack」帧序回归（2026-10-28 用户报障）。

现象：升阶扣币后，升阶面板按钮上的「当前/花费」铜钱还是进门时的旧数
（原版点完按钮就刷新）。

根因：客户端 TV 面板的成功回调在 **ack 当时**重读本地币值渲染标签——
  · EquipShengJiePanel:refresh()  → _costLabel:updateData(me:coin(), cost)
  · EquipStrengthPanel:refresh()  → updateLabel() → _costLabel:updateData(me:coin(), ...)
服务端若把 200（UpdateCharacter）排在 ack 之后发，面板先用旧币值画完，
200 后到也没有任何入口再刷这个面板 → 按钮上的铜钱定格在旧值。

修法：6021 升阶 / 5014·5016 强化的 dispatch 把 200 提到 opcode ack 之前
（洗练 6025「451→200→ack」本来就是这个帧序）。

本测试跑真实服务端往返，断言帧序与 200 的币值。
用法：python tools/equip_coin_refresh_tcp_test.py（需服务端已启动）
"""

import socket
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")
sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "rt", __file__.replace("\\", "/").rsplit("/", 1)[0] + "/reform_tcp_test.py")
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)

from sghy_server.proto import ps_protobuf_pb2 as pb

EQUIP_5STAR = 33115   # q4 5★


def collect_ordered(sock, buf, want_ops, max_frames=60):
    """按到达顺序收集帧，返回 [(op, body), ...]；见到 want_ops 全齐即停。"""
    out = []
    for _ in range(max_frames):
        g = rt.read_frame(sock, buf)
        if g is None:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            continue
        out.append(g)
        if want_ops and all(w in [o for o, _ in out] for w in want_ops):
            break
    return out


def coin_of_frame(op_body):
    op, body = op_body
    assert op == 200, "期望 200，实际 op=%d" % op
    cd = pb.CharacterData()
    cd.ParseFromString(body)
    return cd.coin


def main():
    sock = socket.create_connection(("127.0.0.1", 2724), timeout=10)
    sock.settimeout(3)
    buf = bytearray()
    eg = rt.login(sock, buf, "coin_refresh_order")
    coin0 = eg.character.coin
    print("[login] coin=%s" % coin0)

    # 备料：目标 + 2 材料（同品质），保证强化/升阶都做得动
    for cmd in ("add_coin 100000000", "add_item 20002 100",
                "add_equip %d" % EQUIP_5STAR, "add_equip %d" % EQUIP_5STAR,
                "add_equip %d" % EQUIP_5STAR):
        rt.gm(sock, buf, cmd)
    rt.drain(sock, buf)
    eg = rt.reselect(sock, buf)
    coin0 = eg.character.coin
    ids = [e.id for e in sorted((e for e in eg.equips if e.type == EQUIP_5STAR),
                                key=lambda e: -e.id)[:3]]
    print("[gm] coin=%s equips=%s" % (coin0, ids))

    # ---------- 6021 升阶：帧序必须是 [499, 499, 200, 6021] ----------
    req = pb.IDListMessage()
    for i in ids:
        req.id.append(i)
    rt.drain(sock, buf)
    sock.sendall(rt.encode_request(6021, req))
    frames = collect_ordered(sock, buf, {200, 6021})
    ops = [o for o, _ in frames]
    print("[6021] 帧序:", ops)
    assert ops.count(499) == 2, "应推 2 封 499（消耗 2 件材料）: %s" % ops
    i200, iack = ops.index(200), ops.index(6021)
    assert i200 < iack, "200 必须先于 6021 ack（面板在 ack 当时重读币值）: %s" % ops
    coin_after = coin_of_frame(frames[i200])
    print("[6021] 200 币值=%s（登录 %s）" % (coin_after, coin0))
    assert coin_after < coin0, "200 必须带扣费后的币值"

    # ---------- 5014 强化：帧序必须是 [200, 5014] ----------
    # 重新拿一件没强化过的（上面那件已升阶，强化等级仍是 0，可直接用）
    req1 = pb.IDMessage()
    req1.id = ids[0]
    rt.drain(sock, buf)
    sock.sendall(rt.encode_request(5014, req1))
    frames = collect_ordered(sock, buf, {200, 5014})
    ops = [o for o, _ in frames]
    print("[5014] 帧序:", ops)
    i200, iack = ops.index(200), ops.index(5014)
    assert i200 < iack, "200 必须先于 5014 ack: %s" % ops
    coin_after2 = coin_of_frame(frames[i200])
    print("[5014] 200 币值=%s（上一笔后 %s）" % (coin_after2, coin_after))
    assert coin_after2 < coin_after, "强化也必须扣费并随 200 下发"

    sock.close()
    print("TOTAL: 扣币帧序回归 passed（6021/5014 均为 200 先于 ack）")


if __name__ == "__main__":
    main()
