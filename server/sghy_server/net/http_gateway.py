"""HTTP 网关（aiohttp）：服务器列表 / 公告 / 热更新配置 / GM 接口。

对应 mock 的 ThreadingHTTPServer 部分；GM 行为与 mock 一致
（入库 + 存档 + 通过 core.broadcast 广播），仅改为 HTTP 接口形式。
"""

from urllib.parse import urlparse, parse_qs

from aiohttp import web

import os
import time

from .. import settings
from ..game import core, game_config, mail


def _admin_denied(request):
    """管理接口令牌校验。SGHY_GM_TOKEN 为空时保持旧行为（仅本地调试）。"""
    token = settings.GM_TOKEN
    if not token:
        return None
    got = request.query.get("token") or request.headers.get("X-GM-Token", "")
    if got == token:
        return None
    return web.json_response({"status": False, "msg": "forbidden"}, status=403)


async def handle_server_list(request):
    # 直接下发 core 移植的 SERVER_LIST（与 mock 完全一致，客户端验证过）：
    # data.platform 等字段客户端 onServerListPHPResponse 会读，缺了直接崩。
    return web.json_response(core.SERVER_LIST)


async def handle_update_config(request):
    return web.json_response(core.UPDATE_CONFIG)


async def handle_announce(request):
    return web.json_response(core.ANNOUNCE)


async def handle_guest_login(request):
    # TV 版登录门（LoginPanel:requestPhp）：status!=false 才显示登录按钮。
    # 客户端只消费 status 字段；404 会让 Java 层抛 FileNotFoundException ->
    # visibleBtn(false) + 弹「网络不佳」（2026-10-30 实测踩过）。
    return web.json_response({"status": True, "account": "", "password": ""})


async def handle_account_server_roles(request):
    # 登录前的"账号-区服-角色"查询：返回该账号的角色列表 JSON 字符串。
    # 我们的账号隔离走 LoginOK.characters 主链路，这里回空表即可
    # （客户端 callback(false) 路径：记 0 个角色、继续登录，不拦路）。
    return web.json_response({"GetServerRolesByAccountResult": "[]"})


async def handle_gm(request):
    denied = _admin_denied(request)
    if denied is not None:
        return denied
    qs = parse_qs(urlparse(str(request.url)).query)

    def q(k, d=None):
        v = qs.get(k)
        return v[0] if v else d

    action = q("action", "")
    try:
        if action == "grant_item":
            msg = core.gm_grant_item(int(q("id")), int(q("count", 1)))
        elif action == "grant_equip":
            msg = core.gm_grant_equip(int(q("type")), int(q("count", 1)))
        elif action == "grant_hero":
            msg = core.gm_grant_hero(int(q("type")), int(q("count", 1)))
        elif action == "grant_flag_part":
            msg = core.gm_grant_flag_part(int(q("type")), int(q("count", 1)))
        elif action == "grant_flag_suit":
            msg = core.gm_grant_flag_suit(int(q("group")))
        elif action == "set_vip":
            msg = core.gm_set_vip(int(q("vip", 0)))
        elif action == "set_resource":
            msg = core.gm_set_resource(q("coin"), q("diamond"), q("lv"),
                                       q("add_diamond"))
        elif action == "set_total_pay":
            # 数据修复：本轮累计充值额直接设值（历史版本 round_pay 没落库）
            msg = core.gm_set_total_pay(int(q("value", 0)))
        elif action == "reload_config":
            # 配置热重载（tools/config_tool.py build 末尾会自动调用）
            from ..game import game_config
            msg = game_config.reload_all()
        else:
            msg = ("未知 action: %s（支持 grant_item / grant_equip / grant_hero / "
                   "grant_flag_part / grant_flag_suit / set_vip / set_resource / "
                   "set_total_pay / reload_config）") % action
    except Exception as e:
        msg = "GM 执行异常: %s" % e
    return web.json_response({"status": True, "msg": msg})


MAIL_PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "web", "mail_admin.html")


async def handle_mail_page(request):
    """发信台页面（单文件）。"""
    denied = _admin_denied(request)
    if denied is not None:
        return denied
    return web.FileResponse(MAIL_PAGE)


async def handle_mail_items(request):
    """发放物字典：?q=关键字&limit=60&cat=item|hero|equip。

    cat 口径（用户 2026-10-28 拍板）：
      item  → 正式道具（排除 dummy；含洗练石/改造石 20002）
      hero  → 4/5/6★ 有效武将（排除 dummy 行）
      equip → 紫(4)/神(5)品质、星级在可拥有区间（3~5★）的装备
    """
    denied = _admin_denied(request)
    if denied is not None:
        return denied
    q = (request.query.get("q") or "").strip()
    try:
        limit = max(1, min(300, int(request.query.get("limit") or 60)))
    except ValueError:
        limit = 60
    cat_param = (request.query.get("cat") or "item").strip().lower()
    if cat_param == "hero":
        cat = game_config.hero_catalog()
    elif cat_param == "equip":
        cat = game_config.equip_catalog()
    else:
        cat = game_config.item_catalog()
    if q:
        ql = q.lower()
        cat = [x for x in cat if ql in str(x.get("name", "")).lower() or q in str(x["id"])]
    return web.json_response({"total": len(cat), "items": cat[:limit]})


def _validate_object_award(tid):
    """邮件附件类型校验 -> 错误文案（None=通过）。

    允许：4/5/6★ 有效武将（客户端图鉴口径 is_valid_hero）/ 紫·神品质可拥有
    装备 / 非 dummy 道具（含洗练石）。ID 段不重叠：武将 1xxxx / 装备 3xxxx /
    道具 2xxxx、4xxxx（卡类）。
    """
    from ..game import growth
    tid = int(tid)
    if game_config.load_hero_config().get(tid) is not None:
        if not game_config.is_valid_hero(tid):
            return "武将 %d 不是有效武将（占位/怪物行），不可发放" % tid
        star = int(game_config.load_hero_config()[tid].get("Quality") or 0)
        if star < 4 or star > 6:
            return "武将 %s（%d★）不在可发放范围（邮件仅支持 4/5/6★，低星版本请改发对应高星 ID）" % (
                game_config.get_hero_name(tid), star)
        return None
    if growth.equip_row(tid):
        quality = growth.equip_quality(tid)
        if not growth.is_equip_ownable(tid) or quality not in (4, 5):
            color = growth.EQUIP_QUALITY_COLOR.get(quality, quality)
            return "装备 %s（%s %d★）不在可发放范围（邮件仅支持紫色/神品质且星级有效的装备）" % (
                game_config.get_equip_name(tid), color, growth.equip_star(tid))
        return None
    if game_config.item_exists(tid):
        if game_config.is_dummy_item(tid):
            return "道具 %d（%s）是占位道具，不可发放" % (tid, game_config.get_item_name(tid))
        return None
    return "未知类型 %d（不是有效的武将/装备/道具）" % tid


async def handle_mail_players(request):
    """发信台收件人清单：全部角色 + 归属账号 + 当前活跃角色 id。"""
    denied = _admin_denied(request)
    if denied is not None:
        return denied
    store = request.app.get("store")
    if store is None:
        return web.json_response({"status": False, "msg": "存档未就绪"}, status=503)
    players = await store.repo.players_all()
    accounts = await store.repo.accounts_all() or {}
    id2name = {int(v.get("id") or 0): u for u, v in accounts.items()}
    for p in players:
        p["account"] = id2name.get(int(p.get("account_id") or 0), "")
    return web.json_response({"status": True, "players": players,
                              "active": int(getattr(store, "player_id", 0) or 0)})


def _resolve_targets(store, target, players):
    """发信目标 → 角色 id 列表。

    mode: current(默认，当前活跃角色) / all(全服所有角色) / players(指定角色 id)
          / accounts(指定账号 id，展开成该账号下全部角色)
    """
    target = target or {}
    mode = str(target.get("mode") or "current")
    ids = []
    for x in (target.get("ids") or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if mode == "all":
        return [int(p["id"]) for p in players]
    if mode == "players":
        valid = {int(p["id"]) for p in players}
        return sorted({i for i in ids if i in valid})
    if mode == "accounts":
        want = set(ids)
        return sorted({int(p["id"]) for p in players
                       if int(p.get("account_id") or 0) in want})
    return [int(getattr(store, "player_id", 0) or 0)]


async def handle_mail_send(request):
    """发信：{title, content, awards[], target{mode,ids}, expireDays?}。"""
    denied = _admin_denied(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": False, "msg": "请求体不是合法 JSON"}, status=400)
    title = str(body.get("title") or "").strip()
    if not title:
        return web.json_response({"status": False, "msg": "标题不能为空"}, status=400)
    content = str(body.get("content") or "")
    awards = []
    for a in (body.get("awards") or []):
        if a.get("kind") == "property":
            prop = str(a.get("prop") or "")
            val = int(a.get("value") or 0)
            if prop and val > 0:
                # ★ int32 上限校验（2026-10-28）：超限邮件会让 EnterGame 构包
                #   直接 ValueError，客户端永远登录不进（实测踩坑：发 100 亿铜币）
                if val > 2147483647:
                    return web.json_response(
                        {"status": False, "msg": "数值过大（上限 2147483647），超出会让客户端无法登录"},
                        status=400)
                awards.append({"kind": "property", "prop": prop, "value": val})
        else:
            tid = int(a.get("type") or 0)
            cnt = int(a.get("count") or 0)
            if tid > 0 and cnt > 0:
                err = _validate_object_award(tid)
                if err:
                    return web.json_response({"status": False, "msg": "附件校验失败：" + err}, status=400)
                awards.append({"kind": "object", "type": tid, "count": cnt})
    store = request.app.get("store")
    if store is None:
        return web.json_response({"status": False, "msg": "存档未就绪"}, status=503)
    expire_days = int(body.get("expireDays") or mail.EXPIRE_DAYS)
    try:
        players = await store.repo.players_all()
        pids = _resolve_targets(store, body.get("target"), players)
        if not pids:
            return web.json_response(
                {"status": False, "msg": "收件人为空（没选角色 / 选的账号下没有角色）"}, status=400)
        # 邮件类型由 send_mail 按"有无附件"自动决定，页面传的 type 不再使用
        res = await mail.deliver(
            store, pids, title, content, awards, sender=mail.DEFAULT_SENDER,
            expire_days=expire_days, operator="web",
            batch_id="web%d" % int(time.time()))
        print("[mail] 群体发放 %d 个角色（内存 %d / 落库 %d，失败 %d）：%s" % (
            len(pids),
            sum(1 for x in res["ok"] if x["path"] == "memory"),
            sum(1 for x in res["ok"] if x["path"] == "db"),
            len(res["failed"]), title), flush=True)
        if res["failed"]:
            print("[mail] 失败明细: %s" % res["failed"], flush=True)
    except Exception as e:
        return web.json_response({"status": False, "msg": "发送失败: %s" % e}, status=500)
    msg = "已发送 %d 个角色（附件 %d 项）" % (len(res["ok"]), len(awards))
    if res["failed"]:
        msg += "，%d 个失败：%s" % (
            len(res["failed"]),
            "、".join("%s(%s)" % (f["player"], f["reason"]) for f in res["failed"][:3]))
    if not core.LIVE_SESSIONS:
        print("[mail] 离线角色的邮件已落库，下次登录推送", flush=True)
    return web.json_response({"status": True, "msg": msg,
                              "ok": res["ok"], "failed": res["failed"]})


async def handle_mail_sent(request):
    denied = _admin_denied(request)
    if denied is not None:
        return denied
    return web.json_response({"status": True, "items": mail.list_all()})


def build_app(store=None):
    app = web.Application()
    # 发信台要跨角色读写存档（群体发放），把 SaveStore 挂到 app 上
    app["store"] = store
    app.router.add_get("/api/getserverlist", handle_server_list)
    app.router.add_get("/api/getupdateconfig", handle_update_config)
    app.router.add_get("/api/getannounce", handle_announce)
    app.router.add_get("/api/getguestlogin", handle_guest_login)
    app.router.add_post("/api/getserverrolesbyaccount", handle_account_server_roles)
    app.router.add_get("/api/getserverrolesbyaccount", handle_account_server_roles)
    app.router.add_get("/api/gm", handle_gm)
    app.router.add_get("/mail", handle_mail_page)
    app.router.add_get("/api/mail/items", handle_mail_items)
    app.router.add_get("/api/mail/players", handle_mail_players)
    app.router.add_post("/api/mail/send", handle_mail_send)
    app.router.add_get("/api/mail/sent", handle_mail_sent)
    return app
