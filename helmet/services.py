"""应用服务：核验快照、页面重匹配、停售调查、召回圈定与处置、幂等与监管视图。"""

from __future__ import annotations

from .domain import (
    CallbackReceipt,
    CompensationEntry,
    DisposalRecord,
    DisposalType,
    DomainError,
    HelmetModel,
    Investigation,
    InvestigationTrigger,
    Listing,
    ListingRevision,
    ModelState,
    MODEL_TRANSITIONS,
    Notification,
    Order,
    Producer,
    Recall,
    SampleFlowEvent,
    SerialStatus,
    SerialUnit,
    TERMINAL_SERIAL_STATUSES,
    TransferRecord,
    VerificationSnapshot,
    Batch,
    new_id,
    now,
    to_dict,
)
from .verification import payload_digest

# 可销售状态：只有这些状态下商品页面才允许上架成交
SELLABLE_STATES = frozenset({ModelState.允许销售, ModelState.恢复销售})
# 证书有效状态
CERT_VALID = "有效"
# 超出认证范围的防护宣传词
EXAGGERATED_CLAIMS = ("防撞", "防弹", "绝对安全", "摔不坏", "保命")


def _require(condition, kind, message):
    if not condition:
        raise DomainError(kind, message)


class HelmetService:
    """领域服务：所有写操作在 store.lock 内执行，保证圈定与留痕一致。"""

    def __init__(self, store, authority):
        self.store = store
        self.authority = authority

    # ---------- 基础读取 ----------

    def _get_model(self, model_id):
        model = self.store.models.get(model_id)
        _require(model is not None, "not_found", f"型号不存在: {model_id}")
        return model

    def _get_listing(self, listing_id):
        listing = self.store.listings.get(listing_id)
        _require(listing is not None, "not_found", f"商品页面不存在: {listing_id}")
        return listing

    def _get_recall(self, recall_id):
        recall = self.store.recalls.get(recall_id)
        _require(recall is not None, "not_found", f"召回不存在: {recall_id}")
        return recall

    def _latest_snapshot(self, model_id):
        snapshots = [s for s in self.store.snapshots if s.model_id == model_id]
        return snapshots[-1] if snapshots else None

    # ---------- 生产者与型号提交 ----------

    def register_producer(self, name, contact=""):
        producer = Producer(new_id("prod"), name, contact)
        self.store.producers[producer.producer_id] = producer
        return producer

    def submit_model(
        self,
        producer_id,
        model_name,
        material_structure,
        factory,
        certificate_no,
        applicable_vehicles,
        test_reports=None,
    ):
        """生产者提交型号、材料结构、工厂、证书编号和检测报告；初始为待核验。"""
        _require(
            producer_id in self.store.producers, "not_found", "生产者未登记"
        )
        _require(applicable_vehicles, "invalid", "适用车辆不能为空")
        model = HelmetModel(
            model_id=new_id("model"),
            producer_id=producer_id,
            model_name=model_name,
            material_structure=material_structure,
            factory=factory,
            certificate_no=certificate_no,
            applicable_vehicles=list(applicable_vehicles),
            test_reports=list(test_reports or []),
        )
        model.history.append({"at": now(), "action": "提交型号", "detail": model_name})
        self.store.models[model.model_id] = model
        return model

    # ---------- 可信核验与快照 ----------

    def verify_model(self, model_id):
        """从可信验证接口取得证书状态并保存核验快照；核验通过方可销售。"""
        model = self._get_model(model_id)
        producer = self.store.producers[model.producer_id]
        payload = self.authority.fetch(model.certificate_no)
        status = payload.get("status", "未登记")
        cert_model_name = payload.get("model_name", "")
        cert_holder = payload.get("holder", "")
        mismatch_fields = []
        if status != CERT_VALID:
            mismatch_fields.append(f"证书状态={status}")
        if cert_model_name != model.model_name:
            mismatch_fields.append("证书型号与实际型号不一致")
        if cert_holder != producer.name:
            mismatch_fields.append("证书持证主体与生产主体不一致")
        snapshot = VerificationSnapshot(
            snapshot_id=new_id("snap"),
            model_id=model_id,
            certificate_no=model.certificate_no,
            status=status,
            cert_model_name=cert_model_name,
            cert_holder=cert_holder,
            matched=not mismatch_fields,
            source=self.authority.source,
            verified_at=now(),
            payload_digest=payload_digest(payload),
        )
        with self.store.lock:
            self.store.snapshots.append(snapshot)
            if snapshot.matched and model.state == ModelState.待核验:
                self._transition(model, ModelState.允许销售, "证书核验通过")
        return snapshot

    def _transition(self, model, target, reason):
        _require(
            target in MODEL_TRANSITIONS[model.state],
            "conflict",
            f"型号不能从{model.state.value}转为{target.value}",
        )
        model.state = target
        model.version += 1
        model.history.append(
            {"at": now(), "action": target.value, "detail": reason}
        )

    # ---------- 商品页面与变更重匹配 ----------

    def _match_listing(self, model, title, claimed_vehicles):
        """重新匹配实际型号、证书状态、适用车辆与宣传内容。"""
        mismatches = []
        snapshot = self._latest_snapshot(model.model_id)
        if model.state not in SELLABLE_STATES:
            mismatches.append(f"型号状态为{model.state.value}，不可销售")
        if snapshot is None:
            mismatches.append("尚无核验快照")
        elif not snapshot.matched or snapshot.status != CERT_VALID:
            mismatches.append(f"证书核验未通过（{snapshot.status}）")
        extra = [v for v in claimed_vehicles if v not in model.applicable_vehicles]
        if extra:
            mismatches.append(f"宣称适用车辆超出认证范围: {','.join(extra)}")
        hits = [word for word in EXAGGERATED_CLAIMS if word in (title or "")]
        if hits:
            mismatches.append(f"标题含夸大防护宣传: {','.join(hits)}")
        return not mismatches, mismatches

    def _record_revision(self, listing, title, claimed_vehicles, matched, mismatches):
        listing.revision += 1
        revision = ListingRevision(
            revision_id=new_id("rev"),
            listing_id=listing.listing_id,
            revision=listing.revision,
            title=title,
            claimed_vehicles=list(claimed_vehicles),
            matched=matched,
            mismatches=mismatches,
            checked_at=now(),
        )
        self.store.revisions.append(revision)
        listing.matched = matched
        return revision

    def create_listing(self, store_id, model_id, title, claimed_vehicles):
        model = self._get_model(model_id)
        listing = Listing(
            listing_id=new_id("list"),
            store_id=store_id,
            model_id=model_id,
            title=title,
            claimed_vehicles=list(claimed_vehicles),
        )
        with self.store.lock:
            matched, mismatches = self._match_listing(
                model, title, claimed_vehicles
            )
            self._record_revision(listing, title, claimed_vehicles, matched, mismatches)
            listing.status = "在售" if matched else "下架"
            self.store.listings[listing.listing_id] = listing
        return listing

    def update_listing(self, listing_id, title=None, claimed_vehicles=None,
                       model_id=None):
        """商品页面每次变更都重新匹配实际型号与适用车辆。"""
        listing = self._get_listing(listing_id)
        with self.store.lock:
            if model_id is not None:
                listing.model_id = model_id
            if title is not None:
                listing.title = title
            if claimed_vehicles is not None:
                listing.claimed_vehicles = list(claimed_vehicles)
            model = self._get_model(listing.model_id)
            matched, mismatches = self._match_listing(
                model, listing.title, listing.claimed_vehicles
            )
            self._record_revision(
                listing, listing.title, listing.claimed_vehicles,
                matched, mismatches,
            )
            listing.status = "在售" if matched else "下架"
        return listing

    # ---------- 批次、序列与跨店调拨 ----------

    def register_batch(self, model_id, produced_at="", factory=None):
        model = self._get_model(model_id)
        batch = Batch(
            batch_id=new_id("batch"),
            model_id=model_id,
            factory=factory or model.factory,
            produced_at=produced_at,
        )
        self.store.batches[batch.batch_id] = batch
        return batch

    def register_serials(self, batch_id, serial_numbers, store_id):
        batch = self.store.batches.get(batch_id)
        _require(batch is not None, "not_found", f"批次不存在: {batch_id}")
        created = []
        with self.store.lock:
            for serial_no in serial_numbers:
                _require(
                    serial_no not in self.store.serials,
                    "conflict",
                    f"序列已登记: {serial_no}",
                )
                unit = SerialUnit(
                    serial_no=serial_no,
                    batch_id=batch_id,
                    model_id=batch.model_id,
                    store_id=store_id,
                )
                unit.history.append(
                    {"at": now(), "action": SerialStatus.在库.value,
                     "store_id": store_id}
                )
                self.store.serials[serial_no] = unit
                created.append(unit)
        return created

    def transfer_store(self, transfer_id, serial_no, to_store):
        """跨店调拨：幂等，且不打断批次追踪与召回圈定。"""
        with self.store.lock:
            existing = self.store.transfers.get(transfer_id)
            if existing is not None:
                return existing, False  # 重复调拨请求原样返回
            unit = self.store.serials.get(serial_no)
            _require(unit is not None, "not_found", f"序列不存在: {serial_no}")
            _require(to_store != unit.store_id, "invalid", "调入店铺与当前店铺相同")
            record = TransferRecord(
                transfer_id=transfer_id,
                serial_no=serial_no,
                from_store=unit.store_id,
                to_store=to_store,
                moved_at=now(),
            )
            unit.store_id = to_store
            unit.history.append(
                {"at": record.moved_at, "action": "跨店调拨",
                 "from_store": record.from_store, "to_store": to_store,
                 "transfer_id": transfer_id}
            )
            self.store.transfers[transfer_id] = record
            return record, True

    # ---------- 订单 ----------

    def place_order(self, order_id, store_id, listing_id, consumer_id,
                    serial_numbers, unit_price):
        """下单（在途）；停售、召回或匹配失败的页面不得成交。"""
        with self.store.lock:
            if order_id in self.store.orders:
                return self.store.orders[order_id], False  # 订单号幂等
            listing = self._get_listing(listing_id)
            _require(
                listing.store_id == store_id, "invalid", "页面不属于该店铺"
            )
            _require(listing.status == "在售", "conflict", "商品已下架，不能下单")
            model = self._get_model(listing.model_id)
            _require(
                model.state in SELLABLE_STATES, "conflict",
                f"型号处于{model.state.value}，停售拦截",
            )
            for serial_no in serial_numbers:
                unit = self.store.serials.get(serial_no)
                _require(unit is not None, "not_found", f"序列不存在: {serial_no}")
                _require(
                    unit.model_id == listing.model_id, "conflict",
                    f"序列 {serial_no} 与页面对应型号不一致",
                )
                _require(
                    unit.status == SerialStatus.在库
                    and unit.store_id == store_id,
                    "conflict",
                    f"序列 {serial_no} 当前不可售",
                )
            for serial_no in serial_numbers:
                unit = self.store.serials[serial_no]
                unit.status = SerialStatus.在途
                unit.order_id = order_id
                unit.history.append(
                    {"at": now(), "action": SerialStatus.在途.value,
                     "order_id": order_id}
                )
            order = Order(
                order_id=order_id,
                store_id=store_id,
                listing_id=listing_id,
                consumer_id=consumer_id,
                serials=list(serial_numbers),
                unit_price=unit_price,
            )
            self.store.orders[order_id] = order
            return order, True

    def deliver_order(self, order_id):
        with self.store.lock:
            order = self.store.orders.get(order_id)
            _require(order is not None, "not_found", f"订单不存在: {order_id}")
            _require(order.status == "在途", "conflict", "订单不在在途状态")
            order.status = "已签收"
            order.delivered_at = now()
            for serial_no in order.serials:
                unit = self.store.serials[serial_no]
                unit.status = SerialStatus.已售
                unit.history.append(
                    {"at": order.delivered_at, "action": SerialStatus.已售.value,
                     "order_id": order_id}
                )
            return order

    # ---------- 样品流转与停售调查 ----------

    def record_sample_flow(self, sample_id, model_id, from_party, to_party, purpose):
        event = SampleFlowEvent(
            event_id=new_id("sample"),
            sample_id=sample_id,
            model_id=model_id,
            from_party=from_party,
            to_party=to_party,
            purpose=purpose,
            moved_at=now(),
        )
        self.store.sample_flows.append(event)
        return event

    def open_investigation(self, model_id, trigger, detail, sample_id=None):
        """抽检不合格、证书撤销或夸大防护宣传均可触发停售调查。"""
        trigger = InvestigationTrigger(trigger) if not isinstance(
            trigger, InvestigationTrigger
        ) else trigger
        with self.store.lock:
            model = self._get_model(model_id)
            if model.state not in SELLABLE_STATES:
                # 召回中/调查中/待核验不重复开单，返回该型号当前进行中的调查
                ongoing = next(
                    (inv for inv in self.store.investigations.values()
                     if inv.model_id == model_id and inv.status == "调查中"),
                    None,
                )
                _require(
                    ongoing is not None, "conflict",
                    f"型号处于{model.state.value}，不能发起调查",
                )
                return ongoing, False
            investigation = Investigation(
                investigation_id=new_id("inv"),
                model_id=model_id,
                trigger=trigger,
                detail=detail,
            )
            self.store.investigations[investigation.investigation_id] = investigation
            self._transition(model, ModelState.停售调查, f"{trigger.value}: {detail}")
            self._take_down_listings(model_id, reason=f"停售调查（{trigger.value}）")
            if sample_id:
                self.record_sample_flow(
                    sample_id, model_id, "市场抽检", "检测机构", trigger.value
                )
            return investigation, True

    def close_investigation(self, investigation_id, outcome, recall=False,
                            batch_ids=None, reason=""):
        """调查结案：解除停售恢复销售，或升级正式召回，或退市。"""
        with self.store.lock:
            inv = self.store.investigations.get(investigation_id)
            _require(inv is not None, "not_found", "调查不存在")
            _require(inv.status == "调查中", "conflict", "调查已结案")
            model = self._get_model(inv.model_id)
            inv.status = "已结案"
            inv.closed_at = now()
            inv.outcome = outcome
            if recall:
                created = self._start_recall_locked(
                    model, batch_ids or [], reason or inv.detail,
                    investigation_id=inv.investigation_id,
                )
                return created
            if outcome == "无召回退市":
                self._transition(model, ModelState.已退市, "调查结案：退市")
            else:
                self._transition(model, ModelState.恢复销售, f"调查结案：{outcome}")
                self._rematch_listings(model.model_id)
            return inv

    # ---------- 正式召回 ----------

    def start_recall(self, model_id, batch_ids, reason, investigation_id=None):
        with self.store.lock:
            model = self._get_model(model_id)
            return self._start_recall_locked(
                model, batch_ids, reason, investigation_id
            )

    def _start_recall_locked(self, model, batch_ids, reason, investigation_id):
        if model.state != ModelState.召回中:
            self._transition(model, ModelState.召回中, reason)
        if batch_ids:
            for batch_id in batch_ids:
                batch = self.store.batches.get(batch_id)
                _require(batch is not None, "not_found", f"批次不存在: {batch_id}")
                _require(
                    batch.model_id == model.model_id, "invalid",
                    f"批次 {batch_id} 不属于该型号",
                )
        scoped = [
            unit.serial_no
            for unit in self.store.serials.values()
            if unit.model_id == model.model_id
            and (not batch_ids or unit.batch_id in batch_ids)
        ]
        in_transit = [
            order.order_id
            for order in self.store.orders.values()
            if order.status == "在途"
            and any(serial in scoped for serial in order.serials)
        ]
        recall = Recall(
            recall_id=new_id("recall"),
            model_id=model.model_id,
            investigation_id=investigation_id,
            batch_ids=list(batch_ids),
            reason=reason,
            scoped_serials=scoped,
            in_transit_orders=in_transit,
        )
        self.store.recalls[recall.recall_id] = recall
        self._take_down_listings(model.model_id, reason=f"正式召回: {reason}")
        self._notify_scoped_consumers(recall)
        return recall

    def _take_down_listings(self, model_id, reason):
        for listing in self.store.listings.values():
            if listing.model_id == model_id and listing.status == "在售":
                listing.status = "下架"
                listing.matched = False
                self.store.revisions.append(
                    ListingRevision(
                        revision_id=new_id("rev"),
                        listing_id=listing.listing_id,
                        revision=listing.revision + 1,
                        title=listing.title,
                        claimed_vehicles=list(listing.claimed_vehicles),
                        matched=False,
                        mismatches=[reason],
                        checked_at=now(),
                    )
                )
                listing.revision += 1

    def _rematch_listings(self, model_id):
        for listing in self.store.listings.values():
            if listing.model_id != model_id:
                continue
            model = self.store.models[model_id]
            matched, mismatches = self._match_listing(
                model, listing.title, listing.claimed_vehicles
            )
            self._record_revision(
                listing, listing.title, listing.claimed_vehicles,
                matched, mismatches,
            )
            listing.status = "在售" if matched else "下架"

    def _notify_scoped_consumers(self, recall):
        """圈定后向在途拦截订单与已售序列对应消费者发出召回通知。"""
        order_ids = set(recall.in_transit_orders)
        for serial_no in recall.scoped_serials:
            unit = self.store.serials.get(serial_no)
            if unit and unit.order_id:
                order_ids.add(unit.order_id)
        for order_id in order_ids:
            order = self.store.orders.get(order_id)
            if not order:
                continue
            notification = Notification(
                notification_id=new_id("note"),
                recall_id=recall.recall_id,
                order_id=order_id,
                consumer_id=order.consumer_id,
            )
            self.store.notifications[notification.notification_id] = notification

    # ---------- 召回处置：退货 / 换货 / 销毁 / 复检恢复 ----------

    def _disposal(self, recall, serial_no, action, operator, note, key_extra=""):
        unit = self.store.serials.get(serial_no)
        _require(unit is not None, "not_found", f"序列不存在: {serial_no}")
        _require(
            serial_no in recall.scoped_serials, "invalid",
            "该序列不在召回范围",
        )
        idem_key = (recall.recall_id, serial_no, action.value + key_extra)
        existing_id = self.store.disposal_keys.get(idem_key)
        if existing_id is not None:
            existing = next(
                d for d in self.store.disposals if d.disposal_id == existing_id
            )
            return existing, False  # 重复回调/重复提交不重复处置、不重复赔
        _require(
            serial_no not in recall.disposed, "conflict",
            "该序列已有终态处置",
        )
        _require(
            unit.status not in TERMINAL_SERIAL_STATUSES, "conflict",
            f"序列已处于终态: {unit.status.value}",
        )
        record = DisposalRecord(
            disposal_id=new_id("disp"),
            recall_id=recall.recall_id,
            serial_no=serial_no,
            action=action,
            operator=operator,
            note=note,
            processed_at=now(),
        )
        self.store.disposals.append(record)
        self.store.disposal_keys[idem_key] = record.disposal_id
        recall.disposed[serial_no] = record.disposal_id
        return record, True

    def dispose_return(self, recall_id, serial_no, operator="平台", note=""):
        """退货：已售序列退款入赔付台账（每召回每序列仅一笔）；在途拦截不赔付。"""
        with self.store.lock:
            recall = self._get_recall(recall_id)
            _require(
                recall.status == "召回中" and not recall.lifted, "conflict",
                "召回已解除，不能再按召回退货",
            )
            record, created = self._disposal(
                recall, serial_no, DisposalType.退货, operator, note
            )
            compensation = None
            if created:
                unit = self.store.serials[serial_no]
                unit.status = SerialStatus.已退货
                unit.history.append(
                    {"at": record.processed_at,
                     "action": SerialStatus.已退货.value,
                     "disposal_id": record.disposal_id}
                )
                if unit.order_id:
                    order = self.store.orders[unit.order_id]
                    if order.status == "已签收":
                        compensation = self._book_compensation(
                            recall, order, serial_no
                        )
            else:
                comp = self.store.compensations.get((recall_id, serial_no))
                compensation = comp
            return record, compensation

    def _book_compensation(self, recall, order, serial_no):
        key = (recall.recall_id, serial_no)
        if key in self.store.compensations:
            return self.store.compensations[key]  # 台账唯一，防多赔
        entry = CompensationEntry(
            entry_id=new_id("comp"),
            recall_id=recall.recall_id,
            order_id=order.order_id,
            serial_no=serial_no,
            amount=order.unit_price,
        )
        self.store.compensations[key] = entry
        return entry

    def mark_compensation_paid(self, entry_id):
        with self.store.lock:
            entry = next(
                (e for e in self.store.compensations.values()
                 if e.entry_id == entry_id),
                None,
            )
            _require(entry is not None, "not_found", "赔付记录不存在")
            if entry.status != "已赔付":
                entry.status = "已赔付"
                entry.paid_at = now()
            return entry

    def dispose_exchange(self, recall_id, serial_no, new_serial_no=None,
                         operator="平台", note=""):
        """换货：问题序列入已换货终态；新品须属同型号且不在未解除的召回范围。"""
        with self.store.lock:
            recall = self._get_recall(recall_id)
            _require(
                recall.status == "召回中", "conflict", "召回不在进行中"
            )
            record, created = self._disposal(
                recall, serial_no, DisposalType.换货, operator, note
            )
            if created:
                unit = self.store.serials[serial_no]
                unit.status = SerialStatus.已换货
                unit.history.append(
                    {"at": record.processed_at,
                     "action": SerialStatus.已换货.value,
                     "disposal_id": record.disposal_id}
                )
                if new_serial_no:
                    new_unit = self.store.serials.get(new_serial_no)
                    _require(
                        new_unit is not None, "not_found",
                        f"替换序列不存在: {new_serial_no}",
                    )
                    _require(
                        new_unit.model_id == unit.model_id, "invalid",
                        "替换序列型号不一致",
                    )
                    _require(
                        new_serial_no not in recall.scoped_serials
                        or recall.lifted,
                        "conflict",
                        "替换序列仍在召回范围内",
                    )
                    new_unit.history.append(
                        {"at": now(), "action": "换货发出",
                         "for_serial": serial_no}
                    )
            return record

    def dispose_destroy(self, recall_id, serial_no, operator="监管人员", note=""):
        """销毁：监管监督下的终态处置，逐序列留痕。"""
        with self.store.lock:
            recall = self._get_recall(recall_id)
            _require(
                recall.status == "召回中" and not recall.lifted, "conflict",
                "召回已解除",
            )
            record, created = self._disposal(
                recall, serial_no, DisposalType.销毁, operator, note
            )
            if created:
                unit = self.store.serials[serial_no]
                unit.status = SerialStatus.已销毁
                unit.history.append(
                    {"at": record.processed_at,
                     "action": SerialStatus.已销毁.value,
                     "disposal_id": record.disposal_id}
                )
            return record

    def reinspect_recover(self, recall_id, sample_id=None, operator="检测机构",
                          note="复检合格"):
        """复检恢复：留存复检恢复记录、解除召回约束，型号恢复销售并重新匹配页面。"""
        with self.store.lock:
            recall = self._get_recall(recall_id)
            _require(
                recall.status == "召回中", "conflict", "召回不在进行中"
            )
            model = self._get_model(recall.model_id)
            record = DisposalRecord(
                disposal_id=new_id("disp"),
                recall_id=recall_id,
                serial_no="",
                action=DisposalType.复检恢复,
                operator=operator,
                note=note,
                processed_at=now(),
            )
            self.store.disposals.append(record)
            recall.lifted = True
            recall.status = "已恢复"
            recall.completed_at = now()
            if sample_id:
                self.record_sample_flow(
                    sample_id, recall.model_id, "生产者留样", "检测机构", "复检"
                )
            self._transition(model, ModelState.恢复销售, f"复检恢复: {note}")
            self._rematch_listings(model.model_id)
            return record

    def complete_recall(self, recall_id):
        """全部圈定序列完成终态处置后结案。"""
        with self.store.lock:
            recall = self._get_recall(recall_id)
            pending = [
                s for s in recall.scoped_serials
                if self.store.serials[s].status not in TERMINAL_SERIAL_STATUSES
            ]
            _require(not pending, "conflict",
                     f"仍有 {len(pending)} 个序列未处置，不能结案")
            recall.status = "已完成"
            recall.completed_at = now()
            self._transition(
                self._get_model(recall.model_id), ModelState.已退市, "召回完成退市"
            )
            return recall

    # ---------- 通知送达 ----------

    def send_notification(self, notification_id):
        note = self.store.notifications.get(notification_id)
        _require(note is not None, "not_found", "通知不存在")
        if note.status == "待发送":
            note.status = "已发送"
            note.sent_at = now()
        return note

    def deliver_notification(self, notification_id):
        note = self.store.notifications.get(notification_id)
        _require(note is not None, "not_found", "通知不存在")
        _require(note.status == "已发送", "conflict", "通知尚未发送")
        note.status = "已送达"
        note.delivered_at = now()
        return note

    # ---------- 平台回调（幂等分发） ----------

    def platform_callback(self, callback_id, event, payload):
        """平台/机构回调：同一 callback_id 重复投递只处理一次。"""
        with self.store.lock:
            existing = self.store.callbacks.get(callback_id)
            if existing is not None:
                return existing, False
            result = self._dispatch_event(event, payload)
            receipt = CallbackReceipt(
                callback_id=callback_id,
                event=event,
                result=result,
                processed_at=now(),
            )
            self.store.callbacks[callback_id] = receipt
            return receipt, True

    def _dispatch_event(self, event, payload):
        if event == "certificate_revoked":
            return self._evt_certificate_revoked(payload)
        if event == "inspection_failed":
            inv, _ = self.open_investigation(
                payload["model_id"], InvestigationTrigger.抽检不合格,
                payload.get("detail", "抽检不合格"),
                sample_id=payload.get("sample_id"),
            )
            return {"investigation_id": inv.investigation_id}
        if event == "exaggerated_claim":
            listing = self._get_listing(payload["listing_id"])
            inv, _ = self.open_investigation(
                listing.model_id, InvestigationTrigger.夸大宣传,
                payload.get("detail", f"页面 {listing.listing_id} 夸大防护宣传"),
            )
            return {"investigation_id": inv.investigation_id}
        if event == "order_return":
            record, compensation = self.dispose_return(
                payload["recall_id"], payload["serial_no"],
                operator=payload.get("operator", "平台回调"),
            )
            return {
                "disposal_id": record.disposal_id,
                "compensation_id": compensation.entry_id if compensation else None,
            }
        raise DomainError("invalid", f"未知回调事件: {event}")

    def _evt_certificate_revoked(self, payload):
        cert_no = payload["certificate_no"]
        affected = [
            m for m in self.store.models.values()
            if m.certificate_no == cert_no
        ]
        investigation_ids = []
        for model in affected:
            self.verify_model(model.model_id)  # 留存撤销状态核验快照
            if model.state in SELLABLE_STATES:
                inv, created = self.open_investigation(
                    model.model_id, InvestigationTrigger.证书撤销,
                    payload.get("detail", "可信接口反馈证书已撤销"),
                )
                if created:
                    investigation_ids.append(inv.investigation_id)
        return {"certificate_no": cert_no,
                "investigation_ids": investigation_ids,
                "affected_models": [m.model_id for m in affected]}

    # ---------- 消费者扫码（最小必要视图） ----------

    def consumer_scan(self, serial_no):
        unit = self.store.serials.get(serial_no)
        if unit is None:
            return {
                "serial_no": serial_no,
                "authenticity": "未查询到登记信息",
                "applicable_vehicles": [],
                "disposition_entry": None,
            }
        model = self.store.models[unit.model_id]
        snapshot = self._latest_snapshot(model.model_id)
        view = {
            "serial_no": serial_no,
            "authenticity": "真品" if snapshot and snapshot.matched else "认证存疑",
            "applicable_vehicles": list(model.applicable_vehicles),
            "disposition_entry": None,
        }
        active_recall = next(
            (r for r in self.store.recalls.values()
             if r.model_id == model.model_id and r.status == "召回中"),
            None,
        )
        if unit.status in TERMINAL_SERIAL_STATUSES:
            view["disposition_entry"] = {
                "status": f"已完成{unit.status.value}",
                "message": "该产品已完成处置，无需重复申请",
            }
        elif active_recall and serial_no in active_recall.scoped_serials:
            view["disposition_entry"] = {
                "status": "召回中",
                "recall_id": active_recall.recall_id,
                "reason": active_recall.reason,
                "action_url": f"/recalls/{active_recall.recall_id}/claim",
            }
        elif model.state == ModelState.停售调查:
            view["disposition_entry"] = {
                "status": "核查中",
                "message": "该型号正在接受核查，请关注后续通知",
            }
        return view

    # ---------- 监管视图 ----------

    def regulator_cert_reuse(self):
        """证书复用关系：同一证书被多个型号/主体套用的证据链。"""
        groups = {}
        for model in self.store.models.values():
            groups.setdefault(model.certificate_no, []).append(model)
        result = []
        for cert_no, models in groups.items():
            snapshots = {}
            for m in models:
                snap = self._latest_snapshot(m.model_id)
                if snap:
                    snapshots[m.model_id] = snap
            reuse_signals = []
            registered_names = {s.cert_model_name for s in snapshots.values()}
            registered_holders = {s.cert_holder for s in snapshots.values()}
            if len({m.model_name for m in models}) > 1 and len(models) > 1:
                reuse_signals.append("一张证书绑定多个不同型号名称")
            if len({self.store.producers[m.producer_id].name for m in models}) > 1:
                reuse_signals.append("一张证书被不同生产主体共用")
            for m in models:
                snap = snapshots.get(m.model_id)
                if snap and (
                    snap.cert_model_name != m.model_name
                    or snap.cert_holder
                    != self.store.producers[m.producer_id].name
                ):
                    reuse_signals.append(f"{m.model_id} 型号或主体与证书登记不符")
            result.append({
                "certificate_no": cert_no,
                "registered_model_name": next(iter(registered_names), ""),
                "registered_holder": next(iter(registered_holders), ""),
                "model_count": len(models),
                "models": [{
                    "model_id": m.model_id,
                    "model_name": m.model_name,
                    "producer": self.store.producers[m.producer_id].name,
                    "factory": m.factory,
                    "state": m.state.value,
                } for m in models],
                "reuse_signals": reuse_signals,
            })
        return result

    def regulator_sample_trace(self, sample_id=None):
        events = self.store.sample_flows
        if sample_id:
            events = [e for e in events if e.sample_id == sample_id]
        return [to_dict(e) for e in events]

    def regulator_notifications(self, recall_id=None):
        notes = list(self.store.notifications.values())
        if recall_id:
            notes = [n for n in notes if n.recall_id == recall_id]
        return [{
            "notification_id": n.notification_id,
            "recall_id": n.recall_id,
            "order_id": n.order_id,
            "consumer_id": n.consumer_id,
            "channel": n.channel,
            "status": n.status,
            "sent_at": n.sent_at,
            "delivered_at": n.delivered_at,
        } for n in notes]

    def regulator_recall_unrecovered(self, recall_id=None):
        """仍未召回的去向：未终态处置序列当前所在店铺/订单与通知状态。"""
        report = []
        recalls = self.store.recalls.values()
        if recall_id:
            recalls = [self._get_recall(recall_id)]
        for recall in recalls:
            pending = []
            for serial_no in recall.scoped_serials:
                unit = self.store.serials[serial_no]
                if unit.status in TERMINAL_SERIAL_STATUSES:
                    continue
                pending.append({
                    "serial_no": serial_no,
                    "batch_id": unit.batch_id,
                    "current_store": unit.store_id,
                    "status": unit.status.value,
                    "order_id": unit.order_id,
                    "transferred": any(
                        h.get("action") == "跨店调拨" for h in unit.history
                    ),
                })
            report.append({
                "recall_id": recall.recall_id,
                "model_id": recall.model_id,
                "lifted": recall.lifted,
                "scoped_total": len(recall.scoped_serials),
                "disposed_total": len(recall.disposed),
                "pending_total": len(pending),
                "pending": pending,
            })
        return report

    # ---------- 序列化辅助 ----------

    def public_model(self, model):
        data = to_dict(model)
        data["state"] = model.state.value
        data["latest_snapshot"] = (
            to_dict(self._latest_snapshot(model.model_id))
        )
        return data
