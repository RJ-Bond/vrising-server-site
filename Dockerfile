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
#
# No --reload: docker-compose.yml still bind-mounts ./backend into this container (so
# the admin panel's self-update / a manual `git pull` on the host delivers new files
# without an image rebuild), but this process no longer watches that directory and
# restarts itself the instant a file's mtime changes. It used to — combined with the
# bind mount, that meant install.sh's copy_project_files() (which cp's new backend
# files into the live, bind-mounted directory) could trigger a self-restart mid-deploy,
# before nginx was reloaded or backend/db_migrate.py's migration step ran, serving new
# code against the still-old schema for however long that race won. `docker compose up
# -d --build` (install.sh's own deploy step) already recreates this container in the
# correct order relative to the migration step — that's the intended way this process
# picks up new code, not a file-watcher racing the rest of the deploy.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
