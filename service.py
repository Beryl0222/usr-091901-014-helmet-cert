"""骑行头盔认证与召回的服务入口：组合领域服务、可信验证源与 HTTP 接口。"""

import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from helmet.api import make_handler
from helmet.services import HelmetService
from helmet.store import DataStore
from helmet.verification import StaticCertificateAuthority

SERVICE_ID = "helmet-certification"
SERVICE_NAME = "骑行头盔认证与召回"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")
SEED_CERTS_PATH = Path(__file__).with_name("trusted_certificates.json")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def load_authority():
    """加载内置可信证书源（演示数据来自 trusted_certificates.json）。"""
    records = []
    if SEED_CERTS_PATH.exists():
        records = json.loads(SEED_CERTS_PATH.read_text(encoding="utf-8"))
    return StaticCertificateAuthority(records)


def build_service(authority=None):
    """组装领域服务；测试可注入自定义可信源。"""
    return HelmetService(DataStore(), authority or load_authority())


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


Handler = make_handler(
    build_service(),
    extra_gets={"/health": health_payload, "/contract": load_contract},
)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        build_service()
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
