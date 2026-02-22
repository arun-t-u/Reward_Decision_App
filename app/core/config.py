from pathlib import Path
import json
import time
import os
import asyncio
import logging
from functools import lru_cache
from typing import Dict, Any


logger = logging.getLogger(__name__)


class Settings:
    """
    Application-level configuration and dynamic policy loader.

    - Static infra config loaded once.
    - Policy file auto-refreshes based on TTL.
    - Thread-safe / async-safe.
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


    # Policy reload interval
    POLICY_REFRESH_SECONDS = int(os.getenv("POLICY_REFRESH_SECONDS", 10))

    _policy_cache: Dict[str, Any] | None = None
    _policy_last_loaded: float = 0
    _lock = asyncio.Lock()

    async def get_policy(self) -> Dict[str, Any]:
        """
        Returns policy from cache.
        Reloads from file if TTL expired.

        Async-safe:
        Only one request reloads policy at a time.
        """
        now = time.time()

        # Fast path (no locking)
        if (
            self._policy_cache is not None
            and now - self._policy_last_loaded < self.POLICY_REFRESH_SECONDS
        ):
            return self._policy_cache

        # Slow path (reload needed)
        async with self._lock:
            # Double-check inside lock
            now = time.time()
            if (
                self._policy_cache is not None
                and now - self._policy_last_loaded < self.POLICY_REFRESH_SECONDS
            ):
                return self._policy_cache

            try:
                logger.info("Reloading policy from file...")
                with open(self.POLICY_PATH, "r") as f:
                    policy = json.load(f)

                self._policy_cache = policy
                self._policy_last_loaded = now

                logger.info("Policy reloaded successfully.")

            except Exception as e:
                logger.error(f"Policy reload failed: {e}")

                # If first load ever fails, raise error
                if self._policy_cache is None:
                    raise RuntimeError(
                        "Initial policy load failed. Cannot continue."
                    )

                # Otherwise keep old policy
                logger.warning("Using last known good policy.")

        return self._policy_cache


@lru_cache()
def get_settings() -> Settings:
    """
    Settings are cached because they do not change
    during application lifetime.
    """
    logger.info("Initializing Settings singleton")
    return Settings()

