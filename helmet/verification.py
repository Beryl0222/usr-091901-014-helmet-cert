"""可信证书验证接口：平台据此取得证书状态并保存核验快照。"""

import hashlib
import json
from urllib.request import urlopen


def payload_digest(payload):
    """原始应答的防篡改摘要。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StaticCertificateAuthority:
    """内置可信源：预置证书登记信息，便于演示与测试。"""

    source = "static-authority"

    def __init__(self, records):
        self._records = {record["certificate_no"]: dict(record) for record in records}

    def fetch(self, certificate_no):
        """按证书编号取得登记状态；未登记时仅返回编号与状态。"""
        record = self._records.get(certificate_no)
        if record is None:
            return {"certificate_no": certificate_no, "status": "未登记"}
        return dict(record)

    def set_status(self, certificate_no, status):
        """演示与测试用途：模拟发证机构变更证书状态（如撤销）。"""
        if certificate_no not in self._records:
            raise KeyError(f"证书未登记: {certificate_no}")
        self._records[certificate_no]["status"] = status


class HttpCertificateAuthority:
    """通过 HTTP 访问外部可信验证接口。"""

    def __init__(self, base_url, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.source = f"http:{self.base_url}"

    def fetch(self, certificate_no):
        with urlopen(
            f"{self.base_url}/certificates/{certificate_no}", timeout=self.timeout
        ) as response:
            return json.load(response)
