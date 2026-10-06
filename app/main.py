from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import threading
import time

from .config import settings
from .database import Base, engine
from .routers import enterprises, vehicle_models, credit_records, credit_transactions, statistics
from .routers import credit_market, credit_carryover, credit_prediction
from . import matching

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.APP_NAME,
    description="工信部双积分核算与电耗限值管理系统 - 用于管理新能源汽车企业双积分核算、电耗限值标准、积分交易撮合等业务。新增功能：积分交易市场（挂单交易、价格走势）、跨年度结转、积分预测。",
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc"
)

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


_EXPIRE_SCAN_INTERVAL_SECONDS = 60


def _background_market_recovery() -> None:
    """
    进程级后台恢复（SQLite 单实例部署）：
    - 周期认领任务箱：服务重启后清算未完成的执行计划（部分结果可恢复）；
    - 周期排入授权过期扫描：held 授权到期自动释放，挂单不会永久占用额度。
    所有操作均走数据库写锁，与在线请求串行，回放本身幂等。
    """
    while True:
        try:
            with matching.locked_session(matching.SessionLocal) as db:
                matching.enqueue_expiration_scan(db)
            matching.replay_pending_tasks(batch_limit=100)
        except Exception:
            # 守护线程不得因单次异常退出；下一个周期继续重试
            pass
        time.sleep(_EXPIRE_SCAN_INTERVAL_SECONDS)


@app.on_event("startup")
def _startup_replay_pending_tasks() -> None:
    # 先同步回放一次，尽量在对外服务前恢复宕机前的部分结果
    try:
        matching.replay_pending_tasks(batch_limit=200)
    except Exception:
        pass
    thread = threading.Thread(target=_background_market_recovery, name="market-recovery", daemon=True)
    thread.start()


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
