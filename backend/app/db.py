from fastapi import Request
from psycopg_pool import ConnectionPool


def get_pool(request: Request) -> ConnectionPool:
    # The pool is created when the app starts (see main.py). Endpoints take a connection
    # and give it back inside their own body, in one step: see the note in main.py.
    return request.app.state.pool


def get_redis(request: Request):
    return request.app.state.redis
