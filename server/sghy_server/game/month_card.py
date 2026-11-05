"""月卡 / 周卡（ShopConfig Group=3，真实货币档）。

客户端事实（PayView / UserManager / LevelManager 实读）：
  · 购买走 BuyProp(5210)，回 AwardResult → 奖励弹窗；
  · 有效期走 BuyPropRecordData：
      590 NewBuyPropRecord / 591 UpdateBuyPropRecord / EnterGame.buyPropRecordDatas(20)
      {id=记录id, type=ShopConfig.ID, totalCount=累计购买次数, endDate=**剩余天数**}
    PayView 把 record.endDate 直接当"剩余天数"显示（不是时间戳），
    LevelManager 用 endDate>0 判定月卡扫荡特权 —— 所以服务端必须维护递减的天数。
  · 每日奖励 DayAwards 由**邮件**发放，邮件类型取 ShopConfig.MailType
    （Card1=2 / Card2=3；1016→2，1017/1039→3）。
  · 购买即得 Awards；有效期内续费叠加时长（客户端文案：再次购买延长 N 天）。

存档：PLAYER_DATA["buy_props"] = {str(shop_id): {id, type, totalCount, endDate, last_day}}
      PLAYER_DATA["buy_prop_seq"] = 记录 id 自增序号
"""

import datetime

from . import game_config


def _today():
    return datetime.date.today()


def _ymd(d):
    return "%04d%02d%02d" % (d.year, d.month, d.day)


def _records():
    from . import core
    return core.PLAYER_DATA.setdefault("buy_props", {})


def _norm(rec):
    return {"id": int(rec.get("id") or 0), "type": int(rec.get("type") or 0),
            "totalCount": int(rec.get("totalCount") or 0),
            "endDate": int(rec.get("endDate") or 0)}


def records_snapshot():
    """EnterGame.buyPropRecordDatas 用列表（按记录 id 升序，稳定）。"""
    out = [_norm(r) for r in (_records() or {}).values() if isinstance(r, dict)]
    out.sort(key=lambda r: r["id"])
    return out


# 月卡/至尊月卡（ShopConfig Group=3，配置描述明写「开启扫荡重置功能」）
MONTH_CARD_IDS = (1016, 1017)


def has_active(ids=MONTH_CARD_IDS):
    """是否持有**有效期内的**月卡（客户端 LevelManager:buyLevelTimes 同样判
    `isBuyed(1016/1017)` 且 `record.endDate > 0`，endDate 是剩余天数）。"""
    recs = _records() or {}
    for sid in ids:
        r = recs.get(str(int(sid)))
        if isinstance(r, dict) and int(r.get("endDate") or 0) > 0:
            return True
    return False


def active_days(ids=MONTH_CARD_IDS):
    """有效月卡的剩余天数（取最大）。无月卡返回 0。"""
    best = 0
    recs = _records() or {}
    for sid in ids:
        r = recs.get(str(int(sid)))
        if isinstance(r, dict):
            best = max(best, int(r.get("endDate") or 0))
    return best


def buy(item, count=1):
    """购买/续费。返回 (记录, 是否首次购买)。

    · 有效期内（endDate>0）再买 → 在剩余天数上叠加 Duration×count；
    · 已过期（endDate<=0）再买 → 从 Duration×count 重新起算；
    · totalCount = 累计购买次数（客户端 canBuy/MaxCount 用）。
    · last_day 置为今天：购买当天的每日奖励不发，次日起按天发邮件。
    """
    from . import core
    sid = int(item["id"])
    cnt = int(count or 1)
    duration = int(item.get("duration") or 0) * cnt
    st = _records().get(str(sid))
    is_new = not isinstance(st, dict)
    if is_new:
        seq = int(core.PLAYER_DATA.get("buy_prop_seq") or 0) + 1
        core.PLAYER_DATA["buy_prop_seq"] = seq
        st = {"id": seq, "type": sid, "totalCount": 0, "endDate": 0,
              "last_day": _ymd(_today())}
        _records()[str(sid)] = st
    st["totalCount"] = int(st.get("totalCount") or 0) + cnt
    remain = int(st.get("endDate") or 0)
    st["endDate"] = remain + duration if remain > 0 else duration
    st["last_day"] = _ymd(_today())
    return st, is_new


def settle(push=True):
    """跨天结算（登录与后台巡检都调用）：

    · 每个有效记录按 last_day → 今天的天数，逐天补发 DayAwards 邮件；
    · endDate 同步递减（最少到 0，过期后不再发信）；
    · 返回发生变化的记录列表（调用方推 591 刷新客户端剩余天数）。

    push=True 时立刻把邮件推给在线客户端（300 NewMail）；登录流程传 False
    —— 随后 SelectCharacter 会统一推一遍邮箱，避免同一封邮件推两次。
    """
    from . import core
    from . import mail
    today = _today()
    changed = []
    for sid, rec in list((_records() or {}).items()):
        if not isinstance(rec, dict):
            continue
        end_days = int(rec.get("endDate") or 0)
        if end_days <= 0:
            continue
        item = game_config.get_shop_item(sid)
        if not item or int(item.get("group") or 0) != 3:
            continue
        last = str(rec.get("last_day") or "")
        try:
            last_d = datetime.date(int(last[:4]), int(last[4:6]), int(last[6:]))
        except (ValueError, TypeError):
            last_d = today
        delta = (today - last_d).days
        if delta <= 0:
            continue
        days = min(delta, end_days)
        awards = list(item.get("day_awards") or [])
        if awards:
            card = str(item.get("name") or "月卡").split("·")[0]
            for _ in range(days):
                mid = mail.send_mail(
                    "%s每日奖励" % card, "每日奖励已发放，请查收。", awards,
                    mail_type=int(item.get("mail_type") or 0))
                if push:
                    core.push_mail(mid)
        rec["endDate"] = end_days - days
        rec["last_day"] = _ymd(today)
        changed.append(rec)
    return changed
