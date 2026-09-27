# 骑行头盔认证与召回

核验头盔型号与认证关系，追踪平台销售、抽检和召回执行。项目以领域契约约定参与者、状态和不可破坏的业务原则，围绕同一语义提供认证核验、商品页面匹配、停售调查、正式召回与监管追溯能力。

## 运行

- `python3 service.py --check` 核对服务配置与领域契约。
- `python3 service.py --port 8000` 启动服务，可访问 `/health` 与 `/contract`。
- `python3 -m unittest -v` 运行全部测试（基础契约、领域服务、HTTP 接口）。

## 架构

- `helmet/domain.py` — 实体与状态机：型号档案（待核验→允许销售→停售调查→召回中→恢复销售/已退市）、核验快照、商品页面留痕、批次与序列、调查、召回、处置、赔付台账、调拨、通知、样品流转、回调回执。
- `helmet/store.py` — 进程内数据仓库与幂等索引。
- `helmet/verification.py` — 可信证书验证接口（内置静态源 + HTTP 客户端），应答做防篡改摘要。
- `helmet/services.py` — 应用服务：核验快照、页面重匹配、停售调查、召回圈定与四类处置、幂等、消费者与监管视图。
- `helmet/api.py` — HTTP 路由、Idempotency-Key 支持、统一错误映射（400/404/409）。
- `trusted_certificates.json` — 演示用可信证书登记数据。

## 核心业务规则

- **证书核验**：生产者提交型号、材料结构、工厂、证书编号和检测报告；平台从可信验证接口取得证书状态并保存不可更改的核验快照；证书状态、登记型号、持证主体三者都匹配才允许销售。
- **页面重匹配**：商品页面每次变更都重新匹配实际型号、证书状态、适用车辆范围与夸大宣传词，不匹配即下架并留痕。
- **停售调查**：抽检不合格、证书撤销、夸大防护宣传三类触发；调查与正式召回分别记录，调查可结案恢复、升级召回或退市。
- **正式召回**：圈定批次、在途订单与已售序列，自动向受影响消费者发通知；退货、换货、销毁、复检恢复分别留痕。
- **幂等与防多赔**：平台回调按 `callback_id`、跨店调拨按 `transfer_id`、HTTP 写操作按 `Idempotency-Key` 幂等；同一召回内同一序列的赔付台账只入账一次，重复回调不产生重复处置或重复赔付。
- **跨店调拨**：只改变序列所在店铺，批次归属与流转历史不中断，召回圈定与未召回去向统计不受影响。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/producers` | 登记生产者 |
| POST | `/models` | 提交型号档案 |
| POST | `/models/{id}/verify` | 可信核验并保存快照 |
| POST/PUT | `/listings`, `/listings/{id}` | 商品页面创建与变更（自动重匹配） |
| POST | `/models/{id}/batches`, `/batches/{id}/serials` | 批次与序列登记 |
| POST | `/transfers` | 跨店调拨（幂等） |
| POST | `/orders`, `/orders/{id}/deliver` | 下单（在途）与签收 |
| POST | `/investigations`, `/investigations/{id}/close` | 停售调查与结案（可升级召回） |
| POST | `/recalls` | 直接发起正式召回 |
| POST | `/recalls/{id}/returns` `/exchanges` `/destructions` `/reinspect` `/complete` | 四类处置与结案 |
| POST | `/callbacks` | 平台回调统一入口（幂等） |
| GET | `/scan/{serial}` | 消费者扫码：仅真伪、适用范围、处置入口 |
| GET | `/regulator/cert-reuse` `/sample-trace` `/notifications` `/recall-unrecovered` | 监管追溯视图 |
