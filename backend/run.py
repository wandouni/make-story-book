"""开发服务器入口。

注意 workers 必须为 1：任务表（JobManager）保存在进程内存里，
多 worker 会导致 SSE 订阅连到没有该任务的进程上。
"""

import uvicorn

from app.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True, workers=1)
