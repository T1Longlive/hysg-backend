"""清空挑战副本的今日次数（圣兽岛 / 金银岛 / 阵营副本）。开发调试用。

★ 必须**先停服务端**再跑：服务端内存里的 PLAYER_DATA 会覆盖这里的修改。

用法：
    taskkill /F /PID <sghy-server pid>       # 或直接关掉 start_server 窗口
    cd server && python tools/reset_challenge_state.py
    python -m sghy_server                    # 再启动

存档口径：服务端读的是 **SQLite（data/sghy.db）**，不是 env/player_data.json。
以前在脚本里直接 `core.save_player_data()` 只会写 JSON（非服务端进程里那是文件写），
看起来"清了"其实没生效 —— 这个工具走 store/repo 的正式路径。
"""
import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sghy_server.settings import DB_PATH                        # noqa: E402
from sghy_server.data.store import SaveStore                    # noqa: E402
from sghy_server.game import core                               # noqa: E402  (只为取 CHAR_ID)


async def main():
    store = SaveStore(db_path=DB_PATH, player_id=core.CHAR_ID)
    data = await store.start()
    old = data.get("challenge")
    if not old:
        print("[reset] challenge 状态本来就是空的，无需清理")
    else:
        data.pop("challenge", None)
        store.mark_dirty()
        await store.flush(force=True)
        print("[reset] 已清空 challenge，原值：%s" % old)
    print("[reset] 完成。重启服务端后：圣兽按开岛日重置、金银岛 2 次、阵营回本日那一组")


if __name__ == "__main__":
    asyncio.run(main())
