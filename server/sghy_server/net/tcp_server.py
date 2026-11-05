"""asyncio TCP 游戏服务。

替代 mock 的线程-per-连接模型：单进程事件循环，无锁。
会话注册进 core.LIVE_SESSIONS，GM 广播（core.broadcast）直接复用。
"""

import asyncio

from .framing import FrameDecoder
from ..game.session import GameSession
from ..game import core
from ..settings import TCP_HOST, TCP_PORT


class GameServer:
    def __init__(self):
        self.sessions = set()
        self.store = None      # SaveStore（__main__ 注入；会话切角色/建号用）

    def log(self, msg):
        print(msg, flush=True)

    def register(self, session):
        self.sessions.add(session)
        core.LIVE_SESSIONS.add(session)
        self.log("[tcp] session up: %s (online=%d)" % (session.addr, len(self.sessions)))

    def unregister(self, session):
        self.sessions.discard(session)
        core.LIVE_SESSIONS.discard(session)
        self.log("[tcp] session down: %s (online=%d)" % (session.addr, len(self.sessions)))

    async def _handle(self, reader, writer):
        addr = writer.get_extra_info("peername")
        session = GameSession(self, reader, writer, addr)
        await session.run()

    async def serve(self):
        server = await asyncio.start_server(self._handle, TCP_HOST, TCP_PORT)
        self.log("[tcp] listening on %s:%d" % (TCP_HOST, TCP_PORT))

        async with server:
            await server.serve_forever()
