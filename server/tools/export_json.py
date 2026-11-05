"""把库中存档导出为 JSON（备份 / 与 mock 互通用）。

用法：python tools/export_json.py [输出路径]
默认写 data/export/save_export.json
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sghy_server.data.repo import PlayerRepository
from sghy_server.data.db import Database
from sghy_server.game import core
from sghy_server.settings import DB_PATH


async def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "export",
        "save_export.json")
    repo = PlayerRepository(Database(DB_PATH))
    await repo.db.init()
    data = await repo.export_json(core.CHAR_ID, out)
    print("exported -> %s" % out)
    print("heroes=%d items=%d equips=%d fate=%d levels=%d level_records=%d"
          % (len(data["heroes"]), len(data["items"]), len(data["equips"]),
             len(data["fate"]), len(data["levels"]),
             len(data["level_records"])))
    await repo.db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
