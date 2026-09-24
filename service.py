"""馆际作品借展的运行入口。

提供健康检查与领域 JSON 接口；领域规则全部在 domain.py，
本模块只负责 HTTP 编解码与状态码映射。

配置 ``--state``（或环境变量 ``LOAN_STATE_FILE``）后，每次写命令成功都会
把领域快照原子落盘，进程重启时自动恢复，交接链可继续办理。
"""

import argparse
import json
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from domain import (
    ConflictError,
    DomainError,
    LoanRegistry,
    Route,
    build_routes,
    json_dumps,
)

SERVICE_ID = "museum-loan"
SERVICE_NAME = "馆际作品借展"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class ApiState:
    """进程内共享的领域记录（单实例部署足够；多实例需换持久层）。"""

    def __init__(self, state_file: str | None = None):
        self.state_file = state_file
        self.registry = LoanRegistry()
        self.routes = build_routes()
        self._persist_lock = threading.RLock()
        if state_file and os.path.exists(state_file):
            with open(state_file, "r", encoding="utf-8") as handle:
                self.registry.restore_state(json.load(handle))

    def command_lock(self):
        """命令执行与落盘共用的临界区（GET 也可重入读取）。"""
        return self._persist_lock

    def persist(self) -> None:
        """把领域快照原子写入状态文件（同目录临时文件 + 替换）。"""
        if not self.state_file:
            return
        directory = os.path.dirname(os.path.abspath(self.state_file)) or "."
        os.makedirs(directory, exist_ok=True)
        data = json_dumps(self.registry.snapshot())
        fd, tmp_path = tempfile.mkstemp(prefix=".loan-state-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
            os.replace(tmp_path, self.state_file)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


STATE = ApiState(os.environ.get("LOAN_STATE_FILE"))


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
                with STATE.command_lock():
                    payload = route.handler(STATE.registry, body, match.groupdict())
                    if method == "POST":
                        # 快照为全量状态：命令与落盘同一临界区，保证快照按命令
                        # 提交次序单调前进，不会被并发命令的旧快照覆盖。
                        try:
                            STATE.persist()
                        except OSError as error:
                            self._write_json(500, {"error": f"状态落盘失败：{error}"})
                            return
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
    global STATE
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state", default=None,
                        help="状态快照文件路径（也可用环境变量 LOAN_STATE_FILE）；"
                             "设置后写命令原子落盘，重启自动恢复")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    state_file = args.state or os.environ.get("LOAN_STATE_FILE")
    if state_file:
        STATE = ApiState(state_file)
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
