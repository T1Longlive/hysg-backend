"""battle_validate 单元测试：合法上报放行、六类伪造拒绝。

跑法：python tools/battle_validate_test.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sghy_server.game import battle_validate as bv
from sghy_server.game import core

PASS = 0
FAIL = 0


def check(name, expect_ok, expect_rule, report):
    global PASS, FAIL
    ok, rule, reason = bv.validate_level_end(core.game_config, report)
    good = (ok == expect_ok) and (rule == expect_rule or expect_rule is None)
    if good:
        PASS += 1
        print("  PASS %-42s -> %s | %s" % (name, rule, reason))
    else:
        FAIL += 1
        print("  FAIL %-42s 期望(%s,%s) 实际(%s,%s) | %s" % (
            name, expect_ok, expect_rule, ok, rule, reason))


def base(**kw):
    r = {"lid": 1001, "result": 1, "star": 1,
         "total_damage": 200, "use_time": 30,
         "my_die_count": 0, "my_group_ids": [1, 2],
         "my_group_power": 5000, "server_power": 5000,
         "owner_hero_ids": {1, 2, 3, 5}}
    r.update(kw)
    return r


print("== 合法 ==")
check("正常胜利", True, "win", base())
check("失败报 0 星", True, "loss", base(result=0, star=0))

print("== 关卡存在性 ==")
check("伪造关卡 9999", False, "level_exists", base(lid=9999))

print("== 结果值域 ==")
check("result=2", False, "result_domain", base(result=2))
check("result=-1", False, "result_domain", base(result=-1))

print("== 星级值域 ==")
check("star=3（客户端不可能报）", False, "star_domain", base(star=3))
check("失败却报星", False, "star_domain", base(result=0, star=1))

print("== 用时区间 ==")
check("用时 0 秒", False, "time_range", base(use_time=0))
check("用时 99999 秒", False, "time_range", base(use_time=99999))

print("== 伤害区间 ==")
check("伤害 1（打死人不可能）", False, "damage_range", base(total_damage=1))
check("伤害 9 亿", False, "damage_range", base(total_damage=900000000))
# overkill：客户端 totalHurtVal 含溢出伤害全额，高战力一刀秒 → 总伤远超敌血
check("overkill 高战力总伤 30 万", True, "win",
      base(total_damage=300000, my_group_power=100000, server_power=100000))
check("虚报 500 万（战力10万）", False, "damage_range",
      base(total_damage=5000000, my_group_power=100000, server_power=100000))
# 上限的战力项只认服务端 server_power：客户端虚报战力不能自抬合格线
# （1065 关误杀修复的残留口子：上报值参与上限 = 被校验方自定合格线）
check("虚报战力 9999 万自抬上限", False, "damage_range",
      base(total_damage=300000, my_group_power=99999999, server_power=5000))
check("无服务端战力时不给裕量", False, "damage_range",
      base(total_damage=300000, my_group_power=100000, server_power=None))

print("== 阵容归属 ==")
check("混入他人武将 666", False, "lineup",
      base(my_group_ids=[1, 666]))
check("上阵 8 人", False, "lineup",
      base(my_group_ids=[1, 2, 3, 4, 5, 6, 7, 8]))
check("战力虚报 +100%", False, "lineup",
      base(my_group_power=10000, server_power=5000))

print()
print("TOTAL: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
