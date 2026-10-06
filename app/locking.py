"""
市场撮合的并发控制基础。

SQLite 的写锁语义：同一时刻只有一个连接可以持有 RESERVED 写锁
（BEGIN IMMEDIATE 立即申请），其余连接在 busy_timeout 内排队等待，
而不是在 COMMIT 时才冲突抛错。因此每个会修改订单 / 预授权 / 批次 /
资金账户 / 成交与清算数据的业务流程，都在 locked_session() 内执行：

    并发的两次撮合请求 → 第二个请求在 BEGIN IMMEDIATE 处排队 →
    第一个请求提交释放锁后，第二个请求拿到锁并在同一份最新数据上复查
    （订单状态、授权余额、幂等键），最终同一批挂单只会被消费一次。

业务层另外保证：
1. 锁内做状态复查（double-check），防止等待期间数据已变化；
2. 关键流转有唯一约束兜底（幂等键、execution_no、auth_no 等），
   即使锁机制失效（如未来更换数据库）也不会重复成交；
3. 流程中产生的“待办”（清算、过期释放）写入任务箱并在同一事务提交，
   服务重启后按任务箱回放，回放本身可安全重入。
"""
from contextlib import contextmanager
from datetime import datetime
import uuid

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from .database import engine


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    # 写锁等待窗口：并发写者排队而不是立刻报 database is locked
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


@contextmanager
def locked_session(session_factory):
    """
    提供一个持有数据库级写锁的 Session。

    用法::

        with locked_session(SessionLocal) as db:
            ...  # 全部读写在此处，结束时随 COMMIT 一起原子可见

    实现要点：
    - 在裸 DBAPI 连接上执行 BEGIN IMMEDIATE 拿到写锁；
    - ORM Session 以 create_savepoint 方式绑定到该连接，
      Session.commit() 只释放保存点，不会提前 COMMIT 外层事务；
    - 正常退出时先提交 Session 挂起的变更，再 COMMIT 写锁事务；
    - 异常时回滚，本流程的所有变更（含任务箱写入）整体撤销。
    """
    conn = engine.connect()
    conn.exec_driver_sql("BEGIN IMMEDIATE")
    # expire_on_commit=False：提交后返回的对象标量属性仍可读（会话随后即关闭，
    # 需要最新数据的调用方会在自己的会话中重查），避免 DetachedInstanceError
    db = Session(bind=conn, join_transaction_mode="create_savepoint",
                 future=True, expire_on_commit=False)
    try:
        yield db
        db.commit()
        conn.exec_driver_sql("COMMIT")
    except Exception:
        db.rollback()
        conn.exec_driver_sql("ROLLBACK")
        raise
    finally:
        db.close()
        conn.close()


def now() -> datetime:
    return datetime.utcnow()


def new_instance_id() -> str:
    return f"node-{uuid.uuid4().hex[:12]}"
