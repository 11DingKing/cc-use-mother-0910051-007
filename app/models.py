import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, Boolean, Text, UniqueConstraint
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


class ResourceType(str, enum.Enum):
    """预授权占用的资源类型：卖方积分批次 / 买方资金额度"""
    CREDIT = "credit"
    FUND = "fund"


class AuthStatus(str, enum.Enum):
    """预授权生命周期：冻结中 → 部分消费 → 全部消费；或释放/过期"""
    FROZEN = "frozen"
    PARTIAL_CONSUMED = "partial_consumed"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


class MatchTaskStatus(str, enum.Enum):
    """撮合任务状态：支持宕机回放与可恢复部分结果"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


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
    idempotency_key = Column(String(80), unique=True, nullable=True, index=True, comment="成交幂等键：回放不会重复成交")
    task_item_id = Column(Integer, ForeignKey("match_task_items.id", use_alter=True), nullable=True, comment="来源撮合任务项")
    from_enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    to_enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    sell_order_id = Column(Integer, ForeignKey("credit_orders.id"), comment="卖单ID")
    buy_order_id = Column(Integer, ForeignKey("credit_orders.id"), comment="买单ID")
    credit_amount = Column(Float, nullable=False, comment="交易积分数量")
    unit_price = Column(Float, comment="交易单价(元/分)")
    total_amount = Column(Float, comment="交易总额(元)")
    transaction_date = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="completed", comment="成交状态：matched已成交/cleared已清算/failed")
    settled_amount = Column(Float, default=0.0, comment="已清算数量")
    settled_at = Column(DateTime, comment="清算完成时间")
    settlement_no = Column(String(50), nullable=True, comment="清算单号")
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
    frozen_amount = Column(Float, default=0.0, comment="预授权冻结数量(卖单为积分/买单为资金可购积分)")
    released_amount = Column(Float, default=0.0, comment="累计释放数量(撤单/授权过期)")
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False, comment="订单状态")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = Column(DateTime, comment="过期时间")

    enterprise = relationship("Enterprise", back_populates="credit_orders")
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
    auths = relationship("OrderAuth", back_populates="order")


class OrderAuth(Base):
    """成交前预授权：挂单时冻结卖方积分批次或买方资金额度，撮合只消费有效授权"""
    __tablename__ = "order_auths"

    id = Column(Integer, primary_key=True, index=True)
    auth_no = Column(String(50), unique=True, nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, comment="核算年度")
    resource_type = Column(Enum(ResourceType), nullable=False, comment="资源类型：credit/fund")
    status = Column(Enum(AuthStatus), default=AuthStatus.FROZEN, nullable=False, index=True)
    frozen_amount = Column(Float, nullable=False, comment="冻结数量(积分分额或资金对应积分额度)")
    consumed_amount = Column(Float, default=0.0, comment="已被成交消费数量")
    released_amount = Column(Float, default=0.0, comment="已释放数量(撤单/过期)")
    frozen_fund = Column(Float, default=0.0, comment="买单冻结资金(元)，仅资金授权使用")
    consumed_fund = Column(Float, default=0.0, comment="已成交清算资金(元)")
    released_fund = Column(Float, default=0.0, comment="已释放资金(元)")
    expires_at = Column(DateTime, nullable=False, comment="授权过期时间")
    frozen_at = Column(DateTime, default=datetime.utcnow)
    consumed_at = Column(DateTime)
    released_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    remark = Column(String(500))

    order = relationship("CreditOrder", back_populates="auths")

    @property
    def available_amount(self) -> float:
        """有效(可消费)授权数量"""
        return round(self.frozen_amount - self.consumed_amount - self.released_amount, 2)


class MatchTask(Base):
    """撮合任务：一次撮合涉及多笔订单时的整体编排、幂等与回放依据"""
    __tablename__ = "match_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_no = Column(String(50), unique=True, nullable=False, index=True)
    idempotency_key = Column(String(80), unique=True, nullable=True, index=True, comment="幂等键：并发重复请求只产生一个结果")
    year = Column(Integer, nullable=False)
    task_type = Column(String(20), default="auto", comment="auto/specified")
    status = Column(Enum(MatchTaskStatus), default=MatchTaskStatus.PENDING, nullable=False, index=True)
    total_items = Column(Integer, default=0, comment="计划成交笔数")
    completed_items = Column(Integer, default=0, comment="已落账笔数")
    total_credit_amount = Column(Float, default=0.0, comment="计划成交积分")
    completed_credit_amount = Column(Float, default=0.0, comment="已落账积分")
    error_detail = Column(Text, comment="失败/部分成功原因")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

    items = relationship("MatchTaskItem", back_populates="task", cascade="all, delete-orphan")


class MatchTaskItem(Base):
    """撮合任务中的单笔订单配对：按确定顺序执行，记录可恢复的部分结果"""
    __tablename__ = "match_task_items"
    __table_args__ = (
        UniqueConstraint("sell_order_id", "buy_order_id", "task_id", name="uq_task_order_pair"),
    )

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("match_tasks.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False, comment="确定的执行顺序(价格优先、时间优先)")
    sell_order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=False)
    buy_order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=False)
    sell_auth_id = Column(Integer, ForeignKey("order_auths.id"), comment="卖方积分授权")
    buy_auth_id = Column(Integer, ForeignKey("order_auths.id"), comment="买方资金授权")
    credit_amount = Column(Float, nullable=False, comment="计划成交积分")
    matched_price = Column(Float, nullable=False, comment="成交价")
    total_amount = Column(Float, nullable=False, comment="成交金额(元)")
    status = Column(String(20), default="pending", comment="pending/succeeded/failed/skipped")
    transaction_id = Column(Integer, ForeignKey("credit_transactions.id", use_alter=True), nullable=True, comment="落账成交记录")
    error_detail = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    executed_at = Column(DateTime)

    task = relationship("MatchTask", back_populates="items")
    transaction = relationship("CreditTransaction", foreign_keys=[transaction_id], post_update=True)


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


class MarketFundAccount(Base):
    """买方资金账户：挂买单时校验并冻结资金额度，杜绝余额不足的已成交记录"""
    __tablename__ = "market_fund_accounts"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "year", name="uq_fund_account_ent_year"),
    )

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, comment="核算年度")
    balance = Column(Float, default=0.0, comment="账户可用余额(元)")
    frozen_amount = Column(Float, default=0.0, comment="预授权冻结资金(元)")
    consumed_amount = Column(Float, default=0.0, comment="已成交清算资金(元)")
    released_amount = Column(Float, default=0.0, comment="累计释放冻结资金(元)")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
