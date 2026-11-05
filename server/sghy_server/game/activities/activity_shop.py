"""活动商店 / 超值兑换（ActivityShop / BestDeal，2026-10-23）。

客户端权威链路（调研确认）：
  · 数据：5311 ActivityGroupData.activityShopData = ActivityShopData
      {Grades:[GradeExchangeData{Grade(回显用), CheckTimes, UsedTimes, NeedItems,
        ResultAward, IsLimit, GroupName}], activityTime, isStart(>0 才显示入口)}
  · 兑换：C→S **5357 BuyActivityShop(ActivityShopMessage{id=Grade,
            activityShopChoose:[{type, idList}]})**
          S→C 5357 **GradeExchangeData 单档**（Grade 必须等于请求 id，否则客户端 nil 崩）
      道具/属性类消耗**不进 choose**（服务端按 NeedItems 自扣）；武将/装备实例走 idList。
  · 5358 UpdateActivityShop 客户端无 handler（推了无效）。

规则（2026-10-23 用户拍板）：用元宝/券换武将/装备实例、神将卡、装备卡等。
配置在 activities.json 的 activity_shop.grades。
"""

import time

from ...proto import ps_protobuf_pb2 as pb
from . import common
from .one_pay import _fill_spec


def _cfg():
    return common.activity_cfg("activity_shop")


def _st():
    st = common.act_state().setdefault("activity_shop", {"used": {}})
    # 7 天轮次重置：兑换次数清零（2026-10-23 用户方案）
    cfg = _cfg() or {}
    if common.round_reset(st, int(cfg.get("round_days") or 7)):
        st["used"] = {}
    return st


def snapshot():
    cfg = _cfg()
    if not cfg:
        return None
    start, end, is_open = common.time_window(cfg)
    if not is_open:
        m = pb.ActivityShopData()
        m.isStart = -1
        return m
    st = _st()
    used = st.get("used") or {}
    m = pb.ActivityShopData()
    m.isStart = 1
    m.activityTime.startTime = int(start)
    m.activityTime.endTime = int(end)
    for g in (cfg.get("grades") or []):
        grade = int(g["grade"])
        d = m.Grades.add()
        d.Grade = grade
        d.CheckTimes = int(g.get("checkTimes") or 1)
        d.UsedTimes = int(used.get(str(grade)) or 0)
        d.IsLimit = bool(g.get("isLimit", True))
        d.GroupName = g.get("groupName") or ""
        for n in g.get("needItems") or []:
            _fill_spec(d.NeedItems.add(), n)
        for r in g.get("resultAward") or []:
            _fill_spec(d.ResultAward.add(), r)
    return m


def _find_grade(grade):
    cfg = _cfg() or {}
    return next((g for g in (cfg.get("grades") or [])
                 if int(g["grade"]) == int(grade)), None)


def buy(grade, choose_list=None):
    """兑换（5357）。choose_list = [{type, idList}]（武将/装备实例）。
    返回 (ok, why, awards, new_grade_msg_bytes)。"""
    cfg = _cfg()
    if not cfg:
        return False, "活动未开启", [], None
    g = _find_grade(grade)
    if g is None:
        return False, "档位不存在", [], None
    st = _st()
    used = st.setdefault("used", {})
    cur_used = int(used.get(str(g["grade"])) or 0)
    check = int(g.get("checkTimes") or 1)
    if bool(g.get("isLimit", True)) and cur_used >= check:
        return False, "兑换次数已用完", [], None

    from .. import core
    p = core.PLAYER_DATA.setdefault("player", {})
    # ---- 资源校验与扣除 ----
    consumes = []          # (need_spec, 实例 id 列表)
    for n in (g.get("needItems") or []):
        if n.get("kind") == "property":
            prop = n.get("prop") or ""
            need = int(n.get("value") or 0)
            cur = int(p.get(prop.lower(), 0) or 0) if hasattr(p, "get") else 0
            if prop == "Dollar":
                cur = int(p.get("diamond") or 0)
            elif prop == "Coin":
                cur = int(p.get("coin") or 0)
            if cur < need:
                return False, "%s 不足（需 %d）" % (prop, need), [], None
            consumes.append(("prop", prop, need, []))
        else:
            # 道具 / 武将 / 装备实例：道具按下标（type），实例走 choose
            consumes.append(("object", n, int(n.get("count") or 0), []))
    # 实例类：从 choose_list 里取（武将/装备）
    chosen_ids = []
    for ch in (choose_list or []):
        for iid in (ch.get("idList") or []):
            chosen_ids.append(int(iid))
    # 扣属性/道具
    for kind, key, need, _ in consumes:
        if kind == "prop":
            if key == "Dollar":
                p["diamond"] = int(p.get("diamond") or 0) - need
            elif key == "Coin":
                p["coin"] = int(p.get("coin") or 0) - need
        else:
            # 道具（背包）
            itype = str(int(key.get("type") or 0))
            have = int((core.PLAYER_DATA.get("items") or {}).get(itype) or 0)
            if have < need:
                # 可能是武将/装备实例需求（走 choose）
                if not chosen_ids:
                    return False, "材料不足（物品 %s 需 %d）" % (itype, need), [], None
                continue
            core.PLAYER_DATA["items"][itype] = have - need
    # 实例如需：从武将/装备仓删除（不允许删上阵）
    for iid in chosen_ids:
        hero = (core.PLAYER_DATA.get("heroes") or {}).get(str(iid))
        if hero and int(hero.get("position") or 0) > 0:
            return False, "上阵武将不能作为材料", [], None
    for iid in chosen_ids:
        if (core.PLAYER_DATA.get("heroes") or {}).pop(str(iid), None):
            continue
        store = core.equip_store()
        store.pop(str(iid), None)

    # ---- 发奖 ----
    awards = list(g.get("resultAward") or [])
    core._grant_sign_awards(awards)
    used[str(g["grade"])] = cur_used + 1
    st["used"] = used

    # 5357 回包：GradeExchangeData 单档（Grade 必须回显）
    d = pb.GradeExchangeData()
    d.Grade = int(g["grade"])
    d.CheckTimes = check
    d.UsedTimes = cur_used + 1
    d.IsLimit = bool(g.get("isLimit", True))
    d.GroupName = g.get("groupName") or ""
    for n in (g.get("needItems") or []):
        _fill_spec(d.NeedItems.add(), n)
    for r in (g.get("resultAward") or []):
        _fill_spec(d.ResultAward.add(), r)
    return True, "兑换成功", awards, d.SerializeToString()
