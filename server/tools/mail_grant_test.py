"""邮件物品发放范围单测（2026-10-28 需求）。

口径（用户拍板）：
  · 武将：仅 4/5/6★ **有效**武将（有效性 = 客户端图鉴口径 is_valid_hero）
  · 装备：紫(4)/神(5)品质、星级在可拥有区间（is_equip_ownable）
  · 洗练石：ItemConfig 20002（改造石），普通道具通道

验证三层：
  1. 目录（hero_catalog / equip_catalog）只出范围内的行
  2. http_gateway._validate_object_award 对范围内外类型的判罚
  3. 邮件 claim 实际发放：武将 404 / 装备 403 / 道具 451，实例与数量正确
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_mail_grant_test_player.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core, game_config, growth, mail
from sghy_server.net import http_gateway
from sghy_server.proto import ps_protobuf_pb2 as pb

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s %s" % (name, detail))


class FakeSession:
    def __init__(self):
        self.frames = []

    def send(self, opcode, body):
        self.frames.append((opcode, body))

    def log(self, *args):
        pass


def test_catalogs():
    print("[1] 目录")
    heroes = game_config.hero_catalog()
    check("hero_catalog 非空（有效 4-6★ 应为 286）", len(heroes) == 286, str(len(heroes)))
    check("hero_catalog 星级全在 4-6", all(4 <= h["star"] <= 6 for h in heroes))
    check("hero_catalog 无 dummy", all("dummy" not in h["name"].lower() for h in heroes))
    check("含 5★曹操", any(h["id"] == 10015 and h["name"] == "曹操" for h in heroes))
    check("is_valid_hero(10015)", game_config.is_valid_hero(10015))
    check("is_valid_hero 拒 dummy 14901", not game_config.is_valid_hero(14901))
    check("is_valid_hero 拒怪物 章鱼 18001", not game_config.is_valid_hero(18001))
    check("is_valid_hero 拒魔将 18015", not game_config.is_valid_hero(18015))
    check("is_valid_hero 拒 1★曹操 10011 之外仍有效",
          game_config.is_valid_hero(10011), "1★ 曹操本身是有效武将，只是不在邮件星级范围")

    equips = game_config.equip_catalog()
    check("equip_catalog 非空", len(equips) > 200, str(len(equips)))
    check("equip_catalog 全部紫/神", all(e["quality"] in (4, 5) for e in equips))
    check("equip_catalog 全部 ownable", all(e["ownable"] for e in equips))
    check("含紫3★青龙偃月刀 33113", any(e["id"] == 33113 for e in equips))
    check("不含绿装 31111", all(e["id"] != 31111 for e in equips))


def test_validation():
    print("[2] 附件校验 _validate_object_award")
    v = http_gateway._validate_object_award
    check("4★曹操 通过", v(10014) is None, str(v(10014)))
    check("5★曹操 通过", v(10015) is None)
    check("6★曹操 通过", v(10016) is None)
    check("1★曹操 拒（星级）", v(10011) is not None, str(v(10011)))
    check("dummy武将 14901 拒", v(14901) is not None)
    check("怪物 18001 拒", v(18001) is not None)
    check("紫3★装备 33113 通过", v(33113) is None, str(v(33113)))
    check("紫4★装备 33114 通过", v(33114) is None)
    check("紫5★装备 33115 通过", v(33115) is None)
    check("绿装 31111 拒", v(31111) is not None, str(v(31111)))
    check("蓝装 拒", v(32112) is not None, str(v(32112)))
    check("洗练石 20002 通过", v(20002) is None, str(v(20002)))
    check("普通道具 20014 通过", v(20014) is None, str(v(20014)))
    check("未知 99999999 拒", v(99999999) is not None, str(v(99999999)))


def test_mail_grant():
    print("[3] 邮件发信→dispatch 5005 领取→实例与推送")
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 999999, "lv": 60, "vip": 0},
        "heroes": {}, "equips": {}, "items": {},
        "collect": {}, "hero_collect": {},
    })
    sess = FakeSession()
    core.LIVE_SESSIONS.add(sess)
    try:
        awards = [
            {"kind": "object", "type": 10015, "count": 2},   # 5★曹操 ×2
            {"kind": "object", "type": 33113, "count": 1},   # 紫3★青龙偃月刀
            {"kind": "object", "type": 20002, "count": 50},  # 洗练石
            {"kind": "property", "prop": "Dollar", "value": 100},
        ]
        mid = mail.send_mail("测试", "邮件发放范围", awards)
        req = pb.IDMessage()
        req.id = mid
        core.dispatch(sess, core.OP_GET_MAIL_AWARD, req.SerializeToString())
        heroes = core.PLAYER_DATA["heroes"]
        eqs = core.PLAYER_DATA["equips"]
        items = core.PLAYER_DATA["items"]
        check("发 2 个 5★曹操实例", len(heroes) == 2 and
              all(int(h["type"]) == 10015 and int(h.get("star") or 0) == 5 for h in heroes.values()),
              str([(h["type"], h.get("star")) for h in heroes.values()]))
        check("发 1 件紫3★装备实例", len(eqs) == 1 and
              int(list(eqs.values())[0]["type"]) == 33113,
              str(list(eqs.values())))
        check("洗练石 50 入包", int(items.get("20002") or 0) == 50, str(items.get("20002")))
        check("元宝 +100", int(core.PLAYER_DATA["player"]["diamond"]) == 100)
        ops = [op for op, _ in sess.frames]
        check("广播 404×2（武将）", ops.count(404) == 2, str(ops))
        check("广播 403×1（装备）", ops.count(403) == 1, str(ops))
        check("新道具推 401×1（洗练石）", ops.count(401) == 1, str(ops))
        check("回 ack 5005", ops.count(core.OP_GET_MAIL_AWARD) == 1, str(ops))
        check("已领取邮件被删除", mail.get_mail(mid) is None)
    finally:
        core.LIVE_SESSIONS.discard(sess)


def main():
    print("== 邮件物品发放范围单测 ==")
    growth.init()
    test_catalogs()
    test_validation()
    test_mail_grant()
    if FAILS:
        print("\n失败 %d 项: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("\n全部通过")


if __name__ == "__main__":
    main()
