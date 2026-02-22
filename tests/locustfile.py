import uuid
import random
from datetime import datetime
from locust import HttpUser, task, between

class RewardUser(HttpUser):
    wait_time = between(0.1, 0.5) # Simulate high traffic

    @task
    def decide_reward(self):
        txn_id = str(uuid.uuid4())
        user_id = f"user_{random.randint(1, 1000)}"
        merchant_id = f"m_{random.randint(1, 50)}"
        
        payload = {
            "txn_id": txn_id,
            "user_id": user_id,
            "merchant_id": merchant_id,
            "amount": random.randint(100, 5000), # Higher amount to trigger potential monetary reward
            "txn_type": "UPI",
            "ts": datetime.now().isoformat()
        }
        
        with self.client.post("/reward/decide", json=payload, catch_response=True) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Failed with status {response.status_code}: {response.text}")
