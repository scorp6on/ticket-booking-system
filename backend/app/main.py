from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from app.api.routes import router
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    with ConnectionPool(settings.database_url) as pool:
        app.state.pool = pool
        app.state.redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        yield
        app.state.redis.close()


app = FastAPI(title="Ticket booking", lifespan=lifespan)
app.include_router(router)
