"""账号隔离测试（2026-10-29）。

覆盖：
  · account 层：scrypt 哈希 / 登录即注册 / 密码校验 / 限速 / 游客派生
  · repo 层：账号 CRUD / 角色列表 / 归属查询 / id 分配 / 绑定
  · store 层：create_player / switch_player 数据隔离
  · core 层：apply_player_data 全局重绑 / compute_character_brief 现场还原
  · builders：LoginOK 多角色 / CreateCharacterOK 回包形状
  · ensure_legacy_binding 幂等迁移
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_tmpdir = tempfile.mkdtemp(prefix="sghy_acct_")
os.environ.setdefault("SGHY_PLAYER_DATA", os.path.join(_tmpdir, "player.json"))

from sghy_server.data.db import Database                      # noqa: E402
from sghy_server.data.repo import PlayerRepository            # noqa: E402
from sghy_server.data.store import SaveStore                  # noqa: E402
from sghy_server.game import account as account_svc           # noqa: E402

_PASS = _FAIL = 0


def ok(cond, label):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print("  !! FAIL: %s" % label)


def _reset_account_state():
    account_svc._ACCOUNTS.clear()
    account_svc._FAILS.clear()
    account_svc._repo = None


# ---------------------------------------------------------------- account 层

def test_hash():
    h = account_svc.hash_password("pw123")
    ok(account_svc.verify_password("pw123", h), "hash: 正确密码通过")
    ok(not account_svc.verify_password("pw124", h), "hash: 错误密码拒绝")
    ok(not account_svc.verify_password("pw123", ""), "hash: 空 hash 拒绝")
    ok(not account_svc.verify_password("pw123", "garbage"), "hash: 坏格式拒绝")


def test_login_register():
    _reset_account_state()
    acct, err = account_svc.login_or_register("alice", "pw1", False, "")
    ok(err is None and acct and acct["id"] > 0, "注册: 首次登录建号")
    aid = acct["id"]
    acct2, err2 = account_svc.login_or_register("alice", "pw1", False, "")
    ok(err2 is None and acct2["id"] == aid, "登录: 二次登录同账号同 id")
    _, err3 = account_svc.login_or_register("alice", "WRONG", False, "")
    ok(err3 == "账号或密码错误", "登录: 错误密码被拒")
    ok(account_svc.is_known_username("alice"), "注册表: 已登记")


def test_rate_limit():
    _reset_account_state()
    account_svc.login_or_register("bob", "right", False, "")
    for _ in range(account_svc.FAIL_LIMIT):
        _, err = account_svc.login_or_register("bob", "nope", False, "")
        ok(err == "账号或密码错误", "限速前: 错误密码报密码错")
    _, err = account_svc.login_or_register("bob", "right", False, "")
    ok(err is not None and "1 分钟" in err, "限速: %d 次失败后正确密码也被拒"
       % account_svc.FAIL_LIMIT)


def test_guest():
    _reset_account_state()
    a, ea = account_svc.login_or_register("", "", True, "DEV-ABC")
    b, eb = account_svc.login_or_register("", "", True, "DEV-ABC")
    c, ec = account_svc.login_or_register("", "", True, "DEV-XYZ")
    ok(ea is None and eb is None and ec is None, "游客: 三次登录均成功")
    ok(a["username"] == b["username"] and a["id"] == b["id"], "游客: 同设备同账号")
    ok(a["username"] != c["username"], "游客: 不同设备不同账号")
    ok(a["username"].startswith("guest_"), "游客: 账号名带 guest_ 前缀")


def test_legacy_password_set():
    """迁移旧行（hash 空）：首次登录补设密码。"""
    _reset_account_state()
    account_svc._ACCOUNTS["legacy"] = {"id": 999, "hash": ""}
    _, err = account_svc.login_or_register("legacy", "", False, "")
    ok(err is not None, "旧账号: 空密码提示设置")
    a2, err2 = account_svc.login_or_register("legacy", "setpw", False, "")
    ok(err2 is None, "旧账号: 首次登录补设密码成功")
    a3, err3 = account_svc.login_or_register("legacy", "setpw", False, "")
    ok(err3 is None and a3["id"] == 999, "旧账号: 补设后可正常登录")


# ---------------------------------------------------------------- repo/store 层

def _min_save(name, lv=10, heroes=3):
    return {
        "player": {"name": name, "lv": lv, "exp": 0, "coin": 100, "diamond": 10},
        "heroes": {str(i): {"id": i, "type": 10014, "lv": 1, "position": i if i <= 5 else 0}
                   for i in range(1, heroes + 1)},
        "items": {}, "equips": {}, "fate": {}, "levels": {}, "level_records": {},
    }


async def test_repo_store():
    tmp = tempfile.mkdtemp(prefix="sghy_acct_repo_")
    db = os.path.join(tmp, "t.db")
    database = Database(db)
    await database.init()
    repo = PlayerRepository(database)

    # 账号 CRUD
    a1 = await repo.account_add("u1", "hash1")
    a2 = await repo.account_add("u2", "hash2")
    rows = await repo.accounts_all()
    ok(set(rows) == {"u1", "u2"} and rows["u1"]["id"] == a1, "repo: 建号/全量读")
    await repo.account_set_hash(a1, "hash1b")
    ok((await repo.accounts_all())["u1"]["hash"] == "hash1b", "repo: 改 hash")

    # 角色：u1 两个、u2 一个
    await repo.save(1001, _min_save("甲"))
    await repo.player_bind_account(1001, a1)
    await repo.save(2001, _min_save("乙"))
    await repo.player_bind_account(2001, a1)
    await repo.save(2002, _min_save("丙"))
    await repo.player_bind_account(2002, a2)

    briefs = await repo.players_brief_by_account(a1)
    ok([b["id"] for b in briefs] == [1001, 2001], "repo: 按账号列角色（升序）")
    ok(briefs[0]["name"] == "甲" and briefs[0]["lv"] == 10, "repo: 角色摘要字段")
    ok(await repo.player_account_id(2001) == a1, "repo: 归属查询 u1")
    ok(await repo.player_account_id(2002) == a2, "repo: 归属查询 u2")
    ok(await repo.player_account_id(3001) is None, "repo: 不存在的角色 -> None")

    nid = await repo.next_player_id()
    ok(nid == 2003, "repo: id 分配 max+1（得到 %s）" % nid)

    # store 切换 + 数据隔离
    st = SaveStore(db, 1001, flush_delay=999)
    d1 = await st.start()
    d1["player"]["coin"] = 777
    d2 = await st.switch_player(2001)
    ok(d2["player"]["name"] == "乙", "store: switch 载入目标角色")
    ok(st.player_id == 2001, "store: switch 更新 player_id")
    back = await st.switch_player(1001)
    ok(back["player"]["coin"] == 777, "store: 切回后旧档变更仍在（先 flush 再切）")
    try:
        await st.switch_player(9999)
        ok(False, "store: 不存在的角色应抛 KeyError")
    except KeyError:
        ok(True, "store: 不存在的角色抛 KeyError")

    # create_player（建号路径）
    fresh = _min_save("丁", lv=1)
    await st.create_player(await repo.next_player_id(), a2, fresh)
    ok(await repo.player_account_id(nid) == a2, "store: create_player 绑定账号")
    got = await repo.load(nid)
    ok(got["player"]["name"] == "丁", "store: create_player 落库可回读")
    return tmp


# ---------------------------------------------------------------- core 层

def test_core_switch():
    from sghy_server.game import core
    before = json.dumps(core.PLAYER_DATA, sort_keys=True, default=str)
    lv_before = core.LEVEL_RECORDS

    data = _min_save("测试切档", lv=33, heroes=6)
    data["levels"]["1001"] = {"star": 2, "leftTimes": 5, "buyTimes": 0}
    core.apply_player_data(data, 4242)
    ok(core.CHAR_ID == 4242, "core: apply 后 CHAR_ID 切换")
    ok(core.LEVEL_RECORDS is core.PLAYER_DATA["levels"], "core: LEVEL_RECORDS 别名重绑")
    ok(core.char_name() == "测试切档", "core: char_name 跟随新档")

    before = json.dumps(core.PLAYER_DATA, sort_keys=True, default=str)   # apply 后的现场
    # compute_character_brief：返回摘要 + 现场还原
    brief = core.compute_character_brief(_min_save("摘要号", lv=22), 5150)
    ok(brief["id"] == 5150 and brief["name"] == "摘要号" and brief["lv"] == 22
       and brief["power"] > 0, "core: brief 字段齐（power=%s）" % brief["power"])
    after = json.dumps(core.PLAYER_DATA, sort_keys=True, default=str)
    ok(after == before, "core: brief 不动全局（现场还原）")
    ok(core.LEVEL_RECORDS is core.PLAYER_DATA["levels"], "core: brief 后别名仍正确")

    core.apply_player_data(json.loads(before), 1001)   # 还原现场，后续测试用 1001


def test_build_new_player_data():
    from collections import Counter
    from sghy_server.game import core
    d = core.build_new_player_data("新主公", 2, 1)
    ok(d["player"]["name"] == "新主公", "新档: 名字写入")
    ok(int(d["player"].get("lv") or 0) == 1, "新档: 队伍等级 1（实际 %s）"
       % d["player"].get("lv"))

    heroes = d["heroes"]
    ok(sorted(int(h["type"]) for h in heroes.values()) == [14005, 14015, 14025, 14035],
       "新档: 4 只 5 星圣兽各一")
    ok(all(int(h.get("star") or 0) == 5 for h in heroes.values()), "新档: 圣兽全是 5 星")
    ok(all(int(h.get("position") or 0) == 0 for h in heroes.values()), "new档: 圣兽不上阵")

    cards = Counter(d["item_cards"].values())
    ok(cards[29966] == 2 and cards[29804] == 3 and len(d["item_cards"]) == 5,
       "新档: 五星神将卡×2 + 五星武将卡×3（卡实例）")
    ok(int(d.get("item_card_seq") or 0) == 5, "新档: item_card_seq 对齐")

    eq = sorted(int(e["type"]) for e in d["equips"].values())
    ok(eq == [33513, 33613, 33713, 33813],
       "新档: 4 部位 3 星神装（武/甲/宝/盔）")
    ok(all(int(e.get("host") or 0) == 0 for e in d["equips"].values()),
       "新档: 装备未穿戴")

    fp = sorted(p["type"] for p in d["flag_parts"].values())
    ok(fp == [41001, 41002], "新档: 白色基础组件 2 件（旗冠/旗面）")
    ok(all(p.get("additions") for p in d["flag_parts"].values()),
       "新档: 军旗组件词条已生成")

    ok(d["levels"] == {} and d["level_records"] == {}, "新档: 关卡进度为空")
    ok(not d["items"], "新档: 无堆叠道具（旧测试种子已移除）")


# ---------------------------------------------------------------- builders

def test_builders():
    from sghy_server.proto import ps_protobuf_pb2 as pb
    from sghy_server.game import builders_pb

    m = pb.LoginOK()
    m.ParseFromString(builders_pb.build_login_ok(
        [{"id": 2001, "name": "甲", "lv": 33, "power": 12345},
         {"id": 2002, "name": "乙", "lv": 1, "power": 7}]))
    ok(len(m.characters) == 2, "LoginOK: 两角色")
    ok(m.characters[0].id == 2001 and m.characters[0].name == "甲"
       and m.characters[0].lv == 33 and m.characters[0].power == 12345,
       "LoginOK: 字段往返一致")
    ok(m.characters[1].power == 7, "LoginOK: 第二角色字段")

    m2 = pb.LoginOK()
    m2.ParseFromString(builders_pb.build_login_ok())     # 旧行为（黄金测试口径）
    ok(len(m2.characters) == 1 and m2.characters[0].id == 1001,
       "LoginOK: 无参保持旧行为")

    ck = pb.CreateCharacterOK()
    ck.ParseFromString(builders_pb.build_create_character_ok(
        {"id": 2003, "name": "新", "gender": 2, "type": 1, "lv": 60, "exp": 5}))
    ok(ck.id == 2003 and ck.name == "新" and ck.gender == 2
       and ck.lv == 60 and ck.teamExp == 5, "CreateCharacterOK: 字段往返一致")


# ---------------------------------------------------------------- 迁移

async def test_legacy_binding():
    tmp = tempfile.mkdtemp(prefix="sghy_acct_bind_")
    db = os.path.join(tmp, "t.db")
    database = Database(db)
    await database.init()
    repo = PlayerRepository(database)
    await repo.save(1001, _min_save("旧档"))

    from sghy_server.game.account import ensure_legacy_binding
    await ensure_legacy_binding(repo, "19881695096")
    aid = await repo.player_account_id(1001)
    ok(aid is not None, "迁移: 旧档已绑定账号")
    rows = await repo.accounts_all()
    ok(rows.get("19881695096", {}).get("id") == aid, "迁移: 账号行对齐")

    # 幂等：再来一次不覆盖、不重复建号
    await ensure_legacy_binding(repo, "19881695096")
    ok(await repo.player_account_id(1001) == aid, "迁移: 幂等不覆盖")
    ok(len(await repo.accounts_all()) == 1, "迁移: 不重复建号")


async def main():
    print("== account 层 ==")
    test_hash()
    test_login_register()
    test_rate_limit()
    test_guest()
    test_legacy_password_set()
    print("== repo/store 层 ==")
    await test_repo_store()
    print("== core 层 ==")
    test_core_switch()
    test_build_new_player_data()
    print("== builders ==")
    test_builders()
    print("== 旧档迁移 ==")
    await test_legacy_binding()
    print("=" * 40)
    print("PASS=%d FAIL=%d" % (_PASS, _FAIL))
    if _FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
