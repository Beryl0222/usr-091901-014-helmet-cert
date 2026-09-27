"""HTTP 接口层:把领域服务暴露为 JSON 接口,供 service.py 挂载。"""

import re

from helmet_core import DomainError


def route(service, method, path, body):
    """按路径分发到领域服务;无匹配返回 None,由调用方回 404。"""
    path = path.split("?", 1)[0].rstrip("/") or "/"
    try:
        return _dispatch(service, method, path, body or {})
    except DomainError as error:
        return error.status, {"error": str(error)}


def _dispatch(service, method, path, body):
    get, post = method == "GET", method == "POST"

    def parts(pattern):
        match = re.fullmatch(pattern, path)
        return match.groups() if match else None

    # 生产者:型号提交与证书核验
    if post and path == "/api/producers":
        return 200, service.register_producer(body.get("name", ""),
                                              body.get("credit_code", ""))
    if post and path == "/api/models":
        return 200, service.submit_model(
            body.get("producer_id", ""), body.get("name", ""),
            body.get("material", ""), body.get("structure", ""),
            body.get("factory", ""), body.get("applicable_vehicles", []),
            body.get("cert_number", ""), body.get("test_report", ""))
    if post and (m := parts(r"/api/models/([\w-]+)/verify")):
        return 200, service.verify_model(m[0])
    if get and (m := parts(r"/api/models/([\w-]+)")):
        return 200, service._model_view(service._model(m[0]))

    # 销售平台:商品页面,每次变更重新匹配
    if post and path == "/api/listings":
        return 200, service.create_listing(body.get("shop_id", ""),
                                           body.get("model_id", ""),
                                           body.get("title", ""),
                                           body.get("declared_vehicles", []))
    if post and (m := parts(r"/api/listings/([\w-]+)/revise")):
        return 200, service.revise_listing(m[0], body.get("title"),
                                           body.get("declared_vehicles"))
    if get and (m := parts(r"/api/listings/([\w-]+)")):
        return 200, service._listing_view(service._listing(m[0]))

    # 销售平台:批次与订单
    if post and path == "/api/batches":
        return 200, service.register_batch(body.get("model_id", ""),
                                           body.get("factory", ""),
                                           body.get("serials", []))
    if post and path == "/api/orders":
        return 200, service.create_order(body.get("listing_id", ""),
                                         body.get("serial_no", ""),
                                         body.get("buyer", ""))

    # 停售调查(与正式召回分别记录)
    if post and path == "/api/investigations":
        return 200, service.open_investigation(body.get("trigger", ""),
                                               body.get("model_id"),
                                               body.get("listing_id"),
                                               body.get("detail", ""))
    if post and (m := parts(r"/api/investigations/([\w-]+)/close")):
        return 200, service.close_investigation(m[0], body.get("outcome", ""),
                                                body.get("batch_ids"),
                                                body.get("reason", ""))

    # 正式召回与处置留痕
    if post and path == "/api/recalls":
        return 200, service.open_recall(body.get("model_id", ""),
                                        body.get("batch_ids", []),
                                        body.get("reason", ""))
    if get and (m := parts(r"/api/recalls/([\w-]+)")):
        return 200, service._recall_view(service._recall(m[0]))
    if post and (m := parts(r"/api/recalls/([\w-]+)/dispositions")):
        return 200, service.record_disposition(m[0], body.get("serial_no", ""),
                                               body.get("type", ""),
                                               body.get("actor", ""),
                                               body.get("note", ""))
    if post and (m := parts(r"/api/recalls/([\w-]+)/reinspect")):
        return 200, service.reinspect_restore(m[0], body.get("batch_id", ""),
                                              body.get("report", ""))

    # 平台回调(幂等)与跨店调拨
    if post and path == "/api/callbacks":
        return 200, service.handle_callback(body.get("callback_id", ""),
                                            body.get("type", ""),
                                            body.get("payload"))
    if post and path == "/api/transfers":
        return 200, service.transfer_serial(body.get("transfer_id", ""),
                                            body.get("serial_no", ""),
                                            body.get("to_shop", ""))

    # 样品流转
    if post and path == "/api/samples/dispatch":
        return 200, service.dispatch_sample(
            body.get("sample_id", ""), body.get("model_id", ""),
            body.get("batch_id", ""), body.get("from_party", ""),
            body.get("to_party", ""), body.get("purpose", ""),
            body.get("note", ""))
    if post and (m := parts(r"/api/samples/([\w-]+)/advance")):
        return 200, service.advance_sample(m[0], body.get("to_party", ""),
                                           body.get("note", ""))

    # 消费者扫码:只看必要信息
    if get and (m := parts(r"/api/consumer/scan/([\w-]+)")):
        return 200, service.consumer_scan(m[0])

    # 监管视图
    if get and (m := parts(r"/api/regulator/certs/([\w-]+)/reuse")):
        return 200, service.cert_reuse(m[0])
    if get and (m := parts(r"/api/regulator/samples/([\w-]+)")):
        return 200, service.sample_flow(m[0])
    if get and (m := parts(r"/api/regulator/recalls/([\w-]+)/outstanding")):
        return 200, service.recall_outstanding(m[0])
    if get and (m := parts(r"/api/regulator/recalls/([\w-]+)/notifications")):
        return 200, service.recall_notifications(m[0])
    if get and path == "/api/regulator/events":
        return 200, {"events": service.event_log()}

    return None
