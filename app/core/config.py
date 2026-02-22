from pathlib import Path
import asyncio
import json
import logging
import os
import time
from functools import lru_cache
from typing import Dict, Any, Optional


logger = logging.getLogger(__name__)


class Settings:
    """
    Application-level configuration and dynamic policy loader.

    Background:
        settings.start_reload_loop()  →  asyncio Task that wakes every
        POLICY_REFRESH_SECONDS and reloads the file via a thread-pool
        executor so the event loop is never blocked.
    """

    BASE_DIR = Path(__file__).resolve().parent.parent.parent
    # Policy file path (mounted file)
    POLICY_PATH = Path(
        os.getenv("POLICY_PATH", BASE_DIR / "config" / "policy.json")
    )

    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
    REDIS_DB = int(os.getenv("REDIS_DB", 0))


    # Policy reload interval
    POLICY_REFRESH_SECONDS = int(os.getenv("POLICY_REFRESH_SECONDS", 10))

    _policy_cache: Optional[Dict[str, Any]] = None
    
    def get_policy(self) -> Dict[str, Any]:
        """
        Return the current (cached) policy dict.
        """
        if self._policy_cache is None:
            # Synchronous fallback for the very first call (startup only)
            self._sync_load()
        return self._policy_cache

    async def start_reload_loop(self) -> None:
        """
        Async infinite loop: reload policy file every POLICY_REFRESH_SECONDS.
        Run as an asyncio.Task from the lifespan context manager.

        The file I/O is offloaded to the default thread-pool executor so the
        event loop is never blocked even for large policy files.
        """
        # First load (blocking once is fine at startup, before traffic arrives)
        await self._async_load()

        while True:
            await asyncio.sleep(self.POLICY_REFRESH_SECONDS)
            await self._async_load()

    def _sync_load(self) -> None:
        """
        Synchronous file read — only used as a startup fallback.
        """
        try:
            with open(self.POLICY_PATH, "r") as f:
                policy = json.load(f)
            self._policy_cache = policy
            logger.info("Policy loaded (sync).")
        except Exception as exc:
            logger.error("Policy load failed: %s", exc)
            if self._policy_cache is None:
                raise RuntimeError("Initial policy load failed.") from exc
            logger.warning("Keeping last known good policy.")

    async def _async_load(self) -> None:
        """
        Non-blocking file read via thread-pool executor.
        """
        loop = asyncio.get_running_loop()
        try:
            policy = await loop.run_in_executor(None, self._read_policy_file)
            self._policy_cache = policy
            logger.info("Policy reloaded (async).")
        except Exception as exc:
            logger.error("Policy reload failed: %s", exc)
            if self._policy_cache is None:
                raise RuntimeError("Initial policy load failed.") from exc
            logger.warning("Keeping last known good policy.")

    def _read_policy_file(self) -> Dict[str, Any]:
        """
        Pure blocking helper — called inside thread-pool executor.
        """
        with open(self.POLICY_PATH, "r") as f:
            return json.load(f)


@lru_cache()
def get_settings() -> Settings:
    """
    Settings singleton — cached for the process lifetime.
    """
    logger.info("Initializing Settings singleton")
    return Settings()
