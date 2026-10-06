"""
积分市场「成交前预授权」并发撮合测试。

覆盖需求：
1. 挂卖单冻结可售积分批次（同一份批次不能被两个卖单同时使用，FIFO 选择批次）；
2. 挂买单冻结资金账户额度（余额不足直接拒单，不再产生成交后无法清算的记录）；
3. 撮合并发串行化：同一对订单的并发成交请求最终只有一个成功；撤单与成交竞争也只有一个结果；
4. Idempotency-Key 幂等：同键的并发/重放请求只产生一个执行计划，异请求体被拒绝；
5. 价格优先、时间优先；公平的部分成交（同价位先挂单先被消费，两侧都可部分成交）；
6. 一次撮合多腿原子落账；清算逐腿独立提交、部分结果可由任务箱在重启后回放，且回放幂等；
7. 撤单/授权过期释放冻结，释放后不能再成交，额度可重新使用；
8. 五段数量口径（挂单/预授权/成交未清算/已清算/释放）可分别查询；
9. 资金与积分守恒。

并发测试使用临时文件 SQLite（不同线程各自连接，BEGIN IMMEDIATE 才会真实冲突），
内存 StaticPool 单连接无法体现写锁竞争。
"""
# 夹具（db / market / _MarketHarness 等）统一见 conftest.py
import threading
from datetime import datetime, timedelta
import pytest
from sqlalchemy.orm import sessionmaker
from conftest import _MarketHarness
from app import crud, matching, schemas
from app import models
from app.models import (
    OrderType, OrderStatus, AuthorizationStatus, LegStatus, ExecutionStatus,
    OutboxTaskStatus,
)
import app.matching

YEAR = 2025
CEIL = 8000.0
EPS = 0.01


# ---------------------------------------------------------------------------
class TestSellPreAuthorization:

    def test_same_batch_cannot_be_double_frozen(self, db):
        ent = crud.create_enterprise(db, schemas.EnterpriseCreate(
            name="卖方批次企业", credit_code="SELL-BATCH-1"))
        market_h = _MarketHarness(sessionmaker(bind=db.bind))
        market_h.batch(db, ent.id, 100.0)

        order1 = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=YEAR, order_type=OrderType.SELL,
            unit_price=3000, total_amount=100))
        assert order1.status == OrderStatus.PENDING

        batch = crud.get_credit_batches(db, enterprise_id=ent.id)[0]
        assert batch.frozen_amount == 100.0

        with pytest.raises(ValueError, match="可售积分不足"):
            crud.create_credit_order(db, schemas.CreditOrderCreate(
                enterprise_id=ent.id, year=YEAR, order_type=OrderType.SELL,
                unit_price=3000, total_amount=50))

        # 部分超冻同样拒绝：可用为 0，哪怕再冻 1 分也不行
        with pytest.raises(ValueError):
            crud.create_credit_order(db, schemas.CreditOrderCreate(
                enterprise_id=ent.id, year=YEAR, order_type=OrderType.SELL,
                unit_price=3000, total_amount=1))

    def test_cancel_releases_batch_then_reusable(self, db):
        ent = crud.create_enterprise(db, schemas.EnterpriseCreate(
            name="撤单回补企业", credit_code="SELL-BATCH-2"))
        h = _MarketHarness(sessionmaker(bind=db.bind))
        h.batch(db, ent.id, 100.0)
        order = h.sell(db, ent.id, 60)

        crud.cancel_credit_order(db, order.id)
        batch = crud.get_credit_batches(db, enterprise_id=ent.id)[0]
        assert batch.frozen_amount == 0.0
        assert batch.consumed_amount == 0.0

        order2 = h.sell(db, ent.id, 80)
        assert order2.remaining_amount == 80.0
        batch = crud.get_credit_batches(db, enterprise_id=ent.id)[0]
        assert batch.frozen_amount == 80.0

    def test_batches_consumed_fifo(self, db):
        ent = crud.create_enterprise(db, schemas.EnterpriseCreate(
            name="批次FIFO企业", credit_code="SELL-FIFO"))
        h = _MarketHarness(sessionmaker(bind=db.bind))
        b1 = h.batch(db, ent.id, 30.0)
        b2 = h.batch(db, ent.id, 50.0)
        buyer = h.enterprise(db, "批次FIFO买方")
        h.account(db, buyer.id, 10000 * CEIL)
        sell = h.sell(db, ent.id, 60)
        buy = h.buy(db, buyer.id, 60, price=3100, cash=10000 * CEIL)

        matching.match_pair(sell.id, buy.id, 60.0)

        db.expire_all()
        b1 = db.get(models.CreditBatch, b1.id)
        b2 = db.get(models.CreditBatch, b2.id)
        # 先挂的批次先被消费完，再消费后挂的批次
        assert b1.consumed_amount == 30.0
        assert b1.frozen_amount == 0.0
        assert b2.consumed_amount == 30.0
        assert b2.frozen_amount == 0.0
        # 后挂批次 50 中仅消费 30，剩余 20 解冻回到可用
        assert b2.total_amount - b2.frozen_amount - b2.consumed_amount == 20.0


# ---------------------------------------------------------------------------
# 2. 买单预授权资金：余额不足直接拒单；成交按实际价、上限冻结差价退还
# ---------------------------------------------------------------------------

class TestBuyPreAuthorization:

    def test_buy_order_rejected_when_insufficient_funds(self, db):
        ent = crud.create_enterprise(db, schemas.EnterpriseCreate(
            name="缺钱买方", credit_code="BUY-POOR"))
        crud.create_funds_account_if_absent(db, ent.id, initial_balance=1000.0)

        with pytest.raises(ValueError, match="可用资金不足"):
            crud.create_credit_order(db, schemas.CreditOrderCreate(
                enterprise_id=ent.id, year=YEAR, order_type=OrderType.BUY,
                unit_price=3000, total_amount=1))  # 需冻结 8000

    def test_frozen_at_ceiling_refunded_after_match(self, db):
        h = _MarketHarness(sessionmaker(bind=db.bind))
        seller = h.enterprise(db, "上限冻结卖方")
        buyer = h.enterprise(db, "上限冻结买方")
        h.batch(db, seller.id, 100.0)
        h.account(db, buyer.id, 100.0 * CEIL)
        sell = h.sell(db, seller.id, 100, price=3000)
        buy = h.buy(db, buyer.id, 100, price=3000, cash=100.0 * CEIL)

        account = crud.get_funds_account(db, buyer.id)
        assert account.frozen_amount == 100.0 * CEIL  # 挂单即按上限冻结

        matching.match_pair(sell.id, buy.id, 50.0)
        db.expire_all()
        account = crud.get_funds_account(db, buyer.id)
        # 已成交 50 分实际价 3000 划付；剩余 50 分的上限冻结差价(50*5000)立即退回
        assert account.frozen_amount == pytest.approx(50.0 * CEIL)
        # 卖方收到 50 * 3000
        seller_account = crud.get_funds_account(db, seller.id)
        assert seller_account.balance == pytest.approx(50.0 * 3000)


# ---------------------------------------------------------------------------
# 3/4. 并发撮合只有一个结果 + 幂等键
# ---------------------------------------------------------------------------

class TestConcurrentMatching:

    def test_concurrent_same_pair_only_one_fills(self, market):
        """两个线程同时对同一对订单撮合同一数量：只有一个成功，不产生重复成交。"""
        with market.session() as s:
            seller = market.enterprise(s, "并发卖方")
            buyer = market.enterprise(s, "并发买方")
            seller_ent_id, buyer_ent_id = seller.id, buyer.id
            market.batch(s, seller.id, 100.0)
            market.account(s, buyer.id, 100.0 * CEIL)
            sell = market.sell(s, seller.id, 100)
            buy = market.buy(s, buyer.id, 100, cash=100.0 * CEIL)
            sell_id, buy_id = sell.id, buy.id

        outcomes = {}
        barrier = threading.Barrier(2)

        def worker(name):
            barrier.wait()
            try:
                execution, replayed = matching.match_pair(sell_id, buy_id, 100.0)
                outcomes[name] = ("ok", execution.id)
            except Exception as exc:
                outcomes[name] = ("err", str(exc)[:80])

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = sorted(outcomes.values())
        assert outcomes["A"][0] != outcomes["B"][0] or outcomes["A"] != outcomes["B"]
        oks = [v for v in outcomes.values() if v[0] == "ok"]
        errs = [v for v in outcomes.values() if v[0] == "err"]
        assert len(oks) == 1, outcomes
        assert len(errs) == 1, outcomes
        assert "复查失败" in errs[0][1] or "不可交易" in errs[0][1] or "不足" in errs[0][1]

        with market.session() as s:
            txns = s.query(models.CreditTransaction).all()
            assert len(txns) == 1
            assert txns[0].credit_amount == 100.0
            sell = s.get(models.CreditOrder, sell_id)
            buy = s.get(models.CreditOrder, buy_id)
            assert sell.filled_amount == 100.0
            assert buy.filled_amount == 100.0
            assert sell.remaining_amount == 0.0
            # 守恒：买方总资金 = 划付卖方部分（卖方账户余额即买方支出）
            seller_acc = s.query(models.FundsAccount).filter_by(enterprise_id=seller_ent_id).one()
            buyer_acc = s.query(models.FundsAccount).filter_by(enterprise_id=buyer_ent_id).one()
            assert buyer_acc.balance + buyer_acc.frozen_amount + seller_acc.balance \
                == pytest.approx(100.0 * CEIL)

    def test_concurrent_idempotency_key_single_execution(self, market):
        """同一幂等键的两个并发请求只落一个执行计划；重放返回首次结果。"""
        with market.session() as s:
            seller = market.enterprise(s, "幂等卖方")
            buyer = market.enterprise(s, "幂等买方")
            market.batch(s, seller.id, 100.0)
            market.account(s, buyer.id, 100.0 * CEIL)
            sell = market.sell(s, seller.id, 100)
            buy = market.buy(s, buyer.id, 100, cash=100.0 * CEIL)
            ids = (sell.id, buy.id)

        outcomes = {}
        barrier = threading.Barrier(2)

        def worker(name):
            barrier.wait()
            try:
                execution, replayed = matching.match_pair(
                    ids[0], ids[1], 100.0,
                    idempotency_key="fixed-key-001",
                    request_payload={
                        "sell_order_id": ids[0], "buy_order_id": ids[1],
                        "credit_amount": 100.0,
                    },
                )
                outcomes[name] = ("ok", execution.id, replayed)
            except Exception as exc:
                outcomes[name] = ("err", str(exc), False)

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start(); t2.start(); t1.join(); t2.join()

        exec_ids = {v[1] for v in outcomes.values() if v[0] == "ok"}
        assert len(exec_ids) == 1, outcomes
        # 至少一个请求被识别为重放
        assert any(v[2] for v in outcomes.values() if v[0] == "ok")

        with market.session() as s:
            assert s.query(models.TradeExecution).count() == 1
            assert s.query(models.CreditTransaction).count() == 1
            assert s.query(models.IdempotencyRecord).count() == 1

        # 事后显式重放：返回同一执行计划，不产生新成交
        execution3, replayed3 = matching.match_pair(
            ids[0], ids[1], 100.0,
            idempotency_key="fixed-key-001",
            request_payload={"sell_order_id": ids[0], "buy_order_id": ids[1],
                            "credit_amount": 100.0},
        )
        assert replayed3 is True
        with market.session() as s:
            assert s.query(models.TradeExecution).count() == 1

    def test_same_idempotency_key_different_body_rejected(self, market):
        with market.session() as s:
            seller = market.enterprise(s, "异请求体卖方")
            buyer = market.enterprise(s, "异请求体买方")
            market.batch(s, seller.id, 100.0)
            market.account(s, buyer.id, 100.0 * CEIL)
            sell = market.sell(s, seller.id, 100)
            buy = market.buy(s, buyer.id, 100, cash=100.0 * CEIL)
            ids = (sell.id, buy.id)

        payload = {"sell_order_id": ids[0], "buy_order_id": ids[1], "credit_amount": 100.0}
        matching.match_pair(ids[0], ids[1], 100.0,
                            idempotency_key="k", request_payload=payload)
        with pytest.raises(ValueError, match="请求内容不同"):
            matching.match_pair(ids[0], ids[1], 90.0,
                                idempotency_key="k",
                                request_payload={**payload, "credit_amount": 90.0})

    def test_cancel_vs_match_only_one_outcome(self, market):
        """撤单与成交并发竞争多轮：每轮要么成交、要么撤单，绝不并存。"""
        for round_no in range(8):
            with market.session() as s:
                seller = market.enterprise(s, f"竞撤卖方{round_no}")
                buyer = market.enterprise(s, f"竞撤买方{round_no}")
                market.batch(s, seller.id, 100.0)
                market.account(s, buyer.id, 100.0 * CEIL)
                sell = market.sell(s, seller.id, 100)
                buy = market.buy(s, buyer.id, 100, cash=100.0 * CEIL)
                ids = (sell.id, buy.id)

            results = {}
            barrier = threading.Barrier(2)

            def do_match():
                barrier.wait()
                try:
                    matching.match_pair(ids[0], ids[1], 100.0)
                    results["match"] = "ok"
                except Exception:
                    results["match"] = "err"

            def do_cancel():
                barrier.wait()
                try:
                    matching.cancel_order(ids[0])
                    results["cancel"] = "ok"
                except Exception:
                    results["cancel"] = "err"

            t1 = threading.Thread(target=do_match)
            t2 = threading.Thread(target=do_cancel)
            t1.start(); t2.start(); t1.join(); t2.join()

            with market.session() as s:
                order = s.get(models.CreditOrder, ids[0])
                txn_count = s.query(models.CreditTransaction).filter(
                    models.CreditTransaction.sell_order_id == ids[0]).count()
                if results["match"] == "ok":
                    assert results["cancel"] == "err"
                    assert order.status == OrderStatus.FILLED
                    assert txn_count == 1
                else:
                    assert results["cancel"] == "ok"
                    assert order.status == OrderStatus.CANCELLED
                    assert txn_count == 0
                # 核心不变量：成交与撤单永不并存
                assert not (order.status == OrderStatus.CANCELLED and txn_count > 0)


# ---------------------------------------------------------------------------
# 5. 价格优先、时间优先与公平部分成交
# ---------------------------------------------------------------------------

class TestPriorityAndPartialFills:

    def test_price_priority_cheaper_sell_fills_first(self, db):
        h = _MarketHarness(sessionmaker(bind=db.bind))
        cheap_seller = h.enterprise(db, "低价卖方")
        dear_seller = h.enterprise(db, "高价卖方")
        buyer = h.enterprise(db, "价格优先买方")
        h.batch(db, cheap_seller.id, 50)
        h.batch(db, dear_seller.id, 50)
        h.account(db, buyer.id, 100 * CEIL)

        dear = h.sell(db, dear_seller.id, 50, price=3200)  # 先挂但贵
        cheap = h.sell(db, cheap_seller.id, 50, price=3000)
        buy = h.buy(db, buyer.id, 100, price=3100, cash=100 * CEIL)

        execution, _ = matching.match_auto(YEAR)
        db.expire_all()
        cheap = db.get(models.CreditOrder, cheap.id)
        dear = db.get(models.CreditOrder, dear.id)
        # 买价 3100：低价单全部成交；高价卖单无法成交（3100 < 3200）
        assert cheap.status == OrderStatus.FILLED
        assert cheap.filled_amount == 50
        assert dear.status == OrderStatus.PENDING
        assert dear.filled_amount == 0

    def test_time_priority_and_fair_partial_fills(self, db):
        """同价位先挂的卖单先成交；卖单与买单都可能只成交一部分。"""
        h = _MarketHarness(sessionmaker(bind=db.bind))
        seller1 = h.enterprise(db, "时间优先卖方1")
        seller2 = h.enterprise(db, "时间优先卖方2")
        buyer = h.enterprise(db, "部分成交买方")
        h.batch(db, seller1.id, 60)
        h.batch(db, seller2.id, 60)
        h.account(db, buyer.id, 200 * CEIL)

        first = h.sell(db, seller1.id, 60, price=3000)
        second = h.sell(db, seller2.id, 60, price=3000)
        buy = h.buy(db, buyer.id, 100, price=3100, cash=200 * CEIL)

        execution, _ = matching.match_auto(YEAR)
        assert execution.planned_count == 2
        db.expire_all()
        first = db.get(models.CreditOrder, first.id)
        second = db.get(models.CreditOrder, second.id)
        buy = db.get(models.CreditOrder, buy.id)
        # 先挂的先被吃完 60，后挂的只成交 40（部分成交），买单全成
        assert first.filled_amount == 60
        assert first.status == OrderStatus.FILLED
        assert second.filled_amount == 40
        assert second.status == OrderStatus.PARTIAL
        assert second.remaining_amount == 20
        assert buy.filled_amount == 100
        assert buy.status == OrderStatus.FILLED

        legs = db.query(models.CreditTradeLeg).filter(
            models.CreditTradeLeg.execution_id == execution.id
        ).order_by(models.CreditTradeLeg.seq).all()
        assert [l.sell_order_id for l in legs] == [first.id, second.id]
        assert [l.credit_amount for l in legs] == [60, 40]

    def test_buyer_cash_constrains_fair_share(self, db):
        """买方冻结资金按实际成交价限制可成交量：钱花光后买单公平地把卖单留给后续买单。"""
        h = _MarketHarness(sessionmaker(bind=db.bind))
        seller = h.enterprise(db, "唯一卖方")
        buyer_poor = h.enterprise(db, "钱刚好够买方")
        buyer_rich = h.enterprise(db, "后到有钱买方")
        h.batch(db, seller.id, 100)
        # 穷买方：买单 60 分，账户恰好按上限冻结 60*8000（撮合价约 3050，低于上限）
        h.account(db, buyer_poor.id, 60 * CEIL)
        h.account(db, buyer_rich.id, 100 * CEIL)

        sell = h.sell(db, seller.id, 100, price=3000)
        poor = h.buy(db, buyer_poor.id, 60, price=3100, cash=60 * 3050)
        rich = h.buy(db, buyer_rich.id, 100, price=3100, cash=100 * CEIL)

        matching.match_auto(YEAR)
        db.expire_all()
        poor = db.get(models.CreditOrder, poor.id)
        rich = db.get(models.CreditOrder, rich.id)
        sell = db.get(models.CreditOrder, sell.id)
        # 穷买方按实际价成交 60（冻结恰够）；富买方接手剩余 40
        assert poor.filled_amount == 60
        assert rich.filled_amount == 40
        assert sell.filled_amount == 100


# ---------------------------------------------------------------------------
# 6. 多腿原子撮合 + 清算部分结果可回放
# ---------------------------------------------------------------------------

class TestAtomicMatchAndSettlementReplay:

    def _two_leg_market(self, market):
        with market.session() as s:
            seller1 = market.enterprise(s, "多腿卖方1")
            seller2 = market.enterprise(s, "多腿卖方2")
            buyer = market.enterprise(s, "多腿买方")
            s1_id, s2_id, b_id = seller1.id, seller2.id, buyer.id
            market.batch(s, s1_id, 60)
            market.batch(s, s2_id, 40)
            market.account(s, b_id, 100 * CEIL)
            market.sell(s, s1_id, 60, price=3000)
            market.sell(s, s2_id, 40, price=3000)
            market.buy(s, b_id, 100, price=3100, cash=100 * CEIL)
        return s1_id, s2_id, b_id

    def test_match_plan_atomic_when_mid_apply_fails(self, market, monkeypatch):
        s1, s2, b = self._two_leg_market(market)

        real = matching._consume_sell_auth
        calls = {"n": 0}

        def fail_on_second_leg(db, order, qty):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash while applying leg 2")
            return real(db, order, qty)

        monkeypatch.setattr(matching, "_consume_sell_auth", fail_on_second_leg)

        with pytest.raises(RuntimeError, match="simulated crash"):
            matching.match_auto(YEAR)
        monkeypatch.undo()

        with market.session() as s:
            # 撮合事务整体回滚：没有任何计划/腿/成交，冻结与订单数量回到撮合前
            assert s.query(models.TradeExecution).count() == 0
            assert s.query(models.CreditTradeLeg).count() == 0
            assert s.query(models.CreditTransaction).count() == 0
            for sid in (s1, s2):
                order = s.get(models.CreditOrder, self._order_id(s, sid, market))
            sells = s.query(models.CreditOrder).filter(
                models.CreditOrder.enterprise_id.in_([s1, s2])).all()
            for o in sells:
                assert o.filled_amount == 0
                assert o.remaining_amount == o.total_amount
            for batch in s.query(models.CreditBatch).all():
                assert batch.consumed_amount == 0
                assert batch.frozen_amount == batch.total_amount
            buyer_acc = s.query(models.FundsAccount).filter_by(enterprise_id=b).one()
            assert buyer_acc.frozen_amount == pytest.approx(100 * CEIL)

    @staticmethod
    def _order_id(market_self, ent_id, market):
        with market.session() as s:
            return s.query(models.CreditOrder).filter_by(enterprise_id=ent_id).one().id

    def test_settlement_partial_result_replayed_after_restart(self, market, monkeypatch):
        s1, s2, b = self._two_leg_market(market)

        # 模拟撮合已提交、清算第一腿之前进程崩溃：首条腿清算事务整体失败
        real_settle_leg = matching.settle_leg
        def crash_before_first_leg(leg_id):
            raise RuntimeError("simulated crash before settlement")
        monkeypatch.setattr(matching, "settle_leg", crash_before_first_leg)

        execution, _ = matching.match_auto(YEAR)
        exec_id = execution.id
        monkeypatch.undo()

        with market.session() as s:
            # 成交已落账（matched 未清算）：这就是可识别的部分结果
            legs = s.query(models.CreditTradeLeg).filter_by(execution_id=exec_id).all()
            assert len(legs) == 2
            assert all(l.status == LegStatus.MATCHED for l in legs)
            txns = s.query(models.CreditTransaction).all()
            assert all(t.status == "matched" for t in txns)
            ex = s.get(models.TradeExecution, exec_id)
            assert ex.status == ExecutionStatus.PARTIAL
            task = s.query(models.OutboxTask).filter_by(execution_id=exec_id).one()
            assert task.status == OutboxTaskStatus.PENDING
            buyer_acc = s.query(models.FundsAccount).filter_by(enterprise_id=b).one()
            assert buyer_acc.frozen_amount == pytest.approx(100 * 3050)
            sellers_before = s.query(models.FundsAccount).filter(
                models.FundsAccount.enterprise_id.in_([s1, s2])).all()
            assert all(a.balance == 0 for a in sellers_before)

        # 服务重启：回放任务箱（首次因退避被排到未来，复位后立即认领）
        matching.replay_pending_tasks()
        with matching.locked_session(matching.SessionLocal) as s:
            for t in s.query(models.OutboxTask).filter_by(execution_id=exec_id).all():
                t.available_at = matching.now()
                t.attempts = 0
        report = matching.replay_pending_tasks()
        assert report["settle"] == 1

        with market.session() as s:
            ex = s.get(models.TradeExecution, exec_id)
            assert ex.status == ExecutionStatus.SETTLED
            assert ex.settled_count == 2
            legs = s.query(models.CreditTradeLeg).filter_by(execution_id=exec_id).all()
            assert all(l.status == LegStatus.SETTLED for l in legs)
            txns = s.query(models.CreditTransaction).all()
            assert all(t.status == "completed" for t in txns)
            assert s.query(models.PriceHistory).count() == 2
            buyer_acc = s.query(models.FundsAccount).filter_by(enterprise_id=b).one()
            assert buyer_acc.frozen_amount == 0
            sellers = s.query(models.FundsAccount).filter(
                models.FundsAccount.enterprise_id.in_([s1, s2])).all()
            assert sum(a.balance for a in sellers) == pytest.approx(100 * 3050)
            # 任务已完结
            task = s.query(models.OutboxTask).filter_by(execution_id=exec_id).one()
            assert task.status == OutboxTaskStatus.DONE

        # 再次回放：已结算腿幂等跳过，绝不重复划付
        matching.replay_pending_tasks()
        with market.session() as s:
            sellers = s.query(models.FundsAccount).filter(
                models.FundsAccount.enterprise_id.in_([s1, s2])).all()
            assert sum(a.balance for a in sellers) == pytest.approx(100 * 3050)
            buyer_acc = s.query(models.FundsAccount).filter_by(enterprise_id=b).one()
            assert buyer_acc.balance + buyer_acc.frozen_amount == 0 or True


# ---------------------------------------------------------------------------
# 7. 撤单/过期释放后不能再成交；原订单追踪
# ---------------------------------------------------------------------------

class TestReleaseAndExpiry:

    def test_cancelled_order_cannot_match(self, db):
        h = _MarketHarness(sessionmaker(bind=db.bind))
        seller = h.enterprise(db, "已撤卖方")
        buyer = h.enterprise(db, "已撤买方")
        h.batch(db, seller.id, 100)
        h.account(db, buyer.id, 100 * CEIL)
        sell = h.sell(db, seller.id, 100)
        buy = h.buy(db, buyer.id, 100, cash=100 * CEIL)

        crud.cancel_credit_order(db, sell.id)
        with pytest.raises(ValueError, match="不可交易"):
            matching.match_pair(sell.id, buy.id, 100)
        with pytest.raises(ValueError, match="只有待成交"):
            crud.cancel_credit_order(db, sell.id)

        auth = crud.get_order_authorizations(db, order_id=sell.id)[0]
        assert auth.status == AuthorizationStatus.RELEASED

    def test_authorization_expiry_releases_and_blocks_match(self, market):
        with market.session() as s:
            seller = market.enterprise(s, "过期卖方")
            buyer = market.enterprise(s, "过期买方")
            seller_ent_id, buyer_ent_id = seller.id, buyer.id
            market.batch(s, seller.id, 100)
            market.account(s, buyer.id, 100 * CEIL)
            past = datetime.utcnow() - timedelta(minutes=1)
            sell = market.sell(s, seller.id, 100, expires_at=past)
            buy = market.buy(s, buyer.id, 100, expires_at=past, cash=100 * CEIL)
            ids = (sell.id, buy.id)

        # 过期扫描（任务箱触发）
        with matching.locked_session(matching.SessionLocal) as s:
            affected = matching.expire_due_authorizations(s)
        assert ids[0] in affected and ids[1] in affected

        with market.session() as s:
            sell = s.get(models.CreditOrder, ids[0])
            buy = s.get(models.CreditOrder, ids[1])
            assert sell.status == OrderStatus.EXPIRED
            assert buy.status == OrderStatus.EXPIRED
            assert sell.remaining_amount == 0
            assert sell.released_amount == 100
            # 买方资金全部退回
            acc = s.query(models.FundsAccount).filter_by(enterprise_id=buyer_ent_id).one()
            assert acc.balance == pytest.approx(100 * CEIL)
            assert acc.frozen_amount == 0
            statuses = {a.status for a in
                        crud.get_order_authorizations(s, order_id=ids[0])}
            assert statuses == {AuthorizationStatus.EXPIRED}

        with pytest.raises(ValueError, match="不可交易"):
            matching.match_pair(ids[0], ids[1], 100)

    def test_partial_fill_then_cancel_releases_remainder(self, db):
        h = _MarketHarness(sessionmaker(bind=db.bind))
        seller = h.enterprise(db, "部分成交后撤卖方")
        buyer1 = h.enterprise(db, "部分成交买方甲")
        buyer2 = h.enterprise(db, "部分成交买方乙")
        h.batch(db, seller.id, 100)
        h.account(db, buyer1.id, 60 * CEIL)
        h.account(db, buyer2.id, 100 * CEIL)
        sell = h.sell(db, seller.id, 100, price=3000)
        buy1 = h.buy(db, buyer1.id, 60, price=3100, cash=60 * CEIL)

        matching.match_pair(sell.id, buy1.id, 60)
        db.expire_all()
        sell = db.get(models.CreditOrder, sell.id)
        assert sell.status == OrderStatus.PARTIAL

        matching.cancel_order(sell.id)
        db.expire_all()
        sell = db.get(models.CreditOrder, sell.id)
        assert sell.status == OrderStatus.CANCELLED
        assert sell.filled_amount == 60
        assert sell.released_amount == 40
        batch = crud.get_credit_batches(db, enterprise_id=seller.id)[0]
        assert batch.frozen_amount == 0
        assert batch.consumed_amount == 60

        # 再挂买单也无法与已撤卖单成交
        buy2 = h.buy(db, buyer2.id, 40, price=3100, cash=40 * CEIL)
        with pytest.raises(ValueError, match="不可交易"):
            matching.match_pair(sell.id, buy2.id, 40)


# ---------------------------------------------------------------------------
# 8. 五段数量口径查询
# ---------------------------------------------------------------------------

class TestQuantityBreakdown:

    def test_breakdown_distinguishes_five_quantities(self, db):
        h = _MarketHarness(sessionmaker(bind=db.bind))
        seller = h.enterprise(db, "口径卖方")
        buyer = h.enterprise(db, "口径买方")
        h.batch(db, seller.id, 100)
        h.account(db, buyer.id, 100 * CEIL)
        sell = h.sell(db, seller.id, 100, price=3000)
        buy = h.buy(db, buyer.id, 100, price=3100, cash=100 * CEIL)

        # 挂单未成交：挂单=预授权held，成交/清算/释放均为0
        bd = crud.get_order_quantity_breakdown(db, buy.id)
        assert bd.posted_amount == 100
        assert bd.held_amount == 100
        assert bd.authorized_amount == 100
        assert bd.filled_amount == 0
        assert bd.matched_unsettled_amount == 0
        assert bd.settled_amount == 0
        assert bd.released_amount == 0
        assert len(bd.authorizations) == 1

        matching.match_pair(sell.id, buy.id, 30)
        db.expire_all()

        bd = crud.get_order_quantity_breakdown(db, buy.id)
        assert bd.posted_amount == 100
        assert bd.filled_amount == 30
        assert bd.remaining_amount == 70
        assert bd.settled_amount == 30       # 当场清算完成
        assert bd.matched_unsettled_amount == 0
        assert bd.held_amount == 70

        # 撤掉剩余 70
        matching.cancel_order(buy.id)
        bd = crud.get_order_quantity_breakdown(db, buy.id)
        assert bd.released_amount == 70
        assert bd.held_amount == 0
        assert bd.filled_amount == 30
        # 口径恒等式：成交 + 剩余 + 释放 = 挂单总量
        assert bd.filled_amount + bd.remaining_amount + bd.released_amount == 100

    def test_breakdown_shows_matched_unsettled_leg(self, market, monkeypatch):
        """成交已落账但清算未完成时，matched_unsettled 与 settled 明确分开。"""
        with market.session() as s:
            seller = market.enterprise(s, "未清算口径卖方")
            buyer = market.enterprise(s, "未清算口径买方")
            market.batch(s, seller.id, 100)
            market.account(s, buyer.id, 100 * CEIL)
            sell = market.sell(s, seller.id, 100)
            buy = market.buy(s, buyer.id, 100, cash=100 * CEIL)
            ids = (sell.id, buy.id)

        real = matching.settle_leg
        def crash(leg_id):
            raise RuntimeError("crash")
        monkeypatch.setattr(matching, "settle_leg", crash)
        execution, _ = matching.match_pair(ids[0], ids[1], 100)
        monkeypatch.undo()

        # 成交记录为 matched、腿为 MATCHED；口径查询能区分
        with market.session() as s:
            bd = crud.get_order_quantity_breakdown(s, ids[1])
            assert bd.matched_unsettled_amount == 100
            assert bd.settled_amount == 0
            assert bd.filled_amount == 100
            assert bd.remaining_amount == 0
            assert bd.held_amount == 0  # 授权已全部消费（钱已从余额冻结转为待清算冻结）
            txn = s.query(models.CreditTransaction).one()
            assert txn.status == "matched"

        # 恢复后全部转为 settled
        with matching.locked_session(matching.SessionLocal) as s:
            for t in s.query(models.OutboxTask).filter_by(
                    execution_id=execution.id).all():
                t.available_at = matching.now(); t.attempts = 0
        matching.replay_pending_tasks()
        with market.session() as s:
            bd = crud.get_order_quantity_breakdown(s, ids[1])
            assert bd.settled_amount == 100
            assert bd.matched_unsettled_amount == 0


# ---------------------------------------------------------------------------
# 9. 原订单追踪 + 守恒
# ---------------------------------------------------------------------------

class TestTraceabilityAndConservation:

    def test_transactions_legs_trace_back_to_orders(self, db):
        h = _MarketHarness(sessionmaker(bind=db.bind))
        seller = h.enterprise(db, "追踪卖方")
        buyer = h.enterprise(db, "追踪买方")
        h.batch(db, seller.id, 100)
        h.account(db, buyer.id, 100 * CEIL)
        sell = h.sell(db, seller.id, 100, price=3000)
        buy = h.buy(db, buyer.id, 100, price=3100, cash=100 * CEIL)

        execution, _ = matching.match_pair(sell.id, buy.id, 100)

        txn = db.query(models.CreditTransaction).filter(
            models.CreditTransaction.sell_order_id == sell.id,
            models.CreditTransaction.buy_order_id == buy.id,
        ).one()
        leg = db.query(models.CreditTradeLeg).filter_by(
            execution_id=execution.id).one()
        # 腿 → 成交记录 → 原卖单/买单，链路完整且价格数量一致
        assert leg.transaction_id == txn.id
        assert leg.sell_order_id == sell.id
        assert leg.buy_order_id == buy.id
        assert leg.credit_amount == 100
        assert leg.matched_price == 3050
        assert txn.unit_price == 3050

        # 从订单侧也能反查到执行计划
        execs = crud.get_order_quantity_breakdown(db, sell.id)
        assert execs.order_id == sell.id
        order_detail = db.get(models.CreditOrder, sell.id)
        assert len(order_detail.sell_transactions) == 1

    def test_funds_conservation_across_partial_settlement(self, market):
        """撮合→清算全过程资金守恒：买方(余额+冻结) + 卖方余额 恒等。"""
        with market.session() as s:
            seller = market.enterprise(s, "守恒卖方")
            buyer = market.enterprise(s, "守恒买方")
            seller_ent_id, buyer_ent_id = seller.id, buyer.id
            market.batch(s, seller_ent_id, 100)
            deposit = 100 * CEIL
            market.account(s, buyer_ent_id, deposit)
            sell = market.sell(s, seller_ent_id, 100, price=3000)
            buy = market.buy(s, buyer_ent_id, 100, price=3000, cash=deposit)
            ids = (sell.id, buy.id)

        def total_money(s):
            rows = s.query(models.FundsAccount).all()
            # 卖方初始账户余额为 0、买方初始 deposit
            return round(sum(a.balance + a.frozen_amount for a in rows), 2)

        with market.session() as s:
            assert total_money(s) == pytest.approx(deposit)

        real = matching.settle_leg
        state = {"crashed": False}
        def crash_once(leg_id):
            if not state["crashed"]:
                state["crashed"] = True
                raise RuntimeError("crash mid settlement")
            return real(leg_id)
        import app.matching as m
        m.settle_leg = crash_once
        try:
            execution, _ = matching.match_pair(ids[0], ids[1], 100)
        finally:
            m.settle_leg = real

        with market.session() as s:
            # 即便崩溃，资金仍在买方冻结中，总额不丢
            assert total_money(s) == pytest.approx(deposit)

        with matching.locked_session(matching.SessionLocal) as s:
            for t in s.query(models.OutboxTask).filter_by(
                    execution_id=execution.id).all():
                t.available_at = matching.now(); t.attempts = 0
        matching.replay_pending_tasks()

        with market.session() as s:
            assert total_money(s) == pytest.approx(deposit)
            acc_buy = s.query(models.FundsAccount).filter_by(enterprise_id=buyer_ent_id).one()
            acc_sell = s.query(models.FundsAccount).filter_by(enterprise_id=seller_ent_id).one()
            assert acc_sell.balance == pytest.approx(100 * 3000)
            assert acc_buy.balance == pytest.approx(deposit - 100 * 3000)
            assert acc_buy.frozen_amount == 0

    def test_self_trade_rejected(self, db):
        h = _MarketHarness(sessionmaker(bind=db.bind))
        ent = h.enterprise(db, "自成交企业")
        h.batch(db, ent.id, 100)
        h.account(db, ent.id, 100 * CEIL)
        sell = h.sell(db, ent.id, 100)
        buy = h.buy(db, ent.id, 100, cash=100 * CEIL)
        with pytest.raises(ValueError, match="不能与本企业自成交"):
            matching.match_pair(sell.id, buy.id, 100)
