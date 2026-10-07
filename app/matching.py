"""
成交前预授权撮合引擎。

设计要点：
1. 挂单即冻结：卖单冻结企业积分钟余(credit 授权)，买单冻结资金额度(fund 授权)。
   撮合过程中只消费"有效授权"(frozen - consumed - released)，从根本上杜绝
   两个卖单共用同一份积分、买方余额不足产生已成交记录的问题。
2. 防并发双花：所有额度扣减都走带余量条件的 UPDATE（CAS 风格）：
       UPDATE ... SET consumed = consumed + :x
       WHERE id = :id AND frozen - consumed - released >= :x
   并发请求只有一个能扣减成功；配合 WAL + busy_timeout 与进程内撮合锁。
3. 幂等任务：一次撮合(可能涉及多笔订单对)建模为 MatchTask + 有序 MatchTaskItem。
   成交号带幂等键，撤单/授权过期/服务重启后回放会跳过已成功项，绝不重复成交；
   失败项落库为可恢复的部分结果(task=partial)。
4. 数量五分开：挂单 remaining/filled、授权 frozen/consumed/released、
   成交 credit_amount、清算 settled_amount、释放 released 各自独立可查。
"""
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session

from . import crud, models, schemas
from .models import (
    AuthStatus,
    MatchTaskStatus,
    OrderStatus,
    OrderType,
    ResourceType,
)

EPS = 0.01


def _r2(x: float) -> float:
    return round(float(x or 0.0), 2)


def _now() -> datetime:
    return datetime.utcnow()


# 进程内撮合串行锁：同一进程内的高并发撮合请求按到达顺序排队，
# 排队后的请求通过幂等键/条件更新发现"结果已经存在"，直接返回同一个结果。
_MATCH_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 编号生成
# ---------------------------------------------------------------------------

def _gen_no(db: Session, prefix: str, model) -> str:
    ts = _now().strftime("%Y%m%d%H%M%S%f")[:-3]
    return f"{prefix}{ts}"


def _gen_auth_no(db: Session) -> str:
    return _gen_no(db, "AUTH", models.OrderAuth)


def _gen_task_no(db: Session) -> str:
    return _gen_no(db, "MT", models.MatchTask)


def _gen_settlement_no(db: Session) -> str:
    return _gen_no(db, "STL", models.CreditTransaction)


def _gen_txn_no(db: Session) -> str:
    return _gen_no(db, "TXN", models.CreditTransaction)


# ---------------------------------------------------------------------------
# 资金账户
# ---------------------------------------------------------------------------

def get_or_create_fund_account(
    db: Session, enterprise_id: int, year: int
) -> models.MarketFundAccount:
    account = db.query(models.MarketFundAccount).filter(
        models.MarketFundAccount.enterprise_id == enterprise_id,
        models.MarketFundAccount.year == year,
    ).first()
    if account:
        return account
    account = models.MarketFundAccount(
        enterprise_id=enterprise_id, year=year, balance=0.0
    )
    db.add(account)
    db.flush()
    return account


def deposit_fund(
    db: Session, enterprise_id: int, year: int, amount: float
) -> models.MarketFundAccount:
    """向买方资金账户充值（授信额度）。"""
    if amount <= 0:
        raise ValueError("充值金额必须大于0")
    with _MATCH_LOCK:
        account = get_or_create_fund_account(db, enterprise_id, year)
        account.balance = _r2(account.balance + amount)
        account.updated_at = _now()
        db.commit()
        db.refresh(account)
        return account


def _available_fund(account: models.MarketFundAccount) -> float:
    """可用资金 = 总授信 - 冻结中 - 已清算支出（释放回可用的部分本就不在 frozen 中）。"""
    return _r2(account.balance - account.frozen_amount - account.consumed_amount)


def _freeze_fund_conditional(
    db: Session, account_id: int, amount: float
) -> bool:
    """条件更新冻结资金：余额不足时一行都不锁，返回 False。"""
    amount = _r2(amount)
    stmt = (
        update(models.MarketFundAccount)
        .where(
            models.MarketFundAccount.id == account_id,
            models.MarketFundAccount.balance
            - models.MarketFundAccount.frozen_amount
            - models.MarketFundAccount.consumed_amount
            >= amount - 0.005,
        )
        .values(
            frozen_amount=models.MarketFundAccount.frozen_amount + amount,
            updated_at=_now(),
        )
    )
    result = db.execute(stmt)
    return (result.rowcount or 0) == 1


def _move_fund_for_settlement(
    db: Session, account_id: int, frozen_pay: float, actual_pay: float
) -> bool:
    """清算时把冻结资金转为已支出，并把价差部分解冻回可用。"""
    frozen_pay = _r2(frozen_pay)
    actual_pay = _r2(actual_pay)
    stmt = (
        update(models.MarketFundAccount)
        .where(
            models.MarketFundAccount.id == account_id,
            models.MarketFundAccount.frozen_amount >= frozen_pay - 0.005,
        )
        .values(
            frozen_amount=models.MarketFundAccount.frozen_amount - frozen_pay,
            consumed_amount=models.MarketFundAccount.consumed_amount + actual_pay,
            updated_at=_now(),
        )
    )
    result = db.execute(stmt)
    return (result.rowcount or 0) == 1


def _release_fund_conditional(
    db: Session, account_id: int, amount: float
) -> bool:
    """释放冻结资金回可用余额（撤单/授权过期）。"""
    amount = _r2(amount)
    stmt = (
        update(models.MarketFundAccount)
        .where(
            models.MarketFundAccount.id == account_id,
            models.MarketFundAccount.frozen_amount >= amount - 0.005,
        )
        .values(
            frozen_amount=models.MarketFundAccount.frozen_amount - amount,
            released_amount=models.MarketFundAccount.released_amount + amount,
            updated_at=_now(),
        )
    )
    result = db.execute(stmt)
    return (result.rowcount or 0) == 1


# ---------------------------------------------------------------------------
# 可售积分额度
# ---------------------------------------------------------------------------

def _active_frozen_credit(
    db: Session, enterprise_id: int, year: int, exclude_order_id: Optional[int] = None
) -> float:
    """该企业该年度已被其他活跃卖单冻结、尚未成交/释放的积分。"""
    q = db.query(
        func.coalesce(
            func.sum(
                models.OrderAuth.frozen_amount
                - models.OrderAuth.consumed_amount
                - models.OrderAuth.released_amount
            ),
            0.0,
        )
    ).filter(
        models.OrderAuth.enterprise_id == enterprise_id,
        models.OrderAuth.year == year,
        models.OrderAuth.resource_type == ResourceType.CREDIT,
        models.OrderAuth.status.in_(
            [AuthStatus.FROZEN, AuthStatus.PARTIAL_CONSUMED]
        ),
    )
    if exclude_order_id is not None:
        q = q.filter(models.OrderAuth.order_id != exclude_order_id)
    return _r2(q.scalar())


def sellable_credit(db: Session, enterprise_id: int, year: int) -> float:
    """当前可被卖单冻结的积分钟余 = 最终积分钟余 - 已冻结待成交积分。"""
    from . import crud

    summary = crud.calculate_enterprise_credit_summary_v2(db, enterprise_id, year)
    surplus = max(0.0, summary.final_credit_surplus)
    frozen = _active_frozen_credit(db, enterprise_id, year)
    return _r2(max(0.0, surplus - frozen))


# ---------------------------------------------------------------------------
# 授权条件更新（防双花的核心）
# ---------------------------------------------------------------------------

def _consume_auth_conditional(
    db: Session, auth_id: int, amount: float
) -> bool:
    """
    消费有效授权。只有 frozen-consumed-released >= amount 时才能扣减成功。
    并发的两个撮合在此处被数据库裁决：恰好一个成功，另一个 rowcount=0。
    """
    amount = _r2(amount)
    new_consumed = models.OrderAuth.consumed_amount + amount
    new_status = case(
        (
            models.OrderAuth.frozen_amount - new_consumed - models.OrderAuth.released_amount
            <= EPS,
            AuthStatus.CONSUMED.name,
        ),
        else_=AuthStatus.PARTIAL_CONSUMED.name,
    )
    stmt = (
        update(models.OrderAuth)
        .where(
            models.OrderAuth.id == auth_id,
            models.OrderAuth.status.in_(
                [AuthStatus.FROZEN, AuthStatus.PARTIAL_CONSUMED]
            ),
            models.OrderAuth.frozen_amount
            - models.OrderAuth.consumed_amount
            - models.OrderAuth.released_amount
            >= amount - 0.005,
        )
        .values(
            consumed_amount=new_consumed,
            status=new_status,
            consumed_at=_now(),
            updated_at=_now(),
        )
    )
    result = db.execute(stmt)
    return (result.rowcount or 0) == 1


def _advance_order_conditional(
    db: Session, order_id: int, amount: float
) -> bool:
    """推进订单成交数量，带剩余量条件，防止越过挂单总量成交。"""
    amount = _r2(amount)
    new_filled = models.CreditOrder.filled_amount + amount
    new_remaining = models.CreditOrder.remaining_amount - amount
    new_status = case(
        (new_remaining <= EPS, OrderStatus.FILLED.name),
        else_=OrderStatus.PARTIAL.name,
    )
    stmt = (
        update(models.CreditOrder)
        .where(
            models.CreditOrder.id == order_id,
            models.CreditOrder.status.in_([OrderStatus.PENDING, OrderStatus.PARTIAL]),
            models.CreditOrder.remaining_amount >= amount - 0.005,
        )
        .values(
            filled_amount=new_filled,
            remaining_amount=new_remaining,
            status=new_status,
            updated_at=_now(),
        )
    )
    result = db.execute(stmt)
    return (result.rowcount or 0) == 1


def _compensate_auth(db: Session, auth_id: int, amount: float) -> None:
    """冲正已消费的授权数量（后续步骤失败时调用）。"""
    amount = _r2(amount)
    new_consumed = models.OrderAuth.consumed_amount - amount
    new_status = case(
        (new_consumed <= EPS, AuthStatus.FROZEN.name),
        else_=AuthStatus.PARTIAL_CONSUMED.name,
    )
    db.execute(
        update(models.OrderAuth)
        .where(
            models.OrderAuth.id == auth_id,
            models.OrderAuth.consumed_amount >= amount - 0.005,
        )
        .values(consumed_amount=new_consumed, status=new_status, updated_at=_now())
    )


def _rollback_order(db: Session, order_id: int, amount: float) -> None:
    """冲正订单成交推进。"""
    amount = _r2(amount)
    new_filled = models.CreditOrder.filled_amount - amount
    new_remaining = models.CreditOrder.remaining_amount + amount
    new_status = case(
        (new_filled <= EPS, OrderStatus.PENDING.name),
        else_=OrderStatus.PARTIAL.name,
    )
    db.execute(
        update(models.CreditOrder)
        .where(
            models.CreditOrder.id == order_id,
            models.CreditOrder.filled_amount >= amount - 0.005,
        )
        .values(
            filled_amount=new_filled,
            remaining_amount=new_remaining,
            status=new_status,
            updated_at=_now(),
        )
    )


def _release_auth_remaining(
    db: Session, auth: models.OrderAuth, new_status: AuthStatus
) -> float:
    """释放授权的全部剩余额度；资金授权同步把冻结资金退回可用。"""
    remaining = _r2(
        auth.frozen_amount - auth.consumed_amount - auth.released_amount
    )
    if remaining <= EPS:
        remaining = 0.0
    fund_remaining = _r2(auth.frozen_fund - auth.consumed_fund - auth.released_fund)

    if remaining > 0:
        auth.released_amount = _r2(auth.released_amount + remaining)
    if fund_remaining > 0:
        account = (
            db.query(models.MarketFundAccount)
            .filter(
                models.MarketFundAccount.enterprise_id == auth.enterprise_id,
                models.MarketFundAccount.year == auth.year,
            )
            .first()
        )
        if account:
            _release_fund_conditional(db, account.id, fund_remaining)
        auth.released_fund = _r2(auth.released_fund + fund_remaining)

    auth.status = new_status
    auth.released_at = _now()
    auth.updated_at = _now()

    order = db.query(models.CreditOrder).filter(
        models.CreditOrder.id == auth.order_id
    ).first()
    if order and remaining > 0:
        order.released_amount = _r2(order.released_amount + remaining)
        order.updated_at = _now()
    return remaining


# ---------------------------------------------------------------------------
# 挂单（成交前预授权）
# ---------------------------------------------------------------------------

def create_order_with_auth(
    db: Session, order_in: schemas.CreditOrderCreate
) -> Tuple[Optional[models.CreditOrder], Optional[str]]:
    """挂单并立即冻结对应资源。任一冻结失败则整单不成立。"""
    from . import crud
    from .rules import validate_order_price

    valid, err = validate_order_price(order_in.unit_price)
    if not valid:
        return None, err

    enterprise = crud.get_enterprise(db, order_in.enterprise_id)
    if not enterprise:
        return None, "企业不存在"

    total = _r2(order_in.total_amount)
    if total <= 0:
        return None, "挂单数量必须大于0"
    expires_at = order_in.expires_at or (_now() + timedelta(days=90))

    with _MATCH_LOCK:
        try:
            if order_in.order_type == OrderType.SELL:
                available = sellable_credit(
                    db, order_in.enterprise_id, order_in.year
                )
                if total > available + EPS:
                    return None, (
                        f"挂单数量 {total} 超过当前可售积分钟余 {available}"
                        "（已有卖单冻结的积分不可重复出售）"
                    )
            else:
                account = get_or_create_fund_account(
                    db, order_in.enterprise_id, order_in.year
                )
                db.flush()
                required_fund = _r2(total * order_in.unit_price)
                if _available_fund(account) + EPS < required_fund:
                    return None, (
                        f"买单需冻结资金 {required_fund} 元，"
                        f"可用资金仅 {_available_fund(account)} 元，请先授信/充值"
                    )

            order_no = crud.generate_order_no(db)
            order = models.CreditOrder(
                enterprise_id=order_in.enterprise_id,
                year=order_in.year,
                order_type=order_in.order_type,
                unit_price=order_in.unit_price,
                total_amount=total,
                filled_amount=0.0,
                remaining_amount=total,
                frozen_amount=total,
                released_amount=0.0,
                status=OrderStatus.PENDING,
                remark=order_in.remark,
                expires_at=expires_at,
                order_no=order_no,
            )
            db.add(order)
            db.flush()

            if order_in.order_type == OrderType.SELL:
                auth = models.OrderAuth(
                    auth_no=_gen_auth_no(db),
                    order_id=order.id,
                    enterprise_id=order.enterprise_id,
                    year=order.year,
                    resource_type=ResourceType.CREDIT,
                    status=AuthStatus.FROZEN,
                    frozen_amount=total,
                    expires_at=expires_at,
                    remark="挂单冻结卖方积分批次",
                )
            else:
                required_fund = _r2(total * order.unit_price)
                if not _freeze_fund_conditional(db, account.id, required_fund):
                    db.rollback()
                    return None, "资金冻结失败：可用资金不足"
                auth = models.OrderAuth(
                    auth_no=_gen_auth_no(db),
                    order_id=order.id,
                    enterprise_id=order.enterprise_id,
                    year=order.year,
                    resource_type=ResourceType.FUND,
                    status=AuthStatus.FROZEN,
                    frozen_amount=total,
                    frozen_fund=required_fund,
                    expires_at=expires_at,
                    remark="挂单冻结买方资金额度",
                )
            db.add(auth)
            db.flush()
            db.commit()
            db.refresh(order)
            return order, None
        except Exception:
            db.rollback()
            raise


# ---------------------------------------------------------------------------
# 撤单 / 授权过期（释放，不重复成交）
# ---------------------------------------------------------------------------

def cancel_order(db: Session, order_id: int) -> Tuple[Optional[models.CreditOrder], Optional[str]]:
    with _MATCH_LOCK:
        order = db.query(models.CreditOrder).filter(
            models.CreditOrder.id == order_id
        ).first()
        if not order:
            return None, "订单不存在"
        if order.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            return None, f"订单当前状态 {order.status.value} 不允许取消"
        try:
            for auth in order.auths:
                if auth.status in (AuthStatus.FROZEN, AuthStatus.PARTIAL_CONSUMED):
                    _release_auth_remaining(db, auth, AuthStatus.RELEASED)
            order.status = OrderStatus.CANCELLED
            order.updated_at = _now()
            db.commit()
            db.refresh(order)
            return order, None
        except Exception:
            db.rollback()
            raise


def expire_due_auths(
    db: Session, year: Optional[int] = None, now: Optional[datetime] = None
) -> List[models.OrderAuth]:
    """扫描过期授权并释放；幂等，可被定时任务与服务重启回放共同调用。"""
    now = now or _now()
    expired: List[models.OrderAuth] = []
    with _MATCH_LOCK:
        q = db.query(models.OrderAuth).filter(
            models.OrderAuth.status.in_(
                [AuthStatus.FROZEN, AuthStatus.PARTIAL_CONSUMED]
            ),
            models.OrderAuth.expires_at <= now,
        )
        if year is not None:
            q = q.filter(models.OrderAuth.year == year)
        auths = q.all()
        if not auths:
            return []
        try:
            for auth in auths:
                _release_auth_remaining(db, auth, AuthStatus.EXPIRED)
                order = db.query(models.CreditOrder).filter(
                    models.CreditOrder.id == auth.order_id
                ).first()
                # 整单未再成交且已过期：订单置为取消(过期)；部分成交的保留 partial 事实
                if order and order.expires_at and order.expires_at <= now:
                    if order.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
                        if order.remaining_amount <= EPS:
                            order.status = OrderStatus.FILLED
                        else:
                            order.status = OrderStatus.CANCELLED
                            order.remark = (order.remark or "") + "（授权过期自动撤单）"
                        order.updated_at = _now()
                expired.append(auth)
            db.commit()
            for auth in expired:
                db.refresh(auth)
            return expired
        except Exception:
            db.rollback()
            raise


# ---------------------------------------------------------------------------
# 撮合计划：价格优先、同价时间优先（再以 id 兜底，保证全序确定）
# ---------------------------------------------------------------------------

def _active_orders(db: Session, year: int, otype: OrderType) -> List[models.CreditOrder]:
    orders = (
        db.query(models.CreditOrder)
        .filter(
            models.CreditOrder.year == year,
            models.CreditOrder.order_type == otype,
            models.CreditOrder.status.in_([OrderStatus.PENDING, OrderStatus.PARTIAL]),
        )
        .order_by(
            models.CreditOrder.created_at.asc(),
            models.CreditOrder.id.asc(),
        )
        .all()
    )
    now = _now()
    return [o for o in orders if not o.expires_at or o.expires_at > now]


def _auth_for_order(db: Session, order_id: int) -> Optional[models.OrderAuth]:
    return (
        db.query(models.OrderAuth)
        .filter(
            models.OrderAuth.order_id == order_id,
            models.OrderAuth.status.in_(
                [AuthStatus.FROZEN, AuthStatus.PARTIAL_CONSUMED]
            ),
        )
        .order_by(models.OrderAuth.id.asc())
        .first()
    )


def plan_auto_match(db: Session, year: int) -> List[dict]:
    """
    生成确定性的成交计划：
    - 卖单价格低者优先；买单价格高者优先；同价先挂先成交(id 兜底全序)
    - 数量以"有效授权"为准，天然只消费冻结额度，支持公平部分成交
    - 成交价取买卖报价均值（与原规则一致）
    """
    sells = _active_orders(db, year, OrderType.SELL)
    buys = _active_orders(db, year, OrderType.BUY)

    sell_items = []
    for o in sells:
        auth = _auth_for_order(db, o.id)
        avail = auth.available_amount if auth else 0.0
        if avail > EPS:
            sell_items.append({"order": o, "auth": auth, "remaining": avail})
    buy_items = []
    for o in buys:
        auth = _auth_for_order(db, o.id)
        avail = auth.available_amount if auth else 0.0
        if avail > EPS:
            buy_items.append({"order": o, "auth": auth, "remaining": avail})

    sell_items.sort(key=lambda x: (x["order"].unit_price, x["order"].created_at, x["order"].id))
    buy_items.sort(
        key=lambda x: (-x["order"].unit_price, x["order"].created_at, x["order"].id)
    )

    plan: List[dict] = []
    i = j = 0
    while i < len(sell_items) and j < len(buy_items):
        s = sell_items[i]
        b = buy_items[j]
        if b["order"].unit_price < s["order"].unit_price:
            break  # 买单最高价仍低于卖单最低价，后续都不可能成交

        amount = _r2(min(s["remaining"], b["remaining"]))
        if amount <= EPS:
            break
        price = _r2((s["order"].unit_price + b["order"].unit_price) / 2)
        plan.append(
            {
                "sell_order_id": s["order"].id,
                "buy_order_id": b["order"].id,
                "sell_auth_id": s["auth"].id,
                "buy_auth_id": b["auth"].id,
                "credit_amount": amount,
                "matched_price": price,
                "total_amount": _r2(amount * price),
            }
        )
        s["remaining"] = _r2(s["remaining"] - amount)
        b["remaining"] = _r2(b["remaining"] - amount)
        if s["remaining"] <= EPS:
            i += 1
        if b["remaining"] <= EPS:
            j += 1
    return plan


def _auto_idempotency_key(db: Session, year: int, plan: List[dict]) -> str:
    """
    用参与订单及其当时剩余量的指纹作为幂等键：
    并发的两个 match-all 请求看到同一本订单 → 同指纹 → 唯一约束只放行一个；
    成交完成后订单余量改变，后续请求指纹不同，属于新一轮成交，不会被误吞。
    """
    order_ids = sorted(
        {p["sell_order_id"] for p in plan} | {p["buy_order_id"] for p in plan}
    )
    snapshot = []
    for oid in order_ids:
        o = db.query(models.CreditOrder).filter(models.CreditOrder.id == oid).first()
        auth = _auth_for_order(db, oid)
        avail = auth.available_amount if auth else 0.0
        snapshot.append(f"{oid}:{_r2(avail)}")
    digest = hashlib.sha1("|".join(snapshot).encode()).hexdigest()[:16]
    return f"auto:{year}:{digest}"


def _spec_idempotency_key(req: schemas.CreditOrderMatchRequest) -> str:
    if req.idempotency_key:
        # 客户端显式幂等键：并发/重试严格收敛到同一个结果
        return f"client:{req.idempotency_key}"
    raw = f"spec:{req.sell_order_id}:{req.buy_order_id}:{_r2(req.credit_amount)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# 任务创建与执行
# ---------------------------------------------------------------------------

def _create_task(
    db: Session,
    year: int,
    task_type: str,
    plan: List[dict],
    idem_key: str,
) -> Tuple[Optional[models.MatchTask], Optional[models.MatchTask], Optional[str]]:
    """
    返回 (task, existing, error)。
    若相同幂等键的任务已存在（典型：并发重复请求），existing 即既有结果，task=None。
    """
    existing = (
        db.query(models.MatchTask)
        .filter(models.MatchTask.idempotency_key == idem_key)
        .first()
    )
    if existing:
        return None, existing, None

    task = models.MatchTask(
        task_no=_gen_task_no(db),
        idempotency_key=idem_key,
        year=year,
        task_type=task_type,
        status=MatchTaskStatus.PENDING,
        total_items=len(plan),
        total_credit_amount=_r2(sum(p["credit_amount"] for p in plan)),
    )
    db.add(task)
    try:
        db.flush()
    except Exception:
        # 唯一索引竞争：另一个并发请求已插入同键任务
        db.rollback()
        existing = (
            db.query(models.MatchTask)
            .filter(models.MatchTask.idempotency_key == idem_key)
            .first()
        )
        if existing:
            return None, existing, None
        raise

    for seq, p in enumerate(plan, start=1):
        db.add(
            models.MatchTaskItem(
                task_id=task.id,
                seq=seq,
                sell_order_id=p["sell_order_id"],
                buy_order_id=p["buy_order_id"],
                sell_auth_id=p["sell_auth_id"],
                buy_auth_id=p["buy_auth_id"],
                credit_amount=p["credit_amount"],
                matched_price=p["matched_price"],
                total_amount=p["total_amount"],
                status="pending",
            )
        )
    db.commit()
    db.refresh(task)
    return task, None, None


def _create_specified_task(
    db: Session, req: schemas.CreditOrderMatchRequest
) -> Tuple[Optional[models.MatchTask], Optional[models.MatchTask], Optional[str]]:
    sell = db.query(models.CreditOrder).filter(
        models.CreditOrder.id == req.sell_order_id
    ).first()
    buy = db.query(models.CreditOrder).filter(
        models.CreditOrder.id == req.buy_order_id
    ).first()
    if not sell or not buy:
        return None, None, "订单不存在"
    if sell.order_type != OrderType.SELL:
        return None, None, "卖单类型错误"
    if buy.order_type != OrderType.BUY:
        return None, None, "买单类型错误"
    if sell.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
        return None, None, "卖单状态不可交易"
    if buy.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
        return None, None, "买单状态不可交易"
    if buy.year != sell.year:
        return None, None, "买卖订单年度不一致"
    if buy.unit_price < sell.unit_price:
        return None, None, (
            f"买单价格({buy.unit_price})低于卖单价格({sell.unit_price})，无法成交"
        )

    sell_auth = _auth_for_order(db, sell.id)
    buy_auth = _auth_for_order(db, buy.id)
    if not sell_auth or sell_auth.available_amount <= EPS:
        return None, None, "卖单缺少有效积分预授权（可能已被其他成交消费或已释放）"
    if not buy_auth or buy_auth.available_amount <= EPS:
        return None, None, "买单缺少有效资金预授权（可能已被其他成交消费或已释放）"

    amount = _r2(min(req.credit_amount, sell_auth.available_amount, buy_auth.available_amount))
    if amount <= EPS:
        return None, None, "可成交数量不足"
    price = _r2((sell.unit_price + buy.unit_price) / 2)
    plan = [
        {
            "sell_order_id": sell.id,
            "buy_order_id": buy.id,
            "sell_auth_id": sell_auth.id,
            "buy_auth_id": buy_auth.id,
            "credit_amount": amount,
            "matched_price": price,
            "total_amount": _r2(amount * price),
        }
    ]
    base_key = _spec_idempotency_key(req)
    if req.idempotency_key:
        key = base_key  # 客户端幂等键不受余量快照影响，重试严格收敛
    else:
        # 附带双方余量快照：并发重复请求同键去重；余量变化后的再次成交属于新意图
        key = base_key + f":{_r2(sell_auth.available_amount)}:{_r2(buy_auth.available_amount)}"
    return _create_task(db, sell.year, "specified", plan, key)


def _execute_item(db: Session, task: models.MatchTask, item: models.MatchTaskItem) -> None:
    """
    执行单个订单对：消费双边授权 → 推进订单 → 写成交(幂等) → 立即清算。
    所有余量扣减均为条件更新，竞争失败时本项记为 failed（可恢复的部分结果），
    不影响后续订单对继续落账。
    """
    # 丢弃可能过期的身份映射缓存，确保本项读到前序项扣减后的最新额度
    db.expire_all()

    # 已成交项绝不重复执行（回放/重试的幂等保障）
    if item.status == "succeeded" and item.transaction_id:
        return

    existing_txn = (
        db.query(models.CreditTransaction)
        .filter(
            models.CreditTransaction.idempotency_key
            == f"{task.task_no}:{item.seq}"
        )
        .first()
    )
    if existing_txn:
        item.status = "succeeded"
        item.transaction_id = existing_txn.id
        item.executed_at = existing_txn.created_at
        task.completed_items = (task.completed_items or 0) + 1
        task.completed_credit_amount = _r2(
            task.completed_credit_amount + existing_txn.credit_amount
        )
        return

    sell_auth = db.get(models.OrderAuth, item.sell_auth_id)
    buy_auth = db.get(models.OrderAuth, item.buy_auth_id)
    sell_order = db.get(models.CreditOrder, item.sell_order_id)
    buy_order = db.get(models.CreditOrder, item.buy_order_id)
    if not all([sell_auth, buy_auth, sell_order, buy_order]):
        item.status = "failed"
        item.error_detail = "关联订单或授权不存在"
        return

    # 公平部分成交：若执行时有效额度因并发被吃掉，按当前可成交量收缩，而不是超额成交
    amount = _r2(
        min(
            item.credit_amount,
            sell_auth.available_amount,
            buy_auth.available_amount,
            sell_order.remaining_amount,
            buy_order.remaining_amount,
        )
    )
    if amount <= EPS:
        item.status = "failed"
        item.error_detail = "有效授权或订单余量不足，无法成交"
        return

    price = item.matched_price
    total = _r2(amount * price)
    # 买单按报价冻结，实际按成交均价清算
    frozen_pay = _r2(amount * buy_order.unit_price)
    actual_pay = total

    # 1) 消费卖方积分授权（条件更新，竞争失败即本项失败）
    if not _consume_auth_conditional(db, sell_auth.id, amount):
        item.status = "failed"
        item.error_detail = "卖方积分授权消费失败：并发请求已抢先成交或授权已释放"
        return
    # 2) 消费买方资金授权（积分额度维度）
    if not _consume_auth_conditional(db, buy_auth.id, amount):
        _compensate_auth(db, sell_auth.id, amount)  # 回滚卖方本项消费
        item.status = "failed"
        item.error_detail = "买方资金授权消费失败：并发请求已抢先成交或授权已释放"
        return

    # 3) 推进双边订单
    sell_advanced = _advance_order_conditional(db, sell_order.id, amount)
    buy_advanced = _advance_order_conditional(db, buy_order.id, amount)
    if not sell_advanced or not buy_advanced:
        _compensate_auth(db, sell_auth.id, amount)
        _compensate_auth(db, buy_auth.id, amount)
        if sell_advanced:
            _rollback_order(db, sell_order.id, amount)
        if buy_advanced:
            _rollback_order(db, buy_order.id, amount)
        item.status = "failed"
        item.error_detail = "订单状态或余量已变化，本次成交被拒绝"
        return

    # 4) 写成交记录（带幂等键，回放不会重复成交）
    txn = models.CreditTransaction(
        transaction_no=_gen_txn_no(db),
        idempotency_key=f"{task.task_no}:{item.seq}",
        task_item_id=item.id,
        from_enterprise_id=sell_order.enterprise_id,
        to_enterprise_id=buy_order.enterprise_id,
        sell_order_id=sell_order.id,
        buy_order_id=buy_order.id,
        credit_amount=amount,
        unit_price=price,
        total_amount=actual_pay,
        status="matched",
        settled_amount=0.0,
        remark=f"预授权撮合：卖单{sell_order.order_no} → 买单{buy_order.order_no}",
    )
    db.add(txn)
    db.flush()

    # 5) 立即清算：买方资金已在挂单时冻结，这里不会出现余额不足；
    #    冻结按报价、支出按成交价，价差当场解冻回买方可用额度
    account = (
        db.query(models.MarketFundAccount)
        .filter(
            models.MarketFundAccount.enterprise_id == buy_order.enterprise_id,
            models.MarketFundAccount.year == buy_order.year,
        )
        .first()
    )
    fund_ok = True
    if account:
        fund_ok = _move_fund_for_settlement(
            db, account.id, frozen_pay, actual_pay
        )
    if not fund_ok:
        # 理论不可达（冻结额 >= 应付额是挂单时的不变量），仍做整项补偿，避免脏账
        db.delete(txn)
        db.flush()
        _compensate_auth(db, sell_auth.id, amount)
        _compensate_auth(db, buy_auth.id, amount)
        _rollback_order(db, sell_order.id, amount)
        _rollback_order(db, buy_order.id, amount)
        item.status = "failed"
        item.error_detail = "清算时冻结资金不足，已整项回滚，记录为可恢复的部分结果"
        return

    db.refresh(buy_auth)
    buy_auth.consumed_fund = _r2(buy_auth.consumed_fund + actual_pay)
    price_diff = _r2(frozen_pay - actual_pay)
    if price_diff > 0:
        buy_auth.released_fund = _r2(buy_auth.released_fund + price_diff)
    buy_auth.updated_at = _now()

    txn.status = "cleared"
    txn.settled_amount = amount
    txn.settled_at = _now()
    txn.settlement_no = _gen_settlement_no(db)

    # 清算成功后再写入行情，避免未落账项污染价格历史
    crud.create_price_history(db, txn, sell_order.year)

    item.status = "succeeded"
    item.transaction_id = txn.id
    item.executed_at = _now()
    task.completed_items = (task.completed_items or 0) + 1
    task.completed_credit_amount = _r2(task.completed_credit_amount + amount)


def execute_task(db: Session, task: models.MatchTask) -> models.MatchTask:
    """按 seq 确定顺序执行整个任务；部分失败落库为 partial 并可回放恢复。"""
    with _MATCH_LOCK:
        db.refresh(task)
        if task.status in (MatchTaskStatus.COMPLETED, MatchTaskStatus.PARTIAL):
            if all(
                it.status in ("succeeded", "failed", "skipped")
                for it in task.items
            ):
                return task

        affected: Dict[int, set] = {}
        try:
            task.status = MatchTaskStatus.RUNNING
            task.started_at = task.started_at or _now()
            db.flush()

            from . import crud

            items = sorted(task.items, key=lambda x: x.seq)
            for item in items:
                _execute_item(db, task, item)
                if item.status == "succeeded" and item.transaction_id:
                    txn = db.get(models.CreditTransaction, item.transaction_id)
                    if txn:
                        affected.setdefault(txn.from_enterprise_id, set()).add(task.year)
                        affected.setdefault(txn.to_enterprise_id, set()).add(task.year)
                db.flush()

            failed = [it for it in items if it.status == "failed"]
            if not items:
                task.status = MatchTaskStatus.COMPLETED
            elif failed:
                task.status = MatchTaskStatus.PARTIAL
                task.error_detail = (
                    f"{len(failed)} 笔订单对未成交："
                    + "；".join(
                        f"第{it.seq}笔(卖{it.sell_order_id}/买{it.buy_order_id}):{it.error_detail}"
                        for it in failed
                    )
                )
            else:
                task.status = MatchTaskStatus.COMPLETED
            task.finished_at = _now()
            task.updated_at = _now()

            # 成交后统一刷新涉及企业的年度台账（与原有对账口径保持一致）
            for ent_id, years in affected.items():
                for yr in years:
                    crud.update_annual_summary_with_transactions(db, ent_id, yr)

            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            db.refresh(task)
            task.status = MatchTaskStatus.FAILED
            task.error_detail = f"撮合任务执行异常，待回放恢复：{exc}"[:500]
            task.finished_at = _now()
            db.commit()
        db.refresh(task)
        return task


def run_auto_match(
    db: Session, year: int
) -> Tuple[Optional[models.MatchTask], Optional[models.MatchTask], Optional[str]]:
    """自动撮合入口：并发请求最终只会有一个任务/一个结果。

    进程锁把并发请求串行化：第一个请求成交后，后来的请求看到订单簿已被消费、
    计划为空，此时返回该年度最近一次 auto 任务（确定性收敛，而不是各自报"无可撮合"）；
    只要出现新的有效挂单使计划非空，就会开启一轮新任务。
    """
    with _MATCH_LOCK:
        expire_due_auths(db, year=year)
        plan = plan_auto_match(db, year)
        if not plan:
            recent = (
                db.query(models.MatchTask)
                .filter(
                    models.MatchTask.year == year,
                    models.MatchTask.task_type == "auto",
                )
                .order_by(models.MatchTask.id.desc())
                .first()
            )
            if recent:
                # 回放式收敛：后来的并发/重试请求拿到同一个成交结果
                return None, recent, None
            return None, None, "当前没有可撮合的有效挂单（无有效预授权或价格不匹配）"
        key = _auto_idempotency_key(db, year, plan)
        task, existing, err = _create_task(db, year, "auto", plan, key)
        if err:
            return None, None, err
        if existing:
            return execute_task(db, existing), existing, None
        return execute_task(db, task), None, None


def run_specified_match(
    db: Session, req: schemas.CreditOrderMatchRequest
) -> Tuple[Optional[models.CreditTransaction], Optional[str], Optional[models.MatchTask]]:
    """指定订单撮合，返回 (成交记录, 错误, 幂等命中的既有任务)。"""
    with _MATCH_LOCK:
        task, existing, err = _create_specified_task(db, req)
        if err:
            return None, err, None
        target = existing or task
        execute_task(db, target)
        db.refresh(target)
        succeeded = sorted(
            [it for it in target.items if it.status == "succeeded"],
            key=lambda x: x.seq,
        )
        if not succeeded:
            return None, target.error_detail or "撮合失败", target
        txn = db.get(models.CreditTransaction, succeeded[0].transaction_id)
        return txn, None, target


# ---------------------------------------------------------------------------
# 服务重启后的任务回放
# ---------------------------------------------------------------------------

def resume_pending_tasks(db: Session) -> List[models.MatchTask]:
    """
    重启恢复：重放 PENDING / RUNNING（上次宕机残留）任务。
    已成功的成交项凭幂等键跳过，未完成项继续落账，不会重复成交。
    顺带清理过期授权。
    """
    with _MATCH_LOCK:
        expire_due_auths(db)
        stale = (
            db.query(models.MatchTask)
            .filter(
                models.MatchTask.status.in_(
                    [MatchTaskStatus.PENDING, MatchTaskStatus.RUNNING, MatchTaskStatus.FAILED]
                )
            )
            .order_by(models.MatchTask.id.asc())
            .all()
        )
        recovered = []
        for task in stale:
            if any(it.status == "pending" for it in task.items) or task.status == MatchTaskStatus.FAILED:
                execute_task(db, task)
                recovered.append(task)
        return recovered


# ---------------------------------------------------------------------------
# 查询：挂单/预授权/成交/清算/释放数量五分开
# ---------------------------------------------------------------------------

def order_quantity_view(db: Session, order_id: int) -> Optional[dict]:
    order = db.query(models.CreditOrder).filter(
        models.CreditOrder.id == order_id
    ).first()
    if not order:
        return None

    auths = db.query(models.OrderAuth).filter(
        models.OrderAuth.order_id == order_id
    ).all()
    txns = []
    if order.order_type == OrderType.SELL:
        txns = order.sell_transactions
    else:
        txns = order.buy_transactions

    frozen = _r2(sum(a.frozen_amount for a in auths))
    consumed = _r2(sum(a.consumed_amount for a in auths))
    released_auth = _r2(sum(a.released_amount for a in auths))
    matched = _r2(sum(t.credit_amount for t in txns))
    cleared = _r2(sum(t.settled_amount or 0.0 for t in txns))

    return {
        "order_id": order.id,
        "order_no": order.order_no,
        "order_type": order.order_type.value,
        "status": order.status.value,
        "unit_price": order.unit_price,
        # 挂单视角
        "posted_amount": _r2(order.total_amount),
        "filled_amount": _r2(order.filled_amount),
        "remaining_amount": _r2(order.remaining_amount),
        "released_amount": _r2(order.released_amount),
        # 预授权视角
        "auth": {
            "frozen_amount": frozen,
            "consumed_amount": consumed,
            "released_amount": released_auth,
            "available_amount": _r2(frozen - consumed - released_auth),
            "frozen_fund": _r2(sum(a.frozen_fund for a in auths)),
            "consumed_fund": _r2(sum(a.consumed_fund for a in auths)),
            "released_fund": _r2(sum(a.released_fund for a in auths)),
            "details": [
                {
                    "auth_no": a.auth_no,
                    "resource_type": a.resource_type.value,
                    "status": a.status.value,
                    "frozen_amount": _r2(a.frozen_amount),
                    "consumed_amount": _r2(a.consumed_amount),
                    "released_amount": _r2(a.released_amount),
                    "available_amount": a.available_amount,
                    "expires_at": a.expires_at,
                }
                for a in auths
            ],
        },
        # 成交视角
        "matched_amount": matched,
        # 清算视角
        "cleared_amount": cleared,
        "uncleared_amount": _r2(matched - cleared),
        "transaction_count": len(txns),
    }


def list_auths(
    db: Session,
    enterprise_id: Optional[int] = None,
    order_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[AuthStatus] = None,
) -> List[models.OrderAuth]:
    q = db.query(models.OrderAuth)
    if enterprise_id:
        q = q.filter(models.OrderAuth.enterprise_id == enterprise_id)
    if order_id:
        q = q.filter(models.OrderAuth.order_id == order_id)
    if year:
        q = q.filter(models.OrderAuth.year == year)
    if status:
        q = q.filter(models.OrderAuth.status == status)
    return q.order_by(models.OrderAuth.id.desc()).all()


def get_task(db: Session, task_id: int) -> Optional[models.MatchTask]:
    return db.query(models.MatchTask).filter(models.MatchTask.id == task_id).first()


def list_tasks(
    db: Session, year: Optional[int] = None, status: Optional[MatchTaskStatus] = None
) -> List[models.MatchTask]:
    q = db.query(models.MatchTask)
    if year:
        q = q.filter(models.MatchTask.year == year)
    if status:
        q = q.filter(models.MatchTask.status == status)
    return q.order_by(models.MatchTask.id.desc()).all()


def task_view(task: models.MatchTask) -> dict:
    return {
        "task_id": task.id,
        "task_no": task.task_no,
        "idempotency_key": task.idempotency_key,
        "year": task.year,
        "task_type": task.task_type,
        "status": task.status.value,
        "total_items": task.total_items,
        "completed_items": task.completed_items,
        "total_credit_amount": _r2(task.total_credit_amount),
        "completed_credit_amount": _r2(task.completed_credit_amount),
        "error_detail": task.error_detail,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "items": [
            {
                "seq": it.seq,
                "sell_order_id": it.sell_order_id,
                "buy_order_id": it.buy_order_id,
                "credit_amount": _r2(it.credit_amount),
                "matched_price": it.matched_price,
                "total_amount": _r2(it.total_amount),
                "status": it.status,
                "transaction_id": it.transaction_id,
                "error_detail": it.error_detail,
            }
            for it in sorted(task.items, key=lambda x: x.seq)
        ],
    }
