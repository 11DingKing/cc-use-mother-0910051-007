from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass


@dataclass
class PowerConsumptionLimitTier:
    min_weight: float
    max_weight: float
    limit: float


POWER_CONSUMPTION_LIMIT_TIERS: List[PowerConsumptionLimitTier] = [
    PowerConsumptionLimitTier(0, 1000, 10.5),
    PowerConsumptionLimitTier(1000, 1200, 11.5),
    PowerConsumptionLimitTier(1200, 1400, 12.5),
    PowerConsumptionLimitTier(1400, 1600, 13.5),
    PowerConsumptionLimitTier(1600, 1800, 14.5),
    PowerConsumptionLimitTier(1800, 2000, 15.5),
    PowerConsumptionLimitTier(2000, 2300, 17.0),
    PowerConsumptionLimitTier(2300, 2600, 18.5),
    PowerConsumptionLimitTier(2600, 3000, 20.0),
    PowerConsumptionLimitTier(3000, float('inf'), 22.0),
]


CREDIT_MULTIPLIER = 2.5

WEIGHT_MANIPULATION_THRESHOLD_RATIO = 0.008

SUSPICIOUS_WEIGHT_GAP = 150

CARRYOVER_MAX_YEARS = 3

CARRYOVER_RATIO_BY_YEAR: Dict[int, float] = {
    1: 0.80,
    2: 0.60,
    3: 0.40,
}

DEFAULT_MARKET_PRICE = 3000.0

PRICE_FLOOR = 1000.0

PRICE_CEILING = 8000.0


def calculate_power_consumption_limit(curb_weight: float) -> float:
    for tier in POWER_CONSUMPTION_LIMIT_TIERS:
        if tier.max_weight == float('inf'):
            if curb_weight >= tier.min_weight:
                return tier.limit
        else:
            if tier.min_weight <= curb_weight <= tier.max_weight:
                return tier.limit
    return POWER_CONSUMPTION_LIMIT_TIERS[-1].limit


def calculate_unit_credit(actual_power_consumption: float, limit: float) -> float:
    if limit <= 0 or actual_power_consumption < 0:
        return 0.0
    if actual_power_consumption == 0:
        return round(CREDIT_MULTIPLIER, 4)
    return round((limit - actual_power_consumption) / limit * CREDIT_MULTIPLIER, 4)


def calculate_total_credit(unit_credit: float, annual_output: int) -> float:
    if annual_output <= 0:
        return 0.0
    return round(unit_credit * annual_output, 2)


def detect_weight_manipulation(
    curb_weight: float,
    power_consumption: float,
    range_km: float
) -> bool:
    if curb_weight <= 0 or power_consumption <= 0 or range_km <= 0:
        return False

    pc_weight_ratio = power_consumption / curb_weight
    if pc_weight_ratio > WEIGHT_MANIPULATION_THRESHOLD_RATIO:
        return True

    if curb_weight <= 0:
        return False
    expected_range = power_consumption * 100 / curb_weight * 200
    if range_km < expected_range * 0.6:
        return True

    return False


def get_weight_suggestion(curb_weight: float) -> Dict:
    current_limit = calculate_power_consumption_limit(curb_weight)
    suggestions = []

    for i, tier in enumerate(POWER_CONSUMPTION_LIMIT_TIERS):
        is_in_tier = False
        if tier.max_weight == float('inf'):
            is_in_tier = curb_weight >= tier.min_weight
        else:
            is_in_tier = tier.min_weight <= curb_weight <= tier.max_weight

        if is_in_tier:
            if i > 0:
                lower_tier = POWER_CONSUMPTION_LIMIT_TIERS[i - 1]
                if lower_tier.max_weight != float('inf'):
                    weight_reduction = curb_weight - lower_tier.max_weight
                    if weight_reduction > 0 and weight_reduction <= SUSPICIOUS_WEIGHT_GAP:
                        suggestions.append({
                            "action": "减重",
                            "target_weight": lower_tier.max_weight,
                            "weight_reduction": round(weight_reduction, 1),
                            "new_limit": lower_tier.limit,
                            "limit_reduction": round(current_limit - lower_tier.limit, 2)
                        })
            break

    return {
        "current_weight": curb_weight,
        "current_limit": current_limit,
        "suggestions": suggestions,
        "is_suspicious": len(suggestions) > 0
    }


@dataclass
class EnterpriseCreditSummary:
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


@dataclass
class MatchResult:
    from_enterprise_id: int
    from_enterprise_name: str
    to_enterprise_id: int
    to_enterprise_name: str
    credit_amount: float
    unit_price: float
    total_amount: float


def match_credit_transactions(
    summaries: List[EnterpriseCreditSummary],
    unit_price: float = 3000.0
) -> List[MatchResult]:
    surplus_enterprises = [s for s in summaries if s.credit_surplus > 0.01]
    deficit_enterprises = [s for s in summaries if s.credit_gap > 0.01]

    surplus_enterprises.sort(key=lambda x: x.credit_surplus, reverse=True)
    deficit_enterprises.sort(key=lambda x: x.credit_gap, reverse=True)

    results = []
    surplus_idx = 0
    deficit_idx = 0

    while surplus_idx < len(surplus_enterprises) and deficit_idx < len(deficit_enterprises):
        surplus = surplus_enterprises[surplus_idx]
        deficit = deficit_enterprises[deficit_idx]

        transfer_amount = min(surplus.credit_surplus, deficit.credit_gap)
        transfer_amount = round(transfer_amount, 2)

        if transfer_amount <= 0.01:
            break

        total_amount = round(transfer_amount * unit_price, 2)

        results.append(MatchResult(
            from_enterprise_id=surplus.enterprise_id,
            from_enterprise_name=surplus.enterprise_name,
            to_enterprise_id=deficit.enterprise_id,
            to_enterprise_name=deficit.enterprise_name,
            credit_amount=transfer_amount,
            unit_price=unit_price,
            total_amount=total_amount
        ))

        surplus.credit_surplus = round(surplus.credit_surplus - transfer_amount, 2)
        deficit.credit_gap = round(deficit.credit_gap - transfer_amount, 2)

        if surplus.credit_surplus <= 0.01:
            surplus_idx += 1
        if deficit.credit_gap <= 0.01:
            deficit_idx += 1

    return results


@dataclass
class OrderMatchResult:
    sell_order_id: int
    buy_order_id: int
    sell_enterprise_id: int
    buy_enterprise_id: int
    sell_enterprise_name: str
    buy_enterprise_name: str
    credit_amount: float
    matched_price: float
    total_amount: float


def match_orders_with_price(
    sell_orders: List[dict],
    buy_orders: List[dict]
) -> List[OrderMatchResult]:
    """
    带价格的挂单撮合（价格优先、时间优先）：
    - 卖单按价格从低到高排序（先卖便宜的），同价位按挂单时间从早到晚（再按订单ID）
    - 买单按价格从高到低排序（先买贵的），同价位按挂单时间从早到晚（再按订单ID）
    - 当买单价格 >= 卖单价格时可以成交，成交价取两者均价

    时间优先保证早挂单的可售额度/资金先被消费，部分成交按此确定顺序分配，
    不会因并发请求的调度顺序不同而产生不同结果（排序是纯函数、确定性的）。
    """
    from datetime import datetime as _dt
    earliest = _dt.min

    sell_orders_sorted = sorted(
        [o for o in sell_orders if o["remaining_amount"] > 0.01],
        key=lambda x: (x["unit_price"], x.get("created_at", earliest), x["id"])
    )
    buy_orders_sorted = sorted(
        [o for o in buy_orders if o["remaining_amount"] > 0.01],
        key=lambda x: (-x["unit_price"], x.get("created_at", earliest), x["id"])
    )

    results = []
    sell_idx = 0
    buy_idx = 0

    while sell_idx < len(sell_orders_sorted) and buy_idx < len(buy_orders_sorted):
        sell_order = sell_orders_sorted[sell_idx]
        buy_order = buy_orders_sorted[buy_idx]

        if buy_order["unit_price"] < sell_order["unit_price"]:
            break

        match_amount = min(
            sell_order["remaining_amount"],
            buy_order["remaining_amount"]
        )
        match_amount = round(match_amount, 2)

        if match_amount <= 0.01:
            break

        matched_price = round((sell_order["unit_price"] + buy_order["unit_price"]) / 2, 2)
        total_amount = round(match_amount * matched_price, 2)

        results.append(OrderMatchResult(
            sell_order_id=sell_order["id"],
            buy_order_id=buy_order["id"],
            sell_enterprise_id=sell_order["enterprise_id"],
            buy_enterprise_id=buy_order["enterprise_id"],
            sell_enterprise_name=sell_order.get("enterprise_name", ""),
            buy_enterprise_name=buy_order.get("enterprise_name", ""),
            credit_amount=match_amount,
            matched_price=matched_price,
            total_amount=total_amount
        ))

        sell_order["remaining_amount"] = round(sell_order["remaining_amount"] - match_amount, 2)
        sell_order["filled_amount"] = round(sell_order["filled_amount"] + match_amount, 2)
        buy_order["remaining_amount"] = round(buy_order["remaining_amount"] - match_amount, 2)
        buy_order["filled_amount"] = round(buy_order["filled_amount"] + match_amount, 2)

        if sell_order["remaining_amount"] <= 0.01:
            sell_idx += 1
        if buy_order["remaining_amount"] <= 0.01:
            buy_idx += 1

    return results


def calculate_carryover_amount(
    original_surplus: float,
    years_before: int
) -> Tuple[float, float]:
    """
    计算跨年度结转金额
    规则：
    - 最多结转3年
    - 第1年结转80%，第2年结转60%，第3年结转40%
    - 返回(结转比例, 结转金额)
    """
    if years_before < 1 or years_before > CARRYOVER_MAX_YEARS:
        return 0.0, 0.0

    ratio = CARRYOVER_RATIO_BY_YEAR.get(years_before, 0.0)
    carryover_amount = round(original_surplus * ratio, 2)

    return ratio, carryover_amount


def calculate_chain_carryover_amount(
    original_surplus: float,
    year_diff: int
) -> Tuple[float, float]:
    """
    链式结转：通过逐年结转的方式计算最终结转金额和比例
    例如：2023→2024→2025，相当于2023→2025按2年计算
    """
    if year_diff < 1 or year_diff > CARRYOVER_MAX_YEARS:
        return 0.0, 0.0

    total_ratio = 1.0
    for y in range(1, year_diff + 1):
        year_ratio = CARRYOVER_RATIO_BY_YEAR.get(1, 0.0)
        total_ratio *= year_ratio

    total_ratio = round(total_ratio, 4)
    carryover_amount = round(original_surplus * total_ratio, 2)

    return total_ratio, carryover_amount


def validate_order_price(unit_price: float) -> Tuple[bool, Optional[str]]:
    """
    验证挂单价格是否在合理范围内
    """
    if unit_price < PRICE_FLOOR:
        return False, f"单价低于价格下限 {PRICE_FLOOR} 元/分"
    if unit_price > PRICE_CEILING:
        return False, f"单价高于价格上限 {PRICE_CEILING} 元/分"
    return True, None


@dataclass
class PredictionResult:
    total_positive: float
    total_negative: float
    net_credit: float
    compliance_rate: float
    model_details: List[dict]


def predict_next_year_credit(
    historical_data: List[dict],
    output_growth_rate: float = 0.05,
    pc_improvement_rate: float = 0.02
) -> PredictionResult:
    """
    基于历史数据预测下一年积分情况
    参数：
    - historical_data: 历年车型数据，包含model_code, power_consumption, annual_output, curb_weight等
    - output_growth_rate: 产量年增长率
    - pc_improvement_rate: 电耗年改善率
    """
    if not historical_data:
        return PredictionResult(0.0, 0.0, 0.0, 0.0, [])

    model_details = []
    total_positive = 0.0
    total_negative = 0.0
    compliant_count = 0

    latest_models = {}
    for record in historical_data:
        model_code = record.get("model_code")
        year = record.get("year", 0)
        if model_code not in latest_models or year > latest_models[model_code]["year"]:
            latest_models[model_code] = record

    for model_code, data in latest_models.items():
        current_pc = data.get("power_consumption", 0)
        current_output = data.get("annual_output", 0)
        curb_weight = data.get("curb_weight", 0)
        limit = calculate_power_consumption_limit(curb_weight)

        predicted_pc = round(current_pc * (1 - pc_improvement_rate), 2)
        predicted_output = max(1, int(current_output * (1 + output_growth_rate)))
        predicted_unit_credit = calculate_unit_credit(predicted_pc, limit)
        predicted_total_credit = calculate_total_credit(predicted_unit_credit, predicted_output)

        if predicted_pc <= limit:
            compliant_count += 1

        if predicted_total_credit > 0:
            total_positive += predicted_total_credit
        else:
            total_negative += predicted_total_credit

        model_details.append({
            "model_name": data.get("model_name", ""),
            "model_code": model_code,
            "curb_weight": curb_weight,
            "current_power_consumption": current_pc,
            "predicted_power_consumption": predicted_pc,
            "power_consumption_limit": limit,
            "predicted_output": predicted_output,
            "predicted_unit_credit": predicted_unit_credit,
            "predicted_total_credit": predicted_total_credit
        })

    total_models = len(latest_models)
    compliance_rate = (compliant_count / total_models * 100) if total_models > 0 else 0.0

    return PredictionResult(
        total_positive=round(total_positive, 2),
        total_negative=round(total_negative, 2),
        net_credit=round(total_positive + total_negative, 2),
        compliance_rate=round(compliance_rate, 2),
        model_details=model_details
    )


@dataclass
class EnterpriseCreditSummaryV2(EnterpriseCreditSummary):
    carryover_in: float = 0.0
    carryover_out: float = 0.0
    bought_credit: float = 0.0
    sold_credit: float = 0.0
    final_net_credit: float = 0.0
    final_credit_gap: float = 0.0
    final_credit_surplus: float = 0.0
    is_compliant: bool = True

