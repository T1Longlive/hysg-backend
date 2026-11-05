"""邮件系统（300 新邮件 / 5003 已读 / 5004 删除 / 5005 单领 / 5006 一键领）。

客户端事实（MailManager / MailData / MailEntry / MailBoxPanel 实读）：

  · **邮件列表只从 EnterGame.mails 初始化** —— `GetMails(10002)` 客户端从不调用（死接口）
  · 300 推新邮件（MailData）；5003/5004/5005 请求都是 IDMessage{id}，回包也回 IDMessage{id}
  · **5006（一键领取）无 body**，回包内容客户端**完全不解析**（只当 ack）→ 必须回，空包也行，
    否则 socket:send 的 9 秒超时会弹 Alert + 断线重连
  · 5005 回包只取 `id`，奖励展示用的是**客户端本地的 mail.bonusItems**（来自 MailData.award）
    → 邮件里 award 写错，界面就显示空白格
  · ⚠️ `MailData:setSvrMail` 里 `self.collected = false` 是**硬编码**，proto 也没有 collected 字段
    → 客户端无法表达"已领取"。本模块约定：**领取成功即删除邮件**（claim / claim_all），
    不再下发"已领取"的邮件，避免纯文本邮件被误渲染成可领取/已领取。
  · 邮件类型只由**有无奖励**决定（有奖励 Award / 无奖励 Notice），调用方不能指定：
    client MailEntry 对 Award 恒画「领取奖励」角标，不看附件内容。

存档：`PLAYER_DATA["mails"] = {id: {...}}` + `PLAYER_DATA["mail_seq"]`
每条：{id, senderName, title, content, type, status, sendTime, award[], claimed,
       expireAt, batchId, operator}
  type:   0 Notice / 1 Award / 2 Card1 / 3 Card2
  status: 0 NEW / 1 READ / 2 ARCHIVED
保留 batchId / operator / expireAt 供后续网页端发信与审计使用（用户 2026-10-26 规划）。
"""

import time

MAIL_TYPE_NOTICE, MAIL_TYPE_AWARD = 0, 1
# 月卡/周卡每日奖励专用（ShopConfig.MailType；proto MailType Card1=2 / Card2=3）
MAIL_TYPE_CARD1, MAIL_TYPE_CARD2 = 2, 3
STATUS_NEW, STATUS_READ, STATUS_ARCHIVED = 0, 1, 2

DEFAULT_SENDER = "系统"      # 用户拍板：发信人统一叫"系统"
EXPIRE_DAYS = 30             # 用户拍板：30 天过期


def _data():
    from . import core
    return core.PLAYER_DATA


def mails(data=None):
    return (data or _data()).setdefault("mails", {})


def _seq(data=None):
    d = data or _data()
    return int(d.get("mail_seq") or 0)


def _next_id(data=None):
    d = data or _data()
    n = int(d.get("mail_seq") or 0) + 1
    d["mail_seq"] = n
    return n


# ---------------------------------------------------------------- 查询 / 清理

def prune_expired(data=None, now=None):
    """删掉已过期邮件（登录时扫一次即可）。返回清理条数。"""
    now = int(now or time.time())
    box = mails(data)
    dead = [k for k, m in box.items() if int(m.get("expireAt") or 0) and int(m["expireAt"]) <= now]
    for k in dead:
        box.pop(k, None)
    return len(dead)


def to_snapshot(data=None, now=None):
    """EnterGame.mails 用的列表。

    已过期 / 已领取的邮件**一律不下发**，读取时顺手从存档里清掉。
    领取动作本身（claim / claim_all）已直接删除邮件；这里只兜住历史遗留的
    claimed 行——客户端 proto 没有 collected 字段，无法表达"已领取"状态。
    """
    now = int(now or time.time())
    box = mails(data)
    out = []
    stale = []
    for k, m in box.items():
        claimed = bool(m.get("claimed"))
        expired = int(m.get("expireAt") or 0) > 0 and int(m["expireAt"]) <= now
        if claimed or expired:
            stale.append(k)
            continue
        out.append({
            "id": int(m.get("id") or k),
            "senderName": str(m.get("senderName") or DEFAULT_SENDER),
            "title": str(m.get("title") or ""),
            "content": str(m.get("content") or ""),
            # ★ 2026-10-28 修：`m.get("type") or MAIL_TYPE_AWARD` 把 0（Notice
            #   纯文本）当 falsy 吞掉 → 推给客户端的纯文本邮件全变成 type 1，
            #   客户端 MailEntry 恒画「领取奖励」角标。只对缺键用默认值。
            "type": int(m["type"]) if m.get("type") is not None else MAIL_TYPE_AWARD,
            "status": int(m.get("status") or STATUS_NEW),
            "sendTime": int(m.get("sendTime") or 0),
            "award": list(m.get("award") or []),
        })
    for k in stale:
        box.pop(k, None)
    out.sort(key=lambda x: x["sendTime"], reverse=True)
    return out


# ---------------------------------------------------------------- 发信

def send_mail(title, content, awards=None, sender=DEFAULT_SENDER,
              expire_days=EXPIRE_DAYS, batch_id="", operator="", data=None,
              mail_type=None):
    """发一封邮件，返回新邮件 id。

    awards 用服务端内部格式（同 _awards_to_specs 的输入）：
      {"kind": "object",   "type": 20002, "count": 5}
      {"kind": "property", "prop": "Dollar", "value": 100}

    邮件类型**只由有无奖励决定**，不接受调用方乱指定：
      有奖励 → Award(1)（客户端显示「领取奖励」）
      无奖励 → Notice(0)（客户端显示「查看详情」，且详情按钮为「确定」）
    客户端 MailEntry 对 Award 恒画「领取奖励」角标、不看附件，所以纯文本邮件
    必须是 Notice，否则会被渲染成"可领取"。

    唯一例外是**月卡/周卡每日奖励**：调用方按 ShopConfig.MailType 传
    Card1(2)/Card2(3)，客户端对这两种类型同样画「领取奖励」；无附件时仍强制
    回落 Notice，避免出现"可领取却没有奖励"的邮件。
    """
    d = data or _data()
    now = int(time.time())
    mid = _next_id(d)
    mtype = MAIL_TYPE_AWARD if awards else MAIL_TYPE_NOTICE
    if awards and int(mail_type or 0) in (MAIL_TYPE_CARD1, MAIL_TYPE_CARD2):
        mtype = int(mail_type)
    mails(d)[str(mid)] = {
        "id": mid,
        "senderName": str(sender or DEFAULT_SENDER),
        "title": str(title or ""),
        "content": str(content or ""),
        "type": int(mtype),
        "status": STATUS_NEW,
        "sendTime": now,
        "award": list(awards or []),
        "claimed": False,
        "expireAt": now + int(expire_days) * 86400 if int(expire_days) > 0 else 0,
        "batchId": str(batch_id or ""),
        "operator": str(operator or ""),
    }
    return mid


def get_mail(mid, data=None):
    return (mails(data) or {}).get(str(int(mid or 0)))


# ---------------------------------------------------------------- 群体发放

async def deliver(store, player_ids, title, content, awards=None,
                  sender=DEFAULT_SENDER, expire_days=EXPIRE_DAYS,
                  operator="web", batch_id=""):
    """按**角色 id 列表**批量投递（发信台"选玩家群体"的落地实现）。

    两条完全不同的写入路径，不能混：

      · 目标 == 当前活跃角色（store.player_id）：改内存 PLAYER_DATA，
        走 save_player_data + flush_now 落库，再 push_mail 推 300 —— 在线客户端
        面板立刻出现（否则要重登）。
      · 其它角色（离线 / 非活跃）：repo.load 出整份存档 → 塞邮件 → repo.save。
        heroes/equips 用空 dirty 跳过（没变），只重写 player 行（extra_json 里
        含 mails/mail_seq）。下次登录 EnterGame.mails 自然带上。

    ★ 为什么不能直接改 PLAYER_DATA 再 flush 全服：进程里只有**一个**活动角色
      的存档（store 单实例 + switch_player 原地重绑），其余角色的数据根本不在
      内存。想"发给所有人"只能逐个走 DB。

    返回 {"ok": [{player, mail, path}], "failed": [{player, reason}]}。
    """
    from . import core
    active = int(getattr(store, "player_id", 0) or 0)
    ok, failed = [], []
    for pid in player_ids:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            if pid == active:
                mid = send_mail(title, content, awards, sender=sender,
                                expire_days=expire_days, batch_id=batch_id,
                                operator=operator, data=core.PLAYER_DATA)
                core.save_player_data(core.PLAYER_DATA)
                core.flush_now()
                core.push_mail(mid)
                ok.append({"player": pid, "mail": mid, "path": "memory"})
            else:
                data = await store.repo.load(pid)
                if data is None:
                    failed.append({"player": pid, "reason": "角色不存在"})
                    continue
                mid = send_mail(title, content, awards, sender=sender,
                                expire_days=expire_days, batch_id=batch_id,
                                operator=operator, data=data)
                # heroes/equips 未变 → 空集合让 repo 跳过整表 diff
                await store.repo.save(pid, data,
                                      dirty={"heroes": set(), "equips": set()})
                ok.append({"player": pid, "mail": mid, "path": "db"})
        except Exception as e:                      # 单个角色失败不拖垮整批
            failed.append({"player": pid, "reason": str(e)})
    return {"ok": ok, "failed": failed}


# ---------------------------------------------------------------- 交互

def mark_read(mid, data=None):
    """标记已读。返回 (ok, reason)。"""
    m = get_mail(mid, data)
    if not m:
        return False, "邮件不存在"
    if int(m.get("status") or 0) == STATUS_NEW:
        m["status"] = STATUS_READ
    return True, "ok"


def delete(mid, data=None):
    m = get_mail(mid, data)
    if not m:
        return False, "邮件不存在"
    mails(data).pop(str(int(mid)), None)
    return True, "ok"


def _grant_awards(awards):
    """发奖：统一走 core._grant_sign_awards。

    它会按配置把 ObjectAward 分到：道具（items）/ 卡实例（401）/ 装备实例（403）
    / 武将实例（404）/ 头像框。★ 以前这里无条件 core.add_item，装备附件会被
    塞进 items，客户端当成道具显示且无法穿戴（2026-10-27 邮件发飞将紫金冠）。
    """
    from . import core
    return core._grant_sign_awards(awards or [])


def claim(mid, data=None):
    """单封领奖。返回 (ok, reason, awards)。已领过的返回失败。"""
    m = get_mail(mid, data)
    if not m:
        return False, "邮件不存在", []
    if m.get("claimed"):
        return False, "该邮件奖励已领取", []
    awards = list(m.get("award") or [])
    if not awards:
        return False, "该邮件没有可领取的奖励", []
    _grant_awards(awards)
    # 已领取 → 直接删除（proto 无 collected 字段，client 无法表达"已领取"状态）
    mails(data).pop(str(int(mid)), None)
    return True, "ok", awards


def claim_all(data=None):
    """一键领取所有未领奖励。返回 (ok, reason, 发放汇总, 涉及邮件 id 列表)。"""
    box = mails(data)
    all_awards = []
    keys = []
    ids = []
    for k in sorted(box, key=lambda x: int(box[x].get("sendTime") or 0)):
        m = box[k]
        awards = list(m.get("award") or [])
        if not awards:
            continue
        all_awards.extend(awards)
        keys.append(k)
        ids.append(int(m.get("id") or k))
    if not all_awards:
        return False, "没有可领取的奖励", [], []
    _grant_awards(all_awards)
    # 已领取 → 直接删除（同上）
    for k in keys:
        box.pop(k, None)
    return True, "ok", all_awards, ids


def unread_count(data=None):
    return sum(1 for m in (mails(data) or {}).values()
               if not m.get("claimed") and int(m.get("status") or 0) == STATUS_NEW)


def list_all(data=None):
    """管理端用：所有邮件（含已领/过期状态）。"""
    out = []
    for k, m in (mails(data) or {}).items():
        out.append({
            "id": int(m.get("id") or k),
            "senderName": str(m.get("senderName") or DEFAULT_SENDER),
            "title": str(m.get("title") or ""),
            "content": str(m.get("content") or ""),
            # ★ 2026-10-28 修：同 falsy-zero 问题——0(Notice) 被吞成 1(Award)，
            #   管理端已发记录里纯文本邮件全部显示成「奖励」。
            "type": int(m["type"]) if m.get("type") is not None else MAIL_TYPE_AWARD,
            "status": int(m.get("status") or STATUS_NEW),
            "sendTime": int(m.get("sendTime") or 0),
            "award": list(m.get("award") or []),
            "claimed": bool(m.get("claimed")),
            "expireAt": int(m.get("expireAt") or 0),
            "batchId": str(m.get("batchId") or ""),
            "operator": str(m.get("operator") or ""),
        })
    out.sort(key=lambda x: x["sendTime"], reverse=True)
    return out
