from typing import List, Optional, Tuple, Dict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from . import models, schemas, matching
from .rules import (
    calculate_power_consumption_limit,
    calculate_unit_credit,
    calculate_total_credit,
    detect_weight_manipulation,
    EnterpriseCreditSummary,
    match_credit_transactions,
    calculate_carryover_amount,
    calculate_chain_carryover_amount,
    validate_order_price,
    predict_next_year_credit,
    EnterpriseCreditSummaryV2,
    PRICE_FLOOR,
    PRICE_CEILING,
    DEFAULT_MARKET_PRICE,
    CARRYOVER_MAX_YEARS
)
from .models import (
    CreditRecordStatus,
    OrderType,
    OrderStatus,
    CarryoverStatus
)

VALID_STATUS_TRANSITIONS = {
    CreditRecordStatus.CALCULATED: [CreditRecordStatus.PUBLICIZED],
    CreditRecordStatus.PUBLICIZED: [CreditRecordStatus.CONFIRMED],
    CreditRecordStatus.CONFIRMED: []
}


def validate_status_transition(
    current_status: CreditRecordStatus,
    target_status: CreditRecordStatus
) -> Tuple[bool, Optional[str]]:
    if current_status == target_status:
        return False, f"记录已经是 {target_status.value} 状态，无需重复操作"

    allowed_next = VALID_STATUS_TRANSITIONS.get(current_status, [])
    if target_status not in allowed_next:
        allowed_str = ", ".join([s.value for s in allowed_next]) if allowed_next else "无"
        return False, (
            f"不允许从 {current_status.value} 直接变更为 {target_status.value}。"
            f"合法的下一个状态: {allowed_str}"
        )

    return True, None


def get_enterprise(db: Session, enterprise_id: int) -> Optional[models.Enterprise]:
    return db.query(models.Enterprise).filter(models.Enterprise.id == enterprise_id).first()


def get_enterprise_by_name(db: Session, name: str) -> Optional[models.Enterprise]:
    return db.query(models.Enterprise).filter(models.Enterprise.name == name).first()


def get_enterprises(db: Session, skip: int = 0, limit: int = 100) -> List[models.Enterprise]:
    return db.query(models.Enterprise).offset(skip).limit(limit).all()


def create_enterprise(db: Session, enterprise: schemas.EnterpriseCreate) -> models.Enterprise:
    db_enterprise = models.Enterprise(**enterprise.model_dump())
    db.add(db_enterprise)
    db.commit()
    db.refresh(db_enterprise)
    return db_enterprise


def update_enterprise(
    db: Session, enterprise_id: int, enterprise_update: schemas.EnterpriseUpdate
) -> Optional[models.Enterprise]:
    db_enterprise = get_enterprise(db, enterprise_id)
    if not db_enterprise:
        return None
    update_data = enterprise_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_enterprise, key, value)
    db_enterprise.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_enterprise)
    return db_enterprise


def delete_enterprise(db: Session, enterprise_id: int) -> bool:
    db_enterprise = get_enterprise(db, enterprise_id)
    if not db_enterprise:
        return False
    db.delete(db_enterprise)
    db.commit()
    return True


def get_vehicle_model(db: Session, model_id: int) -> Optional[models.VehicleModel]:
    return db.query(models.VehicleModel).filter(models.VehicleModel.id == model_id).first()


def get_vehicle_model_by_code(db: Session, model_code: str) -> Optional[models.VehicleModel]:
    return db.query(models.VehicleModel).filter(models.VehicleModel.model_code == model_code).first()


def get_vehicle_models(
    db: Session, enterprise_id: Optional[int] = None, skip: int = 0, limit: int = 100
) -> List[models.VehicleModel]:
    query = db.query(models.VehicleModel)
    if enterprise_id:
        query = query.filter(models.VehicleModel.enterprise_id == enterprise_id)
    return query.offset(skip).limit(limit).all()


def create_vehicle_model(db: Session, model: schemas.VehicleModelCreate) -> models.VehicleModel:
    is_suspicious = detect_weight_manipulation(
        model.curb_weight,
        model.power_consumption,
        model.range
    )
    db_model = models.VehicleModel(
        **model.model_dump(),
        is_suspected_weight_manipulation=is_suspicious
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return db_model


def update_vehicle_model(
    db: Session, model_id: int, model_update: schemas.VehicleModelUpdate
) -> Optional[models.VehicleModel]:
    db_model = get_vehicle_model(db, model_id)
    if not db_model:
        return None
    update_data = model_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_model, key, value)

    if "curb_weight" in update_data or "power_consumption" in update_data or "range" in update_data:
        db_model.is_suspected_weight_manipulation = detect_weight_manipulation(
            db_model.curb_weight,
            db_model.power_consumption,
            db_model.range
        )

    db_model.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_model)
    return db_model


def delete_vehicle_model(db: Session, model_id: int) -> bool:
    db_model = get_vehicle_model(db, model_id)
    if not db_model:
        return False
    db.delete(db_model)
    db.commit()
    return True


def calculate_vehicle_credit(
    db: Session, model_id: int, year: Optional[int] = None
) -> Optional[schemas.CalculationResult]:
    db_model = get_vehicle_model(db, model_id)
    if not db_model:
        return None

    calc_year = year or db_model.production_year
    limit = calculate_power_consumption_limit(db_model.curb_weight)
    unit_credit = calculate_unit_credit(db_model.power_consumption, limit)
    total_credit = calculate_total_credit(unit_credit, db_model.annual_output)

    return schemas.CalculationResult(
        vehicle_model_id=db_model.id,
        model_name=db_model.model_name,
        curb_weight=db_model.curb_weight,
        power_consumption_limit=limit,
        actual_power_consumption=db_model.power_consumption,
        unit_credit=unit_credit,
        annual_output=db_model.annual_output,
        total_credit=total_credit,
        is_compliant=db_model.power_consumption <= limit
    )


def create_credit_record(db: Session, model_id: int, year: int) -> Optional[models.CreditRecord]:
    existing = db.query(models.CreditRecord).filter(
        models.CreditRecord.vehicle_model_id == model_id,
        models.CreditRecord.year == year
    ).first()
    if existing:
        return existing

    result = calculate_vehicle_credit(db, model_id, year)
    if not result:
        return None

    db_record = models.CreditRecord(
        vehicle_model_id=model_id,
        year=year,
        power_consumption_limit=result.power_consumption_limit,
        actual_power_consumption=result.actual_power_consumption,
        unit_credit=result.unit_credit,
        total_credit=result.total_credit,
        annual_output=result.annual_output,
        status=CreditRecordStatus.CALCULATED,
        calculated_at=datetime.utcnow()
    )
    db.add(db_record)
    db.commit()
    db.refresh(db_record)
    return db_record


def batch_calculate_credits(db: Session, year: int) -> List[models.CreditRecord]:
    models_list = db.query(models.VehicleModel).filter(
        models.VehicleModel.production_year == year
    ).all()
    records = []
    for model in models_list:
        record = create_credit_record(db, model.id, year)
        if record:
            records.append(record)
    return records


def get_credit_records(
    db: Session,
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[CreditRecordStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.CreditRecord]:
    query = db.query(models.CreditRecord)
    if enterprise_id:
        query = query.join(models.VehicleModel).filter(
            models.VehicleModel.enterprise_id == enterprise_id
        )
    if year:
        query = query.filter(models.CreditRecord.year == year)
    if status:
        query = query.filter(models.CreditRecord.status == status)
    return query.offset(skip).limit(limit).all()


def update_credit_record_status(
    db: Session, record_id: int, status: CreditRecordStatus
) -> Optional[models.CreditRecord]:
    db_record = db.query(models.CreditRecord).filter(models.CreditRecord.id == record_id).first()
    if not db_record:
        return None

    is_valid, error_msg = validate_status_transition(db_record.status, status)
    if not is_valid:
        raise ValueError(error_msg)

    previous_status = db_record.status
    db_record.status = status
    now = datetime.utcnow()

    if status == CreditRecordStatus.PUBLICIZED:
        db_record.publicized_at = now
    elif status == CreditRecordStatus.CONFIRMED:
        db_record.confirmed_at = now

    db_record.updated_at = now
    db.commit()
    db.refresh(db_record)

    if status == CreditRecordStatus.CONFIRMED:
        vehicle_model = get_vehicle_model(db, db_record.vehicle_model_id)
        if vehicle_model:
            update_annual_summary_with_transactions(
                db, vehicle_model.enterprise_id, db_record.year
            )

    return db_record


def batch_update_credit_records_status(
    db: Session, year: int, status: CreditRecordStatus
) -> int:
    records = db.query(models.CreditRecord).filter(
        models.CreditRecord.year == year
    ).all()

    now = datetime.utcnow()
    count = 0
    updated_enterprise_ids = set()

    for record in records:
        is_valid, _ = validate_status_transition(record.status, status)
        if not is_valid:
            continue

        record.status = status
        if status == CreditRecordStatus.PUBLICIZED:
            record.publicized_at = now
        elif status == CreditRecordStatus.CONFIRMED:
            record.confirmed_at = now
            vehicle_model = get_vehicle_model(db, record.vehicle_model_id)
            if vehicle_model:
                updated_enterprise_ids.add(vehicle_model.enterprise_id)
        record.updated_at = now
        count += 1

    db.commit()

    for enterprise_id in updated_enterprise_ids:
        update_annual_summary_with_transactions(db, enterprise_id, year)

    return count


def calculate_enterprise_credit_summary(
    db: Session, enterprise_id: int, year: int
) -> Optional[EnterpriseCreditSummary]:
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        return None

    records = db.query(models.CreditRecord).join(models.VehicleModel).filter(
        models.VehicleModel.enterprise_id == enterprise_id,
        models.CreditRecord.year == year,
        models.CreditRecord.status == CreditRecordStatus.CONFIRMED
    ).all()

    if not records:
        return EnterpriseCreditSummary(
            enterprise_id=enterprise_id,
            enterprise_name=enterprise.name,
            total_positive_credit=0.0,
            total_negative_credit=0.0,
            net_credit=0.0,
            required_credit=0.0,
            credit_gap=0.0,
            credit_surplus=0.0,
            compliance_rate=100.0,
            average_power_consumption=0.0,
            weighted_power_consumption=0.0,
            model_count=0,
            compliant_model_count=0
        )

    total_positive = sum(r.total_credit for r in records if r.total_credit > 0)
    total_negative = sum(r.total_credit for r in records if r.total_credit < 0)
    net_credit = total_positive + total_negative

    total_output = sum(r.annual_output for r in records)
    total_pc_weighted = sum(r.actual_power_consumption * r.annual_output for r in records)
    avg_pc_weighted = round(total_pc_weighted / total_output, 2) if total_output > 0 else 0.0
    avg_pc_simple = round(sum(r.actual_power_consumption for r in records) / len(records), 2) if records else 0.0

    compliant_count = sum(1 for r in records if r.actual_power_consumption <= r.power_consumption_limit)
    compliance_rate = round((compliant_count / len(records)) * 100, 2) if records else 100.0

    required_credit = abs(total_negative) if total_negative < 0 else 0
    credit_gap = max(0, required_credit - total_positive)
    credit_surplus = max(0, total_positive - required_credit)

    return EnterpriseCreditSummary(
        enterprise_id=enterprise_id,
        enterprise_name=enterprise.name,
        total_positive_credit=round(total_positive, 2),
        total_negative_credit=round(total_negative, 2),
        net_credit=round(net_credit, 2),
        required_credit=round(required_credit, 2),
        credit_gap=round(credit_gap, 2),
        credit_surplus=round(credit_surplus, 2),
        compliance_rate=round(compliance_rate, 2),
        average_power_consumption=round(avg_pc_simple, 2),
        weighted_power_consumption=round(avg_pc_weighted, 2),
        model_count=len(records),
        compliant_model_count=compliant_count
    )


def calculate_all_enterprise_summaries(
    db: Session, year: int
) -> List[EnterpriseCreditSummary]:
    enterprises = get_enterprises(db)
    summaries = []
    for ent in enterprises:
        summary = calculate_enterprise_credit_summary(db, ent.id, year)
        if summary:
            summaries.append(summary)
    return summaries


def generate_transaction_no(db: Session, counter: Optional[int] = None) -> str:
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d%H%M%S")
    if counter is not None:
        return f"TXN{timestamp}{counter:04d}"

    max_id = db.query(func.max(models.CreditTransaction.id)).scalar() or 0
    session_new_count = sum(
        1 for obj in db.new
        if isinstance(obj, models.CreditTransaction) and obj.id is None
    )
    next_id = max_id + session_new_count + 1
    return f"TXN{timestamp}{next_id:04d}"


def create_credit_transaction(
    db: Session, transaction: schemas.CreditTransactionCreate
) -> models.CreditTransaction:
    txn_no = generate_transaction_no(db)
    unit_price = transaction.unit_price or 3000.0
    total_amount = transaction.total_amount or (transaction.credit_amount * unit_price)

    db_txn = models.CreditTransaction(
        **transaction.model_dump(exclude={"unit_price", "total_amount"}),
        transaction_no=txn_no,
        unit_price=unit_price,
        total_amount=total_amount
    )
    db.add(db_txn)
    db.commit()
    db.refresh(db_txn)
    return db_txn


def get_credit_transactions(
    db: Session,
    enterprise_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.CreditTransaction]:
    query = db.query(models.CreditTransaction)
    if enterprise_id:
        query = query.filter(
            (models.CreditTransaction.from_enterprise_id == enterprise_id) |
            (models.CreditTransaction.to_enterprise_id == enterprise_id)
        )
    return query.order_by(models.CreditTransaction.transaction_date.desc()).offset(skip).limit(limit).all()


def match_and_execute_transactions(
    db: Session, year: int, unit_price: float = 3000.0
) -> Tuple[List[models.CreditTransaction], float, float]:
    summaries = calculate_all_enterprise_summaries(db, year)
    match_results = match_credit_transactions(summaries, unit_price)

    transactions = []
    for mr in match_results:
        txn = schemas.CreditTransactionCreate(
            from_enterprise_id=mr.from_enterprise_id,
            to_enterprise_id=mr.to_enterprise_id,
            credit_amount=round(mr.credit_amount, 2),
            unit_price=round(mr.unit_price, 2),
            total_amount=round(mr.total_amount, 2),
            remark=f"{year}年度双积分自动撮合交易"
        )
        db_txn = create_credit_transaction(db, txn)
        transactions.append(db_txn)
        update_annual_summary_after_transaction(db, db_txn, year)

    remaining_gap = sum(round(s.credit_gap, 2) for s in summaries if s.credit_gap > 0.01)
    remaining_surplus = sum(round(s.credit_surplus, 2) for s in summaries if s.credit_surplus > 0.01)

    return transactions, round(remaining_gap, 2), round(remaining_surplus, 2)


def get_suspicious_weight_models(db: Session) -> List[models.VehicleModel]:
    return db.query(models.VehicleModel).filter(
        models.VehicleModel.is_suspected_weight_manipulation == True
    ).all()


def get_enterprise_stats(db: Session, year: int) -> List[schemas.EnterpriseStatsResponse]:
    enterprises = get_enterprises(db)
    stats_list = []

    for ent in enterprises:
        models_list = get_vehicle_models(db, enterprise_id=ent.id)
        if not models_list:
            continue

        total_output = sum(m.annual_output for m in models_list)
        avg_pc = round(sum(m.power_consumption for m in models_list) / len(models_list), 2) if models_list else 0.0
        weighted_pc = round(sum(m.power_consumption * m.annual_output for m in models_list) / total_output, 2) if total_output > 0 else 0.0
        avg_limit = round(sum(calculate_power_consumption_limit(m.curb_weight) for m in models_list) / len(models_list), 2) if models_list else 0.0

        records = db.query(models.CreditRecord).join(models.VehicleModel).filter(
            models.VehicleModel.enterprise_id == ent.id,
            models.CreditRecord.year == year
        ).all()

        if records:
            compliant_count = sum(1 for r in records if r.actual_power_consumption <= r.power_consumption_limit)
            compliance_rate = round((compliant_count / len(records)) * 100, 2) if records else 100.0
            total_positive = sum(r.total_credit for r in records if r.total_credit > 0)
            total_negative = sum(r.total_credit for r in records if r.total_credit < 0)
        else:
            compliant_count = sum(1 for m in models_list if m.power_consumption <= calculate_power_consumption_limit(m.curb_weight))
            compliance_rate = round((compliant_count / len(models_list)) * 100, 2) if models_list else 100.0
            total_positive = 0.0
            total_negative = 0.0

        stats_list.append(schemas.EnterpriseStatsResponse(
            enterprise_id=ent.id,
            enterprise_name=ent.name,
            model_count=len(models_list),
            total_output=total_output,
            average_power_consumption=round(avg_pc, 2),
            weighted_power_consumption=round(weighted_pc, 2),
            average_power_consumption_limit=round(avg_limit, 2),
            compliance_rate=round(compliance_rate, 2),
            total_positive_credit=round(total_positive, 2),
            total_negative_credit=round(total_negative, 2),
            net_credit=round(total_positive + total_negative, 2)
        ))

    return stats_list


def generate_order_no(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    count = db.query(models.CreditOrder).filter(
        func.substr(models.CreditOrder.order_no, 3, 8) == today
    ).count() + 1
    return f"OR{today}{count:05d}"


def generate_carryover_no(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    count = db.query(models.CreditCarryover).filter(
        func.substr(models.CreditCarryover.carryover_no, 3, 8) == today
    ).count() + 1
    return f"CO{today}{count:05d}"


def create_credit_order(
    db: Session, order: schemas.CreditOrderCreate
) -> Optional[models.CreditOrder]:
    """
    挂单（成交前预授权）。

    历史调用方直接传入 Session：挂单流程内部需要数据库级写锁串行化，
    因此这里改用 matching 引擎并忽略传入的 db（其事务尚未持锁），
    由引擎在 BEGIN IMMEDIATE 内重新打开会话完成冻结并提交；
    提交后用调用方 Session 重新查询返回绑定实例，避免 DetachedInstance。
    """
    order = matching.create_order(order, credit_batch_ids=order.credit_batch_ids)
    # 引擎在独立写锁会话提交；本调用方 Session 身份映射中的批次/账户等
    # 实例已过期，统一 expire 后再读才能拿到冻结后的最新状态
    db.expire_all()
    return db.query(models.CreditOrder).filter(models.CreditOrder.id == order.id).first()


def get_credit_order(db: Session, order_id: int) -> Optional[models.CreditOrder]:
    return db.query(models.CreditOrder).filter(models.CreditOrder.id == order_id).first()


# ---------------------------------------------------------------------------
# 成交前预授权：资金账户 / 积分批次 / 授权 / 执行计划 / 任务箱
# ---------------------------------------------------------------------------

def create_funds_account_if_absent(
    db: Session, enterprise_id: int, initial_balance: float = 0.0
) -> models.FundsAccount:
    """供初始化/管理端使用：账户已存在则直接返回，不重复入账。"""
    existing = db.query(models.FundsAccount).filter(
        models.FundsAccount.enterprise_id == enterprise_id
    ).first()
    if existing:
        return existing
    account = matching.create_funds_account(db, enterprise_id, initial_balance)
    db.commit()
    db.refresh(account)
    return account


def deposit_funds_account(db: Session, enterprise_id: int, amount: float) -> models.FundsAccount:
    account = matching.deposit_funds_account(db, enterprise_id, amount)
    db.commit()
    db.refresh(account)
    return account


def get_funds_account(db: Session, enterprise_id: int) -> Optional[models.FundsAccount]:
    return db.query(models.FundsAccount).filter(
        models.FundsAccount.enterprise_id == enterprise_id
    ).first()


def create_credit_batch(
    db: Session, enterprise_id: int, year: int, total_amount: float,
    remark: Optional[str] = None
) -> models.CreditBatch:
    batch = matching.create_credit_batch(db, enterprise_id, year, total_amount, remark)
    db.commit()
    db.refresh(batch)
    return batch


def get_credit_batches(
    db: Session, enterprise_id: Optional[int] = None, year: Optional[int] = None,
    skip: int = 0, limit: int = 100
) -> List[models.CreditBatch]:
    query = db.query(models.CreditBatch)
    if enterprise_id:
        query = query.filter(models.CreditBatch.enterprise_id == enterprise_id)
    if year:
        query = query.filter(models.CreditBatch.year == year)
    return query.order_by(models.CreditBatch.created_at.desc()).offset(skip).limit(limit).all()


def get_order_authorizations(
    db: Session, order_id: Optional[int] = None,
    enterprise_id: Optional[int] = None,
    side: Optional[OrderType] = None,
    status: Optional[models.AuthorizationStatus] = None,
    skip: int = 0, limit: int = 100
) -> List[models.OrderAuthorization]:
    query = db.query(models.OrderAuthorization)
    if order_id:
        query = query.filter(models.OrderAuthorization.order_id == order_id)
    if enterprise_id:
        query = query.filter(models.OrderAuthorization.enterprise_id == enterprise_id)
    if side:
        query = query.filter(models.OrderAuthorization.side == side)
    if status:
        query = query.filter(models.OrderAuthorization.status == status)
    return query.order_by(models.OrderAuthorization.created_at.desc()).offset(skip).limit(limit).all()


def get_trade_execution(db: Session, execution_id: int) -> Optional[models.TradeExecution]:
    return db.get(models.TradeExecution, execution_id)


def get_order_quantity_breakdown(db: Session, order_id: int) -> Optional[schemas.OrderQuantityBreakdown]:
    """五段口径：挂单/预授权/成交(未清算)/清算/释放，数量分别可查、可解释。"""
    # 撮合/撤单/过期可能在独立写锁会话提交；先丢弃本会话旧快照
    db.expire_all()
    order = get_credit_order(db, order_id)
    if not order:
        return None

    auths = get_order_authorizations(db, order_id=order_id)
    held = sum(
        _authorization_held(a) for a in auths
    )
    legs = db.query(models.CreditTradeLeg).filter(
        or_(models.CreditTradeLeg.sell_order_id == order_id,
            models.CreditTradeLeg.buy_order_id == order_id)
    ).all()
    # 已成交未清算：成交记录仍为 matched（腿可能 MATCHED 或待重试 FAILED，
    # 二者都属于“成交已落账、清算尚未完成”的可恢复部分结果）
    txns = db.query(models.CreditTransaction).filter(
        or_(models.CreditTransaction.sell_order_id == order_id,
            models.CreditTransaction.buy_order_id == order_id)
    ).all()
    matched_unsettled = round(sum(
        t.credit_amount for t in txns if t.status == "matched"), 2)
    settled = round(sum(t.credit_amount for t in txns if t.status == "completed"), 2)
    return schemas.OrderQuantityBreakdown(
        order_id=order.id,
        order_no=order.order_no,
        side=order.order_type,
        status=order.status,
        posted_amount=round(order.total_amount + order.released_amount, 2)
            if False else round(order.total_amount, 2),
        filled_amount=round(order.filled_amount, 2),
        remaining_amount=round(order.remaining_amount, 2),
        authorized_amount=round(order.authorized_amount, 2),
        held_amount=round(held, 2),
        matched_unsettled_amount=round(matched_unsettled, 2),
        settled_amount=round(settled, 2),
        released_amount=round(order.released_amount, 2),
        authorizations=[schemas.OrderAuthorizationOut.model_validate(a) for a in auths],
    )


def _authorization_held(auth: models.OrderAuthorization) -> float:
    if auth.side == OrderType.BUY:
        return round(auth.amount - auth.consumed_amount - auth.released_amount, 2) / PRICE_CEILING
    return round(auth.amount - auth.consumed_amount - auth.released_amount, 2)


def replay_pending_market_tasks(db: Session, batch_limit: int = 100) -> dict:
    """重启后任务回放入口（路由层也可定时触发）。"""
    return matching.replay_pending_tasks(batch_limit=batch_limit)


def get_credit_orders(
    db: Session,
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    order_type: Optional[OrderType] = None,
    status: Optional[OrderStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.CreditOrder]:
    query = db.query(models.CreditOrder)
    if enterprise_id:
        query = query.filter(models.CreditOrder.enterprise_id == enterprise_id)
    if year:
        query = query.filter(models.CreditOrder.year == year)
    if order_type:
        query = query.filter(models.CreditOrder.order_type == order_type)
    if status:
        query = query.filter(models.CreditOrder.status == status)
    return query.order_by(models.CreditOrder.created_at.desc()).offset(skip).limit(limit).all()


def update_credit_order(
    db: Session, order_id: int, order_update: schemas.CreditOrderUpdate
) -> Optional[models.CreditOrder]:
    # 改单涉及授权增减，必须在写锁事务内串行完成；返回时用调用方会话重查绑定实例
    matching.update_order(order_id, order_update)
    db.expire_all()
    return db.query(models.CreditOrder).filter(models.CreditOrder.id == order_id).first()


def cancel_credit_order(db: Session, order_id: int) -> Optional[models.CreditOrder]:
    # 撤单释放预授权，同样需要写锁内串行，防止与撮合并发
    matching.cancel_order(order_id)
    db.expire_all()
    return db.query(models.CreditOrder).filter(models.CreditOrder.id == order_id).first()


def match_orders_by_id(
    db: Session, match_request: schemas.CreditOrderMatchRequest
) -> Tuple[Optional[models.CreditTransaction], Optional[str]]:
    """
    指定挂单撮合（成交前预授权 + 两阶段成交/清算）。

    返回首条已落账成交记录与错误信息；成交可能处于 matched（待清算）或
    completed（已清算）。清算的具体执行与回放由撮合引擎/任务箱保证。
    """
    # 结束调用方会话可能持有的旧只读快照，确保随后读到引擎写锁事务提交后的最新状态
    db.rollback()
    try:
        execution, _replayed = matching.match_pair(
            sell_order_id=match_request.sell_order_id,
            buy_order_id=match_request.buy_order_id,
            credit_amount=match_request.credit_amount,
            idempotency_key=match_request.idempotency_key,
            trigger_type="manual",
            request_payload=match_request.model_dump(),
        )
    except ValueError as exc:
        return None, str(exc)

    txn = db.query(models.CreditTransaction).filter(
        models.CreditTransaction.sell_order_id == match_request.sell_order_id,
        models.CreditTransaction.buy_order_id == match_request.buy_order_id,
    ).order_by(models.CreditTransaction.id).first()
    if txn is None:
        return None, "撮合未生成成交记录"
    return txn, None


def match_all_pending_orders(
    db: Session, year: int, idempotency_key: Optional[str] = None
) -> Tuple[List[models.CreditTransaction], List[dict], float, float]:
    """
    价格-时间优先的自动连续撮合：整批挂单在一个执行计划内按确定顺序
    生成多腿成交；撮合原子提交，清算逐腿提交（部分结果可恢复回放）。
    """
    # 引擎在独立写锁事务提交；先结束调用方旧快照，返回的汇总才反映最新状态
    db.rollback()
    result = matching.match_auto(year, idempotency_key=idempotency_key)
    if result is None:
        return [], [], 0.0, 0.0
    execution_detached, _replayed = result
    execution = db.get(models.TradeExecution, execution_detached.id)

    transactions = db.query(models.CreditTransaction).join(
        models.CreditTradeLeg,
        models.CreditTradeLeg.transaction_id == models.CreditTransaction.id
    ).filter(
        models.CreditTradeLeg.execution_id == execution.id
    ).order_by(models.CreditTradeLeg.seq).all()

    matched_order_info = []
    for leg in db.query(models.CreditTradeLeg).filter(
        models.CreditTradeLeg.execution_id == execution.id
    ).order_by(models.CreditTradeLeg.seq).all():
        sell_order = get_credit_order(db, leg.sell_order_id)
        buy_order = get_credit_order(db, leg.buy_order_id)
        sell_ent = get_enterprise(db, sell_order.enterprise_id) if sell_order else None
        buy_ent = get_enterprise(db, buy_order.enterprise_id) if buy_order else None
        matched_order_info.append({
            "sell_order_id": leg.sell_order_id,
            "buy_order_id": leg.buy_order_id,
            "sell_enterprise": sell_ent.name if sell_ent else "",
            "buy_enterprise": buy_ent.name if buy_ent else "",
            "credit_amount": leg.credit_amount,
            "matched_price": leg.matched_price,
            "total_amount": leg.total_amount,
            "leg_status": leg.status.value,
        })

    remaining_surplus = sum(
        o.remaining_amount for o in get_credit_orders(
            db, year=year, order_type=OrderType.SELL,
            status=None, limit=100000
        ) if o.status in [OrderStatus.PENDING, OrderStatus.PARTIAL]
    )
    remaining_gap = sum(
        o.remaining_amount for o in get_credit_orders(
            db, year=year, order_type=OrderType.BUY,
            status=None, limit=100000
        ) if o.status in [OrderStatus.PENDING, OrderStatus.PARTIAL]
    )

    return transactions, matched_order_info, round(remaining_gap, 2), round(remaining_surplus, 2)


def create_price_history(
    db: Session, transaction: models.CreditTransaction, year: int
) -> models.PriceHistory:
    db_price = models.PriceHistory(
        year=year,
        trade_date=transaction.transaction_date,
        unit_price=transaction.unit_price or DEFAULT_MARKET_PRICE,
        credit_amount=transaction.credit_amount,
        total_amount=transaction.total_amount or (transaction.credit_amount * (transaction.unit_price or DEFAULT_MARKET_PRICE)),
        from_enterprise_id=transaction.from_enterprise_id,
        to_enterprise_id=transaction.to_enterprise_id,
        transaction_id=transaction.id
    )
    db.add(db_price)
    return db_price


def get_price_history(
    db: Session,
    year: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.PriceHistory]:
    query = db.query(models.PriceHistory)
    if year:
        query = query.filter(models.PriceHistory.year == year)
    if start_date:
        query = query.filter(models.PriceHistory.trade_date >= start_date)
    if end_date:
        query = query.filter(models.PriceHistory.trade_date <= end_date)
    return query.order_by(models.PriceHistory.trade_date.desc()).offset(skip).limit(limit).all()


def get_price_trend(db: Session, year: int) -> schemas.PriceTrendResponse:
    price_records = get_price_history(db, year=year, limit=10000)

    if not price_records:
        return schemas.PriceTrendResponse(
            year=year,
            avg_price=DEFAULT_MARKET_PRICE,
            min_price=PRICE_FLOOR,
            max_price=PRICE_CEILING,
            total_volume=0.0,
            total_value=0.0,
            trade_count=0,
            price_by_date=[]
        )

    prices = [p.unit_price for p in price_records]
    volumes = [p.credit_amount for p in price_records]
    values = [p.total_amount for p in price_records]

    daily_prices: Dict[str, List[float]] = {}
    for p in price_records:
        date_str = p.trade_date.strftime("%Y-%m-%d")
        if date_str not in daily_prices:
            daily_prices[date_str] = []
        daily_prices[date_str].append(p.unit_price)

    price_by_date = []
    for date_str in sorted(daily_prices.keys()):
        daily_avg = sum(daily_prices[date_str]) / len(daily_prices[date_str])
        price_by_date.append({
            "date": date_str,
            "avg_price": round(daily_avg, 2),
            "trade_count": len(daily_prices[date_str])
        })

    return schemas.PriceTrendResponse(
        year=year,
        avg_price=round(sum(prices) / len(prices), 2),
        min_price=round(min(prices), 2),
        max_price=round(max(prices), 2),
        total_volume=round(sum(volumes), 2),
        total_value=round(sum(values), 2),
        trade_count=len(price_records),
        price_by_date=price_by_date
    )


def get_market_overview(db: Session, year: int) -> schemas.MarketOverviewResponse:
    all_sell_orders = get_credit_orders(db, year=year, order_type=OrderType.SELL, limit=10000)
    all_buy_orders = get_credit_orders(db, year=year, order_type=OrderType.BUY, limit=10000)
    # 已成交包含“已成交未清算(matched)”与“已清算(completed)”两阶段
    all_matched_txn = db.query(models.CreditTransaction).filter(
        models.CreditTransaction.status.in_(["completed", "matched"])
    ).all()

    pending_sell = [o for o in all_sell_orders if o.status in [OrderStatus.PENDING, OrderStatus.PARTIAL]]
    pending_buy = [o for o in all_buy_orders if o.status in [OrderStatus.PENDING, OrderStatus.PARTIAL]]

    sell_prices = [o.unit_price for o in all_sell_orders]
    buy_prices = [o.unit_price for o in all_buy_orders]

    return schemas.MarketOverviewResponse(
        year=year,
        total_sell_orders=len(all_sell_orders),
        total_buy_orders=len(all_buy_orders),
        total_sell_volume=round(sum(o.total_amount for o in all_sell_orders), 2),
        total_buy_volume=round(sum(o.total_amount for o in all_buy_orders), 2),
        avg_sell_price=round(sum(sell_prices) / len(sell_prices), 2) if sell_prices else 0.0,
        avg_buy_price=round(sum(buy_prices) / len(buy_prices), 2) if buy_prices else 0.0,
        min_sell_price=round(min(sell_prices), 2) if sell_prices else 0.0,
        max_sell_price=round(max(sell_prices), 2) if sell_prices else 0.0,
        min_buy_price=round(min(buy_prices), 2) if buy_prices else 0.0,
        max_buy_price=round(max(buy_prices), 2) if buy_prices else 0.0,
        pending_sell_volume=round(sum(o.remaining_amount for o in pending_sell), 2),
        pending_buy_volume=round(sum(o.remaining_amount for o in pending_buy), 2),
        matched_count=len(all_matched_txn),
        matched_volume=round(sum(t.credit_amount for t in all_matched_txn), 2),
        matched_value=round(sum(t.total_amount or 0 for t in all_matched_txn), 2)
    )


def create_credit_carryover(
    db: Session, carryover: schemas.CreditCarryoverCreate
) -> models.CreditCarryover:
    carryover_no = generate_carryover_no(db)

    db_carryover = models.CreditCarryover(
        **carryover.model_dump(),
        carryover_no=carryover_no,
        used_amount=0.0,
        remaining_amount=carryover.carryover_amount,
        approved_at=datetime.utcnow()
    )
    db.add(db_carryover)
    db.commit()
    db.refresh(db_carryover)
    return db_carryover


def get_credit_carryover(
    db: Session, carryover_id: int
) -> Optional[models.CreditCarryover]:
    return db.query(models.CreditCarryover).filter(
        models.CreditCarryover.id == carryover_id
    ).first()


def get_credit_carryovers(
    db: Session,
    enterprise_id: Optional[int] = None,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    status: Optional[CarryoverStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.CreditCarryover]:
    query = db.query(models.CreditCarryover)
    if enterprise_id:
        query = query.filter(models.CreditCarryover.enterprise_id == enterprise_id)
    if from_year:
        query = query.filter(models.CreditCarryover.from_year == from_year)
    if to_year:
        query = query.filter(models.CreditCarryover.to_year == to_year)
    if status:
        query = query.filter(models.CreditCarryover.status == status)
    return query.order_by(models.CreditCarryover.created_at.desc()).offset(skip).limit(limit).all()


def execute_yearly_carryover(
    db: Session, from_year: int, to_year: int
) -> List[models.CreditCarryover]:
    enterprises = get_enterprises(db)
    carryovers = []

    year_diff = to_year - from_year
    if year_diff > CARRYOVER_MAX_YEARS:
        return carryovers

    for enterprise in enterprises:
        try:
            current_year = from_year
            current_surplus = 0.0

            start_summary = get_or_create_annual_summary(db, enterprise.id, from_year)
            if not start_summary or start_summary.credit_surplus <= 0.01:
                continue

            current_surplus = start_summary.credit_surplus
            total_carryover_ratio = 1.0

            for step in range(year_diff):
                src_year = from_year + step
                tgt_year = src_year + 1

                src_summary = get_or_create_annual_summary(db, enterprise.id, src_year)
                tgt_summary = get_or_create_annual_summary(db, enterprise.id, tgt_year)

                if step == 0:
                    amount_to_carry = current_surplus
                else:
                    amount_to_carry = tgt_summary.credit_surplus if tgt_summary.credit_surplus > 0.01 else 0.0

                if amount_to_carry <= 0.01:
                    continue

                ratio, carryover_amount = calculate_carryover_amount(
                    amount_to_carry,
                    1
                )
                total_carryover_ratio *= ratio

                if carryover_amount <= 0.01:
                    continue

                carryover_create = schemas.CreditCarryoverCreate(
                    enterprise_id=enterprise.id,
                    from_year=src_year,
                    to_year=tgt_year,
                    original_amount=amount_to_carry,
                    carryover_ratio=ratio,
                    carryover_amount=carryover_amount,
                    remark=f"{src_year}年度结转至{tgt_year}年度，结转比例{int(ratio*100)}%"
                )

                db_carryover = create_credit_carryover(db, carryover_create)
                carryovers.append(db_carryover)

                src_summary.carryover_out = round(src_summary.carryover_out + carryover_amount, 2)
                src_summary.credit_surplus = round(src_summary.credit_surplus - carryover_amount, 2)
                src_summary.final_net_credit = round(src_summary.final_net_credit - carryover_amount, 2)
                src_summary.updated_at = datetime.utcnow()

                tgt_summary.carryover_in = round(tgt_summary.carryover_in + carryover_amount, 2)
                tgt_summary.final_net_credit = round(tgt_summary.final_net_credit + carryover_amount, 2)

                if tgt_summary.credit_gap > 0.01:
                    used_amount = min(carryover_amount, tgt_summary.credit_gap)
                    db_carryover.used_amount = used_amount
                    db_carryover.remaining_amount = round(carryover_amount - used_amount, 2)
                    tgt_summary.credit_gap = round(tgt_summary.credit_gap - used_amount, 2)

                    if tgt_summary.credit_gap <= 0.01:
                        tgt_summary.is_compliant = True
                        tgt_summary.credit_surplus = round(abs(tgt_summary.credit_gap), 2)
                        tgt_summary.credit_gap = 0.0
                else:
                    tgt_summary.credit_surplus = round(tgt_summary.credit_surplus + carryover_amount, 2)

                tgt_summary.updated_at = datetime.utcnow()

            db.commit()

        except Exception as e:
            db.rollback()
            continue

    return carryovers


def get_carryover_summary(
    db: Session, enterprise_id: int, year: int
) -> List[schemas.CarryoverSummaryResponse]:
    carryovers = get_credit_carryovers(
        db, enterprise_id=enterprise_id, to_year=year, status=CarryoverStatus.APPROVED
    )

    result = []
    for c in carryovers:
        enterprise = get_enterprise(db, c.enterprise_id)
        result.append(schemas.CarryoverSummaryResponse(
            enterprise_id=c.enterprise_id,
            enterprise_name=enterprise.name if enterprise else "",
            from_year=c.from_year,
            to_year=c.to_year,
            original_surplus=c.original_amount,
            carryover_ratio=c.carryover_ratio,
            carryover_amount=c.carryover_amount,
            used_amount=c.used_amount,
            remaining_amount=c.remaining_amount,
            status=c.status.value
        ))

    return result


def get_or_create_annual_summary(
    db: Session, enterprise_id: int, year: int
) -> models.AnnualCreditSummary:
    existing = db.query(models.AnnualCreditSummary).filter(
        models.AnnualCreditSummary.enterprise_id == enterprise_id,
        models.AnnualCreditSummary.year == year
    ).first()

    if existing:
        return existing

    db_summary = models.AnnualCreditSummary(
        enterprise_id=enterprise_id,
        year=year
    )
    db.add(db_summary)
    db.commit()
    db.refresh(db_summary)
    return db_summary


def update_annual_summary_with_transactions(
    db: Session, enterprise_id: int, year: int
) -> models.AnnualCreditSummary:
    summary = get_or_create_annual_summary(db, enterprise_id, year)
    base_summary = calculate_enterprise_credit_summary(db, enterprise_id, year)

    if base_summary:
        summary.total_positive_credit = base_summary.total_positive_credit
        summary.total_negative_credit = base_summary.total_negative_credit
        summary.net_credit = base_summary.net_credit
        summary.credit_gap = base_summary.credit_gap
        summary.credit_surplus = base_summary.credit_surplus

    transactions = get_credit_transactions(db, enterprise_id=enterprise_id, limit=10000)
    bought = 0.0
    sold = 0.0
    for txn in transactions:
        txn_year = txn.transaction_date.year if txn.transaction_date else year
        if txn_year == year:
            if txn.to_enterprise_id == enterprise_id:
                bought += txn.credit_amount
            if txn.from_enterprise_id == enterprise_id:
                sold += txn.credit_amount

    summary.bought_credit = round(bought, 2)
    summary.sold_credit = round(sold, 2)

    carryovers_in = get_credit_carryovers(
        db, enterprise_id=enterprise_id, to_year=year, status=CarryoverStatus.APPROVED
    )
    total_carryover_in = sum(c.carryover_amount for c in carryovers_in)
    summary.carryover_in = round(total_carryover_in, 2)

    carryovers_out = get_credit_carryovers(
        db, enterprise_id=enterprise_id, from_year=year, status=CarryoverStatus.APPROVED
    )
    total_carryover_out = sum(c.carryover_amount for c in carryovers_out)
    summary.carryover_out = round(total_carryover_out, 2)

    available_credit = (
        summary.net_credit
        + summary.carryover_in
        + summary.bought_credit
        - summary.sold_credit
        - summary.carryover_out
    )
    summary.final_net_credit = round(available_credit, 2)

    if available_credit >= 0:
        summary.credit_gap = 0.0
        summary.credit_surplus = round(available_credit, 2)
        summary.is_compliant = True
    else:
        summary.credit_gap = round(abs(available_credit), 2)
        summary.credit_surplus = 0.0
        summary.is_compliant = False

    summary.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(summary)
    return summary


def calculate_enterprise_credit_summary_v2(
    db: Session, enterprise_id: int, year: int
) -> EnterpriseCreditSummaryV2:
    base_summary = calculate_enterprise_credit_summary(db, enterprise_id, year)

    annual_summary = update_annual_summary_with_transactions(db, enterprise_id, year)

    v2_summary = EnterpriseCreditSummaryV2(
        enterprise_id=base_summary.enterprise_id,
        enterprise_name=base_summary.enterprise_name,
        total_positive_credit=base_summary.total_positive_credit,
        total_negative_credit=base_summary.total_negative_credit,
        net_credit=base_summary.net_credit,
        required_credit=base_summary.required_credit,
        credit_gap=base_summary.credit_gap,
        credit_surplus=base_summary.credit_surplus,
        compliance_rate=base_summary.compliance_rate,
        average_power_consumption=base_summary.average_power_consumption,
        weighted_power_consumption=base_summary.weighted_power_consumption,
        model_count=base_summary.model_count,
        compliant_model_count=base_summary.compliant_model_count,
        carryover_in=annual_summary.carryover_in,
        carryover_out=annual_summary.carryover_out,
        bought_credit=annual_summary.bought_credit,
        sold_credit=annual_summary.sold_credit,
        final_net_credit=annual_summary.final_net_credit,
        final_credit_gap=annual_summary.credit_gap,
        final_credit_surplus=annual_summary.credit_surplus,
        is_compliant=annual_summary.is_compliant
    )

    return v2_summary


def calculate_all_enterprise_summaries_v2(
    db: Session, year: int
) -> List[EnterpriseCreditSummaryV2]:
    enterprises = get_enterprises(db)
    summaries = []
    for ent in enterprises:
        summary = calculate_enterprise_credit_summary_v2(db, ent.id, year)
        if summary:
            summaries.append(summary)
    return summaries


def get_multi_year_summary(
    db: Session, enterprise_id: int, start_year: int, end_year: int
) -> schemas.EnterpriseMultiYearSummaryResponse:
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        raise ValueError("企业不存在")

    years = list(range(start_year, end_year + 1))
    annual_summaries = []
    total_carryover_in = 0.0
    total_carryover_out = 0.0
    total_bought = 0.0
    total_sold = 0.0

    for year in years:
        summary = update_annual_summary_with_transactions(db, enterprise_id, year)
        annual_summaries.append({
            "year": year,
            "total_positive_credit": summary.total_positive_credit,
            "total_negative_credit": summary.total_negative_credit,
            "net_credit": summary.net_credit,
            "carryover_in": summary.carryover_in,
            "carryover_out": summary.carryover_out,
            "bought_credit": summary.bought_credit,
            "sold_credit": summary.sold_credit,
            "final_net_credit": summary.final_net_credit,
            "credit_gap": summary.credit_gap,
            "credit_surplus": summary.credit_surplus,
            "is_compliant": summary.is_compliant
        })
        total_carryover_in += summary.carryover_in
        total_carryover_out += summary.carryover_out
        total_bought += summary.bought_credit
        total_sold += summary.sold_credit

    return schemas.EnterpriseMultiYearSummaryResponse(
        enterprise_id=enterprise_id,
        enterprise_name=enterprise.name,
        years=years,
        annual_summaries=annual_summaries,
        total_carryover_in=round(total_carryover_in, 2),
        total_carryover_out=round(total_carryover_out, 2),
        total_bought=round(total_bought, 2),
        total_sold=round(total_sold, 2)
    )


def predict_enterprise_credit(
    db: Session,
    enterprise_id: int,
    target_year: int,
    output_growth_rate: float = 0.05,
    pc_improvement_rate: float = 0.02
) -> schemas.CreditPredictionResponse:
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        raise ValueError("企业不存在")

    models_list = get_vehicle_models(db, enterprise_id=enterprise_id, limit=1000)
    if not models_list:
        return schemas.CreditPredictionResponse(
            enterprise_id=enterprise_id,
            enterprise_name=enterprise.name,
            target_year=target_year,
            predicted_total_positive=0.0,
            predicted_total_negative=0.0,
            predicted_net_credit=0.0,
            predicted_compliance_rate=0.0,
            prediction_method="无历史数据",
            assumptions={}
        )

    historical_data = []
    for m in models_list:
        historical_data.append({
            "model_code": m.model_code,
            "model_name": m.model_name,
            "power_consumption": m.power_consumption,
            "annual_output": m.annual_output,
            "curb_weight": m.curb_weight,
            "year": m.production_year
        })

    historical_years = sorted(list(set(m.production_year for m in models_list)))
    historical_credits = []
    for year in historical_years:
        summary = calculate_enterprise_credit_summary(db, enterprise_id, year)
        if summary:
            historical_credits.append({
                "year": year,
                "total_positive": summary.total_positive_credit,
                "total_negative": summary.total_negative_credit,
                "net_credit": summary.net_credit,
                "compliance_rate": summary.compliance_rate
            })

    prediction = predict_next_year_credit(
        historical_data, output_growth_rate, pc_improvement_rate
    )

    model_predictions = [
        schemas.ModelPrediction(**md) for md in prediction.model_details
    ]

    return schemas.CreditPredictionResponse(
        enterprise_id=enterprise_id,
        enterprise_name=enterprise.name,
        target_year=target_year,
        historical_years=historical_years,
        historical_credits=historical_credits,
        predicted_total_positive=prediction.total_positive,
        predicted_total_negative=prediction.total_negative,
        predicted_net_credit=prediction.net_credit,
        predicted_compliance_rate=prediction.compliance_rate,
        model_predictions=model_predictions,
        prediction_method="趋势外推法",
        assumptions={
            "output_growth_rate": output_growth_rate,
            "pc_improvement_rate": pc_improvement_rate,
            "description": "基于历史最新车型数据，按给定增长率和改善率预测下一年度积分"
        }
    )


def predict_all_enterprises_credit(
    db: Session,
    target_year: int,
    output_growth_rate: float = 0.05,
    pc_improvement_rate: float = 0.02
) -> List[schemas.CreditPredictionResponse]:
    enterprises = get_enterprises(db)
    predictions = []
    for ent in enterprises:
        pred = predict_enterprise_credit(
            db, ent.id, target_year, output_growth_rate, pc_improvement_rate
        )
        predictions.append(pred)
    return predictions


def update_annual_summary_after_transaction(
    db: Session, transaction: models.CreditTransaction, year: int
) -> None:
    update_annual_summary_with_transactions(db, transaction.from_enterprise_id, year)
    update_annual_summary_with_transactions(db, transaction.to_enterprise_id, year)


def update_credit_record(
    db: Session, record_id: int, record_update: schemas.CreditRecordUpdate
) -> Optional[models.CreditRecord]:
    db_record = db.query(models.CreditRecord).filter(models.CreditRecord.id == record_id).first()
    if not db_record:
        return None

    if db_record.status == CreditRecordStatus.CONFIRMED:
        raise ValueError("已确认的积分记录不允许修改，如需修改请先联系管理员")

    update_data = record_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_record, key, value)

    db_record.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_record)

    vehicle_model = get_vehicle_model(db, db_record.vehicle_model_id)
    if vehicle_model:
        update_annual_summary_with_transactions(
            db, vehicle_model.enterprise_id, db_record.year
        )

    return db_record

