"""布阵换位语义单元测试（2026-10-22）。

bug 现象：已上阵武将 A 调整到另一个已上阵武将 B 的位置——应**交换**，
实际做成了**替换**（B 被置 0 且客户端不刷新 → 显示空但实际仍上阵）。
规则：A 有原位置 → B 去 A 的原位置（交换）；A 原本未上阵 → B 下阵（置 0）。

用法：python tools/formation_swap_test.py
"""

import sys
import os
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_formation_swap_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core


def make_heroes():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 1, "lv": 60, "vip": 0, "diamond": 0},
        "heroes": {
            "10": {"id": 10, "type": 10016, "lv": 60, "position": 1},
            "20": {"id": 20, "type": 10026, "lv": 60, "position": 3},
            "30": {"id": 30, "type": 10036, "lv": 60, "position": 0},
        },
        "equips": {}, "items": {},
    })


def do_move(hero_id, position):
    return core.do_change_hero_position(hero_id, position)


def test_swap_two_deployed():
    make_heroes()
    displaced = do_move(10, 3)   # 1 号位(10) 换到 3 号位(20 占)
    assert displaced and displaced["id"] == 20
    hs = core.PLAYER_DATA["heroes"]
    assert hs["10"]["position"] == 3, "A 应到 3 号位: %s" % hs["10"]
    assert hs["20"]["position"] == 1, "B 应换到 A 的原 1 号位（交换）: %s" % hs["20"]
    print("PASS 换位交换：10@1 与 20@3 互换 -> 10@3 20@1")


def test_new_deploy_displaces():
    make_heroes()
    displaced = do_move(30, 3)   # 未上阵(30) 挤掉 3 号位(20)
    assert displaced and displaced["id"] == 20
    hs = core.PLAYER_DATA["heroes"]
    assert hs["30"]["position"] == 3
    assert hs["20"]["position"] == 0, "无旧位置可换 → 被挤武将下阵: %s" % hs["20"]
    print("PASS 新上阵挤位：30 上 3 号位，20 下阵（position=0）")


def test_move_to_empty():
    make_heroes()
    displaced = do_move(10, 5)   # 空位
    assert displaced is None
    hs = core.PLAYER_DATA["heroes"]
    assert hs["10"]["position"] == 5 and hs["20"]["position"] == 3
    print("PASS 移到空位：10@5，其他不动")


if __name__ == "__main__":
    test_swap_two_deployed()
    test_new_deploy_displaces()
    test_move_to_empty()
    print("TOTAL: 3 组 passed")
