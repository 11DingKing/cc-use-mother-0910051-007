from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import Base, engine, SessionLocal
from .routers import enterprises, vehicle_models, credit_records, credit_transactions, statistics
from .routers import credit_market, credit_carryover, credit_prediction
from . import matching

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.APP_NAME,
    description="工信部双积分核算与电耗限值管理系统 - 用于管理新能源企业双积分核算、电耗限值标准、积分交易撮合等业务。积分市场支持成交前预授权（卖方积分批次/买方资金额度冻结）、幂等撮合任务、部分结果可恢复、撤单/授权过期/重启回放防重复成交。",
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc"
)


@app.on_event("startup")
def replay_incomplete_match_tasks():
    """服务重启后回放未完成撮合任务并清理过期授权：已成交项凭幂等键跳过，不重复成交"""
    db = SessionLocal()
    try:
        matching.resume_pending_tasks(db)
    finally:
        db.close()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(enterprises.router, prefix=settings.API_V1_PREFIX)
app.include_router(vehicle_models.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_records.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_transactions.router, prefix=settings.API_V1_PREFIX)
app.include_router(statistics.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_market.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_carryover.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_prediction.router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["root"])
def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.VERSION,
        "message": "欢迎使用双积分核算与电耗限值管理系统",
        "docs": "/docs",
        "api_prefix": settings.API_V1_PREFIX
    }


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "healthy"}
