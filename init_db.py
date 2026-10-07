import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base, engine, SessionLocal
from app import crud, schemas, init_data, matching
from app.models import CreditRecordStatus, OrderType, CreditTransaction


def init_database():
    print("=" * 80)
    print("双积分核算与电耗限值管理系统 - 数据库初始化（含多年度、交易市场、结转、预测）")
    print("=" * 80)

    print("\n[1/10] 创建数据库表结构...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("✓ 数据库表创建完成（新增：挂单、价格历史、结转、年度汇总表）")

    db = SessionLocal()

    print("\n[2/10] 初始化车企数据...")
    enterprise_schemas = init_data.get_enterprise_schemas()
    enterprise_ids = []
    for ent_schema in enterprise_schemas:
        db_ent = crud.create_enterprise(db, ent_schema)
        enterprise_ids.append(db_ent.id)
        print(f"  ✓ {db_ent.name} (ID: {db_ent.id})")
    print(f"✓ 共创建 {len(enterprise_ids)} 家车企")

    print("\n[3/10] 初始化多年度车型数据（2022-2025）...")
    vehicle_schemas = init_data.get_vehicle_model_schemas(enterprise_ids)
    year_model_count = {}
    for model_schema in vehicle_schemas:
        db_model = crud.create_vehicle_model(db, model_schema)
        year = db_model.production_year
        if year not in year_model_count:
            year_model_count[year] = 0
        year_model_count[year] += 1
        status = "⚠" if db_model.is_suspected_weight_manipulation else "✓"
        if year == 2025:
            print(f"  {status} {db_model.model_name} ({year}) - 重量:{db_model.curb_weight}kg "
                  f"电耗:{db_model.power_consumption}kWh/100km "
                  f"产量:{db_model.annual_output:,}辆")
    for y, cnt in sorted(year_model_count.items()):
        print(f"  {y}年: {cnt} 个车型")
    print(f"✓ 共创建 {len(vehicle_schemas)} 个车型（2022-2025共{len(year_model_count)}个年度）")

    years = [2022, 2023, 2024, 2025]
    for year in years:
        print(f"\n[4/10] 执行{year}年度积分核算...")
        credit_records = crud.batch_calculate_credits(db, year=year)

        positive_count = sum(1 for r in credit_records if r.total_credit > 0)
        negative_count = sum(1 for r in credit_records if r.total_credit < 0)
        total_positive = sum(r.total_credit for r in credit_records if r.total_credit > 0)
        total_negative = sum(r.total_credit for r in credit_records if r.total_credit < 0)

        print(f"  ✓ {year}年核算完成，共生成 {len(credit_records)} 条积分记录")
        print(f"    正积分车型: {positive_count} 个, 正积分总量: {total_positive:,.2f} 分")
        print(f"    负积分车型: {negative_count} 个, 负积分总量: {total_negative:,.2f} 分")
        print(f"    净积分: {total_positive + total_negative:,.2f} 分")

        print(f"  积分记录状态流转（核算→公示→确认）...")
        count = crud.batch_update_credit_records_status(db, year=year, status=CreditRecordStatus.PUBLICIZED)
        count = crud.batch_update_credit_records_status(db, year=year, status=CreditRecordStatus.CONFIRMED)
        print(f"  ✓ {count} 条记录状态更新为 '已确认'")

        for ent_id in enterprise_ids:
            crud.update_annual_summary_with_transactions(db, ent_id, year)

    print("\n[5/10] 执行跨年度积分结转（2022→2023, 2023→2024, 2024→2025）...")
    for from_y, to_y in [(2022, 2023), (2023, 2024), (2024, 2025)]:
        carryovers = crud.execute_yearly_carryover(db, from_year=from_y, to_year=to_y)
        if carryovers:
            total_carryover = sum(c.carryover_amount for c in carryovers)
            print(f"  ✓ {from_y}→{to_y}: 完成 {len(carryovers)} 笔结转, 共计 {total_carryover:,.2f} 分")
            for c in carryovers:
                print(f"    - {c.enterprise.name}: {c.original_amount:,.2f} × {int(c.carryover_ratio*100)}% = {c.carryover_amount:,.2f} 分")
        else:
            print(f"  ✓ {from_y}→{to_y}: 无可结转积分钟余")

    for year in years:
        for ent_id in enterprise_ids:
            crud.update_annual_summary_with_transactions(db, ent_id, year)

    print("\n[6/10] 创建2025年度积分交易市场挂单（成交前预授权：买单先授信、挂单即冻结）...")
    initial_orders = init_data.get_initial_market_orders(enterprise_ids, year=2025)
    created_orders = []
    for order_data in initial_orders:
        order_type = OrderType.SELL if order_data["order_type"] == "sell" else OrderType.BUY
        if order_type == OrderType.BUY:
            # 买方资金账户授信：按挂单量×报价足额授信，挂单时冻结
            credit_limit = round(order_data["total_amount"] * order_data["unit_price"] * 1.2, 2)
            matching.deposit_fund(db, order_data["enterprise_id"], order_data["year"], credit_limit)
        order_create = schemas.CreditOrderCreate(
            enterprise_id=order_data["enterprise_id"],
            year=order_data["year"],
            order_type=order_type,
            unit_price=order_data["unit_price"],
            total_amount=order_data["total_amount"],
            remark=order_data["remark"]
        )
        try:
            db_order, auth_err = matching.create_order_with_auth(db, order_create)
            if auth_err:
                print(f"  ✗ 创建挂单失败: {auth_err}")
                continue
            created_orders.append(db_order)
            order_type_str = "卖出" if order_type == OrderType.SELL else "买入"
            print(f"  ✓ {db_order.enterprise.name} {order_type_str}: "
                  f"{db_order.total_amount:,.0f} 分 @ {db_order.unit_price:,.0f} 元/分 "
                  f"(订单号: {db_order.order_no}，已预授权冻结)")
        except ValueError as e:
            print(f"  ✗ 创建挂单失败: {e}")
    print(f"✓ 共创建 {len(created_orders)} 个初始挂单（均带有效预授权）")

    print("\n[7/10] 执行挂单撮合交易（只消费有效授权，价格优先/时间优先）...")
    task, existing, match_err = matching.run_auto_match(db, year=2025)
    if match_err and task is None:
        print(f"  ✓ {match_err}")
        matched_orders = []
        transactions = []
        remaining_gap, remaining_surplus = 0.0, 0.0
    else:
        target = task or existing
        view = matching.task_view(target)
        transactions = [
            db.get(CreditTransaction, it["transaction_id"])
            for it in view["items"] if it["status"] == "succeeded" and it["transaction_id"]
        ]
        matched_orders = view["items"]

        if transactions:
            total_amount = sum(t.total_amount for t in transactions if t.total_amount)
            print(f"  ✓ 任务 {target.task_no} 状态={target.status.value}，完成 {len(transactions)} 笔撮合且已清算")
            for i, info in enumerate(matched_orders, 1):
                if info["status"] != "succeeded":
                    continue
                print(
                    f"    卖单{info['sell_order_id']} → 买单{info['buy_order_id']}: "
                    f"{info['credit_amount']:,.2f} 分 @ {info['matched_price']:,.0f} 元/分, "
                    f"金额: {info['total_amount']:,.0f} 元 [{info['status']}]"
                )
            print(f"  交易总金额: {total_amount:,.0f} 元")
        else:
            print("  ✓ 无落账成交")

        remain_sell = sum(
            matching.order_quantity_view(db, o.id)["auth"]["available_amount"]
            for o in created_orders if o.order_type == OrderType.SELL
        )
        remain_buy = sum(
            matching.order_quantity_view(db, o.id)["auth"]["available_amount"]
            for o in created_orders if o.order_type == OrderType.BUY
        )
        remaining_gap, remaining_surplus = round(remain_buy, 2), round(remain_sell, 2)

    print(f"  剩余待买授权量: {remaining_gap:,.2f} 分")
    print(f"  剩余待卖授权量: {remaining_surplus:,.2f} 分")

    for year in years:
        for ent_id in enterprise_ids:
            crud.update_annual_summary_with_transactions(db, ent_id, year)

    print(f"\n[8/10] 执行2025年度传统撮合（作为补充）...")
    transactions2, remaining_gap2, remaining_surplus2 = crud.match_and_execute_transactions(
        db, year=2025, unit_price=3000.0
    )

    if transactions2:
        total_amount = sum(t.total_amount for t in transactions2 if t.total_amount)
        print(f"  ✓ 完成 {len(transactions2)} 笔传统撮合交易")
        for i, txn in enumerate(transactions2, 1):
            print(f"    {i}. {txn.from_enterprise.name} → {txn.to_enterprise.name}: "
                  f"{txn.credit_amount:,.2f} 分 @ {txn.unit_price:,.0f} 元/分, "
                  f"金额: {txn.total_amount:,.0f} 元")
        print(f"  交易总金额: {total_amount:,.0f} 元")
    else:
        print("  ✓ 无需要撮合的交易，所有企业均达标")

    for year in years:
        for ent_id in enterprise_ids:
            crud.update_annual_summary_with_transactions(db, ent_id, year)

    print("\n[9/10] 生成2026年度积分预测...")
    predictions = crud.predict_all_enterprises_credit(
        db, target_year=2026, output_growth_rate=0.05, pc_improvement_rate=0.02
    )
    total_predicted_positive = sum(p.predicted_total_positive for p in predictions)
    total_predicted_negative = sum(p.predicted_total_negative for p in predictions)
    total_predicted_net = sum(p.predicted_net_credit for p in predictions)
    compliant_count = sum(1 for p in predictions if p.predicted_net_credit >= 0)
    print(f"  ✓ 2026年预测: {len(predictions)} 家企业")
    print(f"    预测正积分: {total_predicted_positive:,.2f} 分")
    print(f"    预测负积分: {total_predicted_negative:,.2f} 分")
    print(f"    预测净积分: {total_predicted_net:,.2f} 分")
    print(f"    预测达标企业: {compliant_count} / {len(predictions)} 家")
    for p in predictions:
        status = "✓ 预测达标" if p.predicted_net_credit >= 0 else "✗ 预测缺口"
        print(f"    {p.enterprise_name:15s} | 净积分:{p.predicted_net_credit:>12,.2f} | "
              f"达标率:{p.predicted_compliance_rate:>6.1f}% | {status}")

    print("\n[10/10] 生成价格走势记录...")
    price_trend = crud.get_price_trend(db, year=2025)
    print(f"  ✓ 2025年交易价格统计:")
    print(f"    交易次数: {price_trend.trade_count} 次")
    print(f"    平均价格: {price_trend.avg_price:,.0f} 元/分")
    print(f"    价格区间: {price_trend.min_price:,.0f} - {price_trend.max_price:,.0f} 元/分")
    print(f"    总成交量: {price_trend.total_volume:,.2f} 分")
    print(f"    总成交金额: {price_trend.total_value:,.0f} 元")

    print("\n" + "=" * 80)
    print("✓ 数据库初始化完成！")
    print("=" * 80)

    print("\n各企业2025年度积分汇总（含结转、交易）:")
    print("-" * 120)
    summaries = crud.calculate_all_enterprise_summaries_v2(db, year=2025)
    print(f"{'企业名称':15s} | {'当年正积分':>12s} | {'当年负积分':>12s} | {'结转流入':>10s} | "
          f"{'买入':>10s} | {'卖出':>10s} | {'最终净积分':>12s} | {'最终缺口':>10s} | {'状态':>8s}")
    print("-" * 120)
    for s in summaries:
        status = "✓ 达标" if s.is_compliant else "✗ 缺口"
        print(f"{s.enterprise_name:15s} | {s.total_positive_credit:>12,.2f} | {s.total_negative_credit:>12,.2f} | "
              f"{s.carryover_in:>10,.2f} | {s.bought_credit:>10,.2f} | {s.sold_credit:>10,.2f} | "
              f"{s.final_net_credit:>12,.2f} | {s.final_credit_gap:>10,.2f} | {status:>8s}")

    print("-" * 120)

    print("\n各企业多年度汇总（2022-2025）:")
    print("-" * 100)
    for ent_id in enterprise_ids:
        multi_year = crud.get_multi_year_summary(db, ent_id, 2022, 2025)
        print(f"\n{multi_year.enterprise_name}:")
        print(f"  累计结转流入: {multi_year.total_carryover_in:,.2f} 分")
        print(f"  累计结转流出: {multi_year.total_carryover_out:,.2f} 分")
        print(f"  累计买入积分: {multi_year.total_bought:,.2f} 分")
        print(f"  累计卖出积分: {multi_year.total_sold:,.2f} 分")
        for year_data in multi_year.annual_summaries:
            status = "✓" if year_data["is_compliant"] else "✗"
            print(f"  {year_data['year']}年: 净{year_data['net_credit']:+,.2f} → "
                  f"最终{year_data['final_net_credit']:+,.2f} {status}")

    suspicious = crud.get_suspicious_weight_models(db)
    if suspicious:
        print(f"\n⚠  发现 {len(suspicious)} 个疑似堆重量放宽限值的车型:")
        for m in suspicious:
            print(f"  - {m.enterprise.name} {m.model_name} (重量:{m.curb_weight}kg, 年份:{m.production_year})")

    market_overview = crud.get_market_overview(db, year=2025)
    print(f"\n📈 2025年积分交易市场概览:")
    print(f"  卖单总量: {market_overview.total_sell_volume:,.2f} 分 ({market_overview.total_sell_orders} 单)")
    print(f"  买单总量: {market_overview.total_buy_volume:,.2f} 分 ({market_overview.total_buy_orders} 单)")
    print(f"  平均卖价: {market_overview.avg_sell_price:,.0f} 元/分 (范围:{market_overview.min_sell_price:,.0f}-{market_overview.max_sell_price:,.0f})")
    print(f"  平均买价: {market_overview.avg_buy_price:,.0f} 元/分 (范围:{market_overview.min_buy_price:,.0f}-{market_overview.max_buy_price:,.0f})")
    print(f"  已成交: {market_overview.matched_volume:,.2f} 分, 金额: {market_overview.matched_value:,.0f} 元")

    db.close()
    print("\n" + "=" * 80)
    print("启动命令: uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")
    print("API文档: http://localhost:8000/docs")
    print("=" * 80)
    print("\n新增核心API接口:")
    print("  积分交易市场:")
    print("    POST /api/v1/credit-market/orders          - 创建挂单")
    print("    GET  /api/v1/credit-market/orders          - 查询挂单列表")
    print("    POST /api/v1/credit-market/match           - 指定挂单撮合")
    print("    POST /api/v1/credit-market/match-all/{year} - 撮合所有挂单")
    print("    GET  /api/v1/credit-market/price-trend/{year} - 价格走势")
    print("    GET  /api/v1/credit-market/overview/{year} - 市场概览")
    print("  跨年度结转:")
    print("    POST /api/v1/credit-carryover/execute/{from}/{to} - 执行年度结转")
    print("    GET  /api/v1/credit-carryover/enterprise-credits-v2/{year} - 企业汇总V2")
    print("    GET  /api/v1/credit-carryover/enterprise-multi-year/{id} - 多年度汇总")
    print("  积分预测:")
    print("    GET  /api/v1/credit-prediction/all/{year}  - 全企业预测")
    print("    GET  /api/v1/credit-prediction/summary/{year} - 预测汇总")
    print("    GET  /api/v1/credit-prediction/comparison/{id}/{start}/{end} - 历史vs预测")
    print("=" * 80)


if __name__ == "__main__":
    init_database()
