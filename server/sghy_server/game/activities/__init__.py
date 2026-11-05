"""活动系统主组装（M1 骨架，2026-10-23）。

5311 GetActivityGroup → ActivityGroupData。各活动模块实现
snapshot(cfg) → 填 proto 字段；M1 先统一填时间窗/isStart，
进度数据由各活动里程碑（M2~M6）逐个补实。
"""

from ...proto import ps_protobuf_pb2 as pb
from . import common

# 活动名 → ActivityGroupData proto 字段名（一期 6 个）
_ACTIVITY_FIELD = {
    "online_award": "onlineAwardRecord",      # OnlineAwardRecordData（并入 ActivityData 系，组包时特殊处理）
    "lv_award": None,                          # 客户端走 missionManager，M5 定形态（方案 §8）
    "login_days": "loginDays",
    "total_pay": "totalPay",
    "single_pay": "singlePay",
    "total_consume": "totalConsume",
}


def _fill_activity_time(msg_activity, cfg):
    """给子活动消息填 ActivityTime + isStart（无时间窗的跳过）。"""
    start, end, is_open = common.time_window(cfg)
    if not is_open:
        return False
    if hasattr(msg_activity, "activityTime"):
        at = msg_activity.activityTime
        at.startTime = int(start)
        at.endTime = int(end) if end is not None else 0
    if hasattr(msg_activity, "isStart"):
        msg_activity.isStart = 1
    return True


def build_activity_group():
    """组装 ActivityGroupData（5311 回包 body）。"""
    m = pb.ActivityGroupData()
    cfg_all = common.load_activity_config()

    # ---- M2：充值三兄弟（单笔=onePay / 累计充值 / 累计消费）----
    from . import one_pay, grade_pay
    op = one_pay.snapshot()
    if op is not None:
        m.onePay.CopyFrom(op)
    tp = grade_pay.total_pay_snapshot()
    if tp is not None:
        m.totalPay.CopyFrom(tp)
    tc = grade_pay.total_consume_snapshot()
    if tc is not None:
        m.totalConsume.CopyFrom(tc)

    # ---- 充值自选礼包（PayChose）已砍掉（2026-10-23 用户要求）----
    #   显式下发 singlePay.isStart = -1（关闭）→ 客户端 getSinglePayOpen()=false
    #   → ActivityViewConfig 的 payChose 入口不显示。
    #   注意：空字段会被 pbc 解成默认对象、isStart=nil ≠ -1 → 被误判"开启"，
    #   所以必须显式发 -1（单笔充值入口用 onePayOpen，不受影响）。
    m.singlePay.isStart = -1

    # ---- 限时武将（TimeLimitWarrior）已砍掉（2026-10-23 用户要求）----
    #   getTimeLimitWarriorOpen = data.isStart >= 0 → 显式 -1 关闭。
    #   proto：ActivityGroupData.timeLimitedHeros（TimeLimitedHerosData）。
    m.timeLimitedHeros.isStart = -1

    # ---- M3：连续登录 ----
    from . import login_days
    ld = login_days.snapshot()
    if ld is not None:
        m.loginDays.CopyFrom(ld)

    # ---- 第二批：幸运转盘 / 神秘商店 / 活动商店（2026-10-23）----
    from . import turntable, secret_shop, activity_shop
    tt = turntable.snapshot()
    if tt is not None:
        m.lotteryDrawActivity.CopyFrom(tt)
    ss = secret_shop.snapshot()
    if ss is not None:
        m.SecretShop.CopyFrom(ss)
    ashop = activity_shop.snapshot()
    if ashop is not None:
        m.activityShopData.CopyFrom(ashop)

    return m.SerializeToString()


def on_recharge(total_price, single_price):
    """充值事件钩子（2026-10-23 改：两个充值活动都按 7 天轮次累计）。"""
    from . import one_pay, grade_pay
    one_pay.on_recharge(single_price)
    grade_pay.on_recharge(total_price)


# 进程内已见的轮次（round_days → round_key）；首次只记录，用于识别"进入新一轮"
_LAST_ROUND = {}


def tick():
    """轮次巡检（后台定时调用）：任一配置了 round_days 的活动进入新一轮时，
    立刻触发各活动的惰性重置（snapshot/_state 读取时清进度），返回 True。

    ★ 各活动的重置原本只在"下一次请求"时才发生：轮次结束瞬间若玩家没有
      重开面板，累计充值等进度会一直显示上一轮的数值（用户 2026-10-27 反馈
      "轮次结束后没有清零"）。由调用方在返回 True 时推送 5311 刷新客户端。
    """
    rounds = set()
    for name, cfg in (common.load_activity_config() or {}).items():
        if isinstance(cfg, dict) and cfg.get("enabled"):
            rd = int(cfg.get("round_days") or 0)
            if rd > 0:
                rounds.add(rd)
    changed = False
    for rd in rounds:
        rk = common.round_key(rd)
        old = _LAST_ROUND.get(rd)
        if old is None:
            _LAST_ROUND[rd] = rk          # 进程刚起：只对齐基线，不触发重置
        elif old != rk:
            _LAST_ROUND[rd] = rk
            changed = True
    if changed:
        build_activity_group()            # 惰性重置在读 snapshot 时落地

    # ---- 神秘商店多轮制（2026-10-29 对齐原版）----
    # 轮次切换 / 进入空档 / 空档转营业：广播 5317 整包（客户端有现成
    # handler，面板开着时倒计时走完立刻换商品或显示"下轮开启倒计时"）。
    try:
        from . import secret_shop as _ss
        if _ss.tick():
            common.core.broadcast(5317, _ss.snapshot().SerializeToString())
            common.core.flog("[secret_shop] 轮次切换 -> 已广播 5317")
    except Exception as _e:               # 巡检失败不拖垮 ticker
        import logging
        logging.getLogger(__name__).warning("secret_shop tick 失败: %r", _e)
    return changed
