import logging
import os

import uvicorn

from src.proxy.config import settings

class _ColorFormatter(logging.Formatter):
    """按级别为控制台日志着色：INFO 绿 / WARNING 黄 / ERROR 红。"""

    _COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;41m",
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelno, "")
        record.levelcolor = f"{color}{record.levelname:<7}\033[0m"
        return super().format(record)


_console = logging.StreamHandler()
_console.setFormatter(
    _ColorFormatter(
        "\033[90m%(asctime)s\033[0m | %(levelcolor)s | \033[35m%(name)-10s\033[0m | %(message)s",
        datefmt="%H:%M:%S",
    )
)
# 只让 gateway / v5_quota / stats 输出 INFO，其余仅 WARNING 以上
logging.basicConfig(level=logging.WARNING, handlers=[_console])
for _name in ("gateway", "v5_quota", "stats"):
    logging.getLogger(_name).setLevel(logging.INFO)
# 彻底禁用 uvicorn 的访问日志，只保留 gateway 的业务日志
logging.getLogger("uvicorn.access").handlers = []
logging.getLogger("uvicorn.access").propagate = False

if __name__ == "__main__":
    # 在容器内默认关闭 reload；本地开发可通过 RELOAD=1 开启
    reload = os.environ.get("RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run(
        "src.proxy.app:app",
        host=settings.host,
        port=settings.port,
        reload=reload,
        log_level="warning",
    )
