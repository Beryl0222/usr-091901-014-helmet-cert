# 骑行头盔认证与召回

核验头盔型号与认证关系，追踪平台销售、抽检和召回执行。项目以领域契约约定参与者、状态和不可破坏的业务原则，围绕三条不变量组织后端：

1. **证书必须匹配实际型号和生产主体** —— 生产者提交型号、材料结构、工厂、证书编号与检测报告后，平台从可信验证接口取得证书状态并保存核验快照，持证主体、覆盖型号、证书状态任一不符即不予上架；
2. **停售调查与正式召回分别记录** —— 抽检不合格、证书撤销、夸大宣传触发停售调查；正式召回单独圈定批次、在途订单与已售序列，退货、换货、销毁、复检恢复分别留痕；
3. **跨店流转不得打断问题批次追踪** —— 序列号血统跟随批次而非店铺，调拨只改当前持有方；平台回调按 `callback_id` 去重、调拨按 `transfer_id` 去重、赔付按（召回， 序列）唯一，重复投递不漏算也不多赔。

商品页面每次变更（换标题、改适用车辆）都会重新匹配实际型号与证书快照。消费者扫码只看到真伪、适用范围与处置入口；监管人员可追证书复用关系、样品流转链、通知送达与仍未召回的去向。

## 运行

- `python3 service.py --check` 核对服务配置与领域契约；
- `python3 service.py --port 8000` 启动服务，基础接口为 `/health` 与 `/contract`；
- `python3 -m unittest -v` 运行全部测试。

## 接口概览（均以 `/api` 为前缀）

| 角色 | 接口 |
| --- | --- |
| 生产者 | `POST /producers`、`POST /models`、`POST /models/{id}/verify` |
| 销售平台 | `POST /listings`、`POST /listings/{id}/revise`、`POST /batches`、`POST /orders`、`POST /callbacks`、`POST /transfers` |
| 平台/监管 | `POST /investigations`、`POST /investigations/{id}/close`、`POST /recalls`、`POST /recalls/{id}/dispositions`、`POST /recalls/{id}/reinspect` |
| 消费者 | `GET /consumer/scan/{serial}` |
| 监管人员 | `GET /regulator/certs/{cert}/reuse`、`GET /regulator/samples/{id}`、`GET /regulator/recalls/{id}/notifications`、`GET /regulator/recalls/{id}/outstanding`、`GET /regulator/events` |

代码结构：`helmet_core.py` 为领域核心（实体、状态机、事件日志），`helmet_api.py` 为 JSON 路由层，`service.py` 负责挂载与进程入口。可信验证接口以 `StaticVerificationGateway` 登记簿实现演示，生产环境可替换为对接监管验证接口的客户端。
