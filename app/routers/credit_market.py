from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.orm import Session
from typing import List, Optional, Dict
from datetime import datetime

from .. import crud, schemas, matching
from ..database import get_db
from ..models import OrderType, OrderStatus, AuthorizationStatus

router = APIRouter(prefix="/credit-market", tags=["credit-market"])


# ---------------------------------------------------------------------------
# 挂单
# ---------------------------------------------------------------------------

@router.post("/orders", response_model=schemas.CreditOrder)
def create_order(
    order: schemas.CreditOrderCreate,
    db: Session = Depends(get_db)
):
    try:
        # 挂单即冻结（卖单批次积分 / 买单资金），整个过程在数据库写锁内完成
        return crud.create_credit_order(db=db, order=order)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders", response_model=List[schemas.CreditOrderWithDetail])
def read_orders(
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    order_type: Optional[OrderType] = None,
    status: Optional[OrderStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    return crud.get_credit_orders(
        db,
        enterprise_id=enterprise_id,
        year=year,
        order_type=order_type,
        status=status,
        skip=skip,
        limit=limit
    )


@router.get("/orders/{order_id}", response_model=schemas.CreditOrderWithDetail)
def read_order(order_id: int, db: Session = Depends(get_db)):
    order = crud.get_credit_order(db, order_id=order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@router.put("/orders/{order_id}", response_model=schemas.CreditOrder)
def update_order(
    order_id: int,
    order_update: schemas.CreditOrderUpdate,
    db: Session = Depends(get_db)
):
    try:
        order = crud.update_credit_order(db, order_id=order_id, order_update=order_update)
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        return order
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/orders/{order_id}/cancel", response_model=schemas.CreditOrder)
def cancel_order(order_id: int, db: Session = Depends(get_db)):
    try:
        order = crud.cancel_credit_order(db, order_id=order_id)
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        return order
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}/quantities", response_model=schemas.OrderQuantityBreakdown)
def read_order_quantities(order_id: int, db: Session = Depends(get_db)):
    """五段数量口径：挂单 / 预授权(held) / 成交未清算 / 已清算 / 释放。"""
    breakdown = crud.get_order_quantity_breakdown(db, order_id=order_id)
    if not breakdown:
        raise HTTPException(status_code=404, detail="订单不存在")
    return breakdown


# ---------------------------------------------------------------------------
# 预授权资源：积分批次（卖方可售积分）与资金账户（买方额度）
# ---------------------------------------------------------------------------

@router.post("/credit-batches", response_model=schemas.CreditBatch, status_code=201)
def create_credit_batch(
    batch: schemas.CreditBatchCreate,
    db: Session = Depends(get_db)
):
    try:
        return crud.create_credit_batch(
            db,
            enterprise_id=batch.enterprise_id,
            year=batch.year,
            total_amount=batch.total_amount,
            remark=batch.remark,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/credit-batches", response_model=List[schemas.CreditBatch])
def read_credit_batches(
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    return crud.get_credit_batches(db, enterprise_id=enterprise_id, year=year,
                                   skip=skip, limit=limit)


@router.post("/funds-accounts/{enterprise_id}/deposit", response_model=schemas.FundsAccount)
def deposit_funds(
    enterprise_id: int,
    payload: schemas.FundsAccountDeposit,
    db: Session = Depends(get_db)
):
    try:
        return crud.deposit_funds_account(db, enterprise_id=enterprise_id,
                                         amount=payload.amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/funds-accounts/{enterprise_id}", response_model=schemas.FundsAccount)
def read_funds_account(enterprise_id: int, db: Session = Depends(get_db)):
    account = crud.get_funds_account(db, enterprise_id=enterprise_id)
    if not account:
        raise HTTPException(status_code=404, detail="资金账户不存在")
    return account


@router.get("/authorizations", response_model=List[schemas.OrderAuthorizationOut])
def read_authorizations(
    order_id: Optional[int] = None,
    enterprise_id: Optional[int] = None,
    side: Optional[OrderType] = None,
    status: Optional[AuthorizationStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """预授权流水查询：held（冻结可消费）/ consumed（已成交消费）/ released（撤单）/ expired（过期）。"""
    return crud.get_order_authorizations(
        db, order_id=order_id, enterprise_id=enterprise_id, side=side,
        status=status, skip=skip, limit=limit
    )


# ---------------------------------------------------------------------------
# 撮合（消费有效授权，两阶段成交/清算）
# ---------------------------------------------------------------------------

@router.post("/match", response_model=schemas.MatchPairResponse)
def match_specific_orders(
    match_request: schemas.CreditOrderMatchRequest,
    idempotency_key: Optional[str] = Header(
        default=None,
        alias="Idempotency-Key",
        description="幂等键：并发/重放同键请求只产生一次成交，返回首次结果"),
    db: Session = Depends(get_db)
):
    try:
        execution, replayed = matching.match_pair(
            sell_order_id=match_request.sell_order_id,
            buy_order_id=match_request.buy_order_id,
            credit_amount=match_request.credit_amount,
            idempotency_key=idempotency_key,
            trigger_type="manual",
            request_payload=match_request.model_dump(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    bound = crud.get_trade_execution(db, execution_id=execution.id)
    return schemas.MatchPairResponse(
        replayed=replayed,
        execution=_execution_out(db, bound),
    )


@router.post("/match-all/{year}", response_model=schemas.MatchAutoResponse)
def match_all_orders(
    year: int,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db)
):
    """自动连续撮合：价格-时间优先，全部成交腿在一个执行计划内确定顺序落账。"""
    try:
        result = matching.match_auto(
            year,
            idempotency_key=idempotency_key,
            request_payload={"year": year},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result is None:
        return schemas.MatchAutoResponse(
            success=True,
            message="当前没有可撮合的挂单（价格无交叉或预授权不足）",
            execution=None,
        )
    execution, replayed = result
    bound = crud.get_trade_execution(db, execution_id=execution.id)
    return schemas.MatchAutoResponse(
        success=True,
        message=("幂等重放：返回首次撮合结果" if replayed
                 else f"执行计划 {execution.execution_no} 已提交，"
                      f"共 {execution.planned_count} 腿，状态 {execution.status.value}"),
        execution=_execution_out(db, bound),
    )


# ---------------------------------------------------------------------------
# 执行计划 / 成交腿 / 清算状态
# ---------------------------------------------------------------------------

@router.get("/executions/{execution_id}", response_model=schemas.TradeExecutionOut)
def read_execution(execution_id: int, db: Session = Depends(get_db)):
    execution = crud.get_trade_execution(db, execution_id=execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="执行计划不存在")
    return _execution_out(db, execution)


@router.get("/orders/{order_id}/executions", response_model=List[schemas.TradeExecutionOut])
def read_order_executions(order_id: int, db: Session = Depends(get_db)):
    from .. import models
    if not crud.get_credit_order(db, order_id):
        raise HTTPException(status_code=404, detail="订单不存在")
    legs = db.query(models.CreditTradeLeg).filter(
        (models.CreditTradeLeg.sell_order_id == order_id)
        | (models.CreditTradeLeg.buy_order_id == order_id)
    ).all()
    execution_ids = sorted({l.execution_id for l in legs})
    out = []
    for eid in execution_ids:
        execution = crud.get_trade_execution(db, execution_id=eid)
        if execution:
            out.append(_execution_out(db, execution))
    return out


def _execution_out(db, execution) -> schemas.TradeExecutionOut:
    from .. import models
    legs = db.query(models.CreditTradeLeg).filter(
        models.CreditTradeLeg.execution_id == execution.id
    ).order_by(models.CreditTradeLeg.seq).all()
    data = schemas.TradeExecutionOut.model_validate(execution)
    data.legs = [schemas.CreditTradeLegOut.model_validate(l) for l in legs]
    return data


# ---------------------------------------------------------------------------
# 任务箱：重启回放 / 过期释放
# ---------------------------------------------------------------------------

@router.post("/tasks/replay")
def replay_pending_tasks(db: Session = Depends(get_db)):
    """
    服务重启恢复入口：认领任务箱中未完成的清算/过期任务并回放。
    - 已 SETTLED 的成交腿幂等跳过；
    - 仍 matched 的腿继续清算（上次宕机留下的可恢复部分结果）；
    - 授权过期扫描释放 held 额度。
    """
    return crud.replay_pending_market_tasks(db, batch_limit=100)


@router.post("/authorizations/expire-now")
def expire_authorizations_now(db: Session = Depends(get_db)):
    """管理端立即扫描并释放过期授权（正常由任务箱定时触发）。"""
    with matching.locked_session(matching.SessionLocal) as locked_db:
        affected = matching.expire_due_authorizations(locked_db)
    return {"success": True, "affected_order_ids": affected}


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------

@router.get("/price-history", response_model=List[schemas.PriceHistory])
def read_price_history(
    year: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    return crud.get_price_history(
        db,
        year=year,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit
    )


@router.get("/price-trend/{year}", response_model=schemas.PriceTrendResponse)
def get_price_trend(year: int, db: Session = Depends(get_db)):
    return crud.get_price_trend(db, year=year)


@router.get("/overview/{year}", response_model=schemas.MarketOverviewResponse)
def get_market_overview(year: int, db: Session = Depends(get_db)):
    return crud.get_market_overview(db, year=year)


@router.get("/sell-order-book/{year}")
def get_sell_order_book(
    year: int,
    db: Session = Depends(get_db)
):
    orders = crud.get_credit_orders(
        db,
        year=year,
        order_type=OrderType.SELL,
        status=OrderStatus.PENDING,
        limit=1000
    )
    partial_orders = crud.get_credit_orders(
        db,
        year=year,
        order_type=OrderType.SELL,
        status=OrderStatus.PARTIAL,
        limit=1000
    )
    all_orders = orders + partial_orders

    price_levels: Dict[float, float] = {}
    for o in all_orders:
        if o.remaining_amount <= 0.01:
            continue
        price = o.unit_price
        if price not in price_levels:
            price_levels[price] = 0.0
        price_levels[price] += o.remaining_amount

    order_book = [
        {"price": price, "volume": round(vol, 2)}
        for price, vol in sorted(price_levels.items())
    ]

    return {
        "year": year,
        "order_type": "sell",
        "order_book": order_book,
        "total_volume": round(sum(price_levels.values()), 2),
        "price_levels": len(price_levels)
    }


@router.get("/buy-order-book/{year}")
def get_buy_order_book(
    year: int,
    db: Session = Depends(get_db)
):
    orders = crud.get_credit_orders(
        db,
        year=year,
        order_type=OrderType.BUY,
        status=OrderStatus.PENDING,
        limit=1000
    )
    partial_orders = crud.get_credit_orders(
        db,
        year=year,
        order_type=OrderType.BUY,
        status=OrderStatus.PARTIAL,
        limit=1000
    )
    all_orders = orders + partial_orders

    price_levels: Dict[float, float] = {}
    for o in all_orders:
        if o.remaining_amount <= 0.01:
            continue
        price = o.unit_price
        if price not in price_levels:
            price_levels[price] = 0.0
        price_levels[price] += o.remaining_amount

    order_book = [
        {"price": price, "volume": round(vol, 2)}
        for price, vol in sorted(price_levels.items(), reverse=True)
    ]

    return {
        "year": year,
        "order_type": "buy",
        "order_book": order_book,
        "total_volume": round(sum(price_levels.values()), 2),
        "price_levels": len(price_levels)
    }
