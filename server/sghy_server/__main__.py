"""入口：python -m sghy_server

启动顺序：存档门面（建库/迁移/接管 core.PLAYER_DATA）→ HTTP 网关 → TCP 游戏服务。
退出：KeyboardInterrupt / 异常 -> 最终 flush 落库。
日志同时写 stdout 与 logs/server.log（追加）。
"""

import asyncio
import os
import sys
import time

from .settings import FLUSH_DELAY, HTTP_HOST, HTTP_PORT, LOG_DIR


def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, "server.log")
    f = open(log_path, "a", encoding="utf-8", buffering=1)

    class Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, s):
            for st in self.streams:
                try:
                    st.write(s)
                except Exception:
                    pass

        def flush(self):
            for st in self.streams:
                try:
                    st.flush()
                except Exception:
                    pass

    sys.stdout = Tee(sys.__stdout__, f)
    sys.stderr = Tee(sys.__stderr__, f)
    return log_path


TICK_SECONDS = 10


async def _game_ticker(store):
    """后台巡检（每 10 秒）：① 月卡/周卡跨天结算 ② 活动轮次结束立刻重置并推送。

    这两件事原本都只能等玩家下一次请求才发生，导致"轮次结束后面板还显示上一轮
    数值"（用户 2026-10-27 反馈）。这里主动推进并把结果推给在线会话。
    """
    from .game import core
    from .game import month_card
    from .game.activities import build_activity_group, tick
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            _changed = month_card.settle(push=True)
            if _changed:
                for _rec in _changed:
                    core.push_buy_prop_record(_rec)
                store.mark_dirty()
        except Exception as e:
            core.flog("[month_card] 定时结算失败（%r）" % e)
        try:
            if tick():
                core.broadcast(5311, build_activity_group())
                store.mark_dirty()
                core.flog("[activity] 轮次结束 -> 已重置本轮进度并推送 5311")
        except Exception as e:
            core.flog("[activity] 轮次巡检失败（%r）" % e)


async def _amain():
    from .data.store import SaveStore
    from .game import core
    from .net.tcp_server import GameServer
    from .net.http_gateway import build_app
    from .settings import DB_PATH
    from aiohttp import web

    # ---- 存档：建库/迁移，接管 core 的 PLAYER_DATA 与 save_player_data ----
    store = SaveStore(db_path=DB_PATH, player_id=core.CHAR_ID,
                      json_path=getattr(core, "_PLAYER_DATA_PATH", None),
                      flush_delay=FLUSH_DELAY)
    data = await store.start()
    # 库里有数据 -> 用库的；库是刚迁移的 -> data 即 JSON 内容
    core.PLAYER_DATA = store.data
    core.LEVEL_RECORDS = core.PLAYER_DATA.setdefault("levels", {})
    core.save_player_data = lambda _data: store.mark_dirty()
    core.flush_now = lambda: store.flush_soon()   # 关键操作立即落库
    # VIP 进度单位迁移（2026-10-27）：旧存档 pay_dollar 按人民币累计，而
    # VIPSetConfig.Price / 客户端文案都是元宝（1 元 = 10 元宝）→ ×10 并重算 VIP。
    try:
        if core.migrate_pay_dollar_unit():
            store.mark_dirty()
            print("[pay] pay_dollar 人民币→元宝（×10），vip=%s"
                  % core.PLAYER_DATA["player"].get("vip"), flush=True)
    except Exception as e:
        print("[pay] 单位迁移跳过（%r）" % e, flush=True)
    # 武将卡/定缘卡实例化迁移（2026-10-27）：旧存档里这些卡是按 type 堆叠的，
    # 原版口径每张一个唯一 id → 启动时拆成实例（N 张=N 个卡格），再走数据自检。
    try:
        _moved, _restored, _dropped = core.migrate_card_items()
        if _moved or _restored or _dropped:
            store.mark_dirty()
        print("[item-cards] 堆叠卡→实例 %d / 礼包并回堆叠 %d / 丢弃非法 %d"
              % (_moved, _restored, _dropped), flush=True)
    except Exception as e:
        print("[item-cards] 迁移跳过（%r）" % e, flush=True)
    # 启动自检：清掉不合规数据（幽灵武将/不可拥有装备/非 ItemConfig 的"道具"）。
    # 2026-10-22 实测：签到/收集奖励曾把装备 type（33903）塞进 items，客户端
    # BagManager:addItem → getItemTypeCache 取不到配置，登录直接 LUA ERROR。
    try:
        _removed, _fixed = core._sanitize_player_data()
        if _removed or _fixed:
            store.mark_dirty()
        print("[sanitize] 清理 %d 项 / 修复 %d 项" % (_removed, _fixed), flush=True)
    except Exception as e:
        print("[sanitize] 跳过（%r）" % e, flush=True)
    # 图鉴补种：把当前武将/装备并进 collect 记录并落库（否则要吃/分解后
    # 才第一次保存，期间新号的老库存图鉴会丢）。
    try:
        core.collect_snapshot()
        store.mark_dirty()
    except Exception as e:
        print("[collect] 补种跳过（%r）" % e, flush=True)
    print("[store] 存档就绪: heroes=%d items=%d equips=%d flushes=%d"
          % (len(core.PLAYER_DATA.get("heroes", {})),
             len(core.PLAYER_DATA.get("items", {})),
             len(core.PLAYER_DATA.get("equips", {})),
             store.stats["flushes"]), flush=True)

    # ---- 账号层（2026-10-29 账号隔离）----
    # ① 旧档归属（幂等）：players.id=1001 绑到 LEGACY_OWNER_ACCOUNT
    # ② 注册表载入内存（登录路径同步查，不碰 await）
    from .game import account as account_svc
    from . import settings as _settings
    if not _settings.LEGACY_SINGLE:
        await account_svc.ensure_legacy_binding(store.repo, _settings.LEGACY_OWNER_ACCOUNT,
                                                core.CHAR_ID)
        await account_svc.load_registry(store.repo)
    else:
        print("[account] SGHY_LEGACY_SINGLE=1 -> 跳过账号层（旧行为）", flush=True)

    # ---- 配置预热（2026-10-29）----
    # 配置是**按需**加载的：某张表首次被业务用到才解析。大表解析会阻塞事件循环
    # （LevelConfig 修复前 12.7s → 表现为"打完关卡结算等十几秒"；LevelSetConfig
    # 10MB 约 0.44s → 表现为"第一次点关卡卡一下"）。启动时一次性预热，把成本
    # 挪到开服阶段，代价 <2s，收益是游戏中零首次卡顿。
    try:
        from .game import game_config
        _t0 = time.perf_counter()
        for _name in ("load_level_set_config", "load_level_config",
                      "load_item_config", "load_hero_config",
                      "load_equip_config", "load_mission_config",
                      "load_mission_main", "load_sign_award_config"):
            _fn = getattr(game_config, _name, None)
            if _fn is None:
                continue
            try:
                _fn()
            except Exception as _e:
                print("[warmup] %s 失败（%r）" % (_name, _e), flush=True)
        print("[warmup] 配置已预热 %.0f ms" % ((time.perf_counter() - _t0) * 1000),
              flush=True)
    except Exception as e:
        print("[warmup] 跳过（%r）" % e, flush=True)

    # ---- 后台巡检：月卡跨天结算 / 活动轮次结束重置 ----
    _ticker_task = asyncio.create_task(_game_ticker(store))

    # ---- HTTP 网关 ----
    runner = web.AppRunner(build_app(store))
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()
    print("[http] listening on %s:%d" % (HTTP_HOST, HTTP_PORT), flush=True)

    # ---- TCP 游戏服务 ----
    server = GameServer()
    server.store = store            # 会话层切角色 / 建号要用
    try:
        await server.serve()
    finally:
        print("[store] shutting down, final flush ...", flush=True)
        await store.stop()
        await runner.cleanup()


def main():
    log_path = _setup_logging()
    print("=" * 70)
    print("sghy-server starting at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("log -> %s" % log_path)
    print("=" * 70)
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n[server] bye")


if __name__ == "__main__":
    main()
