"""幸运转盘（LuckyTurntable / LotteryDraw，2026-10-23）。

客户端权威链路（调研确认）：
  · 数据：5311 ActivityGroupData.lotteryDrawActivity = ShowLotteryDrawActivityData
      {ID(>0才显示入口), Date, Cells(必须14条, index 0..13, Probability=权重),
       Currencies(升序，id=第N抽起, count=花费), Times, Indexes(0基已抽中),
       keyCount, currency(2=可元宝买钥匙), price(单把钥匙元宝价)}
  · 抽奖：C→S **31100 ActivityInteractionData{ActivityID, Action=1}**（31100 不回 body！）
          S→C **31200 LotteryDrawActivityResultData{ID, Index(0基), KeyCount, Awards}**
          → 客户端自己 parseAwardStr 弹奖 + 播放落点动画
  · 买钥匙：C→S 31100{ActivityID, Action=0, TargetID=数量}
            S→C **31201 IDMessage{id=新keyCount}**
  · 抽完 14 格 → "宝箱已全部领取"

规则（2026-10-23 用户拍板）：大奖稀有（权重递进）；钥匙制，初始 3 把，可用元宝买。
★ 2026-11-04 修正：已抽中的格子不再参与随机（此前在全部 14 格上掷权重，
  会出现重复抽到同一格、有的格子永远抽不到；现在每抽只从未抽格子里选，
  14 格正好抽 14 次，抽完即"宝箱已全部领取"）。
"""

import random
import time

from ...proto import ps_protobuf_pb2 as pb
from . import common
from .one_pay import _fill_spec


def _cfg():
    return common.activity_cfg("turntable")


def _st():
    return common.act_state().setdefault(
        "turntable", {"times": 0, "keys": None, "indexes": [], "day": ""})


def _init_keys():
    cfg = _cfg() or {}
    return int(cfg.get("init_keys") or 3)


def _ensure():
    st = _st()
    # 7 天轮次重置：次数/已抽格子清零、钥匙回初始（2026-10-23 用户方案）
    cfg = _cfg() or {}
    if common.round_reset(st, int(cfg.get("round_days") or 7)):
        st["times"] = 0
        st["indexes"] = []
        st["keys"] = _init_keys()
    if st.get("keys") is None:
        st["keys"] = _init_keys()
    return st


def _cells():
    cfg = _cfg() or {}
    return cfg.get("cells") or []


def _currencies():
    cfg = _cfg() or {}
    cs = sorted(cfg.get("currencies") or [{"id": 1, "count": 1}], key=lambda c: int(c["id"]))
    return cs


def cost_for_next(st):
    """第 times+1 抽的花费（钥匙数）。"""
    n = int(st.get("times") or 0) + 1
    need = 1
    for c in _currencies():
        if int(c["id"]) <= n:
            need = int(c["count"])
    return need


def snapshot():
    """ShowLotteryDrawActivityData；未配置返回 None（ID=0 关闭）。"""
    cfg = _cfg()
    if not cfg:
        return None
    start, end, is_open = common.time_window(cfg)
    if not is_open:
        m = pb.ShowLotteryDrawActivityData()
        m.ID = 0        # 关闭：ID=0（客户端入口不显示）
        return m
    st = _ensure()
    m = pb.ShowLotteryDrawActivityData()
    m.ID = int(cfg.get("id") or 1)
    m.Date.startTime = int(start)
    m.Date.endTime = int(end)
    cells = _cells()
    for i, c in enumerate(cells):
        cell = m.Cells.add()
        cell.index = i
        _fill_spec(cell.award, c["award"])
        cell.Probability = int(c.get("weight") or 1)
    for c in _currencies():
        cur = m.Currencies.add()
        cur.id = int(c["id"])
        cur.count = int(c["count"])
    m.Times = int(st.get("times") or 0)
    for idx in (st.get("indexes") or []):
        m.Indexes.append(int(idx))
    m.keyCount = int(st.get("keys") or 0)
    m.currency = int(cfg.get("currency") or 2)
    m.price = int(cfg.get("key_price") or 50)
    return m


def draw():
    """抽奖（31100 Action=1）。返回 (ok, why, index0, award, key_count)。"""
    cfg = _cfg()
    if not cfg:
        return False, "活动未开启", -1, None, 0
    st = _ensure()
    cells = _cells()
    if not cells:
        return False, "活动未配置", -1, None, int(st.get("keys") or 0)
    # 只在**还没抽中**的格子里随机：抽到的格子从候选池移除，保证不重复，
    # 且 14 格正好抽 14 次（抽完才算"全部领取"）。
    drawn = {int(i) for i in (st.get("indexes") or []) if 0 <= int(i) < len(cells)}
    remain = [i for i in range(len(cells)) if i not in drawn]
    if not remain:
        return False, "宝箱已全部领取", -1, None, int(st.get("keys") or 0)
    need = cost_for_next(st)
    if int(st.get("keys") or 0) < need:
        return False, "钥匙不足（需 %d 把）" % need, -1, None, int(st.get("keys") or 0)
    # 加权随机（权重递进：大奖稀有）
    weights = [max(1, int(cells[i].get("weight") or 1)) for i in remain]
    idx = random.choices(remain, weights=weights, k=1)[0]
    st["keys"] = int(st.get("keys") or 0) - need
    st["times"] = int(st.get("times") or 0) + 1
    st.setdefault("indexes", []).append(idx)
    return True, "抽奖成功", idx, cells[idx]["award"], int(st.get("keys") or 0)


def buy_keys(count):
    """买钥匙（31100 Action=0）。返回 (ok, why, key_count)。"""
    cfg = _cfg()
    if not cfg:
        return False, "活动未开启", 0
    count = max(1, int(count or 1))
    price = int(cfg.get("key_price") or 50)
    from .. import core
    p = core.PLAYER_DATA.setdefault("player", {})
    total = price * count
    if int(p.get("diamond") or 0) < total:
        return False, "元宝不足（需 %d）" % total, 0
    p["diamond"] = int(p.get("diamond") or 0) - total
    st = _ensure()
    st["keys"] = int(st.get("keys") or 0) + count
    return True, "购买 %d 把钥匙" % count, int(st["keys"] or 0)


def build_result(activity_id, index0, key_count, award):
    """31200 LotteryDrawActivityResultData。"""
    m = pb.LotteryDrawActivityResultData()
    m.ID = int(activity_id)
    m.Index = int(index0)
    m.KeyCount = int(key_count)
    from .. import builders_pb, core
    m.Awards.ParseFromString(
        builders_pb.build_award_result("LuckyDraw", core._awards_to_specs([award])))
    return m.SerializeToString()
