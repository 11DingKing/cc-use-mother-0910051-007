import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, Boolean, Text, UniqueConstraint, Index
from sqlalchemy.orm import relationship

from .database import Base


class CreditRecordStatus(str, enum.Enum):
    CALCULATED = "calculated"
    PUBLICIZED = "publicized"
    CONFIRMED = "confirmed"


class OrderType(str, enum.Enum):
    SELL = "sell"
    BUY = "buy"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CarryoverStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Enterprise(Base):
    __tablename__ = "enterprises"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    short_name = Column(String(50), unique=True, index=True)
    credit_code = Column(String(50), unique=True, index=True)
    address = Column(String(200))
    contact_person = Column(String(50))
    contact_phone = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vehicle_models = relationship("VehicleModel", back_populates="enterprise")
    credit_transactions_from = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.from_enterprise_id",
        back_populates="from_enterprise"
    )
    credit_transactions_to = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.to_enterprise_id",
        back_populates="to_enterprise"
    )
    credit_orders = relationship(
        "CreditOrder",
        foreign_keys="CreditOrder.enterprise_id",
        back_populates="enterprise"
    )
    credit_carryovers_from = relationship(
        "CreditCarryover",
        foreign_keys="CreditCarryover.enterprise_id",
        back_populates="enterprise"
    )


class VehicleModel(Base):
    __tablename__ = "vehicle_models"

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    model_name = Column(String(100), nullable=False, index=True)
    model_code = Column(String(50), unique=True, nullable=False, index=True)
    curb_weight = Column(Float, nullable=False, comment="整备质量(kg)")
    power_consumption = Column(Float, nullable=False, comment="百公里电耗(kWh/100km)")
    range = Column(Float, nullable=False, comment="续航里程(km)")
    annual_output = Column(Integer, nullable=False, comment="年产量(辆)")
    production_year = Column(Integer, nullable=False, comment="生产年份")
    is_suspected_weight_manipulation = Column(Boolean, default=False, comment="是否疑似堆重量放宽限值")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    enterprise = relationship("Enterprise", back_populates="vehicle_models")
    credit_records = relationship("CreditRecord", back_populates="vehicle_model")


class CreditRecord(Base):
    __tablename__ = "credit_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="核算年份")
    power_consumption_limit = Column(Float, nullable=False, comment="电耗限值(kWh/100km)")
    actual_power_consumption = Column(Float, nullable=False, comment="实际电耗(kWh/100km)")
    unit_credit = Column(Float, nullable=False, comment="单车积分(分/辆)")
    total_credit = Column(Float, nullable=False, comment="总积分(分)")
    annual_output = Column(Integer, nullable=False, comment="年产量(辆)")
    status = Column(Enum(CreditRecordStatus), default=CreditRecordStatus.CALCULATED, nullable=False)
    calculated_at = Column(DateTime, default=datetime.utcnow)
    publicized_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vehicle_model = relationship("VehicleModel", back_populates="credit_records")


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    id = Column(Integer, primary_key=True, index=True)
    transaction_no = Column(String(50), unique=True, nullable=False, index=True)
    from_enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    to_enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    sell_order_id = Column(Integer, ForeignKey("credit_orders.id"), comment="卖单ID")
    buy_order_id = Column(Integer, ForeignKey("credit_orders.id"), comment="买单ID")
    credit_amount = Column(Float, nullable=False, comment="交易积分数量")
    unit_price = Column(Float, comment="交易单价(元/分)")
    total_amount = Column(Float, comment="交易总额(元)")
    transaction_date = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="completed", comment="交易状态")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    from_enterprise = relationship(
        "Enterprise",
        foreign_keys=[from_enterprise_id],
        back_populates="credit_transactions_from"
    )
    to_enterprise = relationship(
        "Enterprise",
        foreign_keys=[to_enterprise_id],
        back_populates="credit_transactions_to"
    )
    sell_order = relationship(
        "CreditOrder",
        foreign_keys=[sell_order_id],
        back_populates="sell_transactions"
    )
    buy_order = relationship(
        "CreditOrder",
        foreign_keys=[buy_order_id],
        back_populates="buy_transactions"
    )


class CreditOrder(Base):
    __tablename__ = "credit_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="核算年度")
    order_type = Column(Enum(OrderType), nullable=False, comment="订单类型：sell/buy")
    unit_price = Column(Float, nullable=False, comment="报价单价(元/分)")
    total_amount = Column(Float, nullable=False, comment="挂单总积分数量")
    filled_amount = Column(Float, default=0.0, comment="已成交积分数量")
    remaining_amount = Column(Float, nullable=False, comment="剩余积分数量")
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False, comment="订单状态")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = Column(DateTime, comment="过期时间")
    authorized_amount = Column(Float, default=0.0, comment="预授权冻结的积分/资金额度数量")
    released_amount = Column(Float, default=0.0, comment="已释放(撤单/过期)数量")

    enterprise = relationship("Enterprise", back_populates="credit_orders")
    authorizations = relationship(
        "OrderAuthorization",
        back_populates="order",
        cascade="all, delete-orphan"
    )
    sell_transactions = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.sell_order_id",
        back_populates="sell_order"
    )
    buy_transactions = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.buy_order_id",
        back_populates="buy_order"
    )

    @property
    def held_amount(self) -> float:
        """当前仍被有效预授权冻结、可供继续成交的数量（积分口径）。
        撮合一落账即同时扣减 remaining 与授权 held，二者恒等。"""
        return round(self.remaining_amount or 0.0, 2)

    @property
    def settled_amount(self) -> float:
        return round(sum(
            t.credit_amount for t in self.sell_transactions + self.buy_transactions
            if t.status == "completed"), 2)

    @property
    def matched_unsettled_amount(self) -> float:
        return round(sum(
            t.credit_amount for t in self.sell_transactions + self.buy_transactions
            if t.status == "matched"), 2)


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    year = Column(Integer, nullable=False, comment="年度")
    trade_date = Column(DateTime, default=datetime.utcnow, comment="交易日期")
    unit_price = Column(Float, nullable=False, comment="成交单价(元/分)")
    credit_amount = Column(Float, nullable=False, comment="成交积分数量")
    total_amount = Column(Float, nullable=False, comment="成交总额(元)")
    from_enterprise_id = Column(Integer, ForeignKey("enterprises.id"))
    to_enterprise_id = Column(Integer, ForeignKey("enterprises.id"))
    transaction_id = Column(Integer, ForeignKey("credit_transactions.id"))
    created_at = Column(DateTime, default=datetime.utcnow)


class CreditCarryover(Base):
    __tablename__ = "credit_carryovers"

    id = Column(Integer, primary_key=True, index=True)
    carryover_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    from_year = Column(Integer, nullable=False, comment="结转来源年度")
    to_year = Column(Integer, nullable=False, comment="结转目标年度")
    original_amount = Column(Float, nullable=False, comment="原始正积分结余")
    carryover_ratio = Column(Float, nullable=False, comment="结转比例")
    carryover_amount = Column(Float, nullable=False, comment="实际结转积分数量")
    used_amount = Column(Float, default=0.0, comment="已使用结转积分数量")
    remaining_amount = Column(Float, nullable=False, comment="剩余结转积分数量")
    status = Column(Enum(CarryoverStatus), default=CarryoverStatus.APPROVED, nullable=False, comment="结转状态")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime)

    enterprise = relationship("Enterprise", back_populates="credit_carryovers_from")


class AnnualCreditSummary(Base):
    __tablename__ = "annual_credit_summaries"

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="年度")
    total_positive_credit = Column(Float, default=0.0, comment="当年正积分")
    total_negative_credit = Column(Float, default=0.0, comment="当年负积分")
    net_credit = Column(Float, default=0.0, comment="当年净积分")
    carryover_in = Column(Float, default=0.0, comment="上年结转积分")
    carryover_out = Column(Float, default=0.0, comment="结转下年积分")
    bought_credit = Column(Float, default=0.0, comment="买入积分")
    sold_credit = Column(Float, default=0.0, comment="卖出积分")
    final_net_credit = Column(Float, default=0.0, comment="最终净积分")
    credit_gap = Column(Float, default=0.0, comment="最终积分缺口")
    credit_surplus = Column(Float, default=0.0, comment="最终积分钟余")
    is_compliant = Column(Boolean, default=True, comment="是否达标")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True},
    )


# ============================================================================
# 成交前预授权（Pre-trade Authorization）相关模型
#
# 资金/积分流转五段口径：
#   挂单 total_amount → 预授权 authorized(HELD) → 成交 filled(MATCHED leg)
#   → 清算 settled(SETTLED txn) → 释放 released(撤单/过期 RELEASED/EXPIRED)
# 不变量：authorized = consumed + held + released（每笔授权）
#        批次 total = available + frozen + consumed
#        账户总额 = balance + frozen + 在途(matched 未清算)
# ============================================================================


class BatchStatus(str, enum.Enum):
    ACTIVE = "active"       # 有可用额度
    EXHAUSTED = "exhausted"  # 已售罄/冻结消费完毕


class AuthorizationStatus(str, enum.Enum):
    HELD = "held"            # 冻结中，撮合可消费
    CONSUMED = "consumed"    # 已被成交消费（其中可能尚未清算）
    RELEASED = "released"    # 撤单主动释放
    EXPIRED = "expired"      # 授权过期释放


class ExecutionStatus(str, enum.Enum):
    COMMITTED = "committed"  # 撮合计划已整体落账（腿均为 matched）
    PARTIAL = "partial"      # 清算仅完成部分，剩余可恢复重放
    SETTLED = "settled"      # 全部腿清算完成
    FAILED = "failed"        # 撮合阶段失败，无任何落账


class LegStatus(str, enum.Enum):
    MATCHED = "matched"      # 已成交未清算
    SETTLED = "settled"      # 已清算落账
    FAILED = "failed"        # 清算失败，等待恢复


class OutboxTaskType(str, enum.Enum):
    SETTLE_EXECUTION = "settle_execution"        # 成交后清算回放
    EXPIRE_AUTHORIZATIONS = "expire_authorizations"  # 授权/挂单过期扫描


class OutboxTaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class CreditBatch(Base):
    """卖方可售积分批次：挂卖单时按 FIFO 冻结批次可用额度。"""
    __tablename__ = "credit_batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="批次所属核算年度")
    total_amount = Column(Float, nullable=False, comment="批次积分总量")
    frozen_amount = Column(Float, default=0.0, comment="被挂单预授权冻结数量")
    consumed_amount = Column(Float, default=0.0, comment="已成交消费数量")
    status = Column(Enum(BatchStatus), default=BatchStatus.ACTIVE, nullable=False)
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def available_amount(self) -> float:
        return round(self.total_amount - self.frozen_amount - self.consumed_amount, 2)

    __table_args__ = (
        Index("ix_credit_batches_ent_year", "enterprise_id", "year"),
    )


class FundsAccount(Base):
    """买方资金账户：挂买单冻结资金，清算时把冻结款划付卖方。"""
    __tablename__ = "funds_accounts"

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), unique=True, nullable=False)
    currency = Column(String(10), default="CNY", nullable=False)
    balance = Column(Float, default=0.0, comment="可用资金余额")
    frozen_amount = Column(Float, default=0.0, comment="买单预授权冻结资金")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OrderAuthorization(Base):
    """
    挂单预授权：挂单成功即生成。
    卖单：冻结 CreditBatch 额度（可跨多个批次，每批次一行 HELD）；
    买单：冻结 FundsAccount 资金（一行 HELD，按订单最大应付款）。
    撮合只消费 HELD 且未过期的授权；撤单/过期 RELEASED/EXPIRED 回补额度。
    """
    __tablename__ = "order_authorizations"

    id = Column(Integer, primary_key=True, index=True)
    auth_no = Column(String(50), unique=True, nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=False)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    side = Column(Enum(OrderType), nullable=False, comment="授权方向 sell/buy")
    batch_id = Column(Integer, ForeignKey("credit_batches.id"), nullable=True, comment="卖单冻结的积分批次")
    funds_account_id = Column(Integer, ForeignKey("funds_accounts.id"), nullable=True, comment="买单冻结的资金账户")
    amount = Column(Float, nullable=False, comment="本行冻结数量(积分或资金)")
    consumed_amount = Column(Float, default=0.0, comment="已被成交消费数量")
    released_amount = Column(Float, default=0.0, comment="已释放数量(撤单/过期)")
    status = Column(Enum(AuthorizationStatus), default=AuthorizationStatus.HELD, nullable=False)
    expires_at = Column(DateTime, nullable=False, comment="授权过期时间")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    order = relationship("CreditOrder", back_populates="authorizations")
    batch = relationship("CreditBatch")
    funds_account = relationship("FundsAccount")

    @property
    def held_amount(self) -> float:
        """仍冻结的行单位数量：卖单行为积分，买单行为资金元（订单口径需再除以价格上限折算）。"""
        return round(self.amount - self.consumed_amount - self.released_amount, 2)

    __table_args__ = (
        Index("ix_order_authorizations_order", "order_id"),
        Index("ix_order_authorizations_status", "status"),
    )


class TradeExecution(Base):
    """
    一次撮合的确定性执行计划（工作单）。
    一次撮合涉及多笔订单时，计划内各腿按 seq 确定顺序落账；
    撮合阶段原子提交（COMMITTED），清算阶段允许 PARTIAL 并由任务箱回放恢复。
    """
    __tablename__ = "trade_executions"

    id = Column(Integer, primary_key=True, index=True)
    execution_no = Column(String(50), unique=True, nullable=False, index=True)
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)
    year = Column(Integer, nullable=False)
    trigger_type = Column(String(20), default="manual", comment="manual/auto")
    sell_order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=True)
    buy_order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=True)
    status = Column(Enum(ExecutionStatus), default=ExecutionStatus.COMMITTED, nullable=False)
    planned_count = Column(Integer, default=0, comment="计划成交腿数")
    settled_count = Column(Integer, default=0, comment="已清算腿数")
    failed_count = Column(Integer, default=0, comment="清算失败腿数")
    total_credit_amount = Column(Float, default=0.0, comment="计划成交积分总量")
    total_cash_amount = Column(Float, default=0.0, comment="计划成交资金总量")
    error_detail = Column(Text, comment="部分结果/失败原因，供恢复时解释")
    created_at = Column(DateTime, default=datetime.utcnow)
    committed_at = Column(DateTime)
    settled_at = Column(DateTime)


class CreditTradeLeg(Base):
    """撮合计划中的一腿（一笔卖单与一笔买单的一次部分成交），原订单全程可追踪。"""
    __tablename__ = "credit_trade_legs"

    id = Column(Integer, primary_key=True, index=True)
    execution_id = Column(Integer, ForeignKey("trade_executions.id"), nullable=False)
    seq = Column(Integer, nullable=False, comment="执行计划内确定顺序，从0开始")
    sell_order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=False)
    buy_order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=False)
    credit_amount = Column(Float, nullable=False, comment="本腿成交积分数量")
    matched_price = Column(Float, nullable=False, comment="成交价(元/分)")
    total_amount = Column(Float, nullable=False, comment="本腿成交金额(元)")
    status = Column(Enum(LegStatus), default=LegStatus.MATCHED, nullable=False)
    transaction_id = Column(Integer, ForeignKey("credit_transactions.id"), unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime)

    execution = relationship("TradeExecution")
    transaction = relationship("CreditTransaction", foreign_keys=[transaction_id], post_update=True)

    __table_args__ = (
        Index("ix_trade_legs_exec_seq", "execution_id", "seq", unique=True),
        Index("ix_trade_legs_sell_order", "sell_order_id"),
        Index("ix_trade_legs_buy_order", "buy_order_id"),
    )


class OutboxTask(Base):
    """任务箱：清算回放与授权过期扫描，服务重启后据此回放，认领机制保证不重复执行。"""
    __tablename__ = "outbox_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_type = Column(Enum(OutboxTaskType), nullable=False)
    execution_id = Column(Integer, ForeignKey("trade_executions.id"), nullable=True)
    status = Column(Enum(OutboxTaskStatus), default=OutboxTaskStatus.PENDING, nullable=False)
    payload = Column(Text, comment="JSON 参数")
    available_at = Column(DateTime, default=datetime.utcnow, comment="最早可认领时间")
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=10)
    last_error = Column(Text)
    locked_by = Column(String(64), comment="当前认领者(实例ID)")
    locked_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    execution = relationship("TradeExecution")

    __table_args__ = (
        UniqueConstraint("task_type", "execution_id", name="uq_outbox_task_execution"),
        Index("ix_outbox_claim", "status", "available_at"),
    )


class IdempotencyRecord(Base):
    """API 级幂等记录：同一 Idempotency-Key 的并发/重放请求只产生一个结果。"""
    __tablename__ = "idempotency_records"

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String(64), unique=True, nullable=False, index=True)
    scope = Column(String(64), nullable=False, comment="适用端点/场景")
    request_hash = Column(String(128), comment="请求体指纹，键相同但体不同则拒绝")
    execution_id = Column(Integer, ForeignKey("trade_executions.id"), nullable=True)
    status_code = Column(Integer, default=200)
    response_body = Column(Text, comment="首次成功响应的 JSON 快照")
    created_at = Column(DateTime, default=datetime.utcnow)
