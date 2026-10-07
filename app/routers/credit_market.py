from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional, Dict
from datetime import datetime

from .. import crud, schemas, matching
from ..database import get_db
from ..models import OrderType, OrderStatus, AuthStatus, MatchTaskStatus, CreditTransaction

router = APIRouter(prefix="/credit-market", tags=["credit-market"])


# ---------------------------------------------------------------------------
# 挂单（成交前预授权）
# ---------------------------------------------------------------------------

@router.post("/orders", response_model=schemas.CreditOrder)
def create_order(
    order: schemas.CreditOrderCreate,
    db: Session = Depends(get_db)
):
    db_order, error = matching.create_order_with_auth(db=db, order_in=order)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return db_order


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


@router.get("/orders/{order_id}/quantities", response_model=schemas.OrderQuantityView)
def read_order_quantities(order_id: int, db: Session = Depends(get_db)):
    """挂单/预授权/成交/清算/释放数量五分开查询"""
    view = matching.order_quantity_view(db, order_id)
    if not view:
        raise HTTPException(status_code=404, detail="订单不存在")
    return view


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
    """撤单：释放剩余预授权（卖单退回积分可售额度，买单退回冻结资金）"""
    order, error = matching.cancel_order(db, order_id=order_id)
    if error == "订单不存在":
        raise HTTPException(status_code=404, detail=error)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return order


# ---------------------------------------------------------------------------
# 买方资金账户
# ---------------------------------------------------------------------------

@router.post("/fund-accounts/{enterprise_id}/{year}/deposit", response_model=schemas.FundAccount)
def deposit_fund(enterprise_id: int, year: int, body: schemas.FundDepositRequest,
                 db: Session = Depends(get_db)):
    if not crud.get_enterprise(db, enterprise_id):
        raise HTTPException(status_code=404, detail="企业不存在")
    return matching.deposit_fund(db, enterprise_id, year, body.amount)


@router.get("/fund-accounts/{enterprise_id}/{year}", response_model=schemas.FundAccount)
def read_fund_account(enterprise_id: int, year: int, db: Session = Depends(get_db)):
    account = matching.get_or_create_fund_account(db, enterprise_id, year)
    db.commit()
    return account


# ---------------------------------------------------------------------------
# 预授权查询
# ---------------------------------------------------------------------------

@router.get("/auths", response_model=List[schemas.OrderAuth])
def read_auths(
    enterprise_id: Optional[int] = None,
    order_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[AuthStatus] = None,
    db: Session = Depends(get_db)
):
    return matching.list_auths(
        db, enterprise_id=enterprise_id, order_id=order_id, year=year, status=status
    )


@router.post("/auths/expire", response_model=List[schemas.OrderAuth])
def expire_auths(year: Optional[int] = None, db: Session = Depends(get_db)):
    """手动触发授权过期释放（幂等，重启回放也会自动执行）"""
    return matching.expire_due_auths(db, year=year)


# ---------------------------------------------------------------------------
# 撮合（只消费有效授权）
# ---------------------------------------------------------------------------

@router.post("/match", response_model=schemas.CreditTransactionWithDetail)
def match_specific_orders(
    match_request: schemas.CreditOrderMatchRequest,
    db: Session = Depends(get_db)
):
    txn, error, _task = matching.run_specified_match(db, match_request)
    if error:
        raise HTTPException(status_code=400, detail=error)
    if not txn:
        raise HTTPException(status_code=400, detail="撮合失败")
    return txn


@router.post("/match-all/{year}", response_model=schemas.PreAuthMatchResponse)
def match_all_orders(year: int, db: Session = Depends(get_db)):
    """
    自动撮合：价格优先、同价时间优先，公平部分成交。
    并发重复请求经幂等键裁决，最终只有一个任务、一个结果。
    """
    task, existing, error = matching.run_auto_match(db, year=year)
    if error and task is None and existing is None:
        return schemas.PreAuthMatchResponse(success=True, message=error, transactions=[])

    target = task or existing
    view = matching.task_view(target)
    txn_models = [
        db.get(CreditTransaction, it.transaction_id)
        for it in target.items if it.status == "succeeded" and it.transaction_id
    ]
    txn_models = [t for t in txn_models if t]

    partial = target.status == MatchTaskStatus.PARTIAL
    if existing is not None and task is None:
        message = f"命中幂等结果（任务 {target.task_no}）：并发请求只产生这一个成交结果"
        idem_hit = True
    elif partial:
        message = (f"部分成交：{target.completed_items}/{target.total_items} 笔已落账清算；"
                   "未成交项的授权未被消费，再次撮合即可继续成交（任务内有逐项可查的部分结果）")
        idem_hit = False
    else:
        message = f"成功完成 {target.completed_items} 笔预授权撮合，全部成交已即时清算"
        idem_hit = False

    return schemas.PreAuthMatchResponse(
        success=True,
        message=message,
        idempotent_hit=idem_hit,
        task=view,
        transactions=txn_models,
        partial=partial,
    )


# ---------------------------------------------------------------------------
# 撮合任务与重启回放
# ---------------------------------------------------------------------------

@router.get("/tasks", response_model=List[schemas.MatchTaskView])
def read_tasks(year: Optional[int] = None, status: Optional[MatchTaskStatus] = None,
               db: Session = Depends(get_db)):
    return [matching.task_view(t) for t in matching.list_tasks(db, year=year, status=status)]


@router.get("/tasks/{task_id}", response_model=schemas.MatchTaskView)
def read_task(task_id: int, db: Session = Depends(get_db)):
    task = matching.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="撮合任务不存在")
    return matching.task_view(task)


@router.post("/tasks/resume")
def resume_tasks(db: Session = Depends(get_db)):
    """服务重启/人工触发：回放未完成任务，已成交项跳过，绝不重复成交"""
    recovered = matching.resume_pending_tasks(db)
    return {
        "success": True,
        "message": f"回放完成，恢复 {len(recovered)} 个未完成任务",
        "tasks": [matching.task_view(t) for t in recovered],
    }


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


def _order_book(db: Session, year: int, otype: OrderType, reverse: bool):
    pending = crud.get_credit_orders(db, year=year, order_type=otype,
                                     status=OrderStatus.PENDING, limit=1000)
    partial = crud.get_credit_orders(db, year=year, order_type=otype,
                                     status=OrderStatus.PARTIAL, limit=1000)
    # 盘口只展示"有效预授权"数量，撤单/过期/已被其他成交消费的部分不会挂在盘口
    levels: Dict[float, float] = {}
    for o in pending + partial:
        auth = matching._auth_for_order(db, o.id)
        avail = auth.available_amount if auth else 0.0
        if avail <= 0.01:
            continue
        levels[o.unit_price] = round(levels.get(o.unit_price, 0.0) + avail, 2)

    book = [
        {"price": price, "volume": vol}
        for price, vol in sorted(levels.items(), reverse=reverse)
    ]
    return {
        "year": year,
        "order_type": otype.value,
        "order_book": book,
        "total_volume": round(sum(levels.values()), 2),
        "price_levels": len(levels),
    }


@router.get("/sell-order-book/{year}")
def get_sell_order_book(year: int, db: Session = Depends(get_db)):
    return _order_book(db, year, OrderType.SELL, False)


@router.get("/buy-order-book/{year}")
def get_buy_order_book(year: int, db: Session = Depends(get_db)):
    return _order_book(db, year, OrderType.BUY, True)
