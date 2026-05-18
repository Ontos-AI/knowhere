"""
BaseQuotaManager — Redis-backed quota management for any token-pool API.

Design goals:
- Coordinate token usage across workers with Redis
- Enforce per-token RPM and daily quotas before calling the provider
- Keep selection fair with a quota-aware round-robin cursor
- Cool down tokens temporarily when the provider responds with 429
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from loguru import logger

from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.redis.redis_sync_service import SyncRedisService


@dataclass(frozen=True)
class TokenConfig:
    """Generic configuration for a single API token."""

    token_id: str
    api_key: str
    rpm_limit: int
    daily_limit: int


@dataclass(frozen=True)
class TokenLease:
    """A token reservation returned by acquire_request.

    Callers use ``api_key`` for the Authorization header, and pass
    ``token_id`` back to ``mark_rate_limited`` when the provider
    signals 429.
    """

    token_id: str
    api_key: str
    rpm_limit: int
    daily_limit: int


class BaseQuotaManager:
    """Abstract Redis-backed token pool with round-robin and cooldown.

    Subclasses only need to set:
      - ``SERVICE_PREFIX`` — Redis key namespace  (e.g. ``"ali"`` or ``"mineru"``)
      - ``CURSOR_KEY``     — Redis key for the round-robin cursor
      - ``default_cooldown_seconds`` — fallback cooldown when no Retry-After header
      - ``user_message``   — user-facing error string on exhaustion
    """

    SERVICE_PREFIX: str = "base"
    CURSOR_KEY: str = "base:quota:cursor"

    TOKEN_COOLDOWN_TTL_PADDING: int = 60
    MINUTE_WINDOW_SECONDS: int = 60
    DAY_WINDOW_SECONDS: int = 24 * 60 * 60

    _RESERVE_REQUEST_SCRIPT: str = """
local minute_limit = tonumber(ARGV[1])
local day_limit = tonumber(ARGV[2])
local minute_ttl = tonumber(ARGV[3])
local day_ttl = tonumber(ARGV[4])
local now_ts = tonumber(ARGV[5])

local cooldown_until = tonumber(redis.call('GET', KEYS[3]) or '0')
if cooldown_until > now_ts then
  return {0, 'cooldown', cooldown_until - now_ts}
end

local minute_count = tonumber(redis.call('GET', KEYS[1]) or '0')
if minute_count >= minute_limit then
  local ttl = redis.call('TTL', KEYS[1])
  if ttl < 0 then
    ttl = minute_ttl
    redis.call('EXPIRE', KEYS[1], minute_ttl)
  end
  return {0, 'minute', ttl}
end

local day_count = tonumber(redis.call('GET', KEYS[2]) or '0')
if day_count >= day_limit then
  local ttl = redis.call('TTL', KEYS[2])
  if ttl < 0 then
    ttl = day_ttl
    redis.call('EXPIRE', KEYS[2], day_ttl)
  end
  return {0, 'day', ttl}
end

minute_count = redis.call('INCR', KEYS[1])
if minute_count == 1 then
  redis.call('EXPIRE', KEYS[1], minute_ttl)
end

day_count = redis.call('INCR', KEYS[2])
if day_count == 1 then
  redis.call('EXPIRE', KEYS[2], day_ttl)
end

local minute_ttl_value = redis.call('TTL', KEYS[1])
if minute_ttl_value < 0 then
  minute_ttl_value = minute_ttl
end

local day_ttl_value = redis.call('TTL', KEYS[2])
if day_ttl_value < 0 then
  day_ttl_value = day_ttl
end

return {1, minute_count, day_count, minute_ttl_value, day_ttl_value}
"""

    # ---- overridable defaults ----
    default_cooldown_seconds: int = 60
    user_message: str = "Service is busy right now. Please retry shortly."

    def __init__(
        self, redis_service: SyncRedisService, token_configs: Iterable[TokenConfig]
    ) -> None:
        self.redis = redis_service
        self.tokens: List[TokenConfig] = list(token_configs)
        if not self.tokens:
            raise ValueError(f"{type(self).__name__} requires at least one token")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire_request(
        self,
        operation: str,
        preferred_token_id: Optional[str] = None,
    ) -> TokenLease:
        """Reserve request capacity on one token before calling the provider."""
        now_ts = int(time.time())
        candidate_tokens = self._ordered_tokens(preferred_token_id)
        earliest_retry_after: Optional[int] = None
        retry_period = "minute"

        for token in candidate_tokens:
            reserved, retry_after, quota_period = self._reserve_token_capacity(
                token, now_ts
            )
            if reserved:
                logger.debug(
                    f"Reserved {self.SERVICE_PREFIX} token {token.token_id} "
                    f"for operation={operation}"
                )
                return TokenLease(
                    token_id=token.token_id,
                    api_key=token.api_key,
                    rpm_limit=token.rpm_limit,
                    daily_limit=token.daily_limit,
                )
            retry_after = max(1, retry_after)
            if earliest_retry_after is None or retry_after < earliest_retry_after:
                earliest_retry_after = retry_after
                retry_period = quota_period

        logger.bind(
            service=self.SERVICE_PREFIX,
            step="quota_exhausted",
            operation=operation,
            preferred_token_id=preferred_token_id,
            retry_after=earliest_retry_after or self.default_cooldown_seconds,
            retry_period=retry_period,
            token_count=len(self.tokens),
        ).warning(f"{self.SERVICE_PREFIX} quota exhausted")
        raise UnavailableException(
            internal_message=(
                f"{self.SERVICE_PREFIX} quota exhausted for operation={operation}; "
                f"no token has available capacity right now"
            ),
            retry_after=earliest_retry_after or self.default_cooldown_seconds,
            limit=max(token.rpm_limit for token in self.tokens),
            period=retry_period,
            user_message=self.user_message,
        )

    def mark_rate_limited(
        self, token_id: str, retry_after: Optional[int] = None
    ) -> None:
        """Temporarily cool a token when the provider signals rate limiting."""
        retry_after_seconds = max(1, retry_after or self.default_cooldown_seconds)
        cooldown_until = int(time.time()) + retry_after_seconds
        cooldown_key = self._cooldown_key(token_id)
        self.redis.set(
            cooldown_key,
            cooldown_until,
            ttl=retry_after_seconds + self.TOKEN_COOLDOWN_TTL_PADDING,
        )
        logger.warning(
            f"{self.SERVICE_PREFIX} token {token_id} cooling down for "
            f"{retry_after_seconds}s (until {cooldown_until})"
        )

    # ------------------------------------------------------------------
    # Token parsing utility (shared across subclasses)
    # ------------------------------------------------------------------

    @staticmethod
    def parse_token_specs(
        raw_value: str,
        *,
        default_rpm_limit: int,
        default_daily_limit: int,
    ) -> List[TokenConfig]:
        """Parse token configs from JSON, newline, or comma-separated env values."""
        specs: List[TokenConfig] = []

        def build_token(entry: Any, index: int) -> TokenConfig:
            if isinstance(entry, dict):
                api_key = str(entry.get("api_key") or entry.get("key") or "").strip()
                if not api_key:
                    raise ValueError("Token entry missing api_key")
                token_id = str(
                    entry.get("token_id") or entry.get("id") or f"token-{index + 1}"
                )
                rpm_limit = int(entry.get("rpm_limit") or default_rpm_limit)
                daily_limit = int(entry.get("daily_limit") or default_daily_limit)
                return TokenConfig(
                    token_id=token_id,
                    api_key=api_key,
                    rpm_limit=rpm_limit,
                    daily_limit=daily_limit,
                )

            entry_text = str(entry).strip()
            if not entry_text:
                raise ValueError("Empty token entry")

            token_id = f"token-{index + 1}"
            api_key = entry_text
            if "=" in entry_text:
                maybe_id, maybe_key = entry_text.split("=", 1)
                if maybe_key.strip():
                    token_id = maybe_id.strip() or token_id
                    api_key = maybe_key.strip()
            if not api_key:
                raise ValueError("Token entry missing api_key")

            return TokenConfig(
                token_id=token_id,
                api_key=api_key,
                rpm_limit=default_rpm_limit,
                daily_limit=default_daily_limit,
            )

        try:
            loaded = json.loads(raw_value)
        except json.JSONDecodeError:
            loaded = None

        if isinstance(loaded, list):
            for index, entry in enumerate(loaded):
                specs.append(build_token(entry, index))
            return specs

        for index, line in enumerate(raw_value.replace(",", "\n").splitlines()):
            line = line.strip()
            if not line:
                continue
            specs.append(build_token(line, index))

        return specs

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ordered_tokens(self, preferred_token_id: Optional[str]) -> List[TokenConfig]:
        if preferred_token_id:
            preferred = [
                token for token in self.tokens if token.token_id == preferred_token_id
            ]
            remaining = [
                token
                for token in self._ordered_tokens(None)
                if token.token_id != preferred_token_id
            ]
            return preferred + remaining

        raw_cursor = self.redis.get(self.CURSOR_KEY, 0)
        try:
            cursor = int(raw_cursor or 0)
        except (TypeError, ValueError):
            cursor = 0

        ordered = self.tokens[cursor:] + self.tokens[:cursor]
        next_cursor = (cursor + 1) % len(self.tokens)
        self.redis.set(self.CURSOR_KEY, next_cursor, ttl=self.DAY_WINDOW_SECONDS)
        return ordered

    def _reserve_token_capacity(
        self, token: TokenConfig, now_ts: int
    ) -> tuple[bool, int, str]:
        minute_key = self._minute_key(token.token_id, now_ts)
        day_key = self._day_key(token.token_id, now_ts)
        cooldown_key = self._cooldown_key(token.token_id)

        result = self.redis.eval(
            self._RESERVE_REQUEST_SCRIPT,
            keys=[minute_key, day_key, cooldown_key],
            args=[
                token.rpm_limit,
                token.daily_limit,
                self._seconds_until_next_minute(now_ts),
                self._seconds_until_next_day(now_ts),
                now_ts,
            ],
        )

        if isinstance(result, list) and result and int(result[0]) == 1:
            return True, 0, "minute"

        if isinstance(result, list) and len(result) >= 3:
            period = (
                str(result[1])
                if result[1] in {"minute", "day", "cooldown"}
                else "minute"
            )
            if period == "cooldown":
                period = "minute"
            return False, int(result[2]), period

        return False, self.default_cooldown_seconds, "minute"

    @staticmethod
    def _seconds_until_next_minute(now_ts: int) -> int:
        return max(
            1,
            BaseQuotaManager.MINUTE_WINDOW_SECONDS
            - (now_ts % BaseQuotaManager.MINUTE_WINDOW_SECONDS),
        )

    @staticmethod
    def _seconds_until_next_day(now_ts: int) -> int:
        return max(
            1,
            BaseQuotaManager.DAY_WINDOW_SECONDS
            - (now_ts % BaseQuotaManager.DAY_WINDOW_SECONDS),
        )

    def _minute_key(self, token_id: str, now_ts: int) -> str:
        minute_bucket = now_ts // self.MINUTE_WINDOW_SECONDS
        slot_tag = self._token_slot_tag(token_id)
        return f"{self.SERVICE_PREFIX}:quota:{{{slot_tag}}}:minute:{minute_bucket}"

    def _day_key(self, token_id: str, now_ts: int) -> str:
        day_bucket = now_ts // self.DAY_WINDOW_SECONDS
        slot_tag = self._token_slot_tag(token_id)
        return f"{self.SERVICE_PREFIX}:quota:{{{slot_tag}}}:day:{day_bucket}"

    def _cooldown_key(self, token_id: str) -> str:
        slot_tag = self._token_slot_tag(token_id)
        return f"{self.SERVICE_PREFIX}:quota:{{{slot_tag}}}:cooldown"

    @staticmethod
    def _token_slot_tag(token_id: str) -> str:
        return str(token_id).replace("{", "_").replace("}", "_")
