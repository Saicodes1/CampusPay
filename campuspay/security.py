import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from redis.exceptions import RedisError

from campuspay.errors import Problem, require

basic = HTTPBasic(auto_error=False)
USERS = {"alice": "STUDENT", "bob": "STUDENT", "cafe": "MERCHANT", "books": "MERCHANT", "admin": "ADMIN"}
RATE_SCRIPT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 65) end
return n
"""


@dataclass(frozen=True)
class Actor:
    name: str
    role: str


def limit(request: Request, subject: str, maximum: int):
    try:
        n = request.app.state.redis.eval(RATE_SCRIPT, 1, f"cp:rate:{subject}:{int(time.time()) // 60}")
    except RedisError as exc:
        raise Problem(503, "RATE_LIMIT_SERVICE_UNAVAILABLE") from exc
    require(n <= maximum, "RATE_LIMITED", 429)


def authenticate(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)) -> Actor:
    if not credentials:
        raise Problem(401, "AUTHENTICATION_REQUIRED")
    # Also throttle invalid credentials. Proxy headers are not trusted for this IP limit.
    ip = request.client.host if request.client else "local"
    limit(request, f"login:{ip}", request.app.state.settings.auth_rate_limit)
    valid = secrets.compare_digest(
        credentials.password.encode(), request.app.state.settings.demo_password.encode()
    )
    if credentials.username not in USERS or not valid:
        raise Problem(401, "INVALID_CREDENTIALS")
    limit(request, f"user:{credentials.username}", request.app.state.settings.rate_limit)
    return Actor(credentials.username, USERS[credentials.username])


def role(required: str):
    def check(actor: Actor = Depends(authenticate)):
        require(actor.role == required, "FORBIDDEN", 403)
        return actor

    return check
