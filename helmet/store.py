"""进程内数据仓库：实体表与幂等索引。"""

import threading


class DataStore:
    """集中持有全部领域数据；快照、留痕类表只追加，幂等键唯一。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.producers = {}  # producer_id -> Producer
        self.models = {}  # model_id -> HelmetModel
        self.snapshots = []  # 核验快照，只追加
        self.listings = {}  # listing_id -> Listing
        self.revisions = []  # 商品页面变更留痕，只追加
        self.batches = {}  # batch_id -> Batch
        self.serials = {}  # serial_no -> SerialUnit
        self.orders = {}  # order_id -> Order，天然幂等键
        self.investigations = {}  # investigation_id -> Investigation
        self.recalls = {}  # recall_id -> Recall
        self.disposals = []  # 处置留痕，只追加
        self.disposal_keys = {}  # 处置幂等键 -> disposal_id
        self.compensations = {}  # (recall_id, serial_no) -> CompensationEntry，防多赔
        self.transfers = {}  # transfer_id -> TransferRecord，幂等
        self.notifications = {}  # notification_id -> Notification
        self.sample_flows = []  # 样品流转留痕，只追加
        self.callbacks = {}  # callback_id -> CallbackReceipt，幂等
        self.idempotency = {}  # (method, path, Idempotency-Key) -> (status, payload)
