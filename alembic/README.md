# Alembic migrations

Async-SQLAlchemy setup (see `alembic/env.py` — it bridges the async engine to
Alembic's sync migration context via `connection.run_sync(...)`, the standard
SQLAlchemy 2.0 recipe). `target_metadata` is `backend.models.Base.metadata`,
and the DB URL comes from the same `DATABASE_URL` env var + default that
`backend/database.py` uses (the `sqlalchemy.url` in `alembic.ini` is just a
placeholder, overridden at runtime).

## Generate a new migration

After changing a model in `backend/models.py`:

```
uv run --python 3.12 --with-requirements requirements.txt alembic revision --autogenerate -m "describe the change"
```

Then **read the generated file in `alembic/versions/`** before trusting it —
autogenerate can miss things (server-side defaults vs. Python-side defaults,
some constraint renames, etc.), so cross-check `op.create_table`/`op.add_column`
calls against the model class you changed.

## Apply migrations

```
uv run --python 3.12 --with-requirements requirements.txt alembic upgrade head
```

(Set `DATABASE_URL` first if you're not using the default
`sqlite+aiosqlite:////data/vrising.db` — e.g. point it at a temp file to try
a migration safely before running it against the real DB.)

## Current status — not yet wired into app startup

`backend/main.py`'s `lifespan()` still calls
`await conn.run_sync(Base.metadata.create_all)` on every startup, which is
how the schema has always been bootstrapped for fresh installs (and is what
the initial migration in this directory was generated to match exactly).
That startup hook has **not** been changed or wired together with Alembic
yet — this is intentional and being handled separately, since it needs a
plan for the production DB (which already exists and predates Alembic) that
`create_all()` alone can't safely provide going forward.
