"""
积分市场撮合引擎：成交前预授权 + 两阶段成交/清算 + 任务箱回放。

数量口径（同一张订单在任一时刻满足）：
    total_amount  = filled + remaining                       （挂单口径）
    authorized    = filled + held + released               （预授权口径）
    filled        = matched_unsettled + settled             （成交/清算口径）

- 挂卖单：按 FIFO 冻结 CreditBatch 的可售积分，生成卖单预授权行；
- 挂买单：按价格上限 PRICE_CEILING 冻结 FundsAccount 资金，生成买单预授权行；
- 撮合（match_pair / match_auto）只消费 HELD 且未过期的授权：
  先生成“确定性成交计划”（价格优先、时间优先），在一个写锁事务里把计划
  整体落账为 TradeExecution + CreditTradeLeg(MATCHED) + CreditTransaction
  (status=matched)，要么全部腿落账、要么整笔回滚；
- 清算逐腿独立事务提交（SETTLED）：买方冻结资金划付卖方、批次转 consumed、
  价格历史落账、年度汇总重算；中途宕机只留下可识别、可恢复的部分结果，
  由任务箱在重启后逐腿回放，已 SETTLED 的腿跳过，绝不重复成交/划付；
- 撤单释放仍 HELD 的授权（RELEASED），授权过期由扫描任务释放（EXPIRED）。
"""
import json
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict

from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models, schemas
from .database import SessionLocal
from .locking import locked_session, now, new_instance_id
from .models import (
    OrderType, OrderStatus,
    BatchStatus, AuthorizationStatus, ExecutionStatus, LegStatus,
    OutboxTaskType, OutboxTaskStatus,
    CreditBatch, FundsAccount, OrderAuthorization,
    TradeExecution, CreditTradeLeg, OutboxTask, IdempotencyRecord, CreditTransaction,
)
from .rules import PRICE_CEILING, validate_order_price

EPS = 0.01
SETTLE_LOCK_STALE_SECONDS = 120

INSTANCE_ID = new_instance_id()


# ---------------------------------------------------------------------------
# 单号生成（均在写锁事务内调用，max+count 安全）
# ---------------------------------------------------------------------------

def _seq_no(db: Session, prefix: str, model) -> str:
    from sqlalchemy import func
    max_id = db.query(func.max(model.id)).scalar() or 0
    new_count = sum(1 for o in db.new if isinstance(o, model) and o.id is None)
    n = max_id + new_count + 1
    return f"{prefix}{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{n:05d}"


def _gen_order_no(db) -> str:
    return _seq_no(db, "OR", models.CreditOrder)


def _gen_auth_no(db) -> str:
    return _seq_no(db, "AU", OrderAuthorization)


def _gen_batch_no(db) -> str:
    return _seq_no(db, "BT", CreditBatch)


def _gen_execution_no(db) -> str:
    return _seq_no(db, "EX", TradeExecution)


# ---------------------------------------------------------------------------
# 账户与批次
# ---------------------------------------------------------------------------

def get_or_create_funds_account(db: Session, enterprise_id: int) -> FundsAccount:
    account = db.query(FundsAccount).filter(
        FundsAccount.enterprise_id == enterprise_id
    ).first()
    if account:
        return account
    account = FundsAccount(enterprise_id=enterprise_id, balance=0.0, frozen_amount=0.0)
    db.add(account)
    db.flush()
    return account


def create_funds_account(db: Session, enterprise_id: int, initial_balance: float = 0.0) -> FundsAccount:
    if not db.query(models.Enterprise).filter(models.Enterprise.id == enterprise_id).first():
        raise ValueError("企业不存在")
    existing = db.query(FundsAccount).filter(FundsAccount.enterprise_id == enterprise_id).first()
    if existing:
        raise ValueError("该企业资金账户已存在")
    account = FundsAccount(
        enterprise_id=enterprise_id,
        balance=round(initial_balance, 2),
        frozen_amount=0.0,
    )
    db.add(account)
    db.flush()
    return account


def deposit_funds_account(db: Session, enterprise_id: int, amount: float) -> FundsAccount:
    if amount <= 0:
        raise ValueError("充值金额必须大于0")
    account = get_or_create_funds_account(db, enterprise_id)
    account.balance = round(account.balance + amount, 2)
    db.flush()
    return account


def create_credit_batch(
    db: Session,
    enterprise_id: int,
    year: int,
    total_amount: float,
    remark: Optional[str] = None,
) -> CreditBatch:
    if total_amount <= 0:
        raise ValueError("批次积分数量必须大于0")
    if not db.query(models.Enterprise).filter(models.Enterprise.id == enterprise_id).first():
        raise ValueError("企业不存在")
    batch = CreditBatch(
        batch_no=_gen_batch_no(db),
        enterprise_id=enterprise_id,
        year=year,
        total_amount=round(total_amount, 2),
        frozen_amount=0.0,
        consumed_amount=0.0,
        status=BatchStatus.ACTIVE,
        remark=remark,
    )
    db.add(batch)
    db.flush()
    return batch


def _batch_available(batch: CreditBatch) -> float:
    return round(batch.total_amount - batch.frozen_amount - batch.consumed_amount, 2)


def _refresh_batch_status(batch: CreditBatch) -> None:
    if _batch_available(batch) <= EPS:
        batch.status = BatchStatus.EXHAUSTED
    else:
        batch.status = BatchStatus.ACTIVE


# ---------------------------------------------------------------------------
# 预授权：冻结 / 消费 / 释放
# ---------------------------------------------------------------------------

def _held_amount(auth: OrderAuthorization) -> float:
    """授权行仍可供撮合消费的数量（卖单为积分；买单为资金元）。"""
    return round(auth.amount - auth.consumed_amount - auth.released_amount, 2)


def _refresh_auth_status(auth: OrderAuthorization) -> None:
    held = _held_amount(auth)
    if held <= EPS:
        auth.status = AuthorizationStatus.CONSUMED if auth.consumed_amount > EPS else AuthorizationStatus.RELEASED
    # 仍有余额 → 保持 HELD


def _freeze_sell(
    db: Session, order: models.CreditOrder,
    picks: List[Tuple[CreditBatch, float]], expires_at: datetime
) -> None:
    for batch, qty in picks:
        available = _batch_available(batch)
        if qty > available + EPS:
            raise ValueError(f"批次 {batch.batch_no} 可售积分不足（需要 {qty}，可用 {available}）")
        batch.frozen_amount = round(batch.frozen_amount + qty, 2)
        _refresh_batch_status(batch)
        db.add(OrderAuthorization(
            auth_no=_gen_auth_no(db),
            order_id=order.id,
            enterprise_id=order.enterprise_id,
            side=OrderType.SELL,
            batch_id=batch.id,
            amount=round(qty, 2),
            consumed_amount=0.0,
            released_amount=0.0,
            status=AuthorizationStatus.HELD,
            expires_at=expires_at,
        ))


def _freeze_buy(db: Session, order: models.CreditOrder, cash: float, expires_at: datetime) -> None:
    account = get_or_create_funds_account(db, order.enterprise_id)
    if cash > account.balance + EPS:
        raise ValueError(
            f"买单预授权失败：可用资金不足（需要 {round(cash, 2)} 元，"
            f"可用 {round(account.balance, 2)} 元）"
        )
    account.balance = round(account.balance - cash, 2)
    account.frozen_amount = round(account.frozen_amount + cash, 2)
    db.add(OrderAuthorization(
        auth_no=_gen_auth_no(db),
        order_id=order.id,
        enterprise_id=order.enterprise_id,
        side=OrderType.BUY,
        funds_account_id=account.id,
        amount=round(cash, 2),
        consumed_amount=0.0,
        released_amount=0.0,
        status=AuthorizationStatus.HELD,
        expires_at=expires_at,
    ))


def _sell_auths_held(db: Session, order_id: int) -> List[OrderAuthorization]:
    """订单当前有效的卖单 HELD 授权：未释放且未过期（过期由扫描任务释放，
    但即使扫描尚未运行，撮合也绝不消费已过期授权）。"""
    rows = db.query(OrderAuthorization).filter(
        OrderAuthorization.order_id == order_id,
        OrderAuthorization.side == OrderType.SELL,
        OrderAuthorization.status == AuthorizationStatus.HELD,
        or_(OrderAuthorization.expires_at.is_(None),
            OrderAuthorization.expires_at > now()),
    ).order_by(OrderAuthorization.created_at, OrderAuthorization.id).all()
    return [a for a in rows if _held_amount(a) > EPS]


def _buy_auths_held(db: Session, order_id: int) -> List[OrderAuthorization]:
    """订单当前有效的买单 HELD 授权：未释放且未过期。"""
    rows = db.query(OrderAuthorization).filter(
        OrderAuthorization.order_id == order_id,
        OrderAuthorization.side == OrderType.BUY,
        OrderAuthorization.status == AuthorizationStatus.HELD,
        or_(OrderAuthorization.expires_at.is_(None),
            OrderAuthorization.expires_at > now()),
    ).order_by(OrderAuthorization.created_at, OrderAuthorization.id).all()
    return [a for a in rows if _held_amount(a) > EPS]


def _consume_sell_auth(db: Session, order: models.CreditOrder, qty: float) -> float:
    """按批次 FIFO 消费卖单的 HELD 积分授权，返回实际消费量。"""
    remaining = round(qty, 2)
    for auth in _sell_auths_held(db, order.id):
        if remaining <= EPS:
            break
        take = min(remaining, _held_amount(auth))
        auth.consumed_amount = round(auth.consumed_amount + take, 2)
        batch = auth.batch
        batch.frozen_amount = round(batch.frozen_amount - take, 2)
        batch.consumed_amount = round(batch.consumed_amount + take, 2)
        _refresh_batch_status(batch)
        _refresh_auth_status(auth)
        remaining = round(remaining - take, 2)
    return round(qty - remaining, 2)


def _consume_buy_auth(db: Session, order: models.CreditOrder, cash: float) -> float:
    """消费买单 HELD 资金授权（成交金额落定后划付），返回实际消费量。"""
    remaining = round(cash, 2)
    for auth in _buy_auths_held(db, order.id):
        if remaining <= EPS:
            break
        take = min(remaining, _held_amount(auth))
        auth.consumed_amount = round(auth.consumed_amount + take, 2)
        _refresh_auth_status(auth)
        remaining = round(remaining - take, 2)
    return round(cash - remaining, 2)


def _trim_buy_freeze(db: Session, order: models.CreditOrder) -> float:
    """
    买单按价格上限冻结、按实际成交价成交后，把多冻的资金立即退回余额。
    目标持有额 = 剩余可成交积分 × 价格上限；超出部分逐授权行释放。
    返回退回金额（元）。注意：这不是订单数量口径的释放，不计入 order.released_amount。
    """
    needed = round(order.remaining_amount * PRICE_CEILING, 2)
    held = _buy_held_cash(db, order)
    excess = round(held - needed, 2)
    refunded = 0.0
    if excess > EPS:
        for auth in _buy_auths_held(db, order.id):
            if excess <= EPS:
                break
            take = min(excess, _held_amount(auth))
            _release_auth(db, auth, take, AuthorizationStatus.RELEASED)
            excess = round(excess - take, 2)
            refunded += take
    return round(refunded, 2)


def _buy_held_cash(db: Session, order: models.CreditOrder) -> float:
    return round(sum(_held_amount(a) for a in _buy_auths_held(db, order.id)), 2)


def _release_auth(db: Session, auth: OrderAuthorization, qty: float,
                  status: AuthorizationStatus) -> None:
    """释放授权行上 qty 数量的 HELD 余额，回补批次/资金账户。"""
    held = _held_amount(auth)
    qty = min(round(qty, 2), held)
    if qty <= EPS:
        return
    if auth.side == OrderType.SELL and auth.batch_id:
        batch = auth.batch
        batch.frozen_amount = round(batch.frozen_amount - qty, 2)
        _refresh_batch_status(batch)
    elif auth.side == OrderType.BUY and auth.funds_account_id:
        account = auth.funds_account
        account.frozen_amount = round(account.frozen_amount - qty, 2)
        account.balance = round(account.balance + qty, 2)
    auth.released_amount = round(auth.released_amount + qty, 2)
    if _held_amount(auth) <= EPS:
        # 已被部分/全部成交消费过的授权行，剩余被释放后整体视为 CONSUMED，
        # 与完全未成交就撤单/过期的 RELEASED/EXPIRED 区分开
        auth.status = AuthorizationStatus.CONSUMED if auth.consumed_amount > EPS else status
    db.flush()


# ---------------------------------------------------------------------------
# 挂单 / 撤单 / 改单 / 过期
# ---------------------------------------------------------------------------

def create_order(order_in: schemas.CreditOrderCreate,
                credit_batch_ids: Optional[List[int]] = None) -> models.CreditOrder:
    is_valid, error_msg = validate_order_price(order_in.unit_price)
    if not is_valid:
        raise ValueError(error_msg)
    if order_in.total_amount <= 0:
        raise ValueError("挂单数量必须大于0")

    with locked_session(SessionLocal) as db:
        enterprise = db.query(models.Enterprise).filter(
            models.Enterprise.id == order_in.enterprise_id).first()
        if not enterprise:
            raise ValueError("企业不存在")

        expires_at = order_in.expires_at or (now() + timedelta(days=90))
        order = models.CreditOrder(
            enterprise_id=order_in.enterprise_id,
            year=order_in.year,
            order_type=order_in.order_type,
            unit_price=round(order_in.unit_price, 2),
            total_amount=round(order_in.total_amount, 2),
            filled_amount=0.0,
            remaining_amount=round(order_in.total_amount, 2),
            status=OrderStatus.PENDING,
            remark=order_in.remark,
            expires_at=expires_at,
            authorized_amount=round(order_in.total_amount, 2),
            released_amount=0.0,
            order_no="PENDING",
        )
        db.add(order)
        db.flush()  # 取得 order.id
        order.order_no = _gen_order_no(db)

        if order_in.order_type == OrderType.SELL:
            picks = _select_sell_batches(db, order_in, credit_batch_ids)
            _freeze_sell(db, order, picks, expires_at)
        else:
            max_cash = round(order_in.total_amount * PRICE_CEILING, 2)
            _freeze_buy(db, order, max_cash, expires_at)

        db.flush()
        db.refresh(order)
        # 写锁会话即将关闭：expunge 后对象变为 detached 但保留已加载标量，
        # 调用方可读取 id 等字段，relationship 则应在自己的会话中重查
        db.expunge(order)
        return order


def _select_sell_batches(
    db: Session, order_in: schemas.CreditOrderCreate,
    credit_batch_ids: Optional[List[int]]
) -> List[Tuple[CreditBatch, float]]:
    need = round(order_in.total_amount, 2)
    picks: List[Tuple[CreditBatch, float]] = []

    if credit_batch_ids:
        batches = []
        for bid in credit_batch_ids:
            batch = db.query(CreditBatch).filter(CreditBatch.id == bid).first()
            if not batch:
                raise ValueError(f"积分批次 {bid} 不存在")
            if batch.enterprise_id != order_in.enterprise_id or batch.year != order_in.year:
                raise ValueError(f"批次 {batch.batch_no} 不属于该企业该年度，不能用于此卖单")
            batches.append(batch)
    else:
        batches = db.query(CreditBatch).filter(
            CreditBatch.enterprise_id == order_in.enterprise_id,
            CreditBatch.year == order_in.year,
        ).order_by(CreditBatch.created_at, CreditBatch.id).all()

    for batch in batches:
        available = _batch_available(batch)
        if available <= EPS:
            continue
        take = min(need, available)
        picks.append((batch, round(take, 2)))
        need = round(need - take, 2)
        if need <= EPS:
            break

    if need > EPS:
        raise ValueError(
            f"可售积分不足：卖单需要 {round(order_in.total_amount, 2)} 分，"
            f"当前批次可用仅 {round(order_in.total_amount - need, 2)} 分；"
            "请先创建/补充积分批次后再挂卖单（成交前预授权要求整单冻结）"
        )
    return picks


def cancel_order(order_id: int) -> Optional[models.CreditOrder]:
    with locked_session(SessionLocal) as db:
        order = db.get(models.CreditOrder, order_id)
        if not order:
            return None
        if order.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            raise ValueError("只有待成交或部分成交的订单可以取消")
        released_qty = round(order.remaining_amount, 2)  # 订单口径（积分）
        _release_order_holds(db, order, AuthorizationStatus.RELEASED)
        order.released_amount = round(order.released_amount + released_qty, 2)
        order.remaining_amount = 0.0  # 撤单后不再有可成交量
        order.status = OrderStatus.CANCELLED
        order.updated_at = now()
        db.flush()
        db.refresh(order)
        db.expunge(order)
        return order


def update_order(order_id: int, order_update: schemas.CreditOrderUpdate) -> Optional[models.CreditOrder]:
    with locked_session(SessionLocal) as db:
        order = db.get(models.CreditOrder, order_id)
        if not order:
            return None
        if order.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            raise ValueError("只有待成交或部分成交的订单可以修改")

        data = order_update.model_dump(exclude_unset=True)

        # 订单状态只能由撮合/清算、撤单、过期流程驱动，改单接口不能直接写状态
        if data.get("status") is not None:
            raise ValueError("不能直接修改订单状态：请使用撮合、撤单接口或等待授权过期")

        if "unit_price" in data and data["unit_price"] is not None:
            is_valid, err = validate_order_price(data["unit_price"])
            if not is_valid:
                raise ValueError(err)

        if "total_amount" in data and data["total_amount"] is not None:
            new_total = round(float(data["total_amount"]), 2)
            if new_total < order.filled_amount - EPS:
                raise ValueError("挂单总量不能小于已成交数量")
            delta = round(new_total - order.total_amount, 2)
            _adjust_order_amount(db, order, delta)
            data.pop("total_amount")

        price_changed = ("unit_price" in data and data.get("unit_price") is not None
                         and round(float(data["unit_price"]), 2) != round(order.unit_price, 2))
        for key, value in data.items():
            if value is not None:
                setattr(order, key, value)
        if price_changed:
            # 改价后重新参与时间优先排队（与交易所改价重排一致），保持撮合确定性
            order.created_at = now()
        order.updated_at = now()
        db.flush()
        db.refresh(order)
        db.expunge(order)
        return order


def _adjust_order_amount(db: Session, order: models.CreditOrder, delta: float) -> None:
    if abs(delta) <= EPS:
        return
    if delta > 0:
        if order.order_type == OrderType.SELL:
            order_in = schemas.CreditOrderCreate(
                enterprise_id=order.enterprise_id, year=order.year,
                order_type=order.order_type, unit_price=order.unit_price,
                total_amount=delta,
            )
            picks = _select_sell_batches(db, order_in, None)
            _freeze_sell(db, order, picks, order.expires_at)
        else:
            cash = round(delta * PRICE_CEILING, 2)
            _freeze_buy(db, order, cash, order.expires_at)
        order.authorized_amount = round(order.authorized_amount + delta, 2)
    else:
        release = round(-delta, 2)
        if order.order_type == OrderType.SELL:
            for auth in _sell_auths_held(db, order.id):
                if release <= EPS:
                    break
                take = min(release, _held_amount(auth))
                _release_auth(db, auth, take, AuthorizationStatus.RELEASED)
                release = round(release - take, 2)
        else:
            cash = round(release * PRICE_CEILING, 2)
            for auth in _buy_auths_held(db, order.id):
                if cash <= EPS:
                    break
                take = min(cash, _held_amount(auth))
                _release_auth(db, auth, take, AuthorizationStatus.RELEASED)
                cash = round(cash - take, 2)
        order.authorized_amount = round(order.authorized_amount + delta, 2)
        order.released_amount = round(order.released_amount + (-delta if delta < 0 else 0), 2)
    order.total_amount = round(order.total_amount + delta, 2)
    order.remaining_amount = round(order.remaining_amount + delta, 2)
    _refresh_order_status(order)


def _release_order_holds(
    db: Session, order: models.CreditOrder, status: AuthorizationStatus
) -> float:
    """释放订单所有仍 HELD 的授权；返回本次释放的订单口径数量（积分单位）。"""
    released_qty = 0.0
    auths = db.query(OrderAuthorization).filter(
        OrderAuthorization.order_id == order.id,
        OrderAuthorization.status == AuthorizationStatus.HELD,
    ).all()
    for auth in auths:
        held = _held_amount(auth)
        if held > EPS:
            if order.order_type == OrderType.SELL:
                take = held  # 卖单授权行单位即积分
            else:
                # 买单授权行单位是资金元：按订单价格上限折算回积分口径，
                # 订单 released_amount 与 total_amount 同为积分单位
                take = round(held / PRICE_CEILING, 2)
            _release_auth(db, auth, held, status)
            released_qty += take
    return round(released_qty, 2)


def expire_due_authorizations(db: Session, at: Optional[datetime] = None) -> List[int]:
    """扫描并释放过期授权（调用方负责持写锁/提交）。返回受影响订单ID。"""
    at = at or now()
    affected: List[int] = []

    due_auths = db.query(OrderAuthorization).filter(
        OrderAuthorization.status == AuthorizationStatus.HELD,
        OrderAuthorization.expires_at < at,
    ).all()
    released_by_order: Dict[int, float] = {}
    for auth in due_auths:
        held = _held_amount(auth)
        if held > EPS:
            if auth.side == OrderType.SELL:
                take = held
            else:
                take = round(held / PRICE_CEILING, 2)
            _release_auth(db, auth, held, AuthorizationStatus.EXPIRED)
            released_by_order[auth.order_id] = round(
                released_by_order.get(auth.order_id, 0.0) + take, 2)

    for oid, released_qty in released_by_order.items():
        order = db.get(models.CreditOrder, oid)
        if not order:
            continue
        order.released_amount = round(order.released_amount + released_qty, 2)
        order.remaining_amount = round(order.remaining_amount - released_qty, 2)
        # 挂单已到期：未成交部分随授权过期整体终止（含部分成交后的剩余部分）
        order.status = OrderStatus.EXPIRED
        order.updated_at = now()
        affected.append(oid)

    due_orders = db.query(models.CreditOrder).filter(
        models.CreditOrder.status.in_([OrderStatus.PENDING, OrderStatus.PARTIAL]),
        models.CreditOrder.expires_at < at,
    ).all()
    for order in due_orders:
        released_qty = _release_order_holds(db, order, AuthorizationStatus.EXPIRED)
        order.released_amount = round(order.released_amount + released_qty, 2)
        order.status = OrderStatus.EXPIRED
        order.updated_at = now()
        affected.append(order.id)

    db.flush()
    return sorted(set(affected))


# ---------------------------------------------------------------------------
# 撮合：确定性计划 + 两阶段提交
# ---------------------------------------------------------------------------

def _refresh_order_status(order: models.CreditOrder) -> None:
    if order.remaining_amount <= EPS:
        order.status = OrderStatus.FILLED
    elif order.filled_amount > EPS:
        order.status = OrderStatus.PARTIAL
    else:
        order.status = OrderStatus.PENDING


def _check_counterparties(
    sell: models.CreditOrder, buy: models.CreditOrder
) -> Optional[str]:
    if sell.order_type != OrderType.SELL:
        return "卖单类型错误"
    if buy.order_type != OrderType.BUY:
        return "买单类型错误"
    if sell.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
        return "卖单状态不可交易"
    if buy.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
        return "买单状态不可交易"
    if sell.year != buy.year:
        return "买卖单核算年度不一致，无法成交"
    if sell.enterprise_id == buy.enterprise_id:
        return "不能与本企业自成交"
    if buy.unit_price < sell.unit_price:
        return f"买单价格({buy.unit_price})低于卖单价格({sell.unit_price})，无法成交"
    return None


class _LegPlan:
    __slots__ = ("sell", "buy", "qty", "price")

    def __init__(self, sell, buy, qty, price):
        self.sell = sell
        self.buy = buy
        self.qty = round(qty, 2)
        self.price = round(price, 2)


def _build_pair_plan(db: Session, sell_id: int, buy_id: int, requested: float) -> Tuple[Optional[_LegPlan], Optional[str]]:
    sell = db.get(models.CreditOrder, sell_id)
    buy = db.get(models.CreditOrder, buy_id)
    if not sell or not buy:
        return None, "订单不存在"
    err = _check_counterparties(sell, buy)
    if err:
        return None, err

    matched_price = round((sell.unit_price + buy.unit_price) / 2, 2)
    held_cash = _buy_held_cash(db, buy)
    affordable = round(held_cash / matched_price, 2) if matched_price > 0 else 0.0
    qty = min(round(requested, 2), sell.remaining_amount, buy.remaining_amount, affordable)
    qty = round(qty, 2)
    if qty <= EPS:
        if affordable <= EPS:
            return None, "买单预授权资金余额已不足以成交任何数量（请撤单后重新挂单）"
        return None, "可成交数量不足"
    return _LegPlan(sell, buy, qty, matched_price), None


def _build_auto_plans(db: Session, year: int) -> List[_LegPlan]:
    """
    价格优先、时间优先的确定性连续撮合：
    卖单价升序（同价按挂单时间、订单ID），买单价降序（同价按挂单时间、订单ID）；
    每腿数量同时受卖单剩余积分、买单剩余积分、买单已冻结资金可承接积分数约束，
    任一侧能力耗尽即按序推进另一侧，保证部分成交公平且结果与并发调度顺序无关。
    """
    active = [OrderStatus.PENDING, OrderStatus.PARTIAL]
    sells = db.query(models.CreditOrder).filter(
        models.CreditOrder.year == year,
        models.CreditOrder.order_type == OrderType.SELL,
        models.CreditOrder.status.in_(active),
    ).order_by(models.CreditOrder.unit_price, models.CreditOrder.created_at,
               models.CreditOrder.id).all()
    buys = db.query(models.CreditOrder).filter(
        models.CreditOrder.year == year,
        models.CreditOrder.order_type == OrderType.BUY,
        models.CreditOrder.status.in_(active),
    ).order_by(models.CreditOrder.unit_price.desc(), models.CreditOrder.created_at,
              models.CreditOrder.id).all()

    sells = [o for o in sells if o.remaining_amount > EPS]
    buys = [o for o in buys if o.remaining_amount > EPS]
    db.expire_all()  # 游标计数仅在函数本地使用，避免内存中改动被误写入库

    # 本地游标：剩余积分均按实时授权行计算，绝不改写订单实体（落账统一在
    # _apply_plans 中逐腿更新），避免构建阶段的内存推演污染复查快照
    sell_remaining = {o.id: o.remaining_amount for o in sells}
    buy_remaining = {o.id: o.remaining_amount for o in buys}
    plans: List[_LegPlan] = []
    si = bi = 0
    while si < len(sells) and bi < len(buys):
        sell, buy = sells[si], buys[bi]
        if buy.unit_price < sell.unit_price:
            break  # 此后再无交叉可能（两侧均已排序）

        matched_price = round((sell.unit_price + buy.unit_price) / 2, 2)
        held_cash = _buy_held_cash(db, buy)
        affordable = round(held_cash / matched_price, 2) if matched_price > 0 else 0.0
        qty = round(min(sell_remaining[sell.id], buy_remaining[buy.id], affordable), 2)

        if qty <= EPS:
            # 买单冻结资金已无力承接哪怕一单位：推进买单（卖单留给后续买单，公平推进）
            bi += 1
            continue

        plans.append(_LegPlan(sell, buy, qty, matched_price))
        sell_remaining[sell.id] = round(sell_remaining[sell.id] - qty, 2)
        buy_remaining[buy.id] = round(buy_remaining[buy.id] - qty, 2)

        if sell_remaining[sell.id] <= EPS:
            si += 1
        if buy_remaining[buy.id] <= EPS:
            bi += 1

    return plans


def _apply_plans(
    db: Session, execution: TradeExecution, plans: List[_LegPlan],
    trigger_type: str
) -> None:
    """
    在调用方已持有的写锁事务内，把成交计划整体落账：
    任一腿写入失败 → 异常传播 → 整个撮合事务回滚，不允许出现“计划内半条腿”。
    """
    total_credit = 0.0
    total_cash = 0.0
    for seq, plan in enumerate(plans):
        sell, buy = plan.sell, plan.buy
        qty, price = plan.qty, plan.price
        cash = round(qty * price, 2)

        # 锁内复查（double-check）：排队等写锁期间，订单可能已被其他请求成交/撤销。
        # 必须先 refresh 再读字段，防止使用等锁前的过期快照。
        # 计划构建与落账在同一个写锁事务内，此处的 held 现金已扣除本计划前序腿的消费。
        db.refresh(sell)
        db.refresh(buy)
        err = _check_counterparties(sell, buy)
        if err:
            raise ValueError(f"成交前复查失败：{err}（序号{seq}）")
        if sell.remaining_amount + EPS < qty:
            raise ValueError(f"成交前复查失败：卖单剩余 {sell.remaining_amount} 不足 {qty}")
        if buy.remaining_amount + EPS < qty:
            raise ValueError(f"成交前复查失败：买单剩余 {buy.remaining_amount} 不足 {qty}")
        if _buy_held_cash(db, buy) + EPS < cash:
            raise ValueError("成交前复查失败：买单预授权资金不足")

        txn = CreditTransaction(
            transaction_no="PENDING",
            from_enterprise_id=sell.enterprise_id,
            to_enterprise_id=buy.enterprise_id,
            sell_order_id=sell.id,
            buy_order_id=buy.id,
            credit_amount=qty,
            unit_price=price,
            total_amount=cash,
            transaction_date=now(),
            status="matched",  # 已成交、待清算
            remark=f"撮合成交：卖单{sell.order_no} → 买单{buy.order_no}（计划序号{seq}）",
        )
        db.add(txn)
        db.flush()  # 取 txn.id 供腿与价格历史引用
        txn.transaction_no = _seq_no(db, "TXN", CreditTransaction)

        leg = CreditTradeLeg(
            execution_id=execution.id,
            seq=seq,
            sell_order_id=sell.id,
            buy_order_id=buy.id,
            credit_amount=qty,
            matched_price=price,
            total_amount=cash,
            status=LegStatus.MATCHED,
            transaction_id=txn.id,
        )
        db.add(leg)

        consumed_sell = _consume_sell_auth(db, sell, qty)
        if consumed_sell + EPS < qty:
            raise ValueError("卖单批次预授权不足，成交中止")
        consumed_cash = _consume_buy_auth(db, buy, cash)
        if consumed_cash + EPS < cash:
            raise ValueError("买单资金预授权不足，成交中止")

        sell.filled_amount = round(sell.filled_amount + qty, 2)
        sell.remaining_amount = round(sell.remaining_amount - qty, 2)
        buy.filled_amount = round(buy.filled_amount + qty, 2)
        buy.remaining_amount = round(buy.remaining_amount - qty, 2)
        _refresh_order_status(sell)
        _refresh_order_status(buy)
        # 买单按上限冻结、按实际价成交：成交后把多冻资金立即退回余额，
        # 否则买单剩余积分数下降后超额冻结会永久滞留
        _trim_buy_freeze(db, buy)
        sell.updated_at = now()
        buy.updated_at = now()

        total_credit = round(total_credit + qty, 2)
        total_cash = round(total_cash + cash, 2)

    execution.planned_count = len(plans)
    execution.total_credit_amount = total_credit
    execution.total_cash_amount = total_cash
    execution.status = ExecutionStatus.COMMITTED
    execution.committed_at = now()
    db.flush()


def _enqueue_settle(db: Session, execution_id: int) -> None:
    exists = db.query(OutboxTask).filter(
        OutboxTask.task_type == OutboxTaskType.SETTLE_EXECUTION,
        OutboxTask.execution_id == execution_id,
        OutboxTask.status.in_([OutboxTaskStatus.PENDING, OutboxTaskStatus.RUNNING, OutboxTaskStatus.FAILED]),
    ).first()
    if exists:
        return
    db.add(OutboxTask(
        task_type=OutboxTaskType.SETTLE_EXECUTION,
        execution_id=execution_id,
        status=OutboxTaskStatus.PENDING,
        payload=json.dumps({"execution_id": execution_id}),
        available_at=now(),
        attempts=0,
    ))


def _save_idempotency(
    db: Session, key: str, scope: str, request_hash: Optional[str],
    execution_id: int
) -> None:
    db.add(IdempotencyRecord(
        idempotency_key=key,
        scope=scope,
        request_hash=request_hash,
        execution_id=execution_id,
        status_code=200,
    ))


def _request_hash(payload: dict) -> str:
    import hashlib
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _lookup_idempotency(db: Session, key: str, request_hash: Optional[str]):
    rec = db.query(IdempotencyRecord).filter(
        IdempotencyRecord.idempotency_key == key).first()
    if not rec:
        return None, None
    if request_hash and rec.request_hash and rec.request_hash != request_hash:
        raise ValueError("相同幂等键对应的请求内容不同，拒绝执行")
    execution = db.get(TradeExecution, rec.execution_id) if rec.execution_id else None
    return rec, execution


def match_pair(
    sell_order_id: int, buy_order_id: int, credit_amount: float,
    idempotency_key: Optional[str] = None, trigger_type: str = "manual",
    request_payload: Optional[dict] = None,
) -> Tuple[TradeExecution, bool]:
    """指定两单撮合一腿。返回 (执行计划, 是否为重放请求)。"""
    request_hash = _request_hash(request_payload) if (idempotency_key and request_payload is not None) else None

    with locked_session(SessionLocal) as db:
        if idempotency_key:
            _, existing = _lookup_idempotency(db, idempotency_key, request_hash)
            if existing is not None:
                return existing, True

        plan, err = _build_pair_plan(db, sell_order_id, buy_order_id, credit_amount)
        if err:
            raise ValueError(err)

        execution = TradeExecution(
            execution_no=_gen_execution_no(db),
            idempotency_key=idempotency_key,
            year=plan.sell.year,
            trigger_type=trigger_type,
            sell_order_id=plan.sell.id,
            buy_order_id=plan.buy.id,
            status=ExecutionStatus.COMMITTED,
        )
        db.add(execution)
        db.flush()
        _apply_plans(db, execution, [plan], trigger_type)
        _enqueue_settle(db, execution.id)
        if idempotency_key:
            _save_idempotency(db, idempotency_key, "match", request_hash, execution.id)
        execution_id = execution.id

    # 撮合已提交（MATCHED 部分结果对库可见）；清算在独立事务逐腿提交，可安全回放
    settle_execution(execution_id)
    with locked_session(SessionLocal) as db:
        execution = db.get(TradeExecution, execution_id)
        db.expunge(execution)
        return execution, False


def match_auto(
    year: int, idempotency_key: Optional[str] = None,
    request_payload: Optional[dict] = None,
) -> Optional[Tuple[TradeExecution, bool]]:
    """按价格-时间优先自动撮合年度内全部可成交挂单，生成单个多腿执行计划。"""
    request_hash = _request_hash(request_payload) if (idempotency_key and request_payload is not None) else None

    with locked_session(SessionLocal) as db:
        if idempotency_key:
            _, existing = _lookup_idempotency(db, idempotency_key, request_hash)
            if existing is not None:
                return existing, True

        plans = _build_auto_plans(db, year)
        if not plans:
            return None

        first = plans[0]
        execution = TradeExecution(
            execution_no=_gen_execution_no(db),
            idempotency_key=idempotency_key,
            year=year,
            trigger_type="auto",
            sell_order_id=first.sell.id,
            buy_order_id=first.buy.id,
            status=ExecutionStatus.COMMITTED,
        )
        db.add(execution)
        db.flush()
        _apply_plans(db, execution, plans, "auto")
        _enqueue_settle(db, execution.id)
        if idempotency_key:
            _save_idempotency(db, idempotency_key, "match-auto", request_hash, execution.id)
        execution_id = execution.id

    settle_execution(execution_id)
    with locked_session(SessionLocal) as db:
        return db.get(TradeExecution, execution_id), False


# ---------------------------------------------------------------------------
# 清算（逐腿独立事务 → 部分结果可恢复）
# ---------------------------------------------------------------------------

def settle_leg(leg_id: int) -> bool:
    """清算单腿；已 SETTLED 的腿直接跳过（回放幂等，绝不重复划付）。"""
    with locked_session(SessionLocal) as db:
        leg = db.get(CreditTradeLeg, leg_id)
        if leg is None:
            raise ValueError("成交腿不存在")
        if leg.status == LegStatus.SETTLED:
            return False
        if leg.status == LegStatus.FAILED:
            # FAILED 是上次清算留下的可恢复标记：允许重试
            leg.status = LegStatus.MATCHED

        txn = db.get(CreditTransaction, leg.transaction_id)
        if txn is None or txn.status not in ("matched",):
            raise ValueError("成交腿缺少 matched 状态的成交记录，无法清算")

        buyer_acc = get_or_create_funds_account(db, _buy_order(db, leg).enterprise_id)
        seller_ent_id = _sell_order(db, leg).enterprise_id
        seller_acc = get_or_create_funds_account(db, seller_ent_id)

        cash = leg.total_amount
        if buyer_acc.frozen_amount + EPS < cash:
            # 理论上不可能：买单在挂单时已按价格上限冻结。出现即数据被破坏，留下可恢复标记。
            raise ValueError(
                f"清算失败：买方冻结资金不足（需 {cash}，冻结 {buyer_acc.frozen_amount}）"
            )

        buyer_acc.frozen_amount = round(buyer_acc.frozen_amount - cash, 2)
        seller_acc.balance = round(seller_acc.balance + cash, 2)

        txn.status = "completed"
        leg.status = LegStatus.SETTLED
        leg.settled_at = now()

        db.add(models.PriceHistory(
            year=_sell_order(db, leg).year,
            trade_date=txn.transaction_date,
            unit_price=txn.unit_price,
            credit_amount=txn.credit_amount,
            total_amount=txn.total_amount,
            from_enterprise_id=txn.from_enterprise_id,
            to_enterprise_id=txn.to_enterprise_id,
            transaction_id=txn.id,
        ))
        db.flush()
        _recompute_execution_status(db, leg.execution_id)
        return True


def _sell_order(db: Session, leg: CreditTradeLeg) -> models.CreditOrder:
    return db.get(models.CreditOrder, leg.sell_order_id)


def _buy_order(db: Session, leg: CreditTradeLeg) -> models.CreditOrder:
    return db.get(models.CreditOrder, leg.buy_order_id)


def _recompute_execution_status(db: Session, execution_id: int) -> None:
    execution = db.get(TradeExecution, execution_id)
    legs = db.query(CreditTradeLeg).filter(
        CreditTradeLeg.execution_id == execution_id).all()
    execution.settled_count = sum(1 for l in legs if l.status == LegStatus.SETTLED)
    execution.failed_count = sum(1 for l in legs if l.status == LegStatus.FAILED)
    if execution.settled_count >= execution.planned_count:
        execution.status = ExecutionStatus.SETTLED
        execution.settled_at = now()
    elif execution.settled_count > 0:
        execution.status = ExecutionStatus.PARTIAL
    execution.error_detail = None
    db.flush()


def settle_execution(execution_id: int) -> ExecutionStatus:
    """
    按 seq 确定顺序逐腿清算：每腿独立事务提交。
    某腿失败时，前面腿已落账（清楚记录的部分结果），本腿标记 FAILED、
    执行计划置 PARTIAL、清算任务回到 PENDING 待任务箱回放恢复。
    """
    with locked_session(SessionLocal) as db:
        execution = db.get(TradeExecution, execution_id)
        if execution is None:
            raise ValueError("执行计划不存在")
        if execution.status == ExecutionStatus.SETTLED:
            _mark_settle_task_done(db, execution_id)
            return execution.status
        leg_ids = [
            lid for (lid,) in db.query(CreditTradeLeg.id).filter(
                CreditTradeLeg.execution_id == execution_id,
                CreditTradeLeg.status != LegStatus.SETTLED,
            ).order_by(CreditTradeLeg.seq).all()
        ]

    for leg_id in leg_ids:
        try:
            settle_leg(leg_id)
            # 成交后资金/积分真正过户，重算两侧企业年度汇总（幂等全量重算）
            with locked_session(SessionLocal) as db:
                l = db.get(CreditTradeLeg, leg_id)
                _refresh_summaries_for_leg(db, l)
        except Exception as exc:  # 部分结果保留，等待回放
            with locked_session(SessionLocal) as db:
                # 腿保持 MATCHED（已成交未清算，可直接重试）；执行计划置
                # PARTIAL、清算任务回 PENDING，三者共同标识可恢复的部分结果
                failed_leg = db.get(CreditTradeLeg, leg_id)
                seq_no = failed_leg.seq if failed_leg else "?"
                execution = db.get(TradeExecution, execution_id)
                execution.status = ExecutionStatus.PARTIAL
                execution.error_detail = (
                    f"清算在序号 {seq_no} 处中断：{exc}；前序腿已按确定顺序落账，"
                    "任务箱将在服务重启后回放本计划剩余腿"
                )[:1000]
                _fail_settle_task(db, execution_id, str(exc))
            return ExecutionStatus.PARTIAL

    with locked_session(SessionLocal) as db:
        _recompute_execution_status(db, execution_id)
        execution = db.get(TradeExecution, execution_id)
        final_status = execution.status
        _mark_settle_task_done(db, execution_id)
    return final_status


def _refresh_summaries_for_leg(db: Session, leg: CreditTradeLeg) -> None:
    from . import crud
    sell = _sell_order(db, leg)
    buy = _buy_order(db, leg)
    crud.update_annual_summary_with_transactions(db, sell.enterprise_id, sell.year)
    crud.update_annual_summary_with_transactions(db, buy.enterprise_id, buy.year)


def _mark_settle_task_done(db: Session, execution_id: int) -> None:
    task = db.query(OutboxTask).filter(
        OutboxTask.task_type == OutboxTaskType.SETTLE_EXECUTION,
        OutboxTask.execution_id == execution_id,
    ).first()
    execution = db.get(TradeExecution, execution_id)
    if task and execution and execution.status == ExecutionStatus.SETTLED:
        task.status = OutboxTaskStatus.DONE
        task.last_error = None
        db.flush()


def _fail_settle_task(db: Session, execution_id: int, error: str) -> None:
    task = db.query(OutboxTask).filter(
        OutboxTask.task_type == OutboxTaskType.SETTLE_EXECUTION,
        OutboxTask.execution_id == execution_id,
    ).first()
    if not task:
        return
    task.attempts += 1
    task.last_error = error[:1000]
    if task.attempts >= task.max_attempts:
        task.status = OutboxTaskStatus.DEAD
    else:
        task.status = OutboxTaskStatus.PENDING
        task.available_at = now() + timedelta(seconds=2 ** min(task.attempts, 4))
    task.locked_by = None
    task.locked_at = None
    db.flush()


# ---------------------------------------------------------------------------
# 任务箱：认领 / 回放 / 宕机恢复
# ---------------------------------------------------------------------------

def enqueue_expiration_scan(db: Session) -> None:
    exists = db.query(OutboxTask).filter(
        OutboxTask.task_type == OutboxTaskType.EXPIRE_AUTHORIZATIONS,
        OutboxTask.status.in_([OutboxTaskStatus.PENDING, OutboxTaskStatus.RUNNING]),
    ).first()
    if exists:
        return
    db.add(OutboxTask(
        task_type=OutboxTaskType.EXPIRE_AUTHORIZATIONS,
        status=OutboxTaskStatus.PENDING,
        payload=json.dumps({}),
        available_at=now(),
    ))


def _recover_stale_running(db: Session) -> int:
    cutoff = now() - timedelta(seconds=SETTLE_LOCK_STALE_SECONDS)
    stale = db.query(OutboxTask).filter(
        OutboxTask.status == OutboxTaskStatus.RUNNING,
        or_(OutboxTask.locked_at < cutoff, OutboxTask.locked_at.is_(None)),
    ).all()
    for task in stale:
        # 认领者宕机：释放认领。逐腿清算已独立提交，回放会从下一未清算腿继续
        task.status = OutboxTaskStatus.PENDING
        task.locked_by = None
        task.locked_at = None
    db.flush()
    return len(stale)


def claim_next_task(db: Session) -> Optional[OutboxTask]:
    task = db.query(OutboxTask).filter(
        OutboxTask.status == OutboxTaskStatus.PENDING,
        OutboxTask.available_at <= now(),
    ).order_by(OutboxTask.available_at, OutboxTask.id).first()
    if not task:
        return None
    task.status = OutboxTaskStatus.RUNNING
    task.locked_by = INSTANCE_ID
    task.locked_at = now()
    db.flush()
    return task


def replay_pending_tasks(batch_limit: int = 100) -> dict:
    """
    服务重启 / 定时调用：回收僵死认领 → 逐任务回放。
    清算与过期释放本身幂等，重复调用不会产生重复成交。
    """
    with locked_session(SessionLocal) as db:
        recovered = _recover_stale_running(db)

    processed = {"settle": 0, "expire": 0, "recovered_running": recovered, "dead": 0}
    for _ in range(batch_limit):
        with locked_session(SessionLocal) as db:
            task = claim_next_task(db)
            if not task:
                break
            task_type = task.task_type
            exec_id = task.execution_id
            if task_type == OutboxTaskType.EXPIRE_AUTHORIZATIONS:
                task.status = OutboxTaskStatus.DONE
            db.flush()

        try:
            if task_type == OutboxTaskType.SETTLE_EXECUTION:
                status = settle_execution(exec_id)
                if status == ExecutionStatus.PARTIAL:
                    # _fail_settle_task 已安排延后重试；这里不计入 processed
                    continue
                processed["settle"] += 1
            elif task_type == OutboxTaskType.EXPIRE_AUTHORIZATIONS:
                with locked_session(SessionLocal) as db:
                    expire_due_authorizations(db)
                processed["expire"] += 1
        except Exception as exc:
            with locked_session(SessionLocal) as db:
                t = db.get(OutboxTask, task.id)
                if t:
                    t.attempts += 1
                    t.last_error = str(exc)[:1000]
                    t.status = (OutboxTaskStatus.DEAD if t.attempts >= t.max_attempts
                                else OutboxTaskStatus.PENDING)
                    t.locked_by = None
                    t.locked_at = None
                    if t.status == OutboxTaskStatus.DEAD:
                        processed["dead"] += 1
    return processed
