# Reward Decision App

A high-performance, async FastAPI microservice that evaluates and returns a reward decision for a given payment transaction. The engine is fully policy-driven, deterministic, and designed to sustain **~300 req/s** locally.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Project Structure](#project-structure)
- [Assumptions](#assumptions)
- [Setup](#setup)
- [Running the App](#running-the-app)
- [API Reference](#api-reference)
- [Policy Configuration](#policy-configuration)
- [Running Tests (pytest)](#running-tests-pytest)
- [Load Testing (Locust)](#load-testing-locust)

---

## Architecture Overview

```
Client
  │
  ▼
POST /reward/decide
  │
  ├─► IdempotencyService     (check cache → return early if duplicate txn)
  │
  ├─► DecisionEngine         (deterministic reward calculation)
  │       ├─ PersonaService  (classify user: NEW / RETURNING / POWER)
  │       ├─ Policy (config/policy.json, hot-reloaded every 10s)
  │       └─ CAC cap check   (daily budget per persona via Redis)
  │
  ├─► HTTP Response returned immediately
  │
  └─► BackgroundTasks (async, after response sent)
          ├─ Store idempotency key in Redis
          ├─ Update last-reward timestamp
          └─ Increment CAC spend (if reward_value > 0)
```

**Cache layer:** Redis (default) or an in-process `MemoryCacheClient` if `REDIS_HOST` is unset.  
**Policy reload:** background `asyncio.Task` re-reads `policy.json` every `POLICY_REFRESH_SECONDS` (default 10 s) without blocking the event loop.

---

## Project Structure

```
Reward_Decision_App/
├── app/
│   ├── api/
│   │   ├── dependencies.py   # FastAPI dependency injectors
│   │   ├── models.py         # Pydantic request / response models
│   │   └── routes.py         # API endpoints
│   ├── core/
│   │   └── config.py         # Settings + async policy reload loop
│   ├── db/
│   │   └── cache.py          # Redis & in-memory cache clients
│   ├── services/
│   │   ├── decision_engine.py # Core reward calculation logic
│   │   ├── idempotency.py    # Idempotency key management
│   │   └── persona.py        # User persona classification
│   └── main.py               # FastAPI app + lifespan (startup/shutdown)
├── config/
│   └── policy.json           # Reward policy rules (hot-reloadable)
├── tests/
│   ├── conftest.py           # Shared fixtures (FakeCacheClient, BASE_POLICY)
│   ├── locustfile.py         # Locust load test
│   ├── test_cac_cap.py       # CAC cap enforcement tests
│   ├── test_decision_logic.py # Core reward logic tests
│   └── test_idempotency.py   # Idempotency key tests
├── conftest.py               # Root conftest (asyncio mode)
├── pytest.ini                # pytest settings
└── requirements.txt
```

---

## Assumptions

| # | Assumption |
|---|------------|
| 1 | **Persona is request-time derived** — user tenure / frequency is inferred from Redis state (e.g. first-seen = NEW, high spend = POWER). No external user DB is queried. |
| 2 | **No persistent storage** — all state (idempotency keys, CAC buckets, last-reward timestamps) lives in Redis and is ephemeral. A Redis flush resets all state. |
| 3 | **CAC is a daily rolling window** — the daily spend counter per user resets at midnight UTC (Redis TTL aligned to end-of-day). |
| 4 | **Cooldown is per-user** — a user cannot receive a reward within `cooldown_minutes` of their last reward. Cooldown is skipped if `feature_flags.cooldown_enabled = false`. |
| 5 | **`force_xp_mode` overrides reward type selection** — when `true` in policy, all eligible transactions receive XP only. |
| 6 | **Reward type is policy-weighted, not random** — `reward_types` weights in `policy.json` drive a deterministic selection based on transaction attributes (not `random.choice`). |
| 7 | **Idempotency is composite-keyed** — a duplicate is defined as the same `(txn_id, user_id, merchant_id)` triple within the `idempotency_ttl` window (default 24 h). |
| 8 | **`REDIS_HOST` defaults to `localhost`** — if Redis is not reachable, the app falls back to an in-process `MemoryCacheClient` (suitable for single-worker dev only). |
| 9 | **Multi-worker safe** — idempotency and CAC use atomic Redis operations (`INCR`, pipeline) so concurrent workers produce correct results. |
| 10 | **Windows runs asyncio loop; Linux/WSL2 can use uvloop** for extra throughput (see startup commands below). |

---

## Setup

### Prerequisites

- Python 3.11+
- Redis 7+ (running locally on default port `6379`)

### Install dependencies

```bash
# Create and activate a virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate

# Install packages
pip install -r requirements.txt
```

### Environment variables (optional)

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `localhost` | Redis host (set empty to use in-memory cache) |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis database index |
| `POLICY_PATH` | `config/policy.json` | Path to the policy file |
| `POLICY_REFRESH_SECONDS` | `10` | Hot-reload interval for policy changes |

---

## Running the App

Run all commands from the **project root** (`Reward_Decision_App/`).

### Development (single worker)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Production / Performance (multi-worker)

```bash
# Windows (asyncio loop)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log

# Linux / WSL2 (uvloop — install first: pip install uvloop)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --loop uvloop --no-access-log
```

### Verify it's running

```bash
curl http://localhost:8000/health
# → {"status":"ok"}
```

---

## API Reference

### `GET /health`

Health check.

**Response**
```json
{"status": "ok"}
```

---

### `POST /reward/decide`

Evaluate a transaction and return a reward decision.

**Request body**

```json
{
  "txn_id":     "txn_abc123",
  "user_id":    "user_42",
  "merchant_id": "m_7",
  "amount":     1500.00,
  "txn_type":   "UPI",
  "ts":         "2026-02-23T00:00:00Z"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `txn_id` | string | ✅ | Unique transaction ID (used for idempotency) |
| `user_id` | string | ✅ | User identifier |
| `merchant_id` | string | ✅ | Merchant identifier |
| `amount` | float | ✅ | Transaction amount in INR |
| `txn_type` | string | ✅ | One of `UPI`, `PURCHASE`, `PAYMENT` |
| `ts` | ISO 8601 datetime | ✅ | Transaction timestamp |

**Response**

```json
{
  "txn_id":       "txn_abc123",
  "user_id":      "user_42",
  "reward_type":  "XP",
  "reward_value": 75.0,
  "persona":      "NEW",
  "reason":       ["XP_EARNED", "PERSONA_BONUS"],
  "policy_version": "v1.0"
}
```

**Reason codes**

| Code | Meaning |
|---|---|
| `XP_EARNED` | Base XP reward granted |
| `PERSONA_BONUS` | Persona multiplier applied |
| `CHECKOUT_REWARD` | Checkout cashback granted |
| `GOLD_REWARD` | Gold reward granted |
| `PREFER_XP_MODE` | `force_xp_mode` flag overrode type selection |
| `MAX_XP_CAPPED` | XP capped at `max_xp_per_txn` |
| `CAC_CAP_EXCEEDED` | Daily spend limit reached — reward zeroed |
| `COOLDOWN_ACTIVE` | User rewarded too recently — reward zeroed |

---

## Policy Configuration

Edit `config/policy.json` to change reward rules **without restarting** the server (changes are picked up within `POLICY_REFRESH_SECONDS`).

```jsonc
{
  "policy_version": "v1.0",
  "idempotency_ttl": "86400",        // idempotency window in seconds (24 h)

  "reward_types": {                  // selection weights
    "XP": 0.5,
    "CHECKOUT": 0.3,
    "GOLD": 0.2
  },

  "xp": {
    "xp_per_rupee": 0.1,            // XP = amount × xp_per_rupee × persona_multiplier
    "max_xp_per_txn": 100
  },

  "persona_multipliers": {
    "NEW": 1.5, "RETURNING": 1.0, "POWER": 2.0
  },

  "cac_limits": {                   // daily reward budget per persona (INR)
    "NEW": 100, "RETURNING": 50, "POWER": 200
  },

  "reward_values": {
    "CHECKOUT": { "min": 5,  "max": 50, "percent_of_amount": 0.005 },
    "GOLD":     { "min": 15, "max": 40, "percent_of_amount": 0.003 }
  },

  "feature_flags": {
    "force_xp_mode":    false,      // true → all rewards become XP
    "cooldown_enabled": true,
    "cooldown_minutes": 10,
    "enable_cac_cap":   true
  }
}
```

---

## Running Tests (pytest)

Tests are **fully isolated** — no Redis required. A `FakeCacheClient` (in-memory) and a hard-coded `BASE_POLICY` mirror `policy.json` in `tests/conftest.py`.

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_decision_logic.py -v

# Run a specific test function
# pytest tests/test_decision_logic.py::test_xp_reward_new_user -v

# Run with short traceback on failure
pytest --tb=short
```

### Test modules

| File | What it covers |
|---|---|
| `test_decision_logic.py` | Core reward calculation — XP, CHECKOUT, GOLD, cooldown, force_xp_mode |
| `test_cac_cap.py` | Daily CAC budget enforcement per persona |
| `test_idempotency.py` | Duplicate transaction detection and cached response return |

---

## Load Testing (Locust)

The Locust script (`tests/locustfile.py`) simulates realistic traffic against `POST /reward/decide` using a pool of 200 users and 50 merchants with a unique `txn_id` per request.

> ⚠️ Make sure the app is running (`--workers 4`) before starting a load test.

### Run headless (recommended)

```bash
# 100 virtual users, spawn 10/s, run for 90 seconds — prints a summary on exit
locust -f tests/locustfile.py \
  --host=http://localhost:8000 \
  --users 100 \
  --spawn-rate 10 \
  --headless \
  --run-time 90s
```

> **Prefer the web UI instead?**  
> `locust -f tests/locustfile.py --host=http://localhost:8000`  
> Then open [http://localhost:8089](http://localhost:8089) and set **Users: 100 / Spawn rate: 10**.

A per-run summary (RPS, p50/p95/p99, failures) is automatically printed to the console when the test ends via the `quitting` event hook in `locustfile.py`.

---

## Performance Expectations

**Tool:** Locust · **Workers:** 4 (uvicorn, asyncio loop) · **Platform:** Windows local  
**Setup:** 100 virtual users, spawn rate 10/s, 90 s run, pool of 200 users × 50 merchants

### Results (actual run)

| Metric | Result |
|---|---|
| **Requests sent** | 24 750 |
| **Failures** | 0 (0%) |
| **Peak RPS** | **279 req/s** |
| **Median (p50)** | 46 ms |
| **p95** | 79 ms |
| **p99** | 98 ms |
| **Average** | 54 ms |

### Target vs. Actual

| Metric | Target | Actual | Status |
|---|---|---|---|
| Throughput | ~300 req/s | 279 req/s | ⚠️ close (~7% under) |
| p95 latency | < 100 ms | 79 ms | ✅ |
| p99 latency | < 120 ms | 98 ms | ✅ |
| Error rate | 0% | 0% | ✅ |

### Bottlenecks & Improvements

| Bottleneck | Root Cause | Improvement |
|---|---|---|
| ~7% RPS gap | Windows asyncio loop has higher overhead than uvloop | Switch to Linux/WSL2 + `--loop uvloop` (expect 320–350 req/s) |
| p50 at 46 ms | Multiple Redis round-trips per request (idempotency check + CAC read) | Pipeline reads; use Redis Cluster or local Unix socket |
| Occasional p99 spike to 120 ms | GIL contention across 4 workers on Windows | Run on Linux where `fork`-based workers share memory cleanly |
