"""复现：5★ 装备洗练不消耗道具（2026-10-28 报障）。

根因：growth.init() 的 ExpSetConfig 读 legacy_json 旧反编译，
第 5 行 EquipReformCoin/Stone 为空 -> _num('')=0 -> cost=(0,0)。
rt_all 真配置（app.config.new.ExpSetConfig.lua）第 5 行是 450/3。

用法：venv/Scripts/python.exe tools/repro_reform_cost.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_repro_pd.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from sghy_server.game import core, growth

TARGET = 33115   # q4 5星，两条副属性

print("== 1) equip_reform_cost 按星级取消耗 ==")
for star in range(1, 7):
    print("  %d星 -> (coin=%s, stone=%s)" % ((star,) + growth.equip_reform_cost(star)))

print("== 2) 5★ 装备洗练前后道具/货币 ==")
core.PLAYER_DATA.clear()
core.PLAYER_DATA.update({
    "player": {"coin": 9999999, "lv": 60, "vip": 0, "diamond": 9999},
    "heroes": {},
    "equips": {"1": {"id": 1, "type": TARGET, "host": 0, "strengthen": 0,
                     "grade": 0, "grade_exp": 0,
                     "assist": {"1": {"attr": 1, "value": 100, "kind": 1,
                                      "times": 1, "min": 10, "max": 30}}}},
    "items": {"20002": 9999},
})
before = (core.PLAYER_DATA["player"]["coin"],
          core.PLAYER_DATA["items"]["20002"])
ok, why, eq = core.do_reform_equip(1, 1, 60)
after = (core.PLAYER_DATA["player"]["coin"],
         core.PLAYER_DATA["items"]["20002"])
print("  洗练前 coin/stone =", before)
print("  洗练结果 ok=%s why=%s" % (ok, why))
print("  洗练后 coin/stone =", after)
print("  实扣 coin=%d stone=%d" % (before[0] - after[0], before[1] - after[1]))
print("  客户端 CostGroup 应付: coin=%d stone=3"
      % (450 * (60 + 1)))
if after[1] == before[1]:
    print("  >>> BUG 复现：洗练石未扣（道具未消耗）")
