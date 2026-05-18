"""Monitoring helpers for Redis state and business metrics."""

import asyncio
import time
from typing import Any, Dict, List

from loguru import logger

from shared.services.redis.redis_service import RedisService
from shared.utils.redis_key_builder import redis_key_builder


class RedisMonitor:
    """Redis monitoring service."""

    def __init__(self, redis_service: RedisService):
        self.redis = redis_service
        self._metrics = {}
        self._alerts = []

    async def get_redis_info(self) -> Dict[str, Any]:
        """Get raw Redis INFO output."""
        try:
            client = await self.redis._get_client()
            info = await client.info()
            return info
        except Exception as e:
            logger.error(f"Failed to get Redis info: {e}")
            return {}

    async def get_memory_usage(self) -> Dict[str, Any]:
        """Get Redis memory usage details."""
        try:
            info = await self.get_redis_info()
            memory_info = {
                "used_memory": info.get("used_memory", 0),
                "used_memory_human": info.get("used_memory_human", "0B"),
                "used_memory_rss": info.get("used_memory_rss", 0),
                "used_memory_peak": info.get("used_memory_peak", 0),
                "used_memory_peak_human": info.get("used_memory_peak_human", "0B"),
                "mem_fragmentation_ratio": info.get("mem_fragmentation_ratio", 0),
                "maxmemory": info.get("maxmemory", 0),
                "maxmemory_human": info.get("maxmemory_human", "0B"),
            }
            return memory_info
        except Exception as e:
            logger.error(f"Failed to get memory usage: {e}")
            return {}

    async def get_connection_info(self) -> Dict[str, Any]:
        """Get Redis connection details."""
        try:
            info = await self.get_redis_info()
            connection_info = {
                "connected_clients": info.get("connected_clients", 0),
                "client_recent_max_input_buffer": info.get(
                    "client_recent_max_input_buffer", 0
                ),
                "client_recent_max_output_buffer": info.get(
                    "client_recent_max_output_buffer", 0
                ),
                "blocked_clients": info.get("blocked_clients", 0),
                "tracking_clients": info.get("tracking_clients", 0),
                "clients_in_timeout_table": info.get("clients_in_timeout_table", 0),
            }
            return connection_info
        except Exception as e:
            logger.error(f"Failed to get connection info: {e}")
            return {}

    async def get_stats_info(self) -> Dict[str, Any]:
        """Get Redis statistics."""
        try:
            info = await self.get_redis_info()
            stats_info = {
                "total_commands_processed": info.get("total_commands_processed", 0),
                "instantaneous_ops_per_sec": info.get("instantaneous_ops_per_sec", 0),
                "total_net_input_bytes": info.get("total_net_input_bytes", 0),
                "total_net_output_bytes": info.get("total_net_output_bytes", 0),
                "instantaneous_input_kbps": info.get("instantaneous_input_kbps", 0),
                "instantaneous_output_kbps": info.get("instantaneous_output_kbps", 0),
                "rejected_connections": info.get("rejected_connections", 0),
                "sync_full": info.get("sync_full", 0),
                "sync_partial_ok": info.get("sync_partial_ok", 0),
                "sync_partial_err": info.get("sync_partial_err", 0),
            }
            return stats_info
        except Exception as e:
            logger.error(f"Failed to get statistics: {e}")
            return {}

    async def get_keyspace_info(self) -> Dict[str, Any]:
        """Get Redis keyspace details."""
        try:
            info = await self.get_redis_info()
            keyspace_info = {}

            for key, value in info.items():
                if key.startswith("db"):
                    db_name = key
                    db_info = {}
                    for item in value.split(","):
                        if "=" in item:
                            k, v = item.split("=", 1)
                            db_info[k] = v
                    keyspace_info[db_name] = db_info

            return keyspace_info
        except Exception as e:
            logger.error(f"Failed to get keyspace info: {e}")
            return {}

    async def get_slow_log(self, count: int = 10) -> List[Dict[str, Any]]:
        """Get slow-query log entries."""
        try:
            client = await self.redis._get_client()
            slow_log = await client.slowlog_get(count)

            formatted_log = []
            for entry in slow_log:
                formatted_entry = {
                    "id": entry[0],
                    "timestamp": entry[1],
                    "duration": entry[2],
                    "command": " ".join(entry[3]),
                    "client": entry[4],
                    "client_name": entry[5],
                }
                formatted_log.append(formatted_entry)

            return formatted_log
        except Exception as e:
            logger.error(f"Failed to get slow query log: {e}")
            return []

    async def get_key_count_by_type(self) -> Dict[str, int]:
        """Count keys by database name."""
        try:
            await self.redis._get_client()
            keyspace_info = await self.get_keyspace_info()

            key_counts = {}
            for db_name, db_info in keyspace_info.items():
                key_count = int(db_info.get("keys", 0))
                if key_count > 0:
                    key_counts[db_name] = key_count

            return key_counts
        except Exception as e:
            logger.error(f"Failed to count keys: {e}")
            return {}

    async def get_business_metrics(self) -> Dict[str, Any]:
        """Get application-facing business metrics from Redis."""
        try:
            metrics = {}

            # Online user count.
            online_users = await self.redis.smembers(
                redis_key_builder.set_online_users()
            )
            metrics["online_users_count"] = len(online_users)

            # Active user count.
            active_users = await self.redis.smembers(
                redis_key_builder.set_active_users()
            )
            metrics["active_users_count"] = len(active_users)

            # Processing task count.
            processing_tasks = await self.redis.smembers(
                redis_key_builder.set_processing_tasks()
            )
            metrics["processing_tasks_count"] = len(processing_tasks)

            # Error-log count.
            error_logs = await self.redis.llen(redis_key_builder.list_error_logs())
            metrics["error_logs_count"] = error_logs

            return metrics
        except Exception as e:
            logger.error(f"Failed to get business metrics: {e}")
            return {}

    async def check_health(self) -> Dict[str, Any]:
        """Check overall Redis health."""
        try:
            health_status = {
                "is_healthy": False,
                "ping_latency": 0,
                "memory_usage": 0,
                "connection_count": 0,
                "issues": [],
            }

            # Measure PING latency.
            start_time = time.time()
            ping_result = await self.redis.ping()
            ping_latency = (time.time() - start_time) * 1000  # Convert to milliseconds.

            if ping_result:
                health_status["ping_latency"] = ping_latency
            else:
                health_status["issues"].append("PING failed")

            # Check memory usage.
            memory_info = await self.get_memory_usage()
            if memory_info:
                used_memory = memory_info.get("used_memory", 0)
                max_memory = memory_info.get("maxmemory", 0)

                if max_memory > 0:
                    memory_usage_percent = (used_memory / max_memory) * 100
                    health_status["memory_usage"] = memory_usage_percent

                    if memory_usage_percent > 90:
                        health_status["issues"].append(
                            f"Memory usage too high: {memory_usage_percent:.2f}%"
                        )
                else:
                    health_status["memory_usage"] = 0

            # Check connection count.
            connection_info = await self.get_connection_info()
            connection_count = connection_info.get("connected_clients", 0)
            health_status["connection_count"] = connection_count

            if (
                connection_count > 1000
            ):  # Use 1000 connections as the warning threshold.
                health_status["issues"].append(
                    f"Too many connections: {connection_count}"
                )

            # Check slow queries.
            slow_log = await self.get_slow_log(5)
            if slow_log:
                slow_queries = [
                    log for log in slow_log if log["duration"] > 1000
                ]  # Queries slower than one second.
                if slow_queries:
                    health_status["issues"].append(
                        f"Detected {len(slow_queries)} slow queries"
                    )

            # Compute the overall health flag.
            health_status["is_healthy"] = len(health_status["issues"]) == 0

            return health_status
        except Exception as e:
            logger.error(f"Failed to check Redis health: {e}")
            return {
                "is_healthy": False,
                "ping_latency": 0,
                "memory_usage": 0,
                "connection_count": 0,
                "issues": [f"Health check failed: {e}"],
            }

    async def get_comprehensive_report(self) -> Dict[str, Any]:
        """Get a comprehensive monitoring report."""
        try:
            report = {
                "timestamp": time.time(),
                "health": await self.check_health(),
                "memory": await self.get_memory_usage(),
                "connections": await self.get_connection_info(),
                "stats": await self.get_stats_info(),
                "keyspace": await self.get_keyspace_info(),
                "business_metrics": await self.get_business_metrics(),
                "slow_log": await self.get_slow_log(5),
            }
            return report
        except Exception as e:
            logger.error(f"Failed to generate monitoring report: {e}")
            return {"error": str(e)}

    async def start_monitoring(self, interval: int = 60):
        """Start the monitoring loop."""
        logger.info("Redis monitoring started")

        while True:
            try:
                report = await self.get_comprehensive_report()

                # Check the current health state.
                if not report.get("health", {}).get("is_healthy", False):
                    issues = report.get("health", {}).get("issues", [])
                    for issue in issues:
                        logger.warning(f"Redis health check warning: {issue}")

                # Record the latest metrics snapshot.
                self._metrics[time.time()] = report

                # Drop metrics older than one hour.
                current_time = time.time()
                self._metrics = {
                    k: v for k, v in self._metrics.items() if current_time - k < 3600
                }

                await asyncio.sleep(interval)

            except Exception as e:
                logger.error(f"Error during monitoring: {e}")
                await asyncio.sleep(interval)

    def get_metrics_history(self, duration: int = 3600) -> Dict[str, Any]:
        """Get recent metrics history."""
        current_time = time.time()
        filtered_metrics = {
            k: v for k, v in self._metrics.items() if current_time - k < duration
        }
        return filtered_metrics
