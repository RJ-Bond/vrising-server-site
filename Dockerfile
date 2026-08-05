FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY VERSION ./
# Needed at container startup: backend/db_migrate.py shells out to the `alembic` CLI
# (cwd=/app — see its REPO_ROOT) to bring the schema up to date before the app starts
# serving (see lifespan()/_run_db_migrations() in backend/main.py). alembic.ini's
# script_location=alembic resolves relative to this same /app WORKDIR.
COPY alembic.ini ./
COPY alembic/ ./alembic/

EXPOSE 8000

# --proxy-headers is on by default, but its trust list defaults to 127.0.0.1 only.
# nginx reaches us over the private compose network (vrising_net) with a docker-assigned
# IP, not loopback, so without --forwarded-allow-ips uvicorn ignores X-Forwarded-For/
# X-Real-IP entirely and every request looks like it comes from nginx's own IP — which
# breaks per-client rate limiting (slowapi) and IP-based dedup. Trusting all peers here is
# safe: the container publishes no host ports (see docker-compose.yml), so nginx is the
# only thing that can ever connect to this process, and nginx itself replaces (never
# appends to) X-Forwarded-For before proxying, so the header can't be spoofed by a client.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--reload", "--reload-dir", "/app/backend"]
