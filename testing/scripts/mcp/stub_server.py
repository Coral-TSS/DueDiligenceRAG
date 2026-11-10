"""给 Streamable HTTP 客户端脚本用的本机桩。不连接数据库，也不读取客户数据。

只替换 list_projects 需要的 projects()。监听 127.0.0.1 的临时端口，
就绪后把 URL 打到标准输出。密钥来自环境变量 DDA_MCP_API_KEY，不会打印。
"""
import os
import socket
import sys
import threading
import time

import uvicorn

from dd_agent.config import Settings
from dd_agent.mcp_server import create_mcp_app


class _Projects:
    def projects(self):
        return [{
            'id': '00000000-0000-0000-0000-000000000001',
            'name': '桩项目',
            'created_at': '2026-01-01T00:00:00Z',
        }]


def main():
    key = os.environ.get('DDA_MCP_API_KEY', '').strip()
    if not key:
        print('需要环境变量 DDA_MCP_API_KEY', file=sys.stderr)
        return 1
    app = create_mcp_app(Settings(mcp_api_key=key, mcp_host='127.0.0.1', mcp_port=8766))
    app.state.database = _Projects()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host='127.0.0.1', port=port, log_level='warning', access_log=False))

    def announce():
        deadline = time.time() + 10
        while not server.started:
            if time.time() > deadline:
                print('桩服务没有在限定时间内启动', file=sys.stderr)
                return
            time.sleep(0.02)
        print(f'http://127.0.0.1:{port}/mcp', flush=True)

    threading.Thread(target=announce, daemon=True).start()
    server.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
