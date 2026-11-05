"""邮件群体发放（发信台选收件人）回归测试。2026-10-30

覆盖：
  1. mail.deliver 双路径：活跃角色写内存 + 推 300；离线角色走 repo 落库
  2. 不存在的角色 → failed，不拖垮整批
  3. 批量：每个角色各自一封、mail id 各自从自己的 mail_seq 递增
  4. http_gateway._resolve_targets 四种 mode（current/all/players/accounts）

用法： cd server && python tools/mail_deliver_test.py
"""
import asyncio
import copy
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

_REAL = r"D:/Cocos_Project/HYSG/env/player_data.json"
_TMP = os.path.join(tempfile.gettempdir(), "sghy_mail_deliver_test.json")
if os.path.exists(_REAL):
    shutil.copy2(_REAL, _TMP)
os.environ["SGHY_PLAYER_DATA"] = _TMP

from sghy_server.game import core, mail                        # noqa: E402
from sghy_server.net.http_gateway import _resolve_targets      # noqa: E402

TOTAL = [0, 0]


def check(cond, label):
    TOTAL[0] += 1
    if cond:
        TOTAL[1] += 1
        print("  ok   %s" % label)
    else:
        print("  FAIL %s" % label)
    return cond


class FakeRepo:
    """内存存档仓库：load 深拷贝，save 覆盖。"""

    def __init__(self, players: dict):
        self.rows = {int(k): copy.deepcopy(v) for k, v in players.items()}

    async def load(self, pid):
        return copy.deepcopy(self.rows.get(int(pid)))

    async def save(self, pid, data, dirty=None):
        self.rows[int(pid)] = copy.deepcopy(data)


class FakeStore:
    def __init__(self, active_pid, players):
        self.player_id = active_pid
        self.repo = FakeRepo(players)


def blank_player(pid, name):
    return {"player": {"id": pid, "name": name, "lv": 10},
            "heroes": {}, "items": {}, "equips": {}, "fate": {},
            "levels": {}, "level_records": {}, "mails": {}, "mail_seq": 0}


def _mail_count(data):
    return len(data.get("mails") or {})


async def main():
    print("=" * 60)
    print("邮件群体发放回归（活跃角色走内存 / 其余走 DB）")
    print("=" * 60)

    # 活跃角色 = 1001（core.PLAYER_DATA），离线 2002/2003
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update(blank_player(1001, "司马懿"))
    core.CHAR_ID = 1001
    core.save_player_data = lambda _d: None        # 别写测试用的临时 json
    core.flush_now = lambda: None

    store = FakeStore(1001, {1001: blank_player(1001, "司马懿"),   # DB 里同 id 的副本，用来证明没被写
                             2002: blank_player(2002, "司州冯元放"),
                             2003: blank_player(2003, "烟测2816")})

    print("\n[1] 单角色（活跃）：写内存 + 不走 DB")
    res = await mail.deliver(store, [1001], "测试标题", "正文",
                             [{"kind": "property", "prop": "Coin", "value": 100}])
    check(len(res["ok"]) == 1 and res["ok"][0]["path"] == "memory", "活跃角色走内存（%s）" % res["ok"])
    check(_mail_count(core.PLAYER_DATA) == 1, "内存存档多了一封（%d）" % _mail_count(core.PLAYER_DATA))
    check(_mail_count(store.repo.rows[1001]) == 0, "没有去写 DB 里同 id 的副本（不应发生）")

    print("\n[2] 离线角色：走 repo 落库，内存不动")
    before = _mail_count(core.PLAYER_DATA)
    res = await mail.deliver(store, [2002], "离线补偿", "只给离线那位",
                             [{"kind": "object", "type": 20002, "count": 5}])
    check(len(res["ok"]) == 1 and res["ok"][0]["path"] == "db", "离线角色走 DB（%s）" % res["ok"])
    check(_mail_count(core.PLAYER_DATA) == before, "活跃角色存档没被污染（%d）" % _mail_count(core.PLAYER_DATA))
    mails2002 = store.repo.rows[2002].get("mails") or {}
    check(len(mails2002) == 1, "2002 存档里有一封（%d）" % len(mails2002))
    one = list(mails2002.values())[0]
    check(one.get("title") == "离线补偿" and one.get("type") == 1,
          "标题/类型正确（%s / %s）" % (one.get("title"), one.get("type")))
    check(int(one.get("expireAt") or 0) > 0, "过期时间已写入")

    print("\n[3] 批量（含活跃 + 两个离线 + 一个不存在）")
    res = await mail.deliver(store, [1001, 2002, 2003, 9999], "全服福利", "")
    ok_pids = sorted(x["player"] for x in res["ok"])
    check(ok_pids == [1001, 2002, 2003], "成功 3 个（%s）" % ok_pids)
    check([f["player"] for f in res["failed"]] == [9999], "不存在角色进 failed（%s）" % res["failed"])
    check(_mail_count(core.PLAYER_DATA) == 2, "活跃角色累计 2 封（%d）" % _mail_count(core.PLAYER_DATA))
    check(_mail_count(store.repo.rows[2002]) == 2 and _mail_count(store.repo.rows[2003]) == 1,
          "2002 累计 2 封 / 2003 首封（%d/%d）"
          % (_mail_count(store.repo.rows[2002]), _mail_count(store.repo.rows[2003])))
    ids_2003 = [m["id"] for m in (store.repo.rows[2003]["mails"] or {}).values()]
    check(ids_2003 == [1], "每个角色邮件 id 独立从 1 开始（%s）" % ids_2003)
    # 纯文本邮件必须是 Notice(0)，否则客户端画成"可领取"
    notice = [m for m in (store.repo.rows[2003]["mails"] or {}).values()][0]
    check(notice.get("type") == 0, "无附件邮件 = Notice(0)（%s）" % notice.get("type"))

    print("\n[4] 目标解析 _resolve_targets")
    players = [{"id": 1001, "account_id": 1}, {"id": 2002, "account_id": 1},
               {"id": 2003, "account_id": 2}]
    check(_resolve_targets(store, None, players) == [1001], "缺省 = 当前活跃角色")
    check(_resolve_targets(store, {"mode": "all"}, players) == [1001, 2002, 2003], "all = 全服")
    check(_resolve_targets(store, {"mode": "players", "ids": [2003, 1234]}, players) == [2003],
          "players 过滤掉不存在的 id")
    check(_resolve_targets(store, {"mode": "accounts", "ids": [1]}, players) == [1001, 2002],
          "accounts 展开成该账号全部角色")
    check(_resolve_targets(store, {"mode": "accounts", "ids": [99]}, players) == [],
          "空账号 → 空列表（调用方报错）")

    print("\n[5] 真实 SQLite 往返：落库的邮件重新 load 必须还在")
    from sghy_server.data.store import SaveStore
    tmpdb = os.path.join(tempfile.gettempdir(), "sghy_mail_deliver_it.db")
    for suffix in ("", "-wal", "-shm"):
        p = tmpdb + suffix
        if os.path.exists(p):
            os.remove(p)
    db_store = SaveStore(db_path=tmpdb, player_id=5001, json_path=None)
    seed = await db_store.start()
    seed.setdefault("player", {}).update({"id": 5001, "name": "测试甲", "lv": 20})
    seed.update({"mails": {}, "mail_seq": 0})
    await db_store.repo.save(5001, seed, dirty={"heroes": set(), "equips": set()})
    seed2 = copy.deepcopy(seed)
    seed2["player"]["id"] = 5002
    seed2["player"]["name"] = "测试乙"
    await db_store.repo.save(5002, seed2, dirty={"heroes": set(), "equips": set()})

    res = await mail.deliver(db_store, [5002], "离线角色真实落库", "",
                             [{"kind": "property", "prop": "Diamond", "value": 88}])
    check(len(res["ok"]) == 1 + 0 and res["ok"][0]["path"] == "db", "真实库投递走 DB 路径")
    back = await db_store.repo.load(5002)
    got = list((back.get("mails") or {}).values())
    check(len(got) == 1 and got[0]["title"] == "离线角色真实落库",
          "重新 load 后邮件还在（%s）" % [g.get("title") for g in got])
    check(back["player"]["name"] == "测试乙" and int(back["player"]["lv"]) == 20,
          "同角色其它字段没被写坏（%s / lv%s）"
          % (back["player"]["name"], back["player"]["lv"]))
    check(int(back.get("mail_seq") or 0) == 1, "mail_seq 已自增（%s）" % back.get("mail_seq"))
    await db_store.stop()

    print("\n%s  %d/%d" % ("全部通过" if TOTAL[0] == TOTAL[1] else "有失败项", TOTAL[1], TOTAL[0]))
    return 0 if TOTAL[0] == TOTAL[1] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
