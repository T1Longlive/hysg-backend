"""单笔充值档位（2026-10-23 用户拍板）。

档位 6/25/30/50/68/98/198/328/648 元；98 以下 4 种奖励、其余 3 种；
每档 checkTimes=10（可重复充值领取）。
"""

GRADES = [
    {"dollar": 6,   "checkTimes": 10, "awards": [
        {"kind": "object", "type": 20002, "count": 10},
        {"kind": "object", "type": 20096, "count": 1},
        {"kind": "object", "type": 25026, "count": 1},
        {"kind": "property", "prop": "Dollar", "value": 30}]},
    {"dollar": 25,  "checkTimes": 10, "awards": [
        {"kind": "object", "type": 20007, "count": 5},
        {"kind": "object", "type": 20097, "count": 1},
        {"kind": "object", "type": 29910, "count": 1},
        {"kind": "property", "prop": "Coin", "value": 50000}]},
    {"dollar": 30,  "checkTimes": 10, "awards": [
        {"kind": "object", "type": 20010, "count": 3},
        {"kind": "object", "type": 29901, "count": 1},
        {"kind": "object", "type": 25000, "count": 1},
        {"kind": "property", "prop": "Dollar", "value": 100}]},
    {"dollar": 50,  "checkTimes": 10, "awards": [
        {"kind": "object", "type": 20002, "count": 30},
        {"kind": "object", "type": 29910, "count": 1},
        {"kind": "object", "type": 25029, "count": 1},
        {"kind": "property", "prop": "Dollar", "value": 200}]},
    {"dollar": 68,  "checkTimes": 10, "awards": [
        {"kind": "object", "type": 29901, "count": 1},
        {"kind": "object", "type": 25026, "count": 1},
        {"kind": "property", "prop": "Dollar", "value": 300}]},
    {"dollar": 98,  "checkTimes": 10, "awards": [
        {"kind": "object", "type": 29903, "count": 1},
        {"kind": "object", "type": 29965, "count": 1},
        {"kind": "object", "type": 20114, "count": 1}]},
    {"dollar": 198, "checkTimes": 10, "awards": [
        {"kind": "object", "type": 29966, "count": 1},
        {"kind": "object", "type": 29971, "count": 1},
        {"kind": "object", "type": 20010, "count": 30}]},
    {"dollar": 328, "checkTimes": 10, "awards": [
        {"kind": "object", "type": 20115, "count": 1},
        {"kind": "object", "type": 29965, "count": 2},
        {"kind": "object", "type": 29982, "count": 1}]},
    {"dollar": 648, "checkTimes": 10, "awards": [
        {"kind": "object", "type": 20115, "count": 2},
        {"kind": "object", "type": 29966, "count": 1},
        {"kind": "object", "type": 20117, "count": 1}]},
]


def grades_of():
    """档位列表（activities.json 显式配置优先；未配则用内置 GRADES）。"""
    from . import common
    cfg = common.activity_cfg("single_pay") or {}
    if cfg.get("grades"):
        return cfg["grades"]
    return GRADES
