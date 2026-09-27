"""HTTP 接口测试：完整业务链路、幂等键与错误映射。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from helmet.api import make_handler
from helmet.services import HelmetService
from helmet.store import DataStore
from helmet.verification import StaticCertificateAuthority

CERTS = [
    {"certificate_no": "3C-A", "status": "有效",
     "model_name": "A1", "holder": "甲厂"},
]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = HelmetService(DataStore(), StaticCertificateAuthority(CERTS))
        handler = make_handler(cls.service)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(f"{self.base}{path}", data=data, method=method,
                          headers={"Content-Type": "application/json",
                                   **(headers or {})})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.loads(error.read() or b"{}")
            error.close()
            return error.code, payload

    def test_full_chain_over_http(self):
        status, producer = self.call("POST", "/producers", {"name": "甲厂"})
        self.assertEqual(status, 201)
        pid = producer["producer_id"]

        status, model = self.call("POST", "/models", {
            "producer_id": pid, "model_name": "A1",
            "material_structure": "EPS+PC", "factory": "一号工厂",
            "certificate_no": "3C-A", "applicable_vehicles": ["电动自行车"],
            "test_reports": ["r1.pdf"],
        })
        self.assertEqual(status, 201)
        self.assertEqual(model["state"], "待核验")
        mid = model["model_id"]

        status, result = self.call("POST", f"/models/{mid}/verify")
        self.assertEqual(status, 200)
        self.assertTrue(result["snapshot"]["matched"])
        self.assertEqual(result["model_state"], "允许销售")

        status, listing = self.call("POST", "/listings", {
            "store_id": "store-1", "model_id": mid,
            "title": "折叠头盔", "claimed_vehicles": ["电动自行车"],
        })
        self.assertEqual(status, 201)
        self.assertEqual(listing["status"], "在售")
        lid = listing["listing_id"]

        # 页面变更触发重匹配：宣称超范围车辆 → 下架
        status, updated = self.call("PUT", f"/listings/{lid}",
                                    {"claimed_vehicles": ["摩托车"]})
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "下架")
        self.call("PUT", f"/listings/{lid}",
                  {"claimed_vehicles": ["电动自行车"]})

        status, batch = self.call("POST", f"/models/{mid}/batches",
                                  {"produced_at": "2026-08"})
        bid = batch["batch_id"]
        status, _ = self.call("POST", f"/batches/{bid}/serials", {
            "serial_numbers": ["SN-1", "SN-2"], "store_id": "store-1"})
        self.assertEqual(status, 201)

        status, order = self.call("POST", "/orders", {
            "order_id": "ORD-1", "store_id": "store-1", "listing_id": lid,
            "consumer_id": "c-1", "serial_numbers": ["SN-1"],
            "unit_price": 99.0})
        self.assertEqual(status, 201)
        self.call("POST", "/orders/ORD-1/deliver")

        # 抽检不合格 → 停售调查 → 升级召回
        status, inv = self.call("POST", "/investigations", {
            "model_id": mid, "trigger": "抽检不合格",
            "detail": "冲击吸收不合格", "sample_id": "SMP-1"})
        self.assertEqual(status, 201)
        iid = inv["investigation"]["investigation_id"]
        status, recall = self.call("POST",
                                   f"/investigations/{iid}/close",
                                   {"outcome": "确认缺陷", "recall": True,
                                    "batch_ids": [bid], "reason": "批次缺陷"})
        self.assertEqual(status, 200)
        rid = recall["recall_id"]
        self.assertIn("SN-1", recall["scoped_serials"])

        # 退货：生成赔付；重复提交同一序列返回同一赔付，不多赔
        status, ret = self.call("POST", f"/recalls/{rid}/returns",
                                {"serial_no": "SN-1"})
        self.assertEqual(status, 200)
        comp = ret["compensation"]
        self.assertEqual(comp["amount"], 99.0)
        status, ret2 = self.call("POST", f"/recalls/{rid}/returns",
                                 {"serial_no": "SN-1"})
        self.assertEqual(ret2["compensation"]["entry_id"], comp["entry_id"])

        # 在库序列销毁；全部处置后结案退市
        self.call("POST", f"/recalls/{rid}/destructions",
                  {"serial_no": "SN-2"})
        status, done = self.call("POST", f"/recalls/{rid}/complete")
        self.assertEqual(done["status"], "已完成")

        # 消费者扫码只看到必要信息
        status, view = self.call("GET", "/scan/SN-1")
        self.assertEqual(status, 200)
        self.assertEqual(set(view.keys()), {
            "serial_no", "authenticity", "applicable_vehicles",
            "disposition_entry"})
        self.assertIn("已完成", view["disposition_entry"]["status"])

        # 监管视图
        status, reuse = self.call("GET", "/regulator/cert-reuse")
        self.assertEqual(status, 200)
        self.assertEqual(reuse["certificates"][0]["certificate_no"], "3C-A")
        status, pending = self.call("GET", "/regulator/recall-unrecovered")
        self.assertEqual(pending["recalls"][0]["pending_total"], 0)

    def test_idempotency_key_replays_same_response(self):
        status, producer = self.call("POST", "/producers", {"name": "甲厂"})
        body = {"producer_id": producer["producer_id"], "model_name": "A1",
                "material_structure": "EPS", "factory": "一厂",
                "certificate_no": "3C-A", "applicable_vehicles": ["自行车"]}
        headers = {"Idempotency-Key": "submit-model-1"}
        before = len(self.service.store.models)
        _, first = self.call("POST", "/models", body, headers)
        _, second = self.call("POST", "/models", body, headers)
        self.assertEqual(first["model_id"], second["model_id"])
        self.assertEqual(len(self.service.store.models), before + 1)

    def test_duplicate_transfer_and_callback_are_idempotent(self):
        _, producer = self.call("POST", "/producers", {"name": "甲厂"})
        _, model = self.call("POST", "/models", {
            "producer_id": producer["producer_id"], "model_name": "A1",
            "material_structure": "EPS", "factory": "一厂",
            "certificate_no": "3C-A", "applicable_vehicles": ["自行车"]})
        self.call("POST", f"/models/{model['model_id']}/verify")
        _, batch = self.call("POST", f"/models/{model['model_id']}/batches", {})
        self.call("POST", f"/batches/{batch['batch_id']}/serials",
                  {"serial_numbers": ["SN-T"], "store_id": "store-1"})
        transfer = {"transfer_id": "TR-1", "serial_no": "SN-T",
                    "to_store": "store-2"}
        before = len(self.service.store.transfers)
        _, first = self.call("POST", "/transfers", transfer)
        _, second = self.call("POST", "/transfers", transfer)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.service.store.transfers), before + 1)

        callback = {"callback_id": "CB-1", "event": "certificate_revoked",
                    "payload": {"certificate_no": "3C-A"}}
        _, first = self.call("POST", "/callbacks", callback)
        _, second = self.call("POST", "/callbacks", callback)
        self.assertTrue(first["processed"])
        self.assertFalse(second["processed"])

    def test_error_mapping(self):
        status, payload = self.call("GET", "/models/no-such")
        self.assertEqual(status, 404)
        self.assertEqual(payload["kind"], "not_found")
        status, _ = self.call("POST", "/models", {
            "producer_id": "nobody", "model_name": "X",
            "material_structure": "x", "factory": "x",
            "certificate_no": "3C-A", "applicable_vehicles": ["自行车"]})
        self.assertEqual(status, 404)
        status, _ = self.call("POST", "/nope", {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
