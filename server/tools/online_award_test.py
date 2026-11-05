"""在线奖励/购买 三连修回归（2026-10-28）。

用户报障：
  1. 在线礼包有些"没到账/没及时刷新"
  2. 领取铜钱后主界面铜钱不刷新
  3. 元宝购买提示"服务器异常"

对应三个修复：
  A. buy._vip_limit 崩溃（'list' object has no attribute 'values'）——
     上午 _load_vip 改读 rt_all lua 后返回 list，buy.py 仍按 dict.values() 迭代
  B. 10005/5324 领奖后不推 200（OP_UPDATE_CHARACTER）→ 主界面铜钱/元宝不刷新
  C. 在线时长跨天重置 sync_ts=0 → cur_minutes 恒 0 → 领奖被拒"在线时长不足"
     → 红亮点着点不动（"没到账/没及时刷新"）

用法：python tools/online_award_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_online_award_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sghy_server.game import buy, core, growth
from sghy_server.game.activities import online_award


class Sess:
    def __init__(self):
        self.sent = []

    def send(self, op, body):
        self.sent.append(op)

    def log(self, *a):
        pass


def _reset(vip=8, coin=0, diamond=999999):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"lv": 60, "vip": vip, "coin": coin, "diamond": diamond},
        "heroes": {}, "items": {}, "equips": {}, "fate": {},
        "levels": {}, "level_records": {},
    })


def test_buy_coin_no_crash():
    """A. 买铜钱（10050 路径）不能崩：_vip_cfg 现在可能是 list。"""
    growth.init()
    _reset(vip=8, coin=0, diamond=999999)
    ok, reason, cost, gained, is_free, crit = buy.buy_coin()
    assert ok, "买铜钱应成功，实际被拒: %s" % reason
    assert cost > 0 and gained > 0, (cost, gained)
    assert core.PLAYER_DATA["player"]["coin"] == gained
    print("PASS 买铜钱不崩：VIP8 档位读取正常，扣钻 %d 得铜钱 %d" % (cost, gained))


def test_online_award_claim_pushes_200():
    """B. 领在线礼包（属性奖励）必须推 200，否则主界面货币不刷新。"""
    growth.init()
    _reset()
    st = online_award._st()
    # 强制当前档为"15分钟 铜钱 20000"，并造出达标时长
    cfg = online_award.common.activity_cfg("online_award") or {}
    grades = cfg.get("grades") or []
    coin_grade = None
    for i, g in enumerate(grades, 1):
        if any(a.get("kind") == "property" and a.get("prop") == "Coin"
               for a in (g.get("awards") or [])):
            coin_grade = i
            break
    assert coin_grade, "配置里应有铜钱档位: %s" % grades
    st["grade"] = coin_grade
    st["day"] = online_award._today()
    st["minutes_base"] = float(grades[coin_grade - 1]["minute"]) + 5   # 已达标
    st["sync_ts"] = online_award.common and __import__("time").time()

    before = core.PLAYER_DATA["player"]["coin"]
    s = Sess()
    core.dispatch(s, 10005, b"")
    ops = s.sent
    assert 10005 in ops and 301 in ops, ops
    assert 200 in ops, "属性奖励领奖后必须推 200（主界面货币刷新），实际 %s" % ops
    after = core.PLAYER_DATA["player"]["coin"]
    assert after > before, "铜钱应到账: %s -> %s" % (before, after)
    print("PASS 在线礼包铜钱到账且推 200：%d -> %d，frames=%s" % (before, after, ops))


def test_online_minutes_across_day_reset():
    """C. 跨天重置后在线时长必须继续累计（sync_ts 不能是 0）。"""
    growth.init()
    _reset()
    st = online_award._st()
    st["day"] = "20000101"          # 伪造昨天
    st["grade"] = 5
    st["minutes_base"] = 99.0
    st["sync_ts"] = 0
    st = online_award._refresh_day()
    assert st["grade"] == 1 and st["minutes_base"] == 0.0, st
    assert int(st.get("sync_ts") or 0) > 0, \
        "跨天重置后 sync_ts 必须置为当前时刻（0 会让时长永久卡住），实际 %r" % st.get("sync_ts")
    assert online_award.cur_minutes() >= 0
    print("PASS 跨天重置：grade=1 时长归零且 sync_ts=%s（时长继续累计）" % st["sync_ts"])


if __name__ == "__main__":
    test_buy_coin_no_crash()
    test_online_award_claim_pushes_200()
    test_online_minutes_across_day_reset()
    print("\n全部通过 ✔")
