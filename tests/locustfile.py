"""
Locust load test for /reward/decide endpoint.

Target: ~300 req/s with 100 users, spawn rate 10

Run from the project root:
    locust -f tests/locustfile.py --host=http://localhost:8000

For a higher-stress test (100 users, no wait):
    locust -f tests/locustfile.py --host=http://localhost:8000 --users 100 --spawn-rate 10 --headless --run-time 90s
"""
import uuid
import random
from datetime import datetime
from locust import HttpUser, task, constant_throughput, events
import logging

logger = logging.getLogger("locust.user")

# Shared pool of user/merchant IDs to simulate realistic cache hit patterns-
USER_POOL = [f"user_{i}" for i in range(1, 201)]
MERCHANT_POOL = [f"m_{i}" for i in range(1, 51)]
TXN_TYPES = ["UPI", "PURCHASE", "PAYMENT"]


class RewardUser(HttpUser):
    wait_time = constant_throughput(3)

    @task
    def decide_reward(self):
        txn_id = str(uuid.uuid4())
        user_id = random.choice(USER_POOL)
        merchant_id = random.choice(MERCHANT_POOL)

        payload = {
            "txn_id": txn_id,
            "user_id": user_id,
            "merchant_id": merchant_id,
            "amount": round(random.uniform(50, 10000), 2),
            "txn_type": random.choice(TXN_TYPES),
            "ts": datetime.utcnow().isoformat(),
        }

        with self.client.post(
            "/reward/decide",
            json=payload,
            catch_response=True,
            name="/reward/decide",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(
                    f"status={response.status_code} body={response.text[:120]}"
                )


# ---------------------------------------------------------------------------
# Optional: print a p95/p99 summary at the end of the test run
# ---------------------------------------------------------------------------
@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    stats = environment.runner.stats.get("/reward/decide", "POST")
    if stats:
        logger.info("=" * 60)
        logger.info("Load test finished — /reward/decide summary")
        logger.info(f"  Requests   : {stats.num_requests}")
        logger.info(f"  Failures   : {stats.num_failures}")
        logger.info(f"  RPS (peak) : {stats.total_rps:.1f}")
        logger.info(f"  p50 (ms)   : {stats.get_response_time_percentile(0.50):.1f}")
        logger.info(f"  p95 (ms)   : {stats.get_response_time_percentile(0.95):.1f}")
        logger.info(f"  p99 (ms)   : {stats.get_response_time_percentile(0.99):.1f}")
        logger.info(f"  Avg (ms)   : {stats.avg_response_time:.1f}")
        logger.info("=" * 60)
