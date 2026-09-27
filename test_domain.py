"""领域服务测试：核验、重匹配、调查、召回、幂等与监管追溯。"""

import unittest

from helmet.domain import DisposalType, InvestigationTrigger, ModelState, SerialStatus
from helmet.services import HelmetService
from helmet.store import DataStore
from helmet.verification import StaticCertificateAuthority

CERTS = [
    {"certificate_no": "3C-A", "status": "有效",
     "model_name": "A1", "holder": "甲厂"},
    {"certificate_no": "3C-B", "status": "有效",
     "model_name": "B1", "holder": "乙厂"},
    {"certificate_no": "3C-X", "status": "有效",
     "model_name": "X1", "holder": "甲厂"},
]


def build_service():
    return HelmetService(DataStore(), StaticCertificateAuthority(CERTS))


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.producer = self.svc.register_producer("甲厂")

    def _submit(self, name="A1", cert="3C-A", vehicles=("电动自行车",)):
        return self.svc.submit_model(
            self.producer.producer_id, name, "EPS+PC 一体成型", "一号工厂",
            cert, list(vehicles), test_reports=["report-1.pdf"],
        )

    def test_verify_pass_saves_snapshot_and_allows_sale(self):
        model = self._submit()
        self.assertEqual(model.state, ModelState.待核验)
        snap = self.svc.verify_model(model.model_id)
        self.assertTrue(snap.matched)
        self.assertEqual(snap.status, "有效")
        self.assertEqual(len(snap.payload_digest), 64)
        # 快照不可更改：再次核验只追加，不改旧记录
        self.svc.verify_model(model.model_id)
        self.assertEqual(len(self.svc.store.snapshots), 2)
        self.assertEqual(
            self.svc._get_model(model.model_id).state, ModelState.允许销售
        )

    def test_verify_fails_on_model_or_holder_mismatch(self):
        # 实际型号名与证书登记型号不一致
        model = self._submit(name="A1-山寨版")
        snap = self.svc.verify_model(model.model_id)
        self.assertFalse(snap.matched)
        self.assertEqual(self.svc._get_model(model.model_id).state,
                         ModelState.待核验)
        # 持证主体不一致
        other = self.svc.register_producer("丙店")
        m2 = self.svc.submit_model(
            other.producer_id, "A1", "纸壳折叠", "二号工厂",
            "3C-A", ["电动自行车"],
        )
        snap2 = self.svc.verify_model(m2.model_id)
        self.assertFalse(snap2.matched)
        # 证书登记持证人是甲厂，与提交主体丙店不一致
        self.assertEqual(snap2.cert_holder, "甲厂")

    def test_revoked_certificate_blocks_sale_and_is_snapshotted(self):
        model = self._submit()
        self.svc.verify_model(model.model_id)
        self.svc.authority.set_status("3C-A", "已撤销")
        snap = self.svc.verify_model(model.model_id)
        self.assertEqual(snap.status, "已撤销")
        self.assertFalse(snap.matched)


class ListingRematchTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.producer = self.svc.register_producer("甲厂")
        self.model = self.svc.submit_model(
            self.producer.producer_id, "A1", "EPS+PC", "一号工厂", "3C-A",
            ["电动自行车", "电动滑板车"],
        )
        self.svc.verify_model(self.model.model_id)

    def test_listing_goes_on_sale_when_matched(self):
        listing = self.svc.create_listing(
            "store-1", self.model.model_id, "折叠头盔 A1", ["电动自行车"])
        self.assertEqual(listing.status, "在售")
        self.assertTrue(listing.matched)

    def test_every_change_rematches_vehicle_scope_and_claims(self):
        listing = self.svc.create_listing(
            "store-1", self.model.model_id, "折叠头盔 A1", ["电动自行车"])
        # 宣称超出认证适用车辆 → 下架
        self.svc.update_listing(listing.listing_id, claimed_vehicles=["摩托车"])
        self.assertEqual(listing.status, "下架")
        self.assertIn("超出认证范围", listing and self.svc.store.revisions[-1].mismatches[0])
        # 改回合规车型 → 重新在售
        self.svc.update_listing(listing.listing_id,
                                claimed_vehicles=["电动滑板车"])
        self.assertEqual(listing.status, "在售")
        # 夸大防护宣传 → 下架
        self.svc.update_listing(listing.listing_id, title="绝对安全保命头盔")
        self.assertEqual(listing.status, "下架")
        self.assertIn("夸大", self.svc.store.revisions[-1].mismatches[0])
        # 每次变更都有留痕
        self.assertEqual(listing.revision, 4)

    def test_unverified_model_listing_kept_off_shelf(self):
        m = self.svc.submit_model(
            self.producer.producer_id, "X1", "纸壳", "三号工厂", "3C-X", ["自行车"])
        listing = self.svc.create_listing("store-9", m.model_id, "X1", ["自行车"])
        self.assertEqual(listing.status, "下架")


class InvestigationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.producer = self.svc.register_producer("甲厂")
        self.model = self.svc.submit_model(
            self.producer.producer_id, "A1", "EPS+PC", "一号工厂", "3C-A",
            ["电动自行车"],
        )
        self.svc.verify_model(self.model.model_id)

    def test_three_triggers_open_investigation_and_take_down(self):
        listing = self.svc.create_listing(
            "store-1", self.model.model_id, "A1", ["电动自行车"])
        inv, created = self.svc.open_investigation(
            self.model.model_id, InvestigationTrigger.抽检不合格,
            "冲击吸收不合格", sample_id="SMP-1")
        self.assertTrue(created)
        self.assertEqual(self.svc._get_model(self.model.model_id).state,
                         ModelState.停售调查)
        self.assertEqual(self.svc._get_listing(listing.listing_id).status, "下架")
        # 重复触发不重复开调查单
        again, created2 = self.svc.open_investigation(
            self.model.model_id, InvestigationTrigger.夸大宣传, "再次触发")
        self.assertFalse(created2)
        self.assertEqual(again.investigation_id, inv.investigation_id)

    def test_close_without_recall_resumes_sale(self):
        inv, _ = self.svc.open_investigation(
            self.model.model_id, InvestigationTrigger.夸大宣传, "标题违规")
        self.svc.close_investigation(inv.investigation_id, "整改合格，解除停售")
        self.assertEqual(self.svc._get_model(self.model.model_id).state,
                         ModelState.恢复销售)

    def test_revoked_callback_cascades_to_all_models_sharing_cert(self):
        # 两个型号套用同一张证书（证书复用场景）
        m2 = self.svc.submit_model(
            self.producer.producer_id, "A1", "EPS+PC", "代工二厂", "3C-A",
            ["电动自行车"],
        )
        self.svc.verify_model(m2.model_id)
        # 发证机构撤销证书，可信源状态随之变化
        self.svc.authority.set_status("3C-A", "已撤销")
        receipt, processed = self.svc.platform_callback(
            "cb-1", "certificate_revoked",
            {"certificate_no": "3C-A", "detail": "发证机构撤销"})
        self.assertTrue(processed)
        self.assertEqual(
            self.svc._get_model(self.model.model_id).state,
            ModelState.停售调查)
        self.assertEqual(self.svc._get_model(m2.model_id).state,
                         ModelState.停售调查)
        # 撤销状态核验快照已留存
        self.assertEqual(self.svc.store.snapshots[-1].status, "已撤销")
        # 同一回调重复投递不再处理
        _, processed2 = self.svc.platform_callback(
            "cb-1", "certificate_revoked", {"certificate_no": "3C-A"})
        self.assertFalse(processed2)


class RecallFlowTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        producer = self.svc.register_producer("甲厂")
        self.model = self.svc.submit_model(
            producer.producer_id, "A1", "EPS+PC", "一号工厂", "3C-A",
            ["电动自行车"],
        )
        self.svc.verify_model(self.model.model_id)
        self.listing = self.svc.create_listing(
            "store-1", self.model.model_id, "A1", ["电动自行车"])
        self.bad_batch = self.svc.register_batch(self.model.model_id, "2026-08")
        self.good_batch = self.svc.register_batch(self.model.model_id, "2026-09")
        # 问题批次 3 个：一个在途、一个已售、一个在库
        self.svc.register_serials(self.bad_batch.batch_id,
                                  ["SN-1", "SN-2", "SN-3"], "store-1")
        self.svc.register_serials(self.good_batch.batch_id, ["SN-9"], "store-1")

    def _order(self, order_id, serials, delivered):
        order, _ = self.svc.place_order(
            order_id, "store-1", self.listing.listing_id, "consumer-x",
            serials, unit_price=99.0)
        if delivered:
            self.svc.deliver_order(order_id)
        return order

    def _recall(self, batch_ids=None):
        inv, _ = self.svc.open_investigation(
            self.model.model_id, InvestigationTrigger.抽检不合格,
            "抽检冲击吸收不合格", sample_id="SMP-1")
        result = self.svc.close_investigation(
            inv.investigation_id, "确认批次质量缺陷", recall=True,
            batch_ids=batch_ids or [self.bad_batch.batch_id],
            reason="2026-08 批次外壳破裂")
        return result

    def test_recall_scopes_batch_intransit_and_sold_with_notifications(self):
        self._order("ORD-1", ["SN-1"], delivered=False)  # 在途
        self._order("ORD-2", ["SN-2"], delivered=True)   # 已售
        recall = self._recall()
        self.assertEqual(set(recall.scoped_serials), {"SN-1", "SN-2", "SN-3"})
        self.assertEqual(recall.in_transit_orders, ["ORD-1"])
        self.assertEqual(self.svc._get_model(self.model.model_id).state,
                         ModelState.召回中)
        # 在途订单 + 已售序列对应消费者均产生通知
        order_ids = {n.order_id for n in self.svc.store.notifications.values()}
        self.assertEqual(order_ids, {"ORD-1", "ORD-2"})
        # 合格批次不在圈定范围
        self.assertNotIn("SN-9", recall.scoped_serials)

    def test_return_books_single_compensation_duplicate_callback_no_double_pay(self):
        self._order("ORD-2", ["SN-2"], delivered=True)
        recall = self._recall()
        record, comp = self.svc.dispose_return(recall.recall_id, "SN-2")
        self.assertEqual(comp.amount, 99.0)
        self.assertEqual(comp.status, "待赔付")
        self.assertEqual(
            self.svc.store.serials["SN-2"].status, SerialStatus.已退货)
        # 重复回调走幂等：返回同一处置与同一赔付，不多赔
        receipt, processed = self.svc.platform_callback(
            "cb-ret-1", "order_return",
            {"recall_id": recall.recall_id, "serial_no": "SN-2"})
        self.assertTrue(processed)  # 回调本身首次处理
        self.assertEqual(receipt.result["disposal_id"], record.disposal_id)
        comps = [e for e in self.svc.store.compensations.values()
                 if e.serial_no == "SN-2"]
        self.assertEqual(len(comps), 1)
        # 再次投递同一回调
        _, processed2 = self.svc.platform_callback(
            "cb-ret-1", "order_return",
            {"recall_id": recall.recall_id, "serial_no": "SN-2"})
        self.assertFalse(processed2)
        self.assertEqual(len(self.svc.store.compensations), 1)
        # 终态后不能再处置
        with self.assertRaises(Exception):
            self.svc.dispose_destroy(recall.recall_id, "SN-2")

    def test_intransit_interception_return_has_no_compensation(self):
        self._order("ORD-1", ["SN-1"], delivered=False)
        recall = self._recall()
        _, comp = self.svc.dispose_return(recall.recall_id, "SN-1")
        self.assertIsNone(comp)  # 在途拦截未成交，不赔付

    def test_exchange_and_destroy_are_separately_traced(self):
        self._order("ORD-2", ["SN-2"], delivered=True)
        recall = self._recall()
        self.svc.dispose_exchange(recall.recall_id, "SN-2",
                                  new_serial_no="SN-9")
        self.assertEqual(
            self.svc.store.serials["SN-2"].status, SerialStatus.已换货)
        # 新品 SN-9 留下换货发出痕迹
        self.assertTrue(
            any(h["action"] == "换货发出"
                for h in self.svc.store.serials["SN-9"].history))
        self.svc.dispose_destroy(recall.recall_id, "SN-3", note="监管监督销毁")
        self.assertEqual(
            self.svc.store.serials["SN-3"].status, SerialStatus.已销毁)
        kinds = {d.action for d in self.svc.store.disposals}
        self.assertIn(DisposalType.换货, kinds)
        self.assertIn(DisposalType.销毁, kinds)

    def test_cross_store_transfer_does_not_break_batch_tracking(self):
        # SN-3 在库时跨店调拨，召回圈定仍按批次锁定且显示当前去向
        record, created = self.svc.transfer_store(
            "TR-1", "SN-3", "store-2")
        self.assertTrue(created)
        self.assertEqual(self.svc.store.serials["SN-3"].store_id, "store-2")
        # 重复调拨请求幂等
        again, created2 = self.svc.transfer_store("TR-1", "SN-3", "store-2")
        self.assertFalse(created2)
        self.assertEqual(again.transfer_id, record.transfer_id)
        recall = self._recall()
        self.assertIn("SN-3", recall.scoped_serials)
        report = self.svc.regulator_recall_unrecovered(recall.recall_id)[0]
        sn3 = next(p for p in report["pending"] if p["serial_no"] == "SN-3")
        self.assertEqual(sn3["current_store"], "store-2")
        self.assertTrue(sn3["transferred"])

    def test_reinspect_lifts_recall_and_resumes_sale(self):
        recall = self._recall()
        self.svc.dispose_destroy(recall.recall_id, "SN-3")
        rec = self.svc.reinspect_recover(recall.recall_id, sample_id="SMP-2",
                                         note="整改后复检合格")
        self.assertEqual(rec.action, DisposalType.复检恢复)
        self.assertTrue(recall.lifted)
        self.assertEqual(self.svc._get_model(self.model.model_id).state,
                         ModelState.恢复销售)
        # 页面重新匹配后恢复在售
        self.assertEqual(self.listing.status, "在售")
        # 解除后未处置序列不再显示为未召回风险
        report = self.svc.regulator_recall_unrecovered(recall.recall_id)[0]
        self.assertTrue(report["lifted"])

    def test_complete_recall_requires_all_disposed(self):
        recall = self._recall()
        with self.assertRaises(Exception):
            self.svc.complete_recall(recall.recall_id)
        for sn in ["SN-1", "SN-2", "SN-3"]:
            self.svc.dispose_destroy(recall.recall_id, sn)
        done = self.svc.complete_recall(recall.recall_id)
        self.assertEqual(done.status, "已完成")
        self.assertEqual(self.svc._get_model(self.model.model_id).state,
                         ModelState.已退市)


class SaleBlockingTest(unittest.TestCase):
    def test_order_blocked_while_investigation_or_recall(self):
        svc = build_service()
        producer = svc.register_producer("甲厂")
        model = svc.submit_model(
            producer.producer_id, "A1", "EPS+PC", "一号工厂", "3C-A",
            ["电动自行车"])
        svc.verify_model(model.model_id)
        listing = svc.create_listing("store-1", model.model_id, "A1",
                                     ["电动自行车"])
        batch = svc.register_batch(model.model_id)
        svc.register_serials(batch.batch_id, ["SN-1"], "store-1")
        svc.open_investigation(model.model_id,
                               InvestigationTrigger.抽检不合格, "不合格")
        with self.assertRaises(Exception):
            svc.place_order("ORD-1", "store-1", listing.listing_id,
                            "c1", ["SN-1"], 99.0)


class ConsumerAndRegulatorViewTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        producer = self.svc.register_producer("甲厂")
        self.model = self.svc.submit_model(
            producer.producer_id, "A1", "EPS+PC", "一号工厂", "3C-A",
            ["电动自行车", "电动滑板车"])
        self.svc.verify_model(self.model.model_id)
        self.listing = self.svc.create_listing(
            "store-1", self.model.model_id, "A1", ["电动自行车"])
        batch = self.svc.register_batch(self.model.model_id)
        self.svc.register_serials(batch.batch_id, ["SN-1", "SN-2"], "store-1")
        self.svc.place_order("ORD-1", "store-1", self.listing.listing_id,
                             "consumer-1", ["SN-1"], 99.0)
        self.svc.deliver_order("ORD-1")

    def test_scan_returns_minimal_necessary_view(self):
        view = self.svc.consumer_scan("SN-1")
        self.assertEqual(set(view.keys()),
                         {"serial_no", "authenticity",
                          "applicable_vehicles", "disposition_entry"})
        self.assertEqual(view["authenticity"], "真品")
        self.assertNotIn("producer", view)  # 不含主体/工厂等非必要信息
        self.assertNotIn("certificate_no", view)

    def test_scan_shows_recall_entry_then_done_state(self):
        inv, _ = self.svc.open_investigation(
            self.model.model_id, InvestigationTrigger.抽检不合格, "不合格")
        recall = self.svc.close_investigation(
            inv.investigation_id, "缺陷", recall=True, batch_ids=[],
            reason="全线召回")
        view = self.svc.consumer_scan("SN-1")
        self.assertEqual(view["disposition_entry"]["status"], "召回中")
        self.svc.dispose_return(recall.recall_id, "SN-1")
        view2 = self.svc.consumer_scan("SN-1")
        self.assertIn("已完成", view2["disposition_entry"]["status"])

    def test_regulator_cert_reuse_detects_shared_certificate(self):
        # 另一生产主体用同一张 3C-A 证书提交不同名称型号
        producer2 = self.svc.register_producer("乙厂")
        fake = self.svc.submit_model(
            producer2.producer_id, "PaperRide 折叠王", "纸壳", "无名工厂",
            "3C-A", ["摩托车"])
        self.svc.verify_model(fake.model_id)
        groups = {g["certificate_no"]: g
                  for g in self.svc.regulator_cert_reuse()}
        group = groups["3C-A"]
        self.assertEqual(group["model_count"], 2)
        self.assertTrue(group["reuse_signals"])

    def test_regulator_sample_flow_and_notification_chain(self):
        inv, _ = self.svc.open_investigation(
            self.model.model_id, InvestigationTrigger.抽检不合格, "不合格",
            sample_id="SMP-1")
        recall = self.svc.close_investigation(
            inv.investigation_id, "缺陷", recall=True, batch_ids=[],
            reason="召回")
        flows = self.svc.regulator_sample_trace("SMP-1")
        self.assertTrue(any(f["to_party"] == "检测机构" for f in flows))
        notes = self.svc.regulator_notifications(recall.recall_id)
        note = notes[0]
        self.svc.send_notification(note["notification_id"])
        self.svc.deliver_notification(note["notification_id"])
        delivered = self.svc.regulator_notifications(recall.recall_id)[0]
        self.assertEqual(delivered["status"], "已送达")
        self.assertIsNotNone(delivered["delivered_at"])

    def test_unrecovered_report_tracks_pending_serials(self):
        inv, _ = self.svc.open_investigation(
            self.model.model_id, InvestigationTrigger.抽检不合格, "不合格")
        recall = self.svc.close_investigation(
            inv.investigation_id, "缺陷", recall=True, batch_ids=[],
            reason="召回")
        report = self.svc.regulator_recall_unrecovered(recall.recall_id)[0]
        self.assertEqual(report["pending_total"], 2)
        self.svc.dispose_destroy(recall.recall_id, "SN-1")
        report2 = self.svc.regulator_recall_unrecovered(recall.recall_id)[0]
        self.assertEqual(report2["pending_total"], 1)
        self.assertEqual(report2["disposed_total"], 1)


if __name__ == "__main__":
    unittest.main()
