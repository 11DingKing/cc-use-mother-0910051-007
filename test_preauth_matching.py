"""
成交前预授权撮合引擎测试。

覆盖：
- 挂单即冻结：两个卖单不能共用同一份可售积分；买方余额不足不能挂买单
- 高并发撮合：进程内多线程并发请求，最终只有一个任务、一个成交结果
- 价格优先、同价时间优先、公平部分成交、原订单追踪
- 撤单 / 授权过期 / 服务重启回放均不重复成交
- 挂单/预授权/成交/清算/释放数量五分开查询
- 买方资金账户冻结→清算→释放守恒
"""
import os
import tempfile
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import crud, schemas, matching
from app.models import (
    AuthStatus,
    CreditTransaction,
    MatchTask,
    MatchTaskStatus,
    OrderAuth,
    OrderStatus,
    OrderType,
    ResourceType,
    MarketFundAccount,
)
from app.models import CreditRecordStatus

YEAR = 2025


@pytest.fixture(scope="function")
def engine_factory():
    engines = []

    def _make(url="sqlite:///:memory:"):
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=eng)
        engines.append(eng)
        return eng

    yield _make

    for eng in engines:
        Base.metadata.drop_all(bind=eng)


@pytest.fixture(scope="function")
def db(engine_factory):
    eng = engine_factory()
    Session = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _make_enterprise(db, name, code):
    return crud.create_enterprise(
        db, schemas.EnterpriseCreate(name=name, short_name=name, credit_code=code)
    )


def _give_confirmed_credit(db, ent, total_credit, code="M"):
    """给企业造一笔已确认的正/负积分记录（直接构造，简化测试前置）。"""
    from app.models import VehicleModel, CreditRecord

    model = VehicleModel(
        enterprise_id=ent.id,
        model_name=f"车型-{code}",
        model_code=code,
        curb_weight=1000.0,
        power_consumption=8.0 if total_credit >= 0 else 20.0,
        range=400.0,
        annual_output=abs(int(total_credit)) or 1,
        production_year=YEAR,
    )
    db.add(model)
    db.flush()
    record = CreditRecord(
        vehicle_model_id=model.id,
        year=YEAR,
        power_consumption_limit=10.5,
        actual_power_consumption=model.power_consumption,
        unit_credit=total_credit / (model.annual_output or 1),
        total_credit=float(total_credit),
        annual_output=model.annual_output,
        status=CreditRecordStatus.CONFIRMED,
        calculated_at=datetime.utcnow(),
        publicized_at=datetime.utcnow(),
        confirmed_at=datetime.utcnow(),
    )
    db.add(record)
    db.commit()
    return record


def _sell_order(db, ent_id, amount, price, year=YEAR):
    return matching.create_order_with_auth(
        db,
        schemas.CreditOrderCreate(
            enterprise_id=ent_id, year=year, order_type=OrderType.SELL,
            unit_price=price, total_amount=amount,
        ),
    )


def _buy_order(db, ent_id, amount, price, year=YEAR):
    return matching.create_order_with_auth(
        db,
        schemas.CreditOrderCreate(
            enterprise_id=ent_id, year=year, order_type=OrderType.BUY,
            unit_price=price, total_amount=amount,
        ),
    )


class TestPreAuthOrderPlacement:
    def test_sell_order_freezes_credit_and_blocks_double_spend(self, db):
        """同一企业的两个卖单不能冻结同一份可售积分"""
        seller = _make_enterprise(db, "卖家甲", "E1")
        _give_confirmed_credit(db, seller, 100.0, "M1")

        order1, err1 = _sell_order(db, seller.id, 60.0, 3000.0)
        assert err1 is None and order1 is not None
        assert order1.frozen_amount == 60.0

        # 只剩 40 可售，挂 50 必须被拒
        order2, err2 = _sell_order(db, seller.id, 50.0, 3000.0)
        assert order2 is None
        assert "可售积分钟余" in err2

        # 挂 40 可以
        order3, err3 = _sell_order(db, seller.id, 40.0, 3000.0)
        assert err3 is None and order3 is not None

        # 可售额度归零
        assert matching.sellable_credit(db, seller.id, YEAR) == 0.0

        auths = matching.list_auths(db, enterprise_id=seller.id)
        assert len(auths) == 2
        assert all(a.resource_type == ResourceType.CREDIT for a in auths)

    def test_buy_order_requires_frozen_fund(self, db):
        """买方余额不足不允许挂单，杜绝余额不足的成交"""
        buyer = _make_enterprise(db, "买家乙", "E2")
        matching.deposit_fund(db, buyer.id, YEAR, 100000.0)

        # 需冻结 50 * 3000 = 150000 > 100000
        order, err = _buy_order(db, buyer.id, 50.0, 3000.0)
        assert order is None
        assert "可用资金" in err

        account = (
            db.query(MarketFundAccount)
            .filter(MarketFundAccount.enterprise_id == buyer.id)
            .first()
        )
        assert account.frozen_amount == 0.0  # 失败挂单不留冻结

        order2, err2 = _buy_order(db, buyer.id, 30.0, 3000.0)
        assert err2 is None
        account = db.query(MarketFundAccount).filter(
            MarketFundAccount.enterprise_id == buyer.id
        ).first()
        assert account.frozen_amount == 90000.0


class TestPriceTimePriorityAndPartial:
    def test_price_priority_and_time_priority(self, db):
        """卖单低价先成交、买单高价先成交；同价先挂先成交"""
        seller1 = _make_enterprise(db, "卖家1", "S1")
        seller2 = _make_enterprise(db, "卖家2", "S2")
        seller3 = _make_enterprise(db, "卖家3", "S3")
        buyer = _make_enterprise(db, "买家", "B1")
        _give_confirmed_credit(db, seller1, 100.0, "MS1")
        _give_confirmed_credit(db, seller2, 100.0, "MS2")
        _give_confirmed_credit(db, seller3, 100.0, "MS3")
        matching.deposit_fund(db, buyer.id, YEAR, 1e9)

        o_cheap, _ = _sell_order(db, seller1.id, 50.0, 2900.0)
        o_same_early, _ = _sell_order(db, seller2.id, 50.0, 3000.0)
        o_same_late, _ = _sell_order(db, seller3.id, 50.0, 3000.0)
        bo, _ = _buy_order(db, buyer.id, 120.0, 3100.0)

        task, existing, err = matching.run_auto_match(db, YEAR)
        assert err is None
        assert existing is None
        view = matching.task_view(task)
        succeeded = [i for i in view["items"] if i["status"] == "succeeded"]
        # 50(最便宜) + 50(同价较早) + 20(同价较晚，部分成交)
        assert [(i["sell_order_id"], i["credit_amount"]) for i in succeeded] == [
            (o_cheap.id, 50.0),
            (o_same_early.id, 50.0),
            (o_same_late.id, 20.0),
        ]

        db.refresh(o_same_late)
        assert o_same_late.status == OrderStatus.PARTIAL
        assert o_same_late.filled_amount == 20.0

    def test_fair_partial_fill_when_buy_smaller(self, db):
        """买卖量不等时按较小量公平部分成交"""
        seller = _make_enterprise(db, "卖家", "S")
        buyer = _make_enterprise(db, "买家", "B")
        _give_confirmed_credit(db, seller, 100.0, "MS")
        matching.deposit_fund(db, buyer.id, YEAR, 1e9)

        so, _ = _sell_order(db, seller.id, 100.0, 3000.0)
        bo, _ = _buy_order(db, buyer.id, 30.0, 3000.0)

        matching.run_auto_match(db, YEAR)
        db.refresh(so)
        db.refresh(bo)
        assert so.status == OrderStatus.PARTIAL
        assert so.filled_amount == 30.0
        assert bo.status == OrderStatus.FILLED
        assert bo.remaining_amount == 0.0


class TestConcurrentMatchSingleResult:
    def test_concurrent_auto_match_produces_one_task(self):
        """高并发自动撮合（多线程、独立连接、WAL）：最终只有一个任务、成交不超授权"""
        from sqlalchemy import event

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            eng = create_engine(
                f"sqlite:///{path}",
                connect_args={"check_same_thread": False, "timeout": 30},
            )

            @event.listens_for(eng, "connect")
            def _pragmas(conn, _rec):
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

            Base.metadata.create_all(bind=eng)
            Session = sessionmaker(autocommit=False, autoflush=False, bind=eng)

            setup = Session()
            seller = _make_enterprise(setup, "并发卖家", "CS")
            buyer = _make_enterprise(setup, "并发买家", "CB")
            _give_confirmed_credit(setup, seller, 100.0, "CMS")
            matching.deposit_fund(setup, buyer.id, YEAR, 1e9)
            so, _ = _sell_order(setup, seller.id, 100.0, 3000.0)
            bo, _ = _buy_order(setup, buyer.id, 100.0, 3000.0)
            sell_id, buy_id = so.id, bo.id
            setup.commit()
            setup.close()

            results = []
            errors = []

            def worker():
                session = Session()
                try:
                    task, existing, err = matching.run_auto_match(session, YEAR)
                    results.append(
                        (task.id if task else None,
                         existing.id if existing else None,
                         err)
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    session.close()

            threads = [threading.Thread(target=worker) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, errors
            task_ids = {r[1] or r[0] for r in results if (r[0] or r[1])}
            # 全部线程要么命中同一既有任务，要么拿到同一个新建任务
            assert len(task_ids) == 1, f"并发产生了多个撮合任务: {results}"
            assert all(r[2] is None for r in results), results

            check = Session()
            txn_count = check.query(CreditTransaction).filter(
                CreditTransaction.sell_order_id == sell_id
            ).count()
            txn_volume = (
                check.query(func.coalesce(func.sum(CreditTransaction.credit_amount), 0.0))
                .filter(CreditTransaction.sell_order_id == sell_id)
                .scalar()
            )
            task_count = check.query(MatchTask).count()
            assert txn_count == 1
            assert round(txn_volume, 2) == 100.0  # 12 个并发请求绝不超卖
            assert task_count == 1

            seller_auth = check.query(OrderAuth).filter(
                OrderAuth.order_id == sell_id
            ).first()
            assert seller_auth.status == AuthStatus.CONSUMED
            assert seller_auth.consumed_amount == 100.0
            check.close()
            eng.dispose()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


class TestCancelExpireRelease:
    def test_cancel_releases_credit_and_fund(self, db):
        """撤单释放剩余积分授权与买方冻结资金"""
        seller = _make_enterprise(db, "卖家", "S")
        buyer = _make_enterprise(db, "买家", "B")
        _give_confirmed_credit(db, seller, 100.0, "MS")
        matching.deposit_fund(db, buyer.id, YEAR, 1e9)

        so, _ = _sell_order(db, seller.id, 100.0, 3000.0)
        bo, _ = _buy_order(db, buyer.id, 100.0, 3000.0)

        # 先成交 40
        matching.run_specified_match(
            db,
            schemas.CreditOrderMatchRequest(
                sell_order_id=so.id, buy_order_id=bo.id, credit_amount=40.0
            ),
        )

        order, err = matching.cancel_order(db, so.id)
        assert err is None
        assert order.status == OrderStatus.CANCELLED

        view = matching.order_quantity_view(db, so.id)
        assert view["filled_amount"] == 40.0
        assert view["released_amount"] == 60.0
        assert view["auth"]["available_amount"] == 0.0
        # 释放后积分重新可售
        assert matching.sellable_credit(db, seller.id, YEAR) == 60.0
        # 已成交的撤单不能再撮合
        txn, err2, _ = matching.run_specified_match(
            db,
            schemas.CreditOrderMatchRequest(
                sell_order_id=so.id, buy_order_id=bo.id, credit_amount=10.0
            ),
        )
        assert txn is None
        assert "不可交易" in err2

    def test_expire_auth_releases_and_blocks_retrade(self, db):
        """授权过期自动释放，过期订单不再成交，且过期处理幂等"""
        seller = _make_enterprise(db, "卖家", "S")
        buyer = _make_enterprise(db, "买家", "B")
        _give_confirmed_credit(db, seller, 100.0, "MS")
        matching.deposit_fund(db, buyer.id, YEAR, 1e9)

        past = datetime.utcnow() - timedelta(hours=1)
        so, _ = matching.create_order_with_auth(
            db,
            schemas.CreditOrderCreate(
                enterprise_id=seller.id, year=YEAR, order_type=OrderType.SELL,
                unit_price=3000.0, total_amount=100.0, expires_at=past,
            ),
        )
        bo, _ = matching.create_order_with_auth(
            db,
            schemas.CreditOrderCreate(
                enterprise_id=buyer.id, year=YEAR, order_type=OrderType.BUY,
                unit_price=3000.0, total_amount=100.0, expires_at=past,
            ),
        )

        expired = matching.expire_due_auths(db, YEAR)
        assert len(expired) == 2
        # 幂等：再扫一次不重复释放
        assert matching.expire_due_auths(db, YEAR) == []

        db.refresh(so)
        assert so.status == OrderStatus.CANCELLED
        # 自动撮合不会再吃到过期授权
        task, existing, err = matching.run_auto_match(db, YEAR)
        assert task is None and existing is None
        assert "没有可撮合" in err


class TestReplayIdempotency:
    def test_replay_does_not_duplicate_trades(self, db):
        """模拟重启回放：已成功项跳过，不产生第二笔成交"""
        seller = _make_enterprise(db, "卖家", "S")
        buyer = _make_enterprise(db, "买家", "B")
        _give_confirmed_credit(db, seller, 100.0, "MS")
        matching.deposit_fund(db, buyer.id, YEAR, 1e9)
        so, _ = _sell_order(db, seller.id, 100.0, 3000.0)
        bo, _ = _buy_order(db, buyer.id, 100.0, 3000.0)

        task, _, err = matching.run_auto_match(db, YEAR)
        assert err is None
        txn_before = db.query(CreditTransaction).count()
        assert txn_before == 1

        # 把任务状态伪造成宕机残留，再回放
        task.status = MatchTaskStatus.RUNNING
        db.commit()
        recovered = matching.resume_pending_tasks(db)
        # 任务已全部成功，回放不会重复执行
        assert recovered == []
        assert db.query(CreditTransaction).count() == 1

        # 再次自动撮合：订单已 FILLED、计划为空，请求收敛到最近一次任务，不新增成交
        task2, existing2, err2 = matching.run_auto_match(db, YEAR)
        assert task2 is None and existing2 is not None
        assert existing2.id == task.id
        assert db.query(CreditTransaction).count() == 1

    def test_idempotency_key_blocks_duplicate_specified_match(self, db):
        """同一客户端幂等键的并发/重试请求只成交一次"""
        seller = _make_enterprise(db, "卖家", "S")
        buyer = _make_enterprise(db, "买家", "B")
        _give_confirmed_credit(db, seller, 100.0, "MS")
        matching.deposit_fund(db, buyer.id, YEAR, 1e9)
        so, _ = _sell_order(db, seller.id, 100.0, 3000.0)
        bo, _ = _buy_order(db, buyer.id, 100.0, 3000.0)

        req = schemas.CreditOrderMatchRequest(
            sell_order_id=so.id, buy_order_id=bo.id, credit_amount=50.0,
            idempotency_key="req-001",
        )
        txn1, err1, task1 = matching.run_specified_match(db, req)
        assert err1 is None and txn1 is not None
        txn2, err2, task2 = matching.run_specified_match(db, req)
        # 同键命中既有任务，返回同一笔成交
        assert txn2.id == txn1.id
        assert task2.id == task1.id
        assert db.query(CreditTransaction).count() == 1
        db.refresh(so)
        assert so.filled_amount == 50.0  # 没有被重复扣成 100


class TestQuantityViewAndConservation:
    def test_quantity_view_distinguishes_five_categories(self, db):
        seller = _make_enterprise(db, "卖家", "S")
        buyer = _make_enterprise(db, "买家", "B")
        _give_confirmed_credit(db, seller, 100.0, "MS")
        matching.deposit_fund(db, buyer.id, YEAR, 1e9)
        so, _ = _sell_order(db, seller.id, 100.0, 3000.0)
        bo, _ = _buy_order(db, buyer.id, 40.0, 3200.0)

        txn, err, _ = matching.run_specified_match(
            db,
            schemas.CreditOrderMatchRequest(
                sell_order_id=so.id, buy_order_id=bo.id, credit_amount=40.0
            ),
        )
        assert err is None

        sv = matching.order_quantity_view(db, so.id)
        bv = matching.order_quantity_view(db, bo.id)

        # 卖单：挂单100 / 成交40 / 剩60 / 清算40
        assert sv["posted_amount"] == 100.0
        assert sv["filled_amount"] == 40.0
        assert sv["remaining_amount"] == 60.0
        assert sv["matched_amount"] == 40.0
        assert sv["cleared_amount"] == 40.0
        assert sv["uncleared_amount"] == 0.0
        assert sv["auth"]["frozen_amount"] == 100.0
        assert sv["auth"]["consumed_amount"] == 40.0

        # 买单全部成交
        assert bv["filled_amount"] == 40.0
        assert bv["remaining_amount"] == 0.0

        # 成交即清算：状态 cleared、清算号、买卖订单追踪齐全
        assert txn.status == "cleared"
        assert txn.settled_amount == 40.0
        assert txn.settlement_no
        assert txn.sell_order_id == so.id
        assert txn.buy_order_id == bo.id
        # 成交价 = 均价 3100
        assert txn.unit_price == 3100.0

    def test_fund_conservation_with_price_diff_release(self, db):
        """买单按报价冻结、按成交价清算，价差退回，资金全程守恒"""
        seller = _make_enterprise(db, "卖家", "S")
        buyer = _make_enterprise(db, "买家", "B")
        _give_confirmed_credit(db, seller, 100.0, "MS")
        matching.deposit_fund(db, buyer.id, YEAR, 1e9)

        so, _ = _sell_order(db, seller.id, 100.0, 3000.0)
        bo, _ = _buy_order(db, buyer.id, 10.0, 4000.0)

        matching.run_auto_match(db, YEAR)

        acct = db.query(MarketFundAccount).filter(
            MarketFundAccount.enterprise_id == buyer.id
        ).first()
        # 冻结 10*4000=40000；成交 10*3500=35000；价差 5000 解冻回可用
        assert acct.consumed_amount == 35000.0
        assert acct.frozen_amount == 0.0
        # 余额 = 1e9 - 实际支出 35000
        assert round(acct.balance - acct.consumed_amount, 2) == round(1e9 - 35000.0, 2)

        buy_auth = matching.list_auths(db, order_id=bo.id)[0]
        assert buy_auth.consumed_fund == 35000.0
        assert buy_auth.released_fund == 5000.0

    def test_partial_task_result_is_recoverable(self, db):
        """任务内某一笔失败时记录为可恢复部分结果，其余笔正常落账"""
        # 构造：两个卖单共享一个买方；手工让第二个卖单授权在执行前失效
        s1 = _make_enterprise(db, "卖家1", "S1")
        s2 = _make_enterprise(db, "卖家2", "S2")
        b = _make_enterprise(db, "买家", "B")
        _give_confirmed_credit(db, s1, 100.0, "M1")
        _give_confirmed_credit(db, s2, 100.0, "M2")
        matching.deposit_fund(db, b.id, YEAR, 1e9)

        o1, _ = _sell_order(db, s1.id, 100.0, 3000.0)
        o2, _ = _sell_order(db, s2.id, 100.0, 3010.0)
        bo, _ = _buy_order(db, b.id, 150.0, 3100.0)

        # 买方授权只有 150：计划为 100(o1)+50(o2)。撤掉 o2 后再执行任务，
        # 模拟"计划生成后、执行前授权被释放"的部分失败
        plan = matching.plan_auto_match(db, YEAR)
        assert [(p["sell_order_id"], p["credit_amount"]) for p in plan] == [
            (o1.id, 100.0), (o2.id, 50.0)
        ]
        key = matching._auto_idempotency_key(db, YEAR, plan)
        task, existing, _ = matching._create_task(db, YEAR, "auto", plan, key)
        assert existing is None

        # 任务已创建后释放 o2 的授权
        matching.cancel_order(db, o2.id)

        matching.execute_task(db, task)
        db.refresh(task)
        assert task.status == MatchTaskStatus.PARTIAL
        assert task.completed_items == 1
        assert round(task.completed_credit_amount, 2) == 100.0
        failed = [i for i in task.items if i.status == "failed"]
        assert len(failed) == 1
        assert "授权" in failed[0].error_detail
        # 成功的一笔确实落账并清算，且追踪到原订单
        txn = db.query(CreditTransaction).filter(
            CreditTransaction.sell_order_id == o1.id
        ).one()
        assert txn.status == "cleared"
        assert txn.buy_order_id == bo.id
