"""游戏会话：一个 TCP 连接对应一个 GameSession。

core.dispatch(session, opcode, pb) 需要 session 提供：
  · send(opcode, body) —— 同步写一帧响应（asyncio write 本身是同步缓冲，安全）
  · log(*args)         —— 日志
本类同时实现登录首包（裸 Base64 JSON）解析与**账号鉴权**（2026-10-29 账号隔离）。

★ 账号隔离的会话级拦截：
  · 登录首包：解析 Account/Password/Device/IsVisitor → account.login_or_register
    （内存注册表，同步）→ 失败回 5002 并断开；成功拉取该账号角色列表回 100 LoginOK。
  · 10001 SelectCharacter：校验角色归属（players.account_id）→ store.switch_player
    切档 → core.apply_player_data 重绑全局 → 再走 core.dispatch 原有结算/推信/EnterGame。
  · 10000 CreateCharacter：建号（重名/上限校验）→ 回 101 → 客户端自己发 10001。
"""

import asyncio
import base64
import json
import socket as _socket
import traceback

from ..net.framing import FrameDecoder, encode_frame
from .. import settings as _settings
from . import core
from . import account as account_svc
from . import builders_pb

# 应急回滚开关（SGHY_LEGACY_SINGLE=1）：登录不鉴权、恒回旧行为
_USE_ACCOUNTS = not _settings.LEGACY_SINGLE


class GameSession:
    def __init__(self, server, reader, writer, addr):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.addr = addr
        self.buf = bytearray()
        self.logged_in = False
        self.account = None            # 登录 JSON 里的 Account 原文
        self.account_name = None       # 鉴权后的实际账号名（游客是 guest_<hash8>）
        self.account_id = None         # 鉴权后的 accounts.id
        self.character_id = None       # 当前会话选中的角色 id
        # Login JSON 的 Reconnect 计数：0=客户端新开（重启），>0=同一次运行内断线重连
        self.reconnect = 0
        self._pending_login = None     # 解析出的登录 JSON（待异步处理）
        self._decoder = FrameDecoder()
        self._enable_keepalive()

    def _enable_keepalive(self):
        """★ 2026-11-04：战斗期间客户端与服务端可以 4~5 分钟没有任何收发，
        家宽/模拟器虚拟 NAT 会在这段时间把映射静默回收 —— 客户端打出结果发
        LevelEnd(5031) 时包已经出不去，服务端收不到、客户端等 20 秒后弹
        「网络连接已关闭」，表现就是「打完敌人不弹结算」。
        开 TCP keepalive（60s 起探测、20s 间隔）让内核定期发空包，保住 NAT 映射，
        同时半开连接也能在 ~2 分钟内被识别并回收。"""
        try:
            sock = self.writer.get_extra_info("socket")
            if sock is None:
                return
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            for name, val in (("TCP_KEEPIDLE", 60),
                              ("TCP_KEEPINTVL", 20),
                              ("TCP_KEEPCNT", 3)):
                opt = getattr(_socket, name, None)
                if opt is not None:
                    sock.setsockopt(_socket.IPPROTO_TCP, opt, val)
        except (OSError, AttributeError):
            pass

    # ---- core.dispatch 需要的接口 ----
    def send(self, opcode: int, body: bytes):
        self.writer.write(encode_frame(opcode, body))

    def log(self, *args):
        import time as _t
        tag = "%s:%d" % self.addr
        msg = "[tcp %s] %s" % (tag, " ".join(str(a) for a in args))
        self.server.log("[%s] %s" % (_t.strftime("%H:%M:%S"), msg))

    # ---- 登录首包（只解析，鉴权在 _handle_login 异步做） ----
    def try_login_packet(self) -> bool:
        """裸 Base64(JSON + \\n)，无长度头。解析成功返回 True。"""
        txt = bytes(self.buf).strip()
        if not txt:
            return False
        try:
            raw = base64.b64decode(txt, validate=False)
            req = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return False
        if not isinstance(req, dict) or req.get("Command") != "Login":
            return False
        self.buf = bytearray()
        self._pending_login = req
        return True

    async def _handle_login(self):
        req = self._pending_login
        self._pending_login = None
        self.account = req.get("Account")
        try:
            self.reconnect = int(req.get("Reconnect") or 0)
        except (TypeError, ValueError):
            self.reconnect = 0
        core.on_login_packet(self.reconnect)
        self.log("LOGIN request:", json.dumps(req, ensure_ascii=False)[:400],
                 "reconnect=%d" % self.reconnect)

        if not _USE_ACCOUNTS:
            self.logged_in = True
            self.send(core.OP_LOGIN_OK, core.build_login_ok())   # 旧行为单角色
            return

        is_visitor = str(req.get("IsVisitor", "")).strip().lower() in ("1", "true")
        acct, err = account_svc.login_or_register(
            req.get("Account"), req.get("Password") or "",
            is_visitor, req.get("Device") or "")
        if err:
            self.log("LOGIN 拒绝: %s (account=%r)" % (err, self.account))
            self.send(5002, core.f_int(1, 0) + core.f_str(2, err))
            await asyncio.sleep(0.2)     # 给 write 缓冲落网的时间
            try:
                self.writer.close()
            except Exception:
                pass
            return

        self.account_id = acct["id"]
        self.account_name = acct["username"]
        account_svc.touch_login(acct["id"])
        self.logged_in = True
        characters = await self._character_list()
        self.log("LOGIN ok: account=%s (id=%s) characters=%d"
                 % (acct["username"], acct["id"], len(characters or [])))
        self.send(core.OP_LOGIN_OK, core.build_login_ok(characters))

    async def _character_list(self) -> list:
        """该账号在库中的角色列表（LoginCharacterData 数组的输入）。"""
        store = getattr(self.server, "store", None)
        if store is None or self.account_id is None:
            return []                    # 旧模式（无 store）不会走到这，兜底空表
        out = []
        for r in await store.repo.players_brief_by_account(self.account_id):
            try:
                data = await store.repo.load(r["id"])
            except Exception as e:
                self.log("角色 %s 数据载入失败: %r" % (r["id"], e))
                continue
            if data is None:
                continue
            out.append(core.compute_character_brief(data, r["id"]))
        return out

    # ---- 10000 CreateCharacter ----
    async def _handle_create_character(self, pb):
        store = getattr(self.server, "store", None)
        if store is None or self.account_id is None or not _USE_ACCOUNTS:
            self.send(5002, core.f_int(1, 0) + core.f_str(2, "账号未就绪，请重新登录"))
            return
        nick = (core._read_string_field(pb, 1) or "").strip()
        gender = int(core._read_varint_field(pb, 2) or 1)
        ctype = int(core._read_varint_field(pb, 3) or 1)
        self.log("CreateCharacter(name=%r gender=%s type=%s)" % (nick, gender, ctype))

        if not nick or len(nick) > 7:
            self.send(5002, core.f_int(1, 0) + core.f_str(2, "角色名需为 1~7 个字"))
            return
        if core._name_taken(nick):
            self.send(5002, core.f_int(1, 0) + core.f_str(2, "该名字已被使用，换一个吧"))
            return
        mine = await store.repo.players_brief_by_account(self.account_id)
        if len(mine) >= account_svc.MAX_CHARS_PER_ACCOUNT:
            self.send(5002, core.f_int(1, 0) + core.f_str(2, "角色数量已达上限（%d 个）"
                                                       % account_svc.MAX_CHARS_PER_ACCOUNT))
            return

        pid = await store.repo.next_player_id()
        data = core.build_new_player_data(nick, gender, ctype)
        await store.create_player(pid, self.account_id, data)
        self.log("  -> 建号成功 characterId=%s" % pid)
        # 客户端 onCreateCharacterOK 只消费 id，随后自己发 10001 SelectCharacter
        self.send(core.OP_CREATE_CHARACTER_OK,
                  builders_pb.build_create_character_ok(
                      {"id": pid, "name": nick, "gender": gender,
                       "type": ctype, "lv": int(data["player"].get("lv") or 1),
                       "exp": int(data["player"].get("exp") or 0)}))

    # ---- 10001 SelectCharacter（归属校验 + 切档） ----
    async def _handle_select_character(self, pb):
        store = getattr(self.server, "store", None)
        if store is None or self.account_id is None or not _USE_ACCOUNTS:
            core.dispatch(self, core.OP_SELECT_CHARACTER, pb)   # 旧模式兜底
            return
        cid = int(core.req_id(None, pb, core._read_varint_field) or 0)
        owner = await store.repo.player_account_id(cid)
        if owner != self.account_id:
            self.log("SelectCharacter 拒绝: 角色 %s 不属于账号 %s (owner=%s)"
                     % (cid, self.account_id, owner))
            self.send(5002, core.f_int(1, 0) + core.f_str(2, "角色不存在或不属于当前账号"))
            return
        if cid != core.CHAR_ID:
            data = await store.switch_player(cid)
            core.apply_player_data(data, cid)
            self._reset_session_caches()
            self.log("SelectCharacter -> 已切换到角色 %s" % cid)
        self.character_id = cid
        core.dispatch(self, core.OP_SELECT_CHARACTER, pb)       # 原有结算/推信/EnterGame

    def _reset_session_caches(self):
        """切角色后必须清掉的会话级/进程级缓存。"""
        self._achieve_pushed = {}
        self._periodic_sent = {}

    # ---- 8001 DeleteAccount（设置-注销账号） ----
    async def _handle_delete_account(self):
        """删除当前账号及名下全部角色/存档，回 StringMessage{msg="ok"}。

        客户端收到 ok 后退回登录界面；账号名之后可重新注册（登录即注册）。
        """
        store = getattr(self.server, "store", None)
        name = self.account_name or self.account or ""
        if (not _USE_ACCOUNTS or self.account_id is None
                or store is None or not name):
            self.log("DeleteAccount 拒绝：会话未登录或无 store")
            self.send(core.OP_DELETE_ACCOUNT, core.f_str(1, "当前未登录，无法注销"))
            return
        aid = self.account_id
        self.log("DeleteAccount 请求: account=%s (id=%s)" % (name, aid))
        try:
            await store.discard_current()          # 先停落库，防已删角色被写回
            n = await store.repo.account_delete(aid)
        except Exception as e:
            self.log("DeleteAccount 失败: %r" % (e,))
            self.send(core.OP_DELETE_ACCOUNT, core.f_str(1, "删除失败，请稍后重试"))
            return
        account_svc.forget_account(name)
        self.account_id = None
        self.character_id = None
        self.log("DeleteAccount 完成: account=%s，删除角色 %d 个" % (name, n))
        self.send(core.OP_DELETE_ACCOUNT, core.f_str(1, "ok"))

    # ---- 主循环 ----
    async def run(self):
        self.server.register(self)
        try:
            while True:
                chunk = await self.reader.read(8192)
                if not chunk:
                    self.log("client closed (EOF)")
                    break
                self.buf += chunk
                if not self.logged_in and self.try_login_packet():
                    await self._handle_login()
                    continue
                await self._pump()
                # ★ drain 加 5s 超时：EnterGame 大载荷（~200KB，45武将+百件装备）
                #   在 Windows Proactor 传输上 drain() 挂起后可能不恢复（实测
                #   会话卡死、后续请求全部无人读）。drain 只是背压手段，超时
                #   继续跑不影响正确性。
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            self.log("EXCEPTION in session:")
            traceback.print_exc()
        finally:
            self.server.unregister(self)
            try:
                self.writer.close()
            except Exception:
                pass

    async def _pump(self):
        if self.buf:
            self._decoder.feed(bytes(self.buf))
            self.buf = bytearray()
        while True:
            msg = self._decoder.next_message()
            if msg is None:
                break
            opcode, proto_index, pb = msg
            if opcode == "BAD_MAGIC":
                self.log("!! bad magic, dropped buffer")
                continue
            self.log("<- opCode=%d protoIndex=%d pb=%d bytes" % (opcode, proto_index, len(pb)))
            try:
                if opcode == core.OP_CREATE_CHARACTER:
                    await self._handle_create_character(pb)
                elif opcode == core.OP_SELECT_CHARACTER:
                    await self._handle_select_character(pb)
                elif opcode == core.OP_DELETE_ACCOUNT:
                    await self._handle_delete_account()
                else:
                    core.dispatch(self, opcode, pb)
            except Exception:
                self.log("EXCEPTION in dispatch opCode=%d:" % opcode)
                traceback.print_exc()
                # ★ 必须回一个 5002，否则客户端在等 ack，会一直转圈到超时
                #   （表现：点了没反应 / 输入被锁）。错误码 0 + 可读文案。
                try:
                    self.send(5002, core.f_int(1, 0) + core.f_str(2, "服务器处理异常，请重试"))
                except Exception:
                    pass
