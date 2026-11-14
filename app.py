# -*- coding: utf-8 -*-
"""爱管理 v3.0（凤凰）入口文件。

运行方式：
    python app.py
    浏览器访问 http://127.0.0.1:5000

说明：Sprint 3 的看板使用 SSE 长连接推送，必须开启 threaded=True，
否则一个浏览器占用推送连接后，其他请求会被阻塞（单线程开发服务器）。
生产部署应使用 gunicorn/uWSGI 的多 worker + 反向代理，见 README"部署"一节。
"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("AIMANAGE_HOST", "127.0.0.1"),
        port=int(os.environ.get("AIMANAGE_PORT", "5000")),
        debug=True,
        threaded=True,
    )
