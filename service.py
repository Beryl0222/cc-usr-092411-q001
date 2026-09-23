"""馆际作品借展的运行入口。

提供健康检查与领域 JSON 接口；领域规则全部在 domain.py，
本模块只负责 HTTP 编解码与状态码映射。
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from domain import (
    ConflictError,
    DomainError,
    LoanRegistry,
    Route,
    build_routes,
)

SERVICE_ID = "museum-loan"
SERVICE_NAME = "馆际作品借展"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class ApiState:
    """进程内共享的领域记录（单实例部署足够；多实例需换持久层）。"""

    def __init__(self):
        self.registry = LoanRegistry()
        self.routes = build_routes()


STATE = ApiState()


class Handler(BaseHTTPRequestHandler):
    """健康检查与借展领域接口，供本地联调和运维巡检使用。"""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        if method == "GET" and self.path == "/health":
            self._write_json(200, health_payload())
            return

        body = {}
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._write_json(400, {"error": "请求体须为 UTF-8 JSON"})
                return
            if not isinstance(body, dict):
                self._write_json(400, {"error": "请求体须为 JSON 对象"})
                return

        for route in STATE.routes:  # type: Route
            if route.method != method:
                continue
            match = re.match(route.pattern + r"$", self.path)
            if not match:
                continue
            try:
                payload = route.handler(STATE.registry, body, match.groupdict())
            except ConflictError as error:
                self._write_json(409, {"error": str(error)})
            except DomainError as error:
                self._write_json(400, {"error": str(error)})
            else:
                self._write_json(200, payload)
            return

        self.send_error(404)

    def _write_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        LoanRegistry().register_work(
            title="自检样例", kind="独立作品", owner_org="自检馆"
        )
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
