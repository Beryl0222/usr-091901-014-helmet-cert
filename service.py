"""骑行头盔认证与召回的基础服务入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from helmet_api import route
from helmet_core import HelmetService, StaticVerificationGateway

SERVICE_ID = "helmet-certification"
SERVICE_NAME = "骑行头盔认证与召回"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

# 领域服务实例:可信验证接口默认使用登记簿实现,可替换为监管接口客户端。
SERVICE = HelmetService(StaticVerificationGateway())


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID: raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查、领域契约与认证召回业务接口。"""

    def do_GET(self):
        if self.path == "/health": self._send_json(health_payload()); return
        if self.path == "/contract": self._send_json(load_contract()); return
        if self._api("GET"): return
        self.send_error(404)

    def do_POST(self):
        if self._api("POST"): return
        self.send_error(404)

    def _api(self, method):
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send_json({"error": "请求体不是合法 JSON"}, 400)
                return True
        result = route(SERVICE, method, self.path, body)
        if result is None: return False
        status, payload = result
        self._send_json(payload, status)
        return True

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *_args): return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000); parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract(); assert contract["states"] and contract["invariants"]; print("基础检查通过"); return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__": main()
