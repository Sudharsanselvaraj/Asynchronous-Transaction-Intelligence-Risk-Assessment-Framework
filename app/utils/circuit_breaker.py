"""
Redis-backed Circuit Breaker for handling service failures and preventing cascading errors.
"""
import asyncio
import time
from typing import Any, Callable, Optional

from fastapi import HTTPException, status
from redis.asyncio import from_url as redis_from_url


class CircuitBreaker:
    def __init__(
        self,
        redis_url: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        timeout: Optional[int] = None,
    ):
        self.redis_url = redis_url
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.timeout = timeout
        self.redis = None

    async def initialize(self):
        """Initialize Redis connection."""
        if self.redis is None:
            self.redis = await redis_from_url(self.redis_url, decode_responses=True)

    async def _get_key(self, service: str) -> str:
        return f"circuit_breaker:{service}"

    async def record_failure(self, service: str, error: Optional[Exception] = None):
        """Record a failure for the given service."""
        await self.initialize()
        key = await self._get_key(service)
        
        # Get current failure count and last failure time
        pipe = self.redis.pipeline()
        pipe.hget(key, "failures")
        pipe.hget(key, "last_failure")
        pipe.hget(key, "error")
        pipe.hset(key, "last_failure", str(time.time()))
        pipe.hset(key, "error", str(error) if error else "Unknown error")
        
        failures, last_failure, current_error = pipe.execute()[:2]
        
        failures = int(failures) if failures else 0
        failures += 1
        
        pipe.hset(key, "failures", failures)
        pipe.expire(key, self.recovery_timeout * 2)
        pipe.execute()

    async def record_success(self, service: str):
        """Record a success for the given service."""
        await self.initialize()
        key = await self._get_key(service)
        
        pipe = self.redis.pipeline()
        pipe.hset(key, "failures", 0)
        pipe.hset(key, "last_success", str(time.time()))
        pipe.hset(key, "error", "")
        pipe.execute()

    async def is_circuit_open(self, service: str) -> bool:
        """Check if the circuit is open (service is down)."""
        await self.initialize()
        key = await self._get_key(service)
        
        failures = await self.redis.hget(key, "failures")
        if not failures:
            return False
        
        failures = int(failures)
        if failures >= self.failure_threshold:
            return True
        
        return False

    async def wait_for_reopen(self, service: str) -> bool:
        """Wait for the circuit to reopen (if it's open)."""
        await self.initialize()
        key = await self._get_key(service)
        
        while True:
            if not await self.is_circuit_open(service):
                return True
            
            # Check if recovery timeout has passed
            last_failure = await self.redis.hget(key, "last_failure")
            if last_failure:
                last_failure_time = float(last_failure)
                if time.time() - last_failure_time > self.recovery_timeout:
                    await self.record_success(service)
                    return True
            
            # Wait before checking again
            await asyncio.sleep(1)

    async def call_with_protection(
        self,
        service: str,
        func: Callable,
        *args,
        fallback: Optional[Callable] = None,
        **kwargs,
    ) -> Any:
        """Call the function with circuit breaker protection."""
        if await self.is_circuit_open(service):
            if fallback:
                return await fallback()
            else:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Circuit breaker open for service: {service}"
                )
        
        try:
            result = await func(*args, **kwargs)
            await self.record_success(service)
            return result
        except Exception as e:
            await self.record_failure(service, e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Service {service} unavailable: {str(e)}"
            )

    async def get_status(self, service: str) -> dict:
        """Get circuit breaker status for a service."""
        await self.initialize()
        key = await self._get_key(service)
        
        pipe = self.redis.pipeline()
        pipe.hgetall(key)
        pipe.ttl(key)
        data, ttl = pipe.execute()
        
        return {
            "service": service,
            "is_open": await self.is_circuit_open(service),
            "failure_count": int(data.get("failures", 0)),
            "last_failure": data.get("last_failure"),
            "last_success": data.get("last_success"),
            "error": data.get("error"),
            "ttl": ttl,
        }