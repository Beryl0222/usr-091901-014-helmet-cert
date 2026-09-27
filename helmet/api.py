"""HTTP 接口层：路由、幂等键、统一错误映射。"""

import json
import re
from http.server import BaseHTTPRequestHandler

from .domain import DomainError, to_dict

ERROR_STATUS = {"invalid": 400, "not_found": 404, "conflict": 409}


def _pattern(path):
    """把 /models/{id} 形式编译为正则。"""
    return re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "$")


class Router:
    def __init__(self, service):
        self.service = service
        self.routes = []
        self._register()

    def _register(self):
        add = self.add
        add("POST", "/producers", self.create_producer)
        add("POST", "/models", self.submit_model)
        add("GET", "/models/{model_id}", self.get_model)
        add("POST", "/models/{model_id}/verify", self.verify_model)
        add("POST", "/listings", self.create_listing)
        add("PUT", "/listings/{listing_id}", self.update_listing)
        add("GET", "/listings/{listing_id}", self.get_listing)
        add("POST", "/models/{model_id}/batches", self.create_batch)
        add("POST", "/batches/{batch_id}/serials", self.register_serials)
        add("POST", "/transfers", self.transfer)
        add("POST", "/orders", self.place_order)
        add("POST", "/orders/{order_id}/deliver", self.deliver_order)
        add("POST", "/investigations", self.open_investigation)
        add("POST", "/investigations/{investigation_id}/close", self.close_investigation)
        add("POST", "/recalls", self.start_recall)
        add("GET", "/recalls/{recall_id}", self.get_recall)
        add("POST", "/recalls/{recall_id}/returns", self.dispose_return)
        add("POST", "/recalls/{recall_id}/exchanges", self.dispose_exchange)
        add("POST", "/recalls/{recall_id}/destructions", self.dispose_destroy)
        add("POST", "/recalls/{recall_id}/reinspect", self.reinspect)
        add("POST", "/recalls/{recall_id}/complete", self.complete_recall)
        add("POST", "/compensations/{entry_id}/pay", self.pay_compensation)
        add("POST", "/notifications/{notification_id}/send", self.send_notification)
        add("POST", "/notifications/{notification_id}/deliver", self.deliver_notification)
        add("POST", "/callbacks", self.platform_callback)
        add("POST", "/samples/flows", self.record_sample_flow)
        add("GET", "/scan/{serial_no}", self.consumer_scan)
        add("GET", "/regulator/cert-reuse", self.cert_reuse)
        add("GET", "/regulator/sample-trace", self.sample_trace)
        add("GET", "/regulator/notifications", self.regulator_notifications)
        add("GET", "/regulator/recall-unrecovered", self.unrecovered)

    def add(self, method, path, handler):
        self.routes.append((method, _pattern(path), handler))

    # ---------- 请求分发 ----------

    def dispatch(self, method, path, body, idempotency_key=None):
        if idempotency_key and method in ("POST", "PUT"):
            cache_key = (method, path, idempotency_key)
            if cache_key in self.service.store.idempotency:
                return self.service.store.idempotency[cache_key]
            result = self._invoke(method, path, body)
            self.service.store.idempotency[cache_key] = result
            return result
        return self._invoke(method, path, body)

    def _invoke(self, method, path, body):
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                return handler(body or {}, **match.groupdict())
        raise DomainError("not_found", f"接口不存在: {method} {path}")

    # ---------- 各端点 ----------

    def create_producer(self, body):
        producer = self.service.register_producer(
            name=body.get("name", ""), contact=body.get("contact", "")
        )
        return 201, to_dict(producer)

    def submit_model(self, body):
        model = self.service.submit_model(
            producer_id=body.get("producer_id", ""),
            model_name=body.get("model_name", ""),
            material_structure=body.get("material_structure", ""),
            factory=body.get("factory", ""),
            certificate_no=body.get("certificate_no", ""),
            applicable_vehicles=body.get("applicable_vehicles", []),
            test_reports=body.get("test_reports", []),
        )
        return 201, self.service.public_model(model)

    def get_model(self, body, model_id):
        return 200, self.service.public_model(self.service._get_model(model_id))

    def verify_model(self, body, model_id):
        snapshot = self.service.verify_model(model_id)
        model = self.service._get_model(model_id)
        return 200, {"snapshot": to_dict(snapshot), "model_state": model.state.value}

    def create_listing(self, body):
        listing = self.service.create_listing(
            store_id=body.get("store_id", ""),
            model_id=body.get("model_id", ""),
            title=body.get("title", ""),
            claimed_vehicles=body.get("claimed_vehicles", []),
        )
        return 201, to_dict(listing)

    def update_listing(self, body, listing_id):
        listing = self.service.update_listing(
            listing_id,
            title=body.get("title"),
            claimed_vehicles=body.get("claimed_vehicles"),
            model_id=body.get("model_id"),
        )
        return 200, to_dict(listing)

    def get_listing(self, body, listing_id):
        listing = self.service._get_listing(listing_id)
        revisions = [
            to_dict(r) for r in self.service.store.revisions
            if r.listing_id == listing_id
        ]
        return 200, {"listing": to_dict(listing), "revisions": revisions}

    def create_batch(self, body, model_id):
        batch = self.service.register_batch(
            model_id, produced_at=body.get("produced_at", ""),
            factory=body.get("factory"),
        )
        return 201, to_dict(batch)

    def register_serials(self, body, batch_id):
        units = self.service.register_serials(
            batch_id, body.get("serial_numbers", []), body.get("store_id", "")
        )
        return 201, {"registered": [to_dict(u) for u in units]}

    def transfer(self, body):
        record, created = self.service.transfer_store(
            transfer_id=body.get("transfer_id", ""),
            serial_no=body.get("serial_no", ""),
            to_store=body.get("to_store", ""),
        )
        return (201 if created else 200), {
            "transfer": to_dict(record), "created": created,
        }

    def place_order(self, body):
        order, created = self.service.place_order(
            order_id=body.get("order_id", ""),
            store_id=body.get("store_id", ""),
            listing_id=body.get("listing_id", ""),
            consumer_id=body.get("consumer_id", ""),
            serial_numbers=body.get("serial_numbers", []),
            unit_price=float(body.get("unit_price", 0)),
        )
        return (201 if created else 200), {
            "order": to_dict(order), "created": created,
        }

    def deliver_order(self, body, order_id):
        return 200, to_dict(self.service.deliver_order(order_id))

    def open_investigation(self, body):
        inv, created = self.service.open_investigation(
            model_id=body.get("model_id", ""),
            trigger=body.get("trigger", ""),
            detail=body.get("detail", ""),
            sample_id=body.get("sample_id"),
        )
        return (201 if created else 200), {
            "investigation": to_dict(inv), "created": created,
        }

    def close_investigation(self, body, investigation_id):
        result = self.service.close_investigation(
            investigation_id,
            outcome=body.get("outcome", "解除停售"),
            recall=bool(body.get("recall", False)),
            batch_ids=body.get("batch_ids"),
            reason=body.get("reason", ""),
        )
        return 200, to_dict(result)

    def start_recall(self, body):
        recall = self.service.start_recall(
            model_id=body.get("model_id", ""),
            batch_ids=body.get("batch_ids", []),
            reason=body.get("reason", ""),
            investigation_id=body.get("investigation_id"),
        )
        return 201, to_dict(recall)

    def get_recall(self, body, recall_id):
        recall = self.service._get_recall(recall_id)
        data = to_dict(recall)
        data["compensations"] = [
            to_dict(e) for e in self.service.store.compensations.values()
            if e.recall_id == recall_id
        ]
        data["disposals"] = [
            to_dict(d) for d in self.service.store.disposals
            if d.recall_id == recall_id
        ]
        return 200, data

    def dispose_return(self, body, recall_id):
        record, compensation = self.service.dispose_return(
            recall_id, body.get("serial_no", ""),
            operator=body.get("operator", "平台"), note=body.get("note", ""),
        )
        return 200, {
            "disposal": to_dict(record),
            "compensation": to_dict(compensation) if compensation else None,
        }

    def dispose_exchange(self, body, recall_id):
        record = self.service.dispose_exchange(
            recall_id, body.get("serial_no", ""),
            new_serial_no=body.get("new_serial_no"),
            operator=body.get("operator", "平台"), note=body.get("note", ""),
        )
        return 200, to_dict(record)

    def dispose_destroy(self, body, recall_id):
        record = self.service.dispose_destroy(
            recall_id, body.get("serial_no", ""),
            operator=body.get("operator", "监管人员"), note=body.get("note", ""),
        )
        return 200, to_dict(record)

    def reinspect(self, body, recall_id):
        record = self.service.reinspect_recover(
            recall_id, sample_id=body.get("sample_id"),
            operator=body.get("operator", "检测机构"),
            note=body.get("note", "复检合格"),
        )
        return 200, to_dict(record)

    def complete_recall(self, body, recall_id):
        return 200, to_dict(self.service.complete_recall(recall_id))

    def pay_compensation(self, body, entry_id):
        return 200, to_dict(self.service.mark_compensation_paid(entry_id))

    def send_notification(self, body, notification_id):
        return 200, to_dict(self.service.send_notification(notification_id))

    def deliver_notification(self, body, notification_id):
        return 200, to_dict(self.service.deliver_notification(notification_id))

    def platform_callback(self, body):
        receipt, created = self.service.platform_callback(
            callback_id=body.get("callback_id", ""),
            event=body.get("event", ""),
            payload=body.get("payload", {}),
        )
        return (200 if created else 200), {
            "receipt": to_dict(receipt), "processed": created,
        }

    def record_sample_flow(self, body):
        event = self.service.record_sample_flow(
            sample_id=body.get("sample_id", ""),
            model_id=body.get("model_id", ""),
            from_party=body.get("from_party", ""),
            to_party=body.get("to_party", ""),
            purpose=body.get("purpose", ""),
        )
        return 201, to_dict(event)

    def consumer_scan(self, body, serial_no):
        return 200, self.service.consumer_scan(serial_no)

    def cert_reuse(self, body):
        return 200, {"certificates": self.service.regulator_cert_reuse()}

    def sample_trace(self, body):
        return 200, {"flows": self.service.regulator_sample_trace()}

    def regulator_notifications(self, body):
        return 200, {"notifications": self.service.regulator_notifications()}

    def unrecovered(self, body):
        return 200, {"recalls": self.service.regulator_recall_unrecovered()}


def make_handler(service, base_handler=None, extra_gets=None):
    """构造绑定领域服务的 HTTP 处理器；extra_gets 注入 /health 等只读端点。"""
    router = Router(service)
    Base = base_handler or BaseHTTPRequestHandler
    extra_gets = extra_gets or {}

    class ApiHandler(Base):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in extra_gets:
                self._send_json(extra_gets[path]())
                return
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def _handle(self, method):
            path = self.path.split("?", 1)[0]
            try:
                body = {}
                if method in ("POST", "PUT"):
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                status, payload = router.dispatch(
                    method, path, body,
                    idempotency_key=self.headers.get("Idempotency-Key"),
                )
            except DomainError as error:
                status, payload = ERROR_STATUS[error.kind], {
                    "error": error.message, "kind": error.kind,
                }
            except (json.JSONDecodeError, ValueError) as error:
                status, payload = 400, {"error": f"请求格式错误: {error}"}
            self._send_json(payload, status=status)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return ApiHandler
