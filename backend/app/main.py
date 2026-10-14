from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from app.api.routes import router
from app.config import settings

# FastAPI runs each request in several steps (check the login, run the endpoint, clean
# up), each on whichever of its 40 worker threads is free. When a connection was taken
# in one step and returned in a later one, a request held it while waiting for a free
# thread, while every thread waited for a connection: a deadlock that froze the whole
# API in the load test. So every endpoint takes and returns its connection inside one
# step (`with pool.connection()` in its body), and there is one connection per thread,
# so a thread never waits for one.
WORKER_THREADS = 40


@asynccontextmanager
async def lifespan(app: FastAPI):
    with ConnectionPool(settings.database_url, min_size=4, max_size=WORKER_THREADS) as pool:
        app.state.pool = pool
        app.state.redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        yield
        app.state.redis.close()


app = FastAPI(title="Ticket booking", lifespan=lifespan)
app.include_router(router)
