from pathlib import Path
import json
import time
import os
from functools import lru_cache


class Settings:
    """
    Application-level configuration.
    Loaded once and cached.
    """
    BASE_DIR = Path(__file__).resolve().parent.parent.parent
    # Policy file path (mounted file)
    POLICY_PATH = Path(
        os.getenv("POLICY_PATH", BASE_DIR / "config" / "policy.json")
    )

    # Infra / environment config (STATIC)
    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
    REDIS_DB = int(os.getenv("REDIS_DB", 0))

    IDEMPOTENCY_TTL = int(os.getenv("IDEMPOTENCY_TTL", 24 * 60 * 60))

    # Policy reload interval
    POLICY_REFRESH_SECONDS = int(os.getenv("POLICY_REFRESH_SECONDS", 10))

    _policy_cache = None
    _policy_last_loaded = 0

    def get_policy(self):
        """
        Load policy from file with TTL-based refresh.
        """
        now = time.time()

        if (
            self._policy_cache is None
            or now - self._policy_last_loaded > self.POLICY_REFRESH_SECONDS
        ):
            try:
                with open(self.POLICY_PATH, "r") as f:
                    policy = json.load(f)
                self._policy_cache = policy
                self._policy_last_loaded = now
            except Exception as e:
                # Keep old policy if new one is broken
                print(f"Policy reload failed: {e}")

        return self._policy_cache


@lru_cache()
def get_settings() -> Settings:
    """
    Settings are cached because they do not change
    during application lifetime.
    """
    return Settings()

