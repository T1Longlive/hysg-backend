"""存档对象身份回归（2026-10-30）：切角色后 store._data 必须仍是 core.PLAYER_DATA。

背景：session 切角色走
    data = await store.switch_player(cid)     # 旧实现 self._data = data（换对象）
    core.apply_player_data(data, cid)         # 只 clear+update 旧对象，不换对象
结果 store 落库写的是"登录那一刻的快照"，业务改的都是另一个 dict →
充值累计、任务领取、道具消耗全都不落库，重启/重登即回滚。
（用户反馈"买了好几个 648，累计充值还停在 198"就是这么来的）

用法： cd server && python tools/store_switch_identity_test.py
"""
import asyncio
import copy
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

os.environ["SGHY_PLAYER_DATA"] = os.path.join(
    tempfile.gettempdir(), "sghy_switch_identity_test.json")

from sghy_server.data.store import SaveStore                       # noqa: E402
from sghy_server.game import core                                 # noqa: E402
from sghy_server.game.activities import grade_pay                 # noqa: E402

TOTAL = [0, 0]


def check(cond, label):
    TOTAL[0] += 1
    if cond:
        TOTAL[1] += 1
        print("  ok   %s" % label)
    else:
        print("  FAIL %s" % label)
    return cond


async def main():
    print("=" * 60)
    print("存档对象身份回归（切角色后落库必须写到活字典）")
    print("=" * 60)

    tmpdb = os.path.join(tempfile.gettempdir(), "sghy_switch_identity.db")
    for suf in ("", "-wal", "-shm"):
        p = tmpdb + suf
        if os.path.exists(p):
            os.remove(p)

    store = SaveStore(db_path=tmpdb, player_id=1001, json_path=None)
    await store.start()
    core.PLAYER_DATA = store.data          # 与 __main__ 一致：同一对象
    core.CHAR_ID = 1001
    core.PLAYER_DATA.setdefault("player", {}).update(
        {"id": 1001, "name": "甲", "lv": 20, "pay_dollar": 0, "pay_money": 0})
    # ★ 故意不给 activity 键：模拟"存档里还没有活动数据"的玩家 ——
    #   充值时 act_state() 会 setdefault 当场新建，旧实现只挂在 PLAYER_DATA 上，
    #   与 store._data 分裂后必然丢失（这正是 round_pay 一直是 0 的形态）。
    core.PLAYER_DATA.pop("activity", None)
    core.PLAYER_DATA.update({"mails": {}, "mail_seq": 0})
    await store.repo.save(1001, core.PLAYER_DATA,
                          dirty={"heroes": set(), "equips": set()})

    # 造第二个角色
    other = copy.deepcopy(core.PLAYER_DATA)
    other["player"].update({"id": 2002, "name": "乙"})
    await store.repo.save(2002, other, dirty={"heroes": set(), "equips": set()})

    print("\n[1] 登录切角色（1001 → 2002 → 1001）")
    d = await store.switch_player(2002)
    core.apply_player_data(d, 2002)
    check(store.data is core.PLAYER_DATA, "切到 2002 后 store.data is PLAYER_DATA")

    d = await store.switch_player(1001)
    core.apply_player_data(d, 1001)
    check(store.data is core.PLAYER_DATA, "切回 1001 后 store.data is PLAYER_DATA")

    print("\n[2] 充值累计必须落库（核心症状）")
    grade_pay.on_recharge(648)
    grade_pay.on_recharge(648)
    mem = grade_pay._st("total_pay").get("round_pay")
    check(int(mem or 0) == 1296, "内存 round_pay = 1296（%s）" % mem)
    await store.flush(force=True)
    back = await store.repo.load(1001)
    db_val = int(((back.get("activity") or {}).get("total_pay") or {}).get("round_pay") or 0)
    check(db_val == 1296, "落库 round_pay = 1296（实际 %s）" % db_val)

    print("\n[2b] 复刻用户场景：切到 B 角色 → 在 B 上充 648 → 切回 A → 重登 B 看进度")
    d = await store.switch_player(2002)
    core.apply_player_data(d, 2002)
    grade_pay.on_recharge(648)          # 此时活动角色是 2002
    grade_pay.on_recharge(648)
    await store.switch_player(1001)     # 切回 A（内部会先 flush 2002）
    core.apply_player_data(store.data, 1001)
    back_b = await store.repo.load(2002)
    val_b = int(((back_b.get("activity") or {}).get("total_pay") or {}).get("round_pay") or 0)
    check(val_b == 1296, "2002 的 round_pay 落库 = 1296（实际 %s）" % val_b)
    a_val = int(((await store.repo.load(1001)).get("activity") or {})
                .get("total_pay", {}).get("round_pay") or 0)
    check(a_val == 1296, "1001 自己的累计不受 2002 影响（实际 %s）" % a_val)

    print("\n[3] 普通字段同理（等级/铜钱）")
    core.PLAYER_DATA["player"]["lv"] = 33
    core.PLAYER_DATA["player"]["coin"] = 777
    store.mark_dirty()
    await store.flush(force=True)
    back = await store.repo.load(1001)
    check(int(back["player"]["lv"]) == 33 and int(back["player"]["coin"]) == 777,
          "lv/coin 落库正确（lv%s / coin%s）"
          % (back["player"]["lv"], back["player"]["coin"]))

    await store.stop()
    print("\n%s  %d/%d" % ("全部通过" if TOTAL[0] == TOTAL[1] else "有失败项",
                           TOTAL[1], TOTAL[0]))
    return 0 if TOTAL[0] == TOTAL[1] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
