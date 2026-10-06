"""
pytest 共享夹具：把撮合引擎的数据库单例（写锁连接工厂）重定向到测试库，
保证直接调用 app.matching 的测试与注入的 db Session 指向同一个 SQLite。
- db：StaticPool 内存库，供串行断言；
- market：临时文件库，供多线程通过 BEGIN IMMEDIATE 真实竞争写锁。
"""
import threading
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import crud, schemas, matching
from app import models
from app.models import OrderType


YEAR = 2025
CEIL = 8000.0


@pytest.fixture(scope="function")
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    # 撮合引擎与写锁工具重定向到本测试库
    import app.locking
    old_engine = app.locking.engine
    old_session = matching.SessionLocal
    app.locking.engine = engine
    matching.SessionLocal = Session
    s = Session()
    try:
        yield s
    finally:
        s.close()
        app.locking.engine = old_engine
        matching.SessionLocal = old_session
        Base.metadata.drop_all(engine)


@pytest.fixture(scope="function")
def market(tmp_path):
    """文件数据库 + 引擎单例重定向：多线程在该库上通过写锁真实串行竞争。"""
    path = tmp_path / "market_concurrent.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    import app.locking
    old_engine = app.locking.engine
    old_session = matching.SessionLocal
    app.locking.engine = engine
    matching.SessionLocal = Session
    try:
        yield _MarketHarness(Session)
    finally:
        app.locking.engine = old_engine
        matching.SessionLocal = old_session
        Base.metadata.drop_all(engine)
        engine.dispose()


class _MarketHarness:
    """测试建市辅助工具：企业、批次、资金账户、买卖挂单。"""

    def __init__(self, session_factory):
        self.Session = session_factory

    def session(self):
        return self.Session()

    def enterprise(self, s, name):
        uniq = uuid.uuid4().hex[:8]
        return crud.create_enterprise(
            s, schemas.EnterpriseCreate(
                name=f"{name}-{uniq}", short_name=f"{name[:6]}{uniq[:4]}",
                credit_code=f"C-{name}-{uniq}")
        )

    def batch(self, s, ent_id, amount, year=YEAR):
        return crud.create_credit_batch(s, enterprise_id=ent_id, year=year,
                                        total_amount=amount)

    def account(self, s, ent_id, balance):
        return crud.create_funds_account_if_absent(
            s, ent_id, initial_balance=balance)

    def sell(self, s, ent_id, amount, price=3000.0, year=YEAR, expires_at=None):
        order = crud.create_credit_order(s, schemas.CreditOrderCreate(
            enterprise_id=ent_id, year=year, order_type=OrderType.SELL,
            unit_price=price, total_amount=amount, expires_at=expires_at,
        ))
        return s.query(models.CreditOrder).filter_by(id=order.id).first()

    def buy(self, s, ent_id, amount, price=3100.0, year=YEAR, cash=None,
            expires_at=None):
        if cash is None:
            cash = amount * CEIL
        self.account(s, ent_id, cash)
        order = crud.create_credit_order(s, schemas.CreditOrderCreate(
            enterprise_id=ent_id, year=year, order_type=OrderType.BUY,
            unit_price=price, total_amount=amount, expires_at=expires_at,
        ))
        return s.query(models.CreditOrder).filter_by(id=order.id).first()
