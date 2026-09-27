"""领域模型：型号档案、证书核验快照、商品页面、批次序列、调查与召回。

状态机与领域契约 domain_contract.json 保持一致：
待核验 → 允许销售 → 停售调查 → 召回中 → 恢复销售 / 已退市。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum


def new_id(prefix):
    """生成带前缀的业务标识。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now():
    """当前时间戳。"""
    return time.time()


def to_dict(obj):
    """领域对象转 JSON 可序列化结构。"""
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {key: to_dict(value) for key, value in obj.items()}
    return obj


class DomainError(Exception):
    """业务规则冲突；kind 映射 HTTP 状态：invalid→400, not_found→404, conflict→409。"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


class ModelState(str, Enum):
    """型号档案状态机，取值与领域契约一致。"""

    待核验 = "待核验"
    允许销售 = "允许销售"
    停售调查 = "停售调查"
    召回中 = "召回中"
    恢复销售 = "恢复销售"
    已退市 = "已退市"


MODEL_TRANSITIONS = {
    ModelState.待核验: {ModelState.允许销售, ModelState.已退市},
    ModelState.允许销售: {ModelState.停售调查, ModelState.召回中, ModelState.已退市},
    ModelState.停售调查: {ModelState.恢复销售, ModelState.召回中, ModelState.已退市},
    ModelState.召回中: {ModelState.恢复销售, ModelState.已退市},
    ModelState.恢复销售: {ModelState.停售调查, ModelState.召回中, ModelState.已退市},
    ModelState.已退市: set(),
}


class SerialStatus(str, Enum):
    """单品序列状态；已退货/已换货/已销毁为终态。"""

    在库 = "在库"
    在途 = "在途"
    已售 = "已售"
    已退货 = "已退货"
    已换货 = "已换货"
    已销毁 = "已销毁"


TERMINAL_SERIAL_STATUSES = frozenset(
    {SerialStatus.已退货, SerialStatus.已换货, SerialStatus.已销毁}
)


class InvestigationTrigger(str, Enum):
    """停售调查触发原因。"""

    抽检不合格 = "抽检不合格"
    证书撤销 = "证书撤销"
    夸大宣传 = "夸大宣传"


class DisposalType(str, Enum):
    """召回处置方式，分别留痕。"""

    退货 = "退货"
    换货 = "换货"
    销毁 = "销毁"
    复检恢复 = "复检恢复"


@dataclass
class Producer:
    """生产者档案。"""

    producer_id: str
    name: str
    contact: str = ""


@dataclass
class HelmetModel:
    """型号档案：生产者提交的型号、材料结构、工厂、证书编号与检测报告。"""

    model_id: str
    producer_id: str
    model_name: str
    material_structure: str
    factory: str
    certificate_no: str
    applicable_vehicles: list
    test_reports: list = field(default_factory=list)
    state: ModelState = ModelState.待核验
    version: int = 0
    created_at: float = field(default_factory=now)
    history: list = field(default_factory=list)


@dataclass(frozen=True)
class VerificationSnapshot:
    """核验快照：平台从可信验证接口取得证书状态后留存，不可更改。"""

    snapshot_id: str
    model_id: str
    certificate_no: str
    status: str
    cert_model_name: str
    cert_holder: str
    matched: bool
    source: str
    verified_at: float
    payload_digest: str


@dataclass
class Listing:
    """商品页面：每次变更都重新匹配实际型号与适用车辆。"""

    listing_id: str
    store_id: str
    model_id: str
    title: str
    claimed_vehicles: list
    status: str = "下架"
    matched: bool = False
    revision: int = 0
    created_at: float = field(default_factory=now)


@dataclass(frozen=True)
class ListingRevision:
    """商品页面变更留痕与重新匹配结果。"""

    revision_id: str
    listing_id: str
    revision: int
    title: str
    claimed_vehicles: list
    matched: bool
    mismatches: list
    checked_at: float


@dataclass
class Batch:
    """生产批次。"""

    batch_id: str
    model_id: str
    factory: str
    produced_at: str = ""
    created_at: float = field(default_factory=now)


@dataclass
class SerialUnit:
    """单品序列：跨店调拨只改变所在店铺，批次归属与流转历史不中断。"""

    serial_no: str
    batch_id: str
    model_id: str
    store_id: str
    status: SerialStatus = SerialStatus.在库
    order_id: str | None = None
    history: list = field(default_factory=list)


@dataclass
class Order:
    """销售订单：在途 → 已签收。"""

    order_id: str
    store_id: str
    listing_id: str
    consumer_id: str
    serials: list
    unit_price: float
    status: str = "在途"
    created_at: float = field(default_factory=now)
    delivered_at: float | None = None


@dataclass
class Investigation:
    """停售调查：与正式召回分别记录。"""

    investigation_id: str
    model_id: str
    trigger: InvestigationTrigger
    detail: str
    status: str = "调查中"
    opened_at: float = field(default_factory=now)
    closed_at: float | None = None
    outcome: str | None = None


@dataclass
class Recall:
    """正式召回：圈定批次、在途订单与已售序列。"""

    recall_id: str
    model_id: str
    investigation_id: str | None
    batch_ids: list
    reason: str
    scoped_serials: list
    in_transit_orders: list
    status: str = "召回中"
    created_at: float = field(default_factory=now)
    completed_at: float | None = None
    disposed: dict = field(default_factory=dict)  # serial_no -> disposal_id
    lifted: bool = False  # 复检恢复后解除剩余序列的召回约束


@dataclass(frozen=True)
class DisposalRecord:
    """处置留痕：退货、换货、销毁逐序列记录；复检恢复按召回记录。"""

    disposal_id: str
    recall_id: str
    serial_no: str
    action: DisposalType
    operator: str
    note: str
    processed_at: float


@dataclass
class CompensationEntry:
    """赔付台账：(召回, 序列) 唯一，杜绝重复赔付。"""

    entry_id: str
    recall_id: str
    order_id: str
    serial_no: str
    amount: float
    status: str = "待赔付"
    created_at: float = field(default_factory=now)
    paid_at: float | None = None


@dataclass(frozen=True)
class TransferRecord:
    """跨店调拨留痕；transfer_id 为幂等键。"""

    transfer_id: str
    serial_no: str
    from_store: str
    to_store: str
    moved_at: float


@dataclass
class Notification:
    """召回通知：待发送 → 已发送 → 已送达。"""

    notification_id: str
    recall_id: str
    order_id: str
    consumer_id: str
    channel: str = "站内信"
    status: str = "待发送"
    sent_at: float | None = None
    delivered_at: float | None = None


@dataclass(frozen=True)
class SampleFlowEvent:
    """样品流转留痕：抽检、复检、留样的交接链。"""

    event_id: str
    sample_id: str
    model_id: str
    from_party: str
    to_party: str
    purpose: str
    moved_at: float


@dataclass(frozen=True)
class CallbackReceipt:
    """平台回调回执；callback_id 为幂等键，重复回调返回原回执。"""

    callback_id: str
    event: str
    result: dict
    processed_at: float
