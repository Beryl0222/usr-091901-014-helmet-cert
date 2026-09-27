"""认证与召回领域核心测试,围绕契约三条不变量与关键流程。"""

import json
import threading
import unittest
from urllib.request import Request, urlopen

import service as service_module
from helmet_core import (
    DomainError,
    HelmetService,
    StaticVerificationGateway,
    STATE_DELISTED,
    STATE_ON_SALE,
    STATE_PENDING,
    STATE_RECALLING,
    STATE_RESTORED,
    STATE_SUSPENDED,
    TRIG_CERT_REVOKED,
    TRIG_FALSE_AD,
    TRIG_SAMPLE_FAIL,
)


def build_service():
    """搭好一个证书有效、型号已核验、批次已入库的服务。"""
    gateway = StaticVerificationGateway()
    gateway.register("3C-001", holder="CC-FOLD", models=["FoldA"], status="有效")
    svc = HelmetService(gateway)
    producer = svc.register_producer("折叠科技", "CC-FOLD")
    model = svc.submit_model(producer["producer_id"], "FoldA", "EPS+纸蜂窝",
                             "一体成型", "东莞一厂", ["自行车", "电动自行车"],
                             "3C-001", "RPT-2026-01")
    svc.verify_model(model["model_id"])
    return svc, producer, model


class VerificationTest(unittest.TestCase):
    """不变量一:证书必须匹配实际型号和生产主体。"""

    def test_verify_success_stores_snapshot(self):
        svc, _, model = build_service()
        snap = svc._latest_snapshot(model["model_id"])
        self.assertEqual(snap.result, "匹配")
        self.assertEqual(snap.holder, "CC-FOLD")
        self.assertEqual(snap.cert_status, "有效")
        self.assertEqual(svc.models[model["model_id"]].state, STATE_ON_SALE)

    def test_holder_mismatch_rejected(self):
        gateway = StaticVerificationGateway()
        gateway.register("3C-001", holder="CC-OTHER", models=["FoldA"])
        svc = HelmetService(gateway)
        producer = svc.register_producer("折叠科技", "CC-FOLD")
        model = svc.submit_model(producer["producer_id"], "FoldA", "EPS", "一体",
                                 "一厂", ["自行车"], "3C-001", "RPT-1")
        snap = svc.verify_model(model["model_id"])
        self.assertEqual(snap["result"], "不匹配")
        self.assertIn("证书持证主体与生产主体不一致", snap["mismatches"])
        self.assertEqual(svc.models[model["model_id"]].state, STATE_PENDING)

    def test_model_not_covered_rejected(self):
        gateway = StaticVerificationGateway()
        gateway.register("3C-001", holder="CC-FOLD", models=["FoldA"])
        svc = HelmetService(gateway)
        producer = svc.register_producer("折叠科技", "CC-FOLD")
        model = svc.submit_model(producer["producer_id"], "FoldB", "EPS", "一体",
                                 "一厂", ["自行车"], "3C-001", "RPT-1")
        snap = svc.verify_model(model["model_id"])
        self.assertEqual(snap["result"], "不匹配")
        self.assertIn("型号不在证书覆盖范围内", snap["mismatches"])

    def test_unknown_cert_rejected(self):
        svc = HelmetService(StaticVerificationGateway())
        producer = svc.register_producer("折叠科技", "CC-FOLD")
        model = svc.submit_model(producer["producer_id"], "FoldA", "EPS", "一体",
                                 "一厂", ["自行车"], "3C-404", "RPT-1")
        snap = svc.verify_model(model["model_id"])
        self.assertEqual(snap["result"], "证书不存在")


class ListingMatchTest(unittest.TestCase):
    """商品页面每次变更都重新匹配实际型号与适用车辆。"""

    def test_listing_rematch_on_every_change(self):
        svc, _, model = build_service()
        listing = svc.create_listing("shop-1", model["model_id"], "折叠头盔",
                                     ["自行车"])
        self.assertEqual(listing["status"], "正常")
        # 换标题并夸大适用车辆 -> 重新匹配后拦截
        listing = svc.revise_listing(listing["listing_id"], title="全车型通用",
                                     declared_vehicles=["自行车", "摩托车"])
        self.assertEqual(listing["status"], "已拦截")
        self.assertEqual(listing["matches"][-1]["decision"], "禁止销售")
        self.assertIn("宣称适用车辆超出型号范围", listing["matches"][-1]["reasons"])
        # 改回合法范围 -> 恢复
        listing = svc.revise_listing(listing["listing_id"],
                                     declared_vehicles=["自行车"])
        self.assertEqual(listing["status"], "正常")
        self.assertEqual(len(listing["matches"]), 3)

    def test_listing_blocked_when_model_suspended(self):
        svc, _, model = build_service()
        svc.open_investigation(TRIG_SAMPLE_FAIL, model_id=model["model_id"],
                             detail="抽检冲击吸收不合格")
        listing = svc.create_listing("shop-1", model["model_id"], "折叠头盔",
                                     ["自行车"])
        self.assertEqual(listing["status"], "已拦截")
        self.assertIn(f"型号状态为{STATE_SUSPENDED}",
                      listing["matches"][-1]["reasons"])


class InvestigationAndRecallTest(unittest.TestCase):
    """不变量二:停售调查与正式召回分别记录。"""

    def _recall_setup(self):
        svc, _, model = build_service()
        listing = svc.create_listing("shop-1", model["model_id"], "折叠头盔",
                                     ["自行车"])
        svc.register_batch(model["model_id"], "东莞一厂", ["SN-1", "SN-2", "SN-3"])
        batch_id = next(iter(svc.batches))
        order = svc.create_order(listing["listing_id"], "SN-1", "买家甲")
        svc.deliver_order(order["order_id"])          # SN-1 已售
        svc.create_order(listing["listing_id"], "SN-2", "买家乙")  # SN-2 在途
        return svc, model, batch_id

    def test_investigation_and_recall_recorded_separately(self):
        svc, model, batch_id = self._recall_setup()
        inv = svc.open_investigation(TRIG_FALSE_AD, model_id=model["model_id"],
                                     detail="宣称摩托车防护等级")
        self.assertEqual(svc.models[model["model_id"]].state, STATE_SUSPENDED)
        closed = svc.close_investigation(inv["inv_id"], "转正式召回",
                                         batch_ids=[batch_id], reason="宣传不实")
        recall = svc.recalls[closed["recall_id"]]
        # 两类记录各自独立,互不混用
        self.assertEqual(svc.investigations[inv["inv_id"]].status, "已结案")
        self.assertEqual(svc.investigations[inv["inv_id"]].outcome, "转正式召回")
        self.assertEqual(recall.status, "召回中")
        event_types = [e["type"] for e in svc.events]
        self.assertIn("investigation_opened", event_types)
        self.assertIn("recall_opened", event_types)

    def test_recall_scope_covers_sold_in_transit_and_stock(self):
        svc, model, batch_id = self._recall_setup()
        recall = svc.open_recall(model["model_id"], [batch_id], "抽检不合格")
        self.assertEqual(recall["scope_size"], 3)
        scope = svc.recalls[recall["recall_id"]].scope
        self.assertEqual(scope["SN-1"]["status_at_open"], "已售")
        self.assertEqual(scope["SN-2"]["status_at_open"], "在途")
        self.assertTrue(scope["SN-2"]["order_id"])  # 在途订单被圈定
        self.assertEqual(scope["SN-3"]["status_at_open"], "在库")
        self.assertEqual(svc.models[model["model_id"]].state, STATE_RECALLING)

    def test_dispositions_traced_and_compensated_once(self):
        svc, model, batch_id = self._recall_setup()
        recall = svc.open_recall(model["model_id"], [batch_id], "抽检不合格")
        rid = recall["recall_id"]
        result = svc.record_disposition(rid, "SN-1", "退货", "shop-1")
        self.assertEqual(result["compensation"]["kind"], "退款")
        self.assertEqual(result["compensation"]["payee"], "买家甲")
        # 同一序列重复处置被拒绝,不会多赔
        with self.assertRaises(DomainError):
            svc.record_disposition(rid, "SN-1", "退货", "shop-1")
        self.assertEqual(len(svc.compensations), 1)
        # 范围外序列不能错算
        with self.assertRaises(DomainError):
            svc.record_disposition(rid, "SN-404", "退货", "shop-1")
        svc.record_disposition(rid, "SN-2", "换货", "shop-1")
        svc.record_disposition(rid, "SN-3", "销毁", "shop-1")
        view = svc._recall_view(svc.recalls[rid])
        self.assertEqual(view["status"], "已完成")
        self.assertEqual(view["dispositions_by_type"],
                         {"退货": 1, "换货": 1, "销毁": 1})
        # 销毁不再产生赔付
        self.assertEqual(len(svc.compensations), 2)
        self.assertEqual(svc.models[model["model_id"]].state, STATE_DELISTED)

    def test_reinspect_restore_returns_batch_to_sale(self):
        svc, model, batch_id = self._recall_setup()
        recall = svc.open_recall(model["model_id"], [batch_id], "疑似批次缺陷")
        rid = recall["recall_id"]
        svc.record_disposition(rid, "SN-1", "退货", "shop-1")
        result = svc.reinspect_restore(rid, batch_id, "复检报告 RPT-RE-9 合格")
        self.assertEqual(sorted(result["restored"]), ["SN-2", "SN-3"])
        self.assertEqual(svc.serials["SN-2"].status, "在库")
        self.assertEqual(svc.models[model["model_id"]].state, STATE_RESTORED)
        types = [d["type"] for d in svc.disposition_log]
        self.assertIn("复检恢复", types)  # 复检恢复单独留痕

    def test_cert_revocation_triggers_investigation(self):
        svc, model, _ = self._recall_setup()
        svc.gateway.revoke("3C-001")
        inv = svc.open_investigation(TRIG_CERT_REVOKED, model_id=model["model_id"],
                                     detail="可信接口回告证书撤销")
        self.assertEqual(svc.models[model["model_id"]].state, STATE_SUSPENDED)
        svc.close_investigation(inv["inv_id"], "澄清恢复")
        self.assertEqual(svc.models[model["model_id"]].state, STATE_RESTORED)


class IdempotencyAndTransferTest(unittest.TestCase):
    """不变量三:跨店流转不得打断问题批次追踪;重复回调不漏算不多赔。"""

    def _recall_with_transfer(self):
        svc, _, model = build_service()
        listing = svc.create_listing("shop-1", model["model_id"], "折叠头盔",
                                     ["自行车"])
        svc.register_batch(model["model_id"], "东莞一厂", ["SN-1"])
        batch_id = next(iter(svc.batches))
        order = svc.create_order(listing["listing_id"], "SN-1", "买家甲")
        # 重复投递的签收回调:只生效一次
        payload = {"order_id": order["order_id"]}
        first = svc.handle_callback("cb-1", "order_delivered", payload)
        second = svc.handle_callback("cb-1", "order_delivered", payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(svc.orders[order["order_id"]].state, "已签收")
        recall = svc.open_recall(model["model_id"], [batch_id], "抽检不合格")
        return svc, recall, batch_id

    def test_duplicate_callback_not_double_counted(self):
        svc, recall, _ = self._recall_with_transfer()
        rid = recall["recall_id"]
        svc.record_disposition(rid, "SN-1", "退货", "shop-1")
        # 平台以不同 callback_id 重发同一调拨,transfer_id 兜底幂等
        t1 = svc.handle_callback("cb-2", "transfer",
                                 {"transfer_id": "tr-1", "serial_no": "SN-1",
                                  "to_shop": "shop-2"})
        t2 = svc.handle_callback("cb-3", "transfer",
                                 {"transfer_id": "tr-1", "serial_no": "SN-1",
                                  "to_shop": "shop-2"})
        self.assertEqual(t1["result"]["transfer_id"], t2["result"]["transfer_id"])
        self.assertEqual(len(svc.transfers), 1)
        self.assertEqual(len(svc.compensations), 1)  # 不多赔

    def test_cross_store_transfer_keeps_batch_trace(self):
        svc, recall, batch_id = self._recall_with_transfer()
        rid = recall["recall_id"]
        svc.transfer_serial("tr-9", "SN-1", "shop-2")
        svc.transfer_serial("tr-10", "SN-1", "shop-3")
        outstanding = svc.recall_outstanding(rid)
        self.assertEqual(outstanding["count"], 1)
        entry = outstanding["outstanding"][0]
        self.assertEqual(entry["current_shop"], "shop-3")  # 去向追到当前店铺
        self.assertEqual(entry["batch_id"], batch_id)      # 批次血统未断
        self.assertEqual(len(entry["transfers"]), 2)
        # 调拨后处置仍只计一次赔付
        svc.record_disposition(rid, "SN-1", "退货", "shop-3")
        self.assertEqual(len(svc.compensations), 1)


class ConsumerAndRegulatorTest(unittest.TestCase):
    """消费者只看必要信息;监管可追复用、流转、送达与去向。"""

    def _recalled_service(self):
        svc, producer, model = build_service()
        listing = svc.create_listing("shop-1", model["model_id"], "折叠头盔",
                                     ["自行车"])
        svc.register_batch(model["model_id"], "东莞一厂", ["SN-1", "SN-2"])
        batch_id = next(iter(svc.batches))
        order = svc.create_order(listing["listing_id"], "SN-1", "买家甲")
        svc.deliver_order(order["order_id"])
        recall = svc.open_recall(model["model_id"], [batch_id], "抽检不合格")
        return svc, producer, model, recall

    def test_consumer_scan_shows_only_necessary(self):
        svc, _, _, recall = self._recalled_service()
        view = svc.consumer_scan("SN-1")
        self.assertTrue(view["authentic"])
        self.assertEqual(view["model"], "FoldA")
        self.assertEqual(view["applicable_vehicles"], ["自行车", "电动自行车"])
        self.assertEqual(view["recall_status"], "召回中")
        self.assertTrue(view["disposal_entry"])
        self.assertNotIn("factory", view)  # 不暴露工厂、证书等内部信息
        self.assertNotIn("cert_number", view)
        # 未知序列:只告知不真
        self.assertEqual(svc.consumer_scan("SN-X"), {"serial_no": "SN-X",
                                                     "authentic": False})

    def test_regulator_traces_cert_reuse_across_shops(self):
        svc, producer, model, _ = self._recalled_service()
        # 另一主体套用同一张 3C 证书、换店重新销售
        other = svc.register_producer("借壳商行", "CC-OTHER")
        fake = svc.submit_model(other["producer_id"], "FoldA-Pro", "EPS", "一体",
                                "二厂", ["自行车"], "3C-001", "RPT-2")
        svc.create_listing("shop-9", model["model_id"], "同款头盔", ["自行车"])
        reuse = svc.cert_reuse("3C-001")
        self.assertEqual(sorted(reuse["producers"]), ["CC-FOLD", "CC-OTHER"])
        self.assertIn("shop-1", reuse["shops"])
        self.assertIn("shop-9", reuse["shops"])
        self.assertIn("一张证书对应多个生产主体", reuse["flags"])
        self.assertIn("证书被非持证主体套用: FoldA-Pro", reuse["flags"])
        self.assertIn("型号不在证书覆盖范围: FoldA-Pro", reuse["flags"])

    def test_regulator_traces_notifications_and_outstanding(self):
        svc, _, _, recall = self._recalled_service()
        rid = recall["recall_id"]
        notices = svc.recall_notifications(rid)
        recipients = {n["recipient"] for n in notices["notifications"]}
        self.assertIn("买家甲", recipients)   # 已售通知消费者
        self.assertIn("shop-1", recipients)   # 店铺后台通知
        self.assertEqual(notices["pending"], len(notices["notifications"]))
        # 通知送达回调,重复投递不重复计
        notice_id = notices["notifications"][0]["notice_id"]
        svc.handle_callback("cb-n1", "notice_delivered", {"notice_id": notice_id})
        svc.handle_callback("cb-n1", "notice_delivered", {"notice_id": notice_id})
        self.assertEqual(svc.recall_notifications(rid)["delivered"], 1)
        # 处置 SN-1 后,仍未召回的只剩 SN-2 的去向
        svc.record_disposition(rid, "SN-1", "退货", "shop-1")
        outstanding = svc.recall_outstanding(rid)
        self.assertEqual([o["serial_no"] for o in outstanding["outstanding"]],
                         ["SN-2"])

    def test_regulator_traces_sample_flow(self):
        svc, _, model, _ = self._recalled_service()
        batch_id = next(iter(svc.batches))
        svc.dispatch_sample("SMP-1", model["model_id"], batch_id,
                            "平台仓库", "检测院", "抽检")
        svc.advance_sample("SMP-1", "监管留样库", "检测完成留样")
        flow = svc.sample_flow("SMP-1")
        self.assertEqual([h["to"] for h in flow["chain"]],
                         ["检测院", "监管留样库"])
        self.assertEqual(flow["purpose"], "抽检")


class ApiSmokeTest(unittest.TestCase):
    """接口层冒烟:走一遍 HTTP 主流程。"""

    @classmethod
    def setUpClass(cls):
        gateway = StaticVerificationGateway()
        gateway.register("3C-001", holder="CC-FOLD", models=["FoldA"])
        service_module.SERVICE = HelmetService(gateway)
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service_module.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(f"{self.base}{path}", data=data, method=method,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def test_full_flow_over_http(self):
        producer = self.call("POST", "/api/producers",
                             {"name": "折叠科技", "credit_code": "CC-FOLD"})
        model = self.call("POST", "/api/models", {
            "producer_id": producer["producer_id"], "name": "FoldA",
            "material": "EPS", "structure": "一体", "factory": "一厂",
            "applicable_vehicles": ["自行车"], "cert_number": "3C-001",
            "test_report": "RPT-1"})
        snap = self.call("POST", f"/api/models/{model['model_id']}/verify")
        self.assertEqual(snap["result"], "匹配")
        listing = self.call("POST", "/api/listings", {
            "shop_id": "shop-1", "model_id": model["model_id"],
            "title": "折叠头盔", "declared_vehicles": ["自行车"]})
        self.assertEqual(listing["status"], "正常")
        batch = self.call("POST", "/api/batches", {
            "model_id": model["model_id"], "factory": "一厂",
            "serials": ["SN-1"]})
        order = self.call("POST", "/api/orders", {
            "listing_id": listing["listing_id"], "serial_no": "SN-1",
            "buyer": "买家甲"})
        self.call("POST", "/api/callbacks", {
            "callback_id": "cb-1", "type": "order_delivered",
            "payload": {"order_id": order["order_id"]}})
        recall = self.call("POST", "/api/recalls", {
            "model_id": model["model_id"], "batch_ids": [batch["batch_id"]],
            "reason": "抽检不合格"})
        scan = self.call("GET", "/api/consumer/scan/SN-1")
        self.assertEqual(scan["recall_status"], "召回中")
        outstanding = self.call("GET",
                                f"/api/regulator/recalls/{recall['recall_id']}/outstanding")
        self.assertEqual(outstanding["count"], 1)


if __name__ == "__main__":
    unittest.main()
