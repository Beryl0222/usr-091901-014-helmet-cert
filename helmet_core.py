"""骑行头盔认证与召回的领域核心。

围绕领域契约(domain_contract.json)的三条不可破坏原则组织:
1. 证书必须匹配实际型号和生产主体 —— 上架前留存可信接口的核验快照;
2. 停售调查与正式召回分别记录 —— 两套事件流互不混用;
3. 跨店流转不得打断问题批次追踪 —— 序列号血统跟随批次而非店铺。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# 型号状态(与领域契约一致)
STATE_PENDING = "待核验"
STATE_ON_SALE = "允许销售"
STATE_SUSPENDED = "停售调查"
STATE_RECALLING = "召回中"
STATE_RESTORED = "恢复销售"
STATE_DELISTED = "已退市"
SELLABLE_STATES = (STATE_ON_SALE, STATE_RESTORED)

# 处置方式(退货、换货、销毁、复检恢复分别留痕)
DISP_RETURN = "退货"
DISP_EXCHANGE = "换货"
DISP_DESTROY = "销毁"
DISP_REINSPECT = "复检恢复"
SHOP_DISPOSITIONS = (DISP_RETURN, DISP_EXCHANGE, DISP_DESTROY)

# 停售调查触发原因
TRIG_SAMPLE_FAIL = "抽检不合格"
TRIG_CERT_REVOKED = "证书撤销"
TRIG_FALSE_AD = "夸大宣传"
TRIGGERS = (TRIG_SAMPLE_FAIL, TRIG_CERT_REVOKED, TRIG_FALSE_AD)

# 序列号状态
SERIAL_STOCK = "在库"
SERIAL_IN_TRANSIT = "在途"
SERIAL_SOLD = "已售"
SERIAL_RETURNED = "已退"
SERIAL_EXCHANGED = "已换"
SERIAL_DESTROYED = "已销毁"


class DomainError(Exception):
    """业务规则冲突,携带 HTTP 状态码供接口层映射。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class StaticVerificationGateway:
    """可信证书验证接口的登记簿实现(演示与测试用)。

    生产环境可替换为对接监管验证接口的客户端,返回结构保持一致:
    {"cert_number", "holder", "models", "status"};证书不存在时返回 None。
    """

    def __init__(self):
        self._certs = {}

    def register(self, cert_number, holder, models, status="有效"):
        self._certs[cert_number] = {
            "cert_number": cert_number,
            "holder": holder,
            "models": list(models),
            "status": status,
        }

    def revoke(self, cert_number):
        if cert_number in self._certs:
            self._certs[cert_number]["status"] = "撤销"

    def fetch(self, cert_number):
        cert = self._certs.get(cert_number)
        return dict(cert) if cert else None


@dataclass
class Producer:
    producer_id: str
    name: str
    credit_code: str  # 生产主体统一社会信用代码,用于比对证书持证主体


@dataclass
class HelmetModel:
    model_id: str
    producer_id: str
    name: str  # 型号
    material: str  # 材料
    structure: str  # 结构
    factory: str  # 工厂
    applicable_vehicles: list  # 适用车辆
    cert_number: str  # 证书编号
    test_report: str  # 检测报告编号
    state: str = STATE_PENDING


@dataclass
class Snapshot:
    snapshot_id: str
    model_id: str
    cert_number: str
    holder: str
    covered_models: list
    cert_status: str
    result: str  # 匹配 / 不匹配 / 证书不存在
    mismatches: list
    fetched_at: str


@dataclass
class Listing:
    listing_id: str
    shop_id: str
    model_id: str
    title: str
    declared_vehicles: list
    version: int = 1
    status: str = "正常"  # 正常 / 已拦截 / 已停售
    matches: list = field(default_factory=list)  # MatchRecord 字典,每次变更追加


@dataclass
class Batch:
    batch_id: str
    model_id: str
    factory: str
    produced_at: str
    serials: list


@dataclass
class Serial:
    serial_no: str
    batch_id: str
    model_id: str
    shop_id: str
    status: str = SERIAL_STOCK


@dataclass
class Order:
    order_id: str
    listing_id: str
    serial_no: str
    shop_id: str
    buyer: str
    state: str = "在途"  # 在途 / 已签收


@dataclass
class Investigation:
    inv_id: str
    trigger: str
    model_id: str
    listing_id: str
    detail: str
    status: str = "调查中"  # 调查中 / 已结案
    outcome: str = ""
    recall_id: str = ""
    opened_at: str = ""
    closed_at: str = ""


@dataclass
class Recall:
    recall_id: str
    model_id: str
    batch_ids: list
    reason: str
    status: str = "召回中"  # 召回中 / 已完成
    scope: dict = field(default_factory=dict)  # serial_no -> 圈定时快照
    dispositions: dict = field(default_factory=dict)  # serial_no -> 处置记录(每序列仅一条)
    opened_at: str = ""
    closed_at: str = ""


@dataclass
class Notification:
    notice_id: str
    recall_id: str
    recipient: str
    channel: str  # 店铺后台 / 短信
    status: str = "待送达"  # 待送达 / 已送达
    sent_at: str = ""
    delivered_at: str = ""


@dataclass
class Sample:
    sample_id: str
    model_id: str
    batch_id: str
    purpose: str  # 型式试验 / 抽检 / 复检
    chain: list = field(default_factory=list)  # 样品流转链 [{"from","to","at","note"}]


class HelmetService:
    """认证与召回领域服务,全部状态保存在内存仓库中。"""

    def __init__(self, gateway, clock=None):
        self.gateway = gateway
        self._now = clock or _utc_now
        self._counters = {}
        self.producers = {}
        self.models = {}
        self.listings = {}
        self.batches = {}
        self.serials = {}
        self.orders = {}
        self.investigations = {}
        self.recalls = {}
        self.snapshots = []
        self.disposition_log = []
        self.compensations = {}  # (recall_id, serial_no) -> 赔付记录,保证不多赔
        self.transfers = {}  # transfer_id -> 调拨记录,跨店调拨幂等
        self.callbacks = {}  # callback_id -> 处理结果,平台回调幂等
        self.notifications = {}
        self.samples = {}
        self.events = []

    # ---------- 基础工具 ----------

    def _next(self, prefix):
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def _emit(self, event_type, **data):
        self.events.append(
            {"seq": len(self.events) + 1, "type": event_type, "at": self._now(), "data": data}
        )

    def _producer(self, producer_id):
        if producer_id not in self.producers:
            raise DomainError(f"生产者不存在: {producer_id}", 404)
        return self.producers[producer_id]

    def _model(self, model_id):
        if model_id not in self.models:
            raise DomainError(f"型号不存在: {model_id}", 404)
        return self.models[model_id]

    def _listing(self, listing_id):
        if listing_id not in self.listings:
            raise DomainError(f"商品页面不存在: {listing_id}", 404)
        return self.listings[listing_id]

    def _serial(self, serial_no):
        if serial_no not in self.serials:
            raise DomainError(f"序列号不存在: {serial_no}", 404)
        return self.serials[serial_no]

    def _recall(self, recall_id):
        if recall_id not in self.recalls:
            raise DomainError(f"召回不存在: {recall_id}", 404)
        return self.recalls[recall_id]

    def _latest_snapshot(self, model_id):
        for snap in reversed(self.snapshots):
            if snap.model_id == model_id:
                return snap
        return None

    # ---------- 生产者:型号与证书核验 ----------

    def register_producer(self, name, credit_code):
        producer = Producer(self._next("prod"), name, credit_code)
        self.producers[producer.producer_id] = producer
        self._emit("producer_registered", producer_id=producer.producer_id, name=name)
        return self._producer_view(producer)

    def submit_model(self, producer_id, name, material, structure, factory,
                     applicable_vehicles, cert_number, test_report):
        self._producer(producer_id)
        if not applicable_vehicles:
            raise DomainError("适用车辆不能为空")
        model = HelmetModel(
            self._next("model"), producer_id, name, material, structure, factory,
            list(applicable_vehicles), cert_number, test_report,
        )
        self.models[model.model_id] = model
        self._emit("model_submitted", model_id=model.model_id, name=name,
                   cert_number=cert_number)
        return self._model_view(model)

    def verify_model(self, model_id):
        """从可信验证接口取得证书状态并保存核验快照。"""
        model = self._model(model_id)
        if model.state not in (STATE_PENDING,):
            raise DomainError(f"型号当前状态为{model.state},无需核验")
        producer = self._producer(model.producer_id)
        cert = self.gateway.fetch(model.cert_number)
        mismatches = []
        if cert is None:
            result, holder, covered, status = "证书不存在", "", [], ""
            mismatches.append("可信接口查无此证书编号")
        else:
            holder, covered, status = cert["holder"], cert["models"], cert["status"]
            if holder != producer.credit_code:
                mismatches.append("证书持证主体与生产主体不一致")
            if model.name not in covered:
                mismatches.append("型号不在证书覆盖范围内")
            if status != "有效":
                mismatches.append(f"证书状态为{status}")
            result = "匹配" if not mismatches else "不匹配"
        snap = Snapshot(self._next("snap"), model_id, model.cert_number, holder,
                        list(covered), status, result, mismatches, self._now())
        self.snapshots.append(snap)
        if result == "匹配":
            model.state = STATE_ON_SALE
        self._emit("model_verified", model_id=model_id, snapshot_id=snap.snapshot_id,
                   result=result, mismatches=mismatches)
        return self._snapshot_view(snap)

    # ---------- 销售平台:商品页面与匹配 ----------

    def create_listing(self, shop_id, model_id, title, declared_vehicles):
        self._model(model_id)
        listing = Listing(self._next("lst"), shop_id, model_id, title,
                          list(declared_vehicles))
        self.listings[listing.listing_id] = listing
        self._rematch(listing)
        self._emit("listing_created", listing_id=listing.listing_id, shop_id=shop_id,
                   model_id=model_id)
        return self._listing_view(listing)

    def revise_listing(self, listing_id, title=None, declared_vehicles=None):
        """商品页面每次变更都重新匹配实际型号与适用车辆。"""
        listing = self._listing(listing_id)
        if title is not None:
            listing.title = title
        if declared_vehicles is not None:
            listing.declared_vehicles = list(declared_vehicles)
        listing.version += 1
        record = self._rematch(listing)
        self._emit("listing_revised", listing_id=listing_id, version=listing.version,
                   decision=record["decision"])
        return self._listing_view(listing)

    def _rematch(self, listing):
        model = self._model(listing.model_id)
        reasons = []
        if model.state not in SELLABLE_STATES:
            reasons.append(f"型号状态为{model.state}")
        snap = self._latest_snapshot(model.model_id)
        if snap is None or snap.result != "匹配":
            reasons.append("证书核验未通过")
        if not set(listing.declared_vehicles) <= set(model.applicable_vehicles):
            reasons.append("宣称适用车辆超出型号范围")
        decision = "允许销售" if not reasons else "禁止销售"
        record = {"listing_id": listing.listing_id, "version": listing.version,
                  "matched_model": model.name, "decision": decision,
                  "reasons": reasons, "at": self._now()}
        listing.matches.append(record)
        if listing.status != "已停售":
            listing.status = "正常" if decision == "允许销售" else "已拦截"
        return record

    # ---------- 销售平台:批次、序列与订单 ----------

    def register_batch(self, model_id, factory, serials, produced_at=None):
        model = self._model(model_id)
        if not serials:
            raise DomainError("批次序列号不能为空")
        batch = Batch(self._next("bat"), model_id, factory,
                      produced_at or self._now(), list(serials))
        self.batches[batch.batch_id] = batch
        for serial_no in serials:
            if serial_no in self.serials:
                raise DomainError(f"序列号重复: {serial_no}")
            self.serials[serial_no] = Serial(serial_no, batch.batch_id, model_id, factory)
        self._emit("batch_registered", batch_id=batch.batch_id, model_id=model_id,
                   size=len(serials))
        return self._batch_view(batch)

    def create_order(self, listing_id, serial_no, buyer):
        listing = self._listing(listing_id)
        serial = self._serial(serial_no)
        if listing.status != "正常":
            raise DomainError(f"商品页面状态为{listing.status},不能下单")
        if serial.model_id != listing.model_id:
            raise DomainError("序列号与页面型号不一致")
        if serial.status != SERIAL_STOCK:
            raise DomainError(f"序列号状态为{serial.status},不能出售")
        order = Order(self._next("ord"), listing_id, serial_no, listing.shop_id, buyer)
        self.orders[order.order_id] = order
        serial.status = SERIAL_IN_TRANSIT
        serial.shop_id = listing.shop_id
        self._emit("order_created", order_id=order.order_id, serial_no=serial_no,
                   shop_id=listing.shop_id)
        return self._order_view(order)

    def deliver_order(self, order_id):
        if order_id not in self.orders:
            raise DomainError(f"订单不存在: {order_id}", 404)
        order = self.orders[order_id]
        if order.state != "在途":
            raise DomainError(f"订单状态为{order.state}")
        order.state = "已签收"
        self.serials[order.serial_no].status = SERIAL_SOLD
        self._emit("order_delivered", order_id=order_id, serial_no=order.serial_no)
        return self._order_view(order)

    # ---------- 停售调查(与正式召回分别记录) ----------

    def open_investigation(self, trigger, model_id=None, listing_id=None, detail=""):
        if trigger not in TRIGGERS:
            raise DomainError(f"调查触发原因须为: {'、'.join(TRIGGERS)}")
        if not model_id and not listing_id:
            raise DomainError("调查须指向型号或商品页面")
        if listing_id and not model_id:
            model_id = self._listing(listing_id).model_id
        model = self._model(model_id)
        inv = Investigation(self._next("inv"), trigger, model_id, listing_id or "",
                            detail, opened_at=self._now())
        self.investigations[inv.inv_id] = inv
        if model.state in SELLABLE_STATES:
            model.state = STATE_SUSPENDED
        for lst in self.listings.values():
            if lst.model_id == model_id and lst.status == "正常":
                lst.status = "已停售"
        self._emit("investigation_opened", inv_id=inv.inv_id, trigger=trigger,
                   model_id=model_id)
        return self._investigation_view(inv)

    def close_investigation(self, inv_id, outcome, batch_ids=None, reason=""):
        """结案:澄清恢复 / 责令退市 / 转正式召回。"""
        inv = self._investigation(inv_id)
        if inv.status != "调查中":
            raise DomainError("调查已结案")
        model = self._model(inv.model_id)
        inv.status, inv.closed_at, inv.outcome = "已结案", self._now(), outcome
        if outcome == "澄清恢复":
            if model.state == STATE_SUSPENDED:
                model.state = STATE_RESTORED
            self._reopen_listings(model.model_id)
        elif outcome == "责令退市":
            model.state = STATE_DELISTED
        elif outcome == "转正式召回":
            recall = self.open_recall(model.model_id, batch_ids or [], reason or inv.detail)
            inv.recall_id = recall["recall_id"]
        else:
            raise DomainError("结案结论须为: 澄清恢复、责令退市、转正式召回")
        self._emit("investigation_closed", inv_id=inv_id, outcome=outcome,
                   recall_id=inv.recall_id)
        return self._investigation_view(inv)

    def _investigation(self, inv_id):
        if inv_id not in self.investigations:
            raise DomainError(f"调查不存在: {inv_id}", 404)
        return self.investigations[inv_id]

    def _reopen_listings(self, model_id):
        for lst in self.listings.values():
            if lst.model_id == model_id and lst.status == "已停售":
                lst.status = "正常"
                self._rematch(lst)

    # ---------- 正式召回:圈定批次、在途订单与已售序列 ----------

    def open_recall(self, model_id, batch_ids, reason):
        model = self._model(model_id)
        if model.state == STATE_DELISTED:
            raise DomainError("型号已退市,无需召回")
        if not batch_ids:
            raise DomainError("召回须圈定至少一个批次")
        recall = Recall(self._next("rec"), model_id, list(batch_ids), reason,
                        opened_at=self._now())
        for batch_id in batch_ids:
            if batch_id not in self.batches:
                raise DomainError(f"批次不存在: {batch_id}", 404)
            batch = self.batches[batch_id]
            if batch.model_id != model_id:
                raise DomainError(f"批次{batch_id}不属于该型号")
            for serial_no in batch.serials:
                serial = self.serials[serial_no]
                order = self._open_order_of(serial_no)
                recall.scope[serial_no] = {
                    "batch_id": batch_id, "shop_id": serial.shop_id,
                    "status_at_open": serial.status,
                    "order_id": order.order_id if order else "",
                }
        self.recalls[recall.recall_id] = recall
        model.state = STATE_RECALLING
        for lst in self.listings.values():
            if lst.model_id == model_id:
                lst.status = "已停售"
        self._notify_recall(recall)
        self._emit("recall_opened", recall_id=recall.recall_id, model_id=model_id,
                   batch_ids=list(batch_ids), scope_size=len(recall.scope))
        return self._recall_view(recall)

    def _open_order_of(self, serial_no):
        for order in self.orders.values():
            if order.serial_no == serial_no and order.state == "在途":
                return order
        return None

    def _notify_recall(self, recall):
        shops, buyers = set(), set()
        for serial_no in recall.scope:
            shops.add(self.serials[serial_no].shop_id)
        for order in self.orders.values():
            if order.serial_no in recall.scope:
                shops.add(order.shop_id)
                buyers.add(order.buyer)
        for shop_id in sorted(shops):
            self._add_notification(recall.recall_id, shop_id, "店铺后台")
        for buyer in sorted(buyers):
            self._add_notification(recall.recall_id, buyer, "短信")

    def _add_notification(self, recall_id, recipient, channel):
        notice = Notification(self._next("ntc"), recall_id, recipient, channel,
                              sent_at=self._now())
        self.notifications[notice.notice_id] = notice
        return notice

    # ---------- 召回处置:退货、换货、销毁、复检恢复分别留痕 ----------

    def record_disposition(self, recall_id, serial_no, disp_type, actor, note=""):
        recall = self._recall(recall_id)
        if recall.status != "召回中":
            raise DomainError("召回已完成,不能再登记处置")
        if serial_no not in recall.scope:
            raise DomainError("序列号不在召回范围内,不能漏算也不能错算")
        if serial_no in recall.dispositions:
            raise DomainError("序列号已完成处置,重复登记会导致多赔", 409)
        if disp_type not in SHOP_DISPOSITIONS:
            raise DomainError(f"处置方式须为: {'、'.join(SHOP_DISPOSITIONS)};复检恢复请走复检通道")
        serial = self.serials[serial_no]
        record = {"disp_id": self._next("dsp"), "recall_id": recall_id,
                  "serial_no": serial_no, "type": disp_type, "actor": actor,
                  "note": note, "at": self._now()}
        recall.dispositions[serial_no] = record
        self.disposition_log.append(record)
        serial.status = {DISP_RETURN: SERIAL_RETURNED, DISP_EXCHANGE: SERIAL_EXCHANGED,
                         DISP_DESTROY: SERIAL_DESTROYED}[disp_type]
        compensation = None
        if disp_type in (DISP_RETURN, DISP_EXCHANGE):
            compensation = self._compensate(recall, serial_no, disp_type)
        self._emit("disposition_recorded", recall_id=recall_id, serial_no=serial_no,
                   type=disp_type)
        self._maybe_close_recall(recall)
        return {"disposition": record, "compensation": compensation,
                "recall": self._recall_view(recall)}

    def _compensate(self, recall, serial_no, disp_type):
        """赔付按(召回,序列)唯一,跨店调拨与重复回调都不会多赔。"""
        key = (recall.recall_id, serial_no)
        if key in self.compensations:
            return self.compensations[key]
        order = self._order_of_serial(serial_no)
        payee = order.buyer if order else self.serials[serial_no].shop_id
        compensation = {"comp_id": self._next("cmp"), "recall_id": recall.recall_id,
                        "serial_no": serial_no,
                        "kind": "退款" if disp_type == DISP_RETURN else "换新",
                        "payee": payee, "at": self._now()}
        self.compensations[key] = compensation
        self._emit("compensation_granted", recall_id=recall.recall_id,
                   serial_no=serial_no, kind=compensation["kind"], payee=payee)
        return compensation

    def _order_of_serial(self, serial_no):
        for order in self.orders.values():
            if order.serial_no == serial_no:
                return order
        return None

    def reinspect_restore(self, recall_id, batch_id, report):
        """批次复检合格:范围内未处置序列留痕"复检恢复"并回到在库。"""
        recall = self._recall(recall_id)
        if recall.status != "召回中":
            raise DomainError("召回已完成")
        if batch_id not in recall.batch_ids:
            raise DomainError("批次不在召回范围内")
        restored = []
        for serial_no, scope in recall.scope.items():
            if scope["batch_id"] != batch_id or serial_no in recall.dispositions:
                continue
            record = {"disp_id": self._next("dsp"), "recall_id": recall_id,
                      "serial_no": serial_no, "type": DISP_REINSPECT,
                      "actor": "检测机构", "note": report, "at": self._now()}
            recall.dispositions[serial_no] = record
            self.disposition_log.append(record)
            self.serials[serial_no].status = SERIAL_STOCK
            restored.append(serial_no)
        self._emit("batch_reinspected", recall_id=recall_id, batch_id=batch_id,
                   report=report, restored=restored)
        self._maybe_close_recall(recall)
        return {"restored": restored, "recall": self._recall_view(recall)}

    def _maybe_close_recall(self, recall):
        if set(recall.scope) <= set(recall.dispositions):
            recall.status = "已完成"
            recall.closed_at = self._now()
            model = self._model(recall.model_id)
            has_reinspect = any(d["type"] == DISP_REINSPECT
                                for d in recall.dispositions.values())
            model.state = STATE_RESTORED if has_reinspect else STATE_DELISTED
            if model.state == STATE_RESTORED:
                self._reopen_listings(model.model_id)
            self._emit("recall_closed", recall_id=recall.recall_id,
                       model_state=model.state)

    # ---------- 平台回调与跨店调拨(幂等) ----------

    def handle_callback(self, callback_id, cb_type, payload):
        """重复平台回调按 callback_id 去重,处理结果原样返回。"""
        if not callback_id:
            raise DomainError("回调缺少 callback_id")
        if callback_id in self.callbacks:
            return {"duplicate": True, "result": self.callbacks[callback_id]}
        handlers = {"order_delivered": self._cb_order_delivered,
                    "transfer": self._cb_transfer,
                    "notice_delivered": self._cb_notice_delivered}
        if cb_type not in handlers:
            raise DomainError(f"未知回调类型: {cb_type}")
        result = handlers[cb_type](payload or {})
        self.callbacks[callback_id] = result
        self._emit("callback_handled", callback_id=callback_id, type=cb_type)
        return {"duplicate": False, "result": result}

    def _cb_order_delivered(self, payload):
        return self.deliver_order(payload.get("order_id", ""))

    def _cb_transfer(self, payload):
        return self.transfer_serial(payload.get("transfer_id", ""),
                                    payload.get("serial_no", ""),
                                    payload.get("to_shop", ""))

    def _cb_notice_delivered(self, payload):
        notice_id = payload.get("notice_id", "")
        if notice_id not in self.notifications:
            raise DomainError(f"通知不存在: {notice_id}", 404)
        notice = self.notifications[notice_id]
        notice.status = "已送达"
        notice.delivered_at = self._now()
        return self._notification_view(notice)

    def transfer_serial(self, transfer_id, serial_no, to_shop):
        """跨店调拨:序列号血统跟随批次,调拨只改当前持有店铺。"""
        if not transfer_id:
            raise DomainError("调拨缺少 transfer_id")
        if transfer_id in self.transfers:
            return self.transfers[transfer_id]
        serial = self._serial(serial_no)
        record = {"transfer_id": transfer_id, "serial_no": serial_no,
                  "batch_id": serial.batch_id, "from_shop": serial.shop_id,
                  "to_shop": to_shop, "at": self._now()}
        serial.shop_id = to_shop
        self.transfers[transfer_id] = record
        self._emit("serial_transferred", transfer_id=transfer_id, serial_no=serial_no,
                   batch_id=serial.batch_id, from_shop=record["from_shop"],
                   to_shop=to_shop)
        return record

    # ---------- 样品流转 ----------

    def dispatch_sample(self, sample_id, model_id, batch_id, from_party, to_party,
                        purpose, note=""):
        self._model(model_id)
        if sample_id in self.samples:
            raise DomainError(f"样品编号重复: {sample_id}", 409)
        sample = Sample(sample_id, model_id, batch_id, purpose)
        sample.chain.append({"from": from_party, "to": to_party, "at": self._now(),
                             "note": note})
        self.samples[sample_id] = sample
        self._emit("sample_dispatched", sample_id=sample_id, model_id=model_id,
                   purpose=purpose)
        return self._sample_view(sample)

    def advance_sample(self, sample_id, to_party, note=""):
        if sample_id not in self.samples:
            raise DomainError(f"样品不存在: {sample_id}", 404)
        sample = self.samples[sample_id]
        sample.chain.append({"from": sample.chain[-1]["to"], "to": to_party,
                             "at": self._now(), "note": note})
        self._emit("sample_advanced", sample_id=sample_id, to_party=to_party)
        return self._sample_view(sample)

    # ---------- 消费者扫码(只看必要信息) ----------

    def consumer_scan(self, serial_no):
        serial = self.serials.get(serial_no)
        if serial is None:
            return {"serial_no": serial_no, "authentic": False}
        model = self._model(serial.model_id)
        snap = self._latest_snapshot(model.model_id)
        view = {"serial_no": serial_no,
                "authentic": bool(snap and snap.result == "匹配"),
                "model": model.name,
                "applicable_vehicles": list(model.applicable_vehicles),
                "recall_status": None, "disposal_entry": None, "disposition": None}
        for recall in self.recalls.values():
            if serial_no not in recall.scope:
                continue
            view["recall_status"] = recall.status
            disp = recall.dispositions.get(serial_no)
            if disp:
                view["disposition"] = disp["type"]
            elif recall.status == "召回中":
                view["disposal_entry"] = "请通过购买店铺或平台客服办理退货、换货"
        return view

    # ---------- 监管视图 ----------

    def cert_reuse(self, cert_number):
        """证书复用关系:哪些型号、生产主体、店铺在用同一张证书。"""
        registry = self.gateway.fetch(cert_number)
        models = [m for m in self.models.values() if m.cert_number == cert_number]
        producers = sorted({self._producer(m.producer_id).credit_code for m in models})
        shops = sorted({lst.shop_id for lst in self.listings.values()
                        if self._model(lst.model_id).cert_number == cert_number})
        flags = []
        if registry:
            for m in models:
                holder = self._producer(m.producer_id).credit_code
                if holder != registry["holder"]:
                    flags.append(f"证书被非持证主体套用: {m.name}")
                if m.name not in registry["models"]:
                    flags.append(f"型号不在证书覆盖范围: {m.name}")
        if len(producers) > 1:
            flags.append("一张证书对应多个生产主体")
        if registry is None:
            flags.append("可信接口查无此证书")
        return {"cert_number": cert_number,
                "registry": registry,
                "models": [self._model_view(m) for m in models],
                "producers": producers, "shops": shops,
                "snapshots": [self._snapshot_view(s) for s in self.snapshots
                              if s.cert_number == cert_number],
                "flags": flags}

    def recall_outstanding(self, recall_id):
        """仍未召回的去向:按序列当前位置追踪,跨店调拨不丢失。"""
        recall = self._recall(recall_id)
        outstanding = []
        for serial_no in recall.scope:
            if serial_no in recall.dispositions:
                continue
            serial = self.serials[serial_no]
            transfers = [t for t in self.transfers.values()
                         if t["serial_no"] == serial_no]
            outstanding.append({
                "serial_no": serial_no, "batch_id": serial.batch_id,
                "current_shop": serial.shop_id, "status": serial.status,
                "scoped_shop": recall.scope[serial_no]["shop_id"],
                "transfers": transfers})
        return {"recall_id": recall_id, "outstanding": outstanding,
                "count": len(outstanding)}

    def recall_notifications(self, recall_id):
        self._recall(recall_id)
        notices = [self._notification_view(n) for n in self.notifications.values()
                   if n.recall_id == recall_id]
        return {"recall_id": recall_id, "notifications": notices,
                "delivered": sum(1 for n in notices if n["status"] == "已送达"),
                "pending": sum(1 for n in notices if n["status"] == "待送达")}

    def sample_flow(self, sample_id):
        if sample_id not in self.samples:
            raise DomainError(f"样品不存在: {sample_id}", 404)
        return self._sample_view(self.samples[sample_id])

    def event_log(self):
        return list(self.events)

    # ---------- 视图 ----------

    def _producer_view(self, p):
        return {"producer_id": p.producer_id, "name": p.name,
                "credit_code": p.credit_code}

    def _model_view(self, m):
        return {"model_id": m.model_id, "producer_id": m.producer_id, "name": m.name,
                "material": m.material, "structure": m.structure, "factory": m.factory,
                "applicable_vehicles": list(m.applicable_vehicles),
                "cert_number": m.cert_number, "test_report": m.test_report,
                "state": m.state}

    def _snapshot_view(self, s):
        return {"snapshot_id": s.snapshot_id, "model_id": s.model_id,
                "cert_number": s.cert_number, "holder": s.holder,
                "covered_models": list(s.covered_models), "cert_status": s.cert_status,
                "result": s.result, "mismatches": list(s.mismatches),
                "fetched_at": s.fetched_at}

    def _listing_view(self, lst):
        return {"listing_id": lst.listing_id, "shop_id": lst.shop_id,
                "model_id": lst.model_id, "title": lst.title,
                "declared_vehicles": list(lst.declared_vehicles),
                "version": lst.version, "status": lst.status,
                "matches": list(lst.matches)}

    def _batch_view(self, b):
        return {"batch_id": b.batch_id, "model_id": b.model_id, "factory": b.factory,
                "produced_at": b.produced_at, "serials": list(b.serials)}

    def _order_view(self, o):
        return {"order_id": o.order_id, "listing_id": o.listing_id,
                "serial_no": o.serial_no, "shop_id": o.shop_id, "buyer": o.buyer,
                "state": o.state}

    def _investigation_view(self, inv):
        return {"inv_id": inv.inv_id, "trigger": inv.trigger, "model_id": inv.model_id,
                "listing_id": inv.listing_id, "detail": inv.detail,
                "status": inv.status, "outcome": inv.outcome,
                "recall_id": inv.recall_id, "opened_at": inv.opened_at,
                "closed_at": inv.closed_at}

    def _recall_view(self, r):
        by_type = {}
        for d in r.dispositions.values():
            by_type[d["type"]] = by_type.get(d["type"], 0) + 1
        return {"recall_id": r.recall_id, "model_id": r.model_id,
                "batch_ids": list(r.batch_ids), "reason": r.reason,
                "status": r.status, "scope_size": len(r.scope),
                "disposed": len(r.dispositions),
                "outstanding": len(r.scope) - len(r.dispositions),
                "dispositions_by_type": by_type,
                "opened_at": r.opened_at, "closed_at": r.closed_at}

    def _notification_view(self, n):
        return {"notice_id": n.notice_id, "recall_id": n.recall_id,
                "recipient": n.recipient, "channel": n.channel,
                "status": n.status, "sent_at": n.sent_at,
                "delivered_at": n.delivered_at}

    def _sample_view(self, s):
        return {"sample_id": s.sample_id, "model_id": s.model_id,
                "batch_id": s.batch_id, "purpose": s.purpose,
                "chain": list(s.chain)}
