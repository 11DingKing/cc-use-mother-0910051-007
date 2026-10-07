# 企业双积分核算与交易服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖企业、车型、年度核算、订单撮合、交易结转和统计。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 积分市场高并发撮合：成交前预授权

市场撮合模块（`app/matching.py`）针对"两个卖单共用同一份积分、买方余额不足留下已成交记录、并发/回放重复成交"做了如下保证：

- **挂单即冻结（成交前预授权）**：卖单挂单时冻结企业积分钟余（`credit` 授权），买单挂单时按报价冻结资金额度（`fund` 授权，需先在资金账户授信/充值）。冻结不足直接拒绝挂单。撮合只消费"有效授权"（`frozen - consumed - released`），从源头杜绝双花与余额不足成交。
- **条件更新防并发**：授权消费、订单推进、资金冻结/清算都是带余量条件的 `UPDATE ... WHERE 余量 >= ?`（CAS 风格），配合 SQLite WAL、`busy_timeout` 与进程内撮合锁。并发请求由数据库裁决，恰好一个成功。
- **幂等撮合任务**：一次撮合（可能含多笔订单对）建模为 `MatchTask` + 按确定顺序（价格优先、同价时间优先、id 兜底全序）排列的 `MatchTaskItem`。成交记录带幂等键；指定撮合可传客户端 `idempotency_key`。后来的并发请求收敛到同一个任务/同一个成交结果。
- **可恢复的部分结果**：任务中某笔竞争失败时，前置的授权/订单消费精确冲正，本项落为 `failed`，其余订单对继续落账，任务记为 `partial`，可回放恢复。
- **撤单 / 授权过期 / 重启回放不重复成交**：撤单与过期释放剩余授权（卖单退回可售额度、买单退回冻结资金）；重启（或 `POST /tasks/resume`）回放 `pending/running/failed` 任务，已成交项凭幂等键跳过。
- **成交即清算**：买单按报价冻结、按成交均价清算，价差当场解冻回可用资金；成交记录区分 `matched`/`cleared`。
- **数量五分开查询**：挂单（posted/filled/remaining/released）、预授权（frozen/consumed/released/available）、成交（matched）、清算（cleared/uncleared）、释放（released）在 `GET /orders/{id}/quantities` 分别呈现，并保留买卖原订单追踪。

### 主要新增接口

- `POST /api/v1/credit-market/fund-accounts/{enterprise_id}/{year}/deposit` 买方资金授信
- `GET  /api/v1/credit-market/fund-accounts/{enterprise_id}/{year}` 资金账户（余额/冻结/已清算/释放）
- `POST /api/v1/credit-market/orders` 挂单（卖单冻结积分、买单冻结资金）
- `POST /api/v1/credit-market/orders/{id}/cancel` 撤单并释放剩余预授权
- `GET  /api/v1/credit-market/orders/{id}/quantities` 挂单/授权/成交/清算/释放数量视图
- `GET  /api/v1/credit-market/auths`、`POST /api/v1/credit-market/auths/expire` 预授权查询与过期释放
- `POST /api/v1/credit-market/match`（可带 `idempotency_key`）、`POST /api/v1/credit-market/match-all/{year}` 撮合
- `GET  /api/v1/credit-market/tasks`、`GET /tasks/{id}`、`POST /tasks/resume` 撮合任务查询与重启回放

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 -m pytest -q test_dual_credit_integration.py test_preauth_matching.py
```

## 编译

```bash
python3 -m compileall -q .
```

## 接口验收

```bash
python3 -c "from app.main import app; assert len(app.routes) > 5; print(len(app.routes))"
```

## 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
