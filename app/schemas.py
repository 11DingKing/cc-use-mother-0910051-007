from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field

from .models import (
    CreditRecordStatus, OrderType, OrderStatus, CarryoverStatus,
    BatchStatus, AuthorizationStatus, ExecutionStatus, LegStatus,
    OutboxTaskType, OutboxTaskStatus,
)


class EnterpriseBase(BaseModel):
    name: str = Field(..., max_length=100, description="企业名称")
    short_name: Optional[str] = Field(None, max_length=50, description="企业简称")
    credit_code: Optional[str] = Field(None, max_length=50, description="统一社会信用代码")
    address: Optional[str] = Field(None, max_length=200, description="企业地址")
    contact_person: Optional[str] = Field(None, max_length=50, description="联系人")
    contact_phone: Optional[str] = Field(None, max_length=50, description="联系电话")


class EnterpriseCreate(EnterpriseBase):
    pass


class EnterpriseUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    short_name: Optional[str] = Field(None, max_length=50)
    credit_code: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=200)
    contact_person: Optional[str] = Field(None, max_length=50)
    contact_phone: Optional[str] = Field(None, max_length=50)


class Enterprise(EnterpriseBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EnterpriseWithStats(Enterprise):
    model_config = {"protected_namespaces": (), "from_attributes": True}

    model_count: int = 0
    total_annual_output: int = 0
    total_credit: float = 0.0


class VehicleModelBase(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int = Field(..., description="所属企业ID")
    model_name: str = Field(..., max_length=100, description="车型名称")
    model_code: str = Field(..., max_length=50, description="车型代码")
    curb_weight: float = Field(..., gt=0, description="整备质量(kg)")
    power_consumption: float = Field(..., gt=0, description="百公里电耗(kWh/100km)")
    range: float = Field(..., gt=0, description="续航里程(km)")
    annual_output: int = Field(..., ge=0, description="年产量(辆)")
    production_year: int = Field(..., description="生产年份")


class VehicleModelCreate(VehicleModelBase):
    pass


class VehicleModelUpdate(BaseModel):
    enterprise_id: Optional[int] = None
    model_name: Optional[str] = Field(None, max_length=100)
    model_code: Optional[str] = Field(None, max_length=50)
    curb_weight: Optional[float] = Field(None, gt=0)
    power_consumption: Optional[float] = Field(None, gt=0)
    range: Optional[float] = Field(None, gt=0)
    annual_output: Optional[int] = Field(None, ge=0)
    production_year: Optional[int] = None
    is_suspected_weight_manipulation: Optional[bool] = None


class VehicleModel(VehicleModelBase):
    id: int
    is_suspected_weight_manipulation: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VehicleModelWithEnterprise(VehicleModel):
    enterprise: Enterprise
    power_consumption_limit: Optional[float] = None
    unit_credit: Optional[float] = None

    class Config:
        from_attributes = True


class CreditRecordBase(BaseModel):
    vehicle_model_id: int
    year: int
    power_consumption_limit: float
    actual_power_consumption: float
    unit_credit: float
    total_credit: float
    annual_output: int


class CreditRecordCreate(CreditRecordBase):
    pass


class CreditRecord(CreditRecordBase):
    id: int
    status: CreditRecordStatus
    calculated_at: Optional[datetime] = None
    publicized_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CreditRecordWithDetail(CreditRecord):
    vehicle_model: VehicleModel
    enterprise: Optional[Enterprise] = None

    class Config:
        from_attributes = True


class CreditRecordStatusUpdate(BaseModel):
    status: CreditRecordStatus


class CreditRecordUpdate(BaseModel):
    power_consumption_limit: Optional[float] = None
    actual_power_consumption: Optional[float] = None
    unit_credit: Optional[float] = None
    total_credit: Optional[float] = None
    annual_output: Optional[int] = None


class CreditTransactionBase(BaseModel):
    from_enterprise_id: int
    to_enterprise_id: int
    credit_amount: float
    unit_price: Optional[float] = None
    total_amount: Optional[float] = None
    remark: Optional[str] = None


class CreditTransactionCreate(CreditTransactionBase):
    pass


class CreditTransaction(CreditTransactionBase):
    id: int
    transaction_no: str
    transaction_date: datetime
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class CreditTransactionWithDetail(CreditTransaction):
    from_enterprise: Enterprise
    to_enterprise: Enterprise

    class Config:
        from_attributes = True


class CalculationResult(BaseModel):
    model_config = {"protected_namespaces": ()}

    vehicle_model_id: int
    model_name: str
    curb_weight: float
    power_consumption_limit: float
    actual_power_consumption: float
    unit_credit: float
    annual_output: int
    total_credit: float
    is_compliant: bool


class EnterpriseCreditSummaryResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int
    enterprise_name: str
    total_positive_credit: float
    total_negative_credit: float
    net_credit: float
    required_credit: float
    credit_gap: float
    credit_surplus: float
    compliance_rate: float
    average_power_consumption: float
    weighted_power_consumption: float
    model_count: int
    compliant_model_count: int


class MatchResultResponse(BaseModel):
    from_enterprise_id: int
    from_enterprise_name: str
    to_enterprise_id: int
    to_enterprise_name: str
    credit_amount: float
    unit_price: float
    total_amount: float


class MatchAndExecuteResponse(BaseModel):
    success: bool
    message: str
    transactions: List[CreditTransactionWithDetail] = []
    remaining_gap: float = 0.0
    remaining_surplus: float = 0.0


class WeightSuggestionResponse(BaseModel):
    current_weight: float
    current_limit: float
    is_suspicious: bool
    suggestions: List[dict]


class EnterpriseStatsResponse(BaseModel):
    enterprise_id: int
    enterprise_name: str
    model_count: int
    total_output: int
    average_power_consumption: float
    weighted_power_consumption: float
    average_power_consumption_limit: float
    compliance_rate: float
    total_positive_credit: float
    total_negative_credit: float
    net_credit: float


class SuspiciousModelResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    model_name: str
    model_code: str
    enterprise_name: str
    curb_weight: float
    power_consumption: float
    range: float
    power_consumption_limit: float
    annual_output: int
    weight_analysis: dict


class CreditOrderBase(BaseModel):
    enterprise_id: int
    year: int
    order_type: OrderType
    unit_price: float = Field(..., gt=0, description="报价单价(元/分)")
    total_amount: float = Field(..., gt=0, description="挂单总积分数量")
    remark: Optional[str] = Field(None, max_length=500)
    expires_at: Optional[datetime] = None


class CreditOrderCreate(CreditOrderBase):
    credit_batch_ids: Optional[List[int]] = Field(
        None, description="卖单指定冻结的积分批次；不传则按FIFO自动选择"
    )


class CreditOrderUpdate(BaseModel):
    unit_price: Optional[float] = Field(None, gt=0)
    total_amount: Optional[float] = Field(None, gt=0)
    status: Optional[OrderStatus] = None
    remark: Optional[str] = None


class CreditOrder(CreditOrderBase):
    id: int
    order_no: str
    filled_amount: float
    remaining_amount: float
    status: OrderStatus
    authorized_amount: float = Field(0.0, description="已预授权冻结的总数量（积分/资金对应的积分额度）")
    released_amount: float = Field(0.0, description="撤单/过期已释放数量")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CreditOrderWithDetail(CreditOrder):
    enterprise: Enterprise
    sell_transactions: List["CreditTransaction"] = []
    buy_transactions: List["CreditTransaction"] = []
    authorizations: List["OrderAuthorizationOut"] = []
    # 五段数量口径，便于查询时直接区分
    matched_unsettled_amount: float = 0.0
    settled_amount: float = 0.0
    held_amount: float = 0.0

    class Config:
        from_attributes = True


class CreditOrderMatchRequest(BaseModel):
    buy_order_id: int
    sell_order_id: int
    credit_amount: float
    idempotency_key: Optional[str] = Field(None, max_length=64, description="客户端幂等键；同键重放返回首次结果")


class CreditBatchCreate(BaseModel):
    enterprise_id: int
    year: int
    total_amount: float = Field(..., gt=0, description="批次可售积分总量")
    remark: Optional[str] = None


class CreditBatch(BaseModel):
    id: int
    batch_no: str
    enterprise_id: int
    year: int
    total_amount: float
    frozen_amount: float = Field(..., description="被卖单预授权冻结数量")
    consumed_amount: float = Field(..., description="已成交消费数量")
    available_amount: float = Field(..., description="可用=总-冻结-已消费")
    status: BatchStatus
    remark: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FundsAccountDeposit(BaseModel):
    amount: float = Field(..., gt=0, description="入账金额(元)")


class FundsAccount(BaseModel):
    id: int
    enterprise_id: int
    currency: str
    balance: float = Field(..., description="可用资金")
    frozen_amount: float = Field(..., description="买单预授权冻结资金")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OrderAuthorizationOut(BaseModel):
    id: int
    auth_no: str
    order_id: int
    enterprise_id: int
    side: OrderType
    batch_id: Optional[int] = None
    funds_account_id: Optional[int] = None
    amount: float = Field(..., description="本行冻结数量（卖单为积分，买单为资金元）")
    consumed_amount: float
    released_amount: float
    held_amount: float = Field(..., description="仍冻结=amount-consumed-released")
    status: AuthorizationStatus
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CreditTradeLegOut(BaseModel):
    id: int
    execution_id: int
    seq: int
    sell_order_id: int
    buy_order_id: int
    credit_amount: float
    matched_price: float
    total_amount: float
    status: LegStatus
    transaction_id: Optional[int] = None
    created_at: datetime
    settled_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TradeExecutionOut(BaseModel):
    id: int
    execution_no: str
    idempotency_key: Optional[str] = None
    year: int
    trigger_type: str
    sell_order_id: Optional[int] = None
    buy_order_id: Optional[int] = None
    status: ExecutionStatus
    planned_count: int
    settled_count: int
    failed_count: int
    total_credit_amount: float
    total_cash_amount: float
    error_detail: Optional[str] = None
    legs: List[CreditTradeLegOut] = []
    created_at: datetime
    committed_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MatchPairResponse(BaseModel):
    replayed: bool = Field(..., description="是否命中幂等键的重复/重放请求")
    execution: TradeExecutionOut


class MatchAutoResponse(BaseModel):
    success: bool
    message: str
    execution: Optional[TradeExecutionOut] = None


class OutboxTaskOut(BaseModel):
    id: int
    task_type: OutboxTaskType
    execution_id: Optional[int] = None
    status: OutboxTaskStatus
    attempts: int
    max_attempts: int
    last_error: Optional[str] = None
    locked_by: Optional[str] = None
    available_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class OrderQuantityBreakdown(BaseModel):
    """挂单五段口径查询：挂单/预授权/成交/清算/释放数量各不相同。"""
    model_config = {"protected_namespaces": ()}

    order_id: int
    order_no: str
    side: OrderType
    status: OrderStatus
    posted_amount: float = Field(..., description="挂单总数量")
    filled_amount: float = Field(..., description="已成交=已清算+已成交未清算")
    remaining_amount: float = Field(..., description="挂单剩余可成交数量")
    authorized_amount: float = Field(..., description="预授权累计冻结数量")
    held_amount: float = Field(..., description="当前仍被预授权冻结数量")
    matched_unsettled_amount: float = Field(..., description="已成交未清算数量")
    settled_amount: float = Field(..., description="已清算数量")
    released_amount: float = Field(..., description="撤单/过期释放数量")
    authorizations: List[OrderAuthorizationOut] = []


class MatchWithOrdersResponse(BaseModel):
    success: bool
    message: str
    transactions: List[CreditTransactionWithDetail] = []
    matched_orders: List[dict] = []
    remaining_gap: float = 0.0
    remaining_surplus: float = 0.0


class PriceHistoryBase(BaseModel):
    year: int
    trade_date: datetime
    unit_price: float
    credit_amount: float
    total_amount: float
    from_enterprise_id: Optional[int] = None
    to_enterprise_id: Optional[int] = None
    transaction_id: Optional[int] = None


class PriceHistoryCreate(PriceHistoryBase):
    pass


class PriceHistory(PriceHistoryBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class PriceTrendResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    year: int
    avg_price: float
    min_price: float
    max_price: float
    total_volume: float
    total_value: float
    trade_count: int
    price_by_date: List[dict] = []


class CreditCarryoverBase(BaseModel):
    enterprise_id: int
    from_year: int
    to_year: int
    original_amount: float
    carryover_ratio: float
    carryover_amount: float
    remark: Optional[str] = None


class CreditCarryoverCreate(CreditCarryoverBase):
    pass


class CreditCarryoverUpdate(BaseModel):
    status: Optional[CarryoverStatus] = None
    remark: Optional[str] = None


class CreditCarryover(CreditCarryoverBase):
    id: int
    carryover_no: str
    used_amount: float
    remaining_amount: float
    status: CarryoverStatus
    created_at: datetime
    approved_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CreditCarryoverWithDetail(CreditCarryover):
    enterprise: Enterprise

    class Config:
        from_attributes = True


class AnnualCreditSummaryBase(BaseModel):
    enterprise_id: int
    year: int
    total_positive_credit: float = 0.0
    total_negative_credit: float = 0.0
    net_credit: float = 0.0
    carryover_in: float = 0.0
    carryover_out: float = 0.0
    bought_credit: float = 0.0
    sold_credit: float = 0.0
    final_net_credit: float = 0.0
    credit_gap: float = 0.0
    credit_surplus: float = 0.0
    is_compliant: bool = True


class AnnualCreditSummaryCreate(AnnualCreditSummaryBase):
    pass


class AnnualCreditSummary(AnnualCreditSummaryBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AnnualCreditSummaryWithDetail(AnnualCreditSummary):
    enterprise: Enterprise
    carryovers: List[CreditCarryover] = []
    transactions: List[CreditTransactionWithDetail] = []

    class Config:
        from_attributes = True


class EnterpriseMultiYearSummaryResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int
    enterprise_name: str
    years: List[int] = []
    annual_summaries: List[dict] = []
    total_carryover_in: float = 0.0
    total_carryover_out: float = 0.0
    total_bought: float = 0.0
    total_sold: float = 0.0


class CreditPredictionRequest(BaseModel):
    enterprise_id: Optional[int] = None
    target_year: int
    output_growth_rate: Optional[float] = Field(0.05, description="产量年增长率")
    pc_improvement_rate: Optional[float] = Field(0.02, description="电耗年改善率")


class ModelPrediction(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_name: str
    model_code: str
    curb_weight: float
    current_power_consumption: float
    predicted_power_consumption: float
    power_consumption_limit: float
    predicted_output: int
    predicted_unit_credit: float
    predicted_total_credit: float


class CreditPredictionResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int
    enterprise_name: str
    target_year: int
    historical_years: List[int] = []
    historical_credits: List[dict] = []
    predicted_total_positive: float
    predicted_total_negative: float
    predicted_net_credit: float
    predicted_compliance_rate: float
    model_predictions: List[ModelPrediction] = []
    prediction_method: str
    assumptions: dict


class MarketOverviewResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    year: int
    total_sell_orders: int
    total_buy_orders: int
    total_sell_volume: float
    total_buy_volume: float
    avg_sell_price: float
    avg_buy_price: float
    min_sell_price: float
    max_sell_price: float
    min_buy_price: float
    max_buy_price: float
    pending_sell_volume: float
    pending_buy_volume: float
    matched_count: int
    matched_volume: float
    matched_value: float


class CarryoverSummaryResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int
    enterprise_name: str
    from_year: int
    to_year: int
    original_surplus: float
    carryover_ratio: float
    carryover_amount: float
    used_amount: float
    remaining_amount: float
    status: str


CreditOrderWithDetail.model_rebuild()
