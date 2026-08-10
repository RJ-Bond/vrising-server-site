"""Lightweight load test for the highest-traffic realistic paths on this site:
the homepage stats widget, the leaderboard, and login. Manual/on-demand tool —
NOT wired into CI (load tests belong in a terminal you're watching, not a push
hook).

Setup (once):
    uv run --python 3.12 --with-requirements requirements-dev.txt pip show locust \
        >/dev/null 2>&1 || uv pip install -r requirements-dev.txt
  (locust is in requirements-dev.txt; `uv run --with-requirements requirements-dev.txt`
  picks it up automatically — see the run command below, no separate install step
  needed if you're already using uv the way scripts/test_backend.sh does.)

Run against a local dev server (NEVER point this at the production site — it's
a stress test, not a health check):
    1. Start the backend locally, e.g.:
         DATABASE_URL="sqlite+aiosqlite:////tmp/loadtest.db" \
           uv run --python 3.12 --with-requirements requirements.txt \
           uvicorn backend.main:app --port 8000
       A throwaway DB is deliberate — this script's login task can register
       scratch accounts (see LOAD_TEST_SEED_USERS below) and you don't want
       those landing in a DB you care about.
    2. In another terminal, from the repo root:
         uv run --python 3.12 --with-requirements requirements-dev.txt \
           locust -f loadtest/locustfile.py --host http://127.0.0.1:8000
       Then open http://127.0.0.1:8089 for the Locust web UI (pick user count
       and spawn rate there), or add --headless -u 50 -r 5 -t 2m for a
       scripted run with no browser needed.

Rate limiting — read before cranking up user count:
    POST /api/auth/login is limited to 10/minute PER SOURCE IP
    (backend/rate_limit.py: key_func=get_remote_address). Every simulated
    Locust user shares one real IP (yours), so this limit applies to the
    *whole* load test's login traffic combined, not per simulated user. This
    is intentional brute-force protection, not a bug to route around — the
    LoginUser class below is deliberately low-weight with a long wait_time so
    it stays well under that ceiling even at moderate --user counts, and a
    burst of 429s from it under heavy load is the correct, expected result,
    not a failure of the app. Don't "fix" this by hammering login harder.

Seeding login credentials:
    On first run, LoginUser.on_start registers one throwaway account per
    simulated login-user (POST /api/auth/register) and reuses it for
    subsequent logins in that run. This is why a scratch DB matters (see
    step 1) — a real run will leave `loadtest0`, `loadtest1`, ... accounts
    behind. Set LOAD_TEST_USERNAME/LOAD_TEST_PASSWORD env vars instead to log
    into one pre-existing account repeatedly if you'd rather not register new
    ones (e.g. testing against a DB you don't want to write scratch users
    into).
"""
import os
import random
import string

from locust import HttpUser, task, between


def _random_suffix(n=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class BrowsingUser(HttpUser):
    """Anonymous visitor: homepage stats + leaderboard, the two endpoints named
    in the task brief plus the monitor widget every page pulls in alongside them
    (same "highest-traffic realistic path" as the other two — homepage-stats and
    monitor/stats render side-by-side in the same widget, see index.js loadStats()).
    Weighted heavily relative to LoginUser below since this is the traffic
    profile of most visits: browse without ever logging in.
    """
    weight = 20
    wait_time = between(2, 6)  # roughly matches a human reading a page between clicks

    @task(5)
    def homepage_stats(self):
        self.client.get("/api/homepage-stats", name="/api/homepage-stats")

    @task(4)
    def leaderboard(self):
        # server=1/period=all/page=1 mirrors leaderboard.html's default view (the
        # one every direct visit to that page loads first, before any filter click).
        self.client.get(
            "/api/leaderboard?server=1&period=all&page=1&per_page=20",
            name="/api/leaderboard",
        )

    @task(3)
    def monitor_stats(self):
        self.client.get("/api/monitor/stats?server=1", name="/api/monitor/stats")


class LoginUser(HttpUser):
    """A much smaller slice of traffic that actually authenticates. Kept low-weight
    and slow (see module docstring's rate-limiting section) so it can't blow past
    POST /api/auth/login's 10/minute-per-IP limit on its own."""
    weight = 1
    wait_time = between(20, 40)

    def on_start(self):
        env_user = os.environ.get("LOAD_TEST_USERNAME")
        env_pw = os.environ.get("LOAD_TEST_PASSWORD")
        if env_user and env_pw:
            self.username, self.password = env_user, env_pw
            return
        # No pre-existing account given — register a throwaway one. Distinct
        # username per simulated user (not shared across them) so login attempts
        # don't collide with each other's sessions/cookies.
        self.username = f"loadtest_{_random_suffix()}"
        self.password = "LoadTest123!"
        self.client.post(
            "/api/auth/register",
            json={
                "username": self.username,
                # example.com (RFC 2606 reserved-for-documentation), not the also-reserved
                # .invalid TLD — pydantic's EmailStr (email-validator) explicitly rejects
                # special-use TLDs like .invalid as "not a valid email address", even
                # though RFC 2606 reserves it for exactly this kind of test data.
                "email": f"{self.username}@example.com",
                "password": self.password,
            },
            name="/api/auth/register (setup)",
        )

    @task
    def login(self):
        with self.client.post(
            "/api/auth/login",
            json={"username": self.username, "password": self.password},
            name="/api/auth/login",
            catch_response=True,
        ) as resp:
            # 429 here is the rate limiter doing its job (see module docstring) —
            # don't count it as a load-test failure, just note it and move on.
            if resp.status_code == 429:
                resp.success()
            elif resp.status_code != 200:
                resp.failure(f"login failed: {resp.status_code} {resp.text[:200]}")
