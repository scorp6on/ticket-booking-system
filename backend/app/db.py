from fastapi import Request


def get_conn(request: Request):
    # The pool is created when the app starts (see main.py); reuse its open
    # connections instead of connecting on every request.
    with request.app.state.pool.connection() as conn:
        yield conn


def get_redis(request: Request):
    return request.app.state.redis
