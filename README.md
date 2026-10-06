# 企业双积分核算与交易服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖企业、车型、年度核算、订单撮合、交易结转和统计。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 积分市场：成交前预授权与两阶段撮合

高并发撮合下防止“同一份可售积分被两个卖单重复使用”“买方余额不足却留下成交记录”，市场模块在原有挂单撮合之上实现了：

- **挂单即冻结（Pre-trade Authorization）**
  - 卖单：先登记可售积分批次 `CreditBatch`，挂单时按 FIFO 冻结批次额度，生成卖单 `OrderAuthorization(held)`；
  - 买单：买方先有资金账户 `FundsAccount`，挂单时按价格上限冻结资金（买单资金不足直接拒单，不再产生无法清算的成交）。
- **撮合只消费有效授权**：撮合一律在数据库写锁（SQLite `BEGIN IMMEDIATE` + `busy_timeout`）内串行执行，锁内复查订单状态与授权余额；已撤单/已过期/额度不足的授权绝不成交。
- **两阶段：成交（matched）→ 清算（settled）**
  - 多腿撮合先产生确定性执行计划 `TradeExecution` 与成交腿 `CreditTradeLeg`，在一个事务里要么全部腿落账（成交记录 `status=matched`），要么整笔回滚；
  - 清算逐腿独立事务：买方冻结资金划付卖方、批次转 consumed、价格历史落账、年度汇总重算；中途宕机留下的部分结果（计划 `partial`、腿仍 `matched`）由**任务箱** `OutboxTask` 在服务重启/定时扫描时按腿序号回放，已清算的腿幂等跳过，绝不重复成交。
- **撤单 / 授权过期**：释放仍冻结的批次额度与资金（`released/expired`），释放后订单不可再成交；过期扫描任务同样在任务箱中，重启自动回放。
- **幂等**：撮合端点支持 `Idempotency-Key` 请求头（同键同请求体只产生一个执行计划，重放返回首次结果；同键不同体拒绝）；唯一约束兜底。
- **价格优先、时间优先**：卖单价升序、买单价降序，同价按挂单时间（再按订单 ID）确定排序；两侧均可公平部分成交，买单冻结资金按实际成交价不足时自动把卖方剩余让给后续买单。
- **五段数量口径**可分别查询：挂单 `posted`、预授权 `held/authorized`、已成交未清算 `matched_unsettled`、已清算 `settled`、释放 `released`（`GET /credit-market/orders/{id}/quantities`）。
- **可追踪/可解释**：腿、成交记录均回溯原卖单/买单与执行计划、成交序号；并发请求最终只有一个结果的原因是：①写锁在数据库层串行化所有撮合；②锁内复查；③幂等键与唯一约束。

主要新接口（均在 `/api/v1/credit-market` 下）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/credit-batches` | 登记卖方可售积分批次 |
| GET | `/credit-batches` | 批次查询（总量/冻结/已消费/可用） |
| POST | `/funds-accounts/{enterprise_id}/deposit` | 买方资金入账 |
| GET | `/funds-accounts/{enterprise_id}` | 资金账户（余额/冻结） |
| GET | `/authorizations` | 预授权流水（held/consumed/released/expired） |
| POST | `/orders` | 挂单即冻结（卖单批次、买单资金） |
| POST | `/orders/{id}/cancel` | 撤单并释放授权 |
| GET | `/orders/{id}/quantities` | 五段数量口径 |
| POST | `/match` | 指定两单撮合（支持 Idempotency-Key） |
| POST | `/match-all/{year}` | 价格-时间优先自动连续撮合（多腿执行计划） |
| GET | `/executions/{id}` | 执行计划与全部成交腿、清算状态 |
| GET | `/orders/{id}/executions` | 订单关联的执行计划 |
| POST | `/tasks/replay` | 重启后任务箱回放（清算/过期扫描） |
| POST | `/authorizations/expire-now` | 立即扫描释放过期授权 |

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 -m pytest -q test_dual_credit_integration.py
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
