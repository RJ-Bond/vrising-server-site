from slowapi import Limiter
from slowapi.util import get_remote_address

# default_limits only takes effect once SlowAPIMiddleware is registered (see main.py) —
# it's what protects every route that has no @limiter.limit of its own (previously none,
# since default_limits was empty and the middleware wasn't added either — most routers
# had zero rate-limiting at all). 200/minute per IP is generous for a real visitor,
# tight enough to blunt basic scripted hammering; routes with their own tighter
# @limiter.limit (auth, points spend, server restart) still take precedence.
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
