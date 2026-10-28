from concurrent.futures import ThreadPoolExecutor

from shared.services.ai.retrieval_ledger import (
    cleanup_retrieval_ledger,
    get_current_retrieval_ledger,
    init_retrieval_ledger,
    record_retrieval_call,
)
from shared.services.ai.token_tracking import (
    bind_token_tracker,
    cleanup_token_tracker,
    empty_token_usage,
    get_current_token_tracker,
    get_current_token_tracker_root_id,
    init_token_tracker,
    merge_token_usage,
    record_provider_usage,
    record_tokens,
)


def test_native_thread_usage_is_recorded_on_parse_tracker() -> None:
    tracker = init_token_tracker()
    root_id = get_current_token_tracker_root_id()

    def record_from_thread() -> None:
        with bind_token_tracker(root_id):
            record_tokens(
                {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
                model="test-model",
                task="parser.test.thread",
            )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(record_from_thread).result()

        assert get_current_token_tracker() is tracker
        assert tracker["prompt_tokens"] == 11
        assert tracker["completion_tokens"] == 7
        assert tracker["total_tokens"] == 18
        assert tracker["calls"] == 1
        assert tracker["by_task"]["parser.test.thread"]["total_tokens"] == 18
        assert tracker["by_model"]["test-model"]["total_tokens"] == 18
    finally:
        cleanup_token_tracker()


def test_record_tokens_adds_duration_to_task_bucket() -> None:
    tracker = init_token_tracker()
    try:
        record_tokens(
            {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            model="m",
            task="page_memory.tag",
            duration_ms=40,
        )
        record_tokens(
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model="m",
            task="page_memory.tag",
            duration_ms=10,
        )
        assert tracker["duration_ms"] == 50
        assert tracker["by_task"]["page_memory.tag"]["duration_ms"] == 50
        assert tracker["by_task"]["page_memory.tag"]["calls"] == 2
        assert tracker["by_model"]["m"]["duration_ms"] == 50
    finally:
        cleanup_token_tracker()


def test_merge_token_usage_adds_task_buckets_without_replacing() -> None:
    dest = empty_token_usage()
    merge_token_usage(
        dest,
        {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "calls": 1,
            "duration_ms": 20,
            "by_task": {
                "document_agent.coarse_profile": {
                    "prompt_tokens": 10,
                    "total_tokens": 14,
                    "calls": 1,
                    "duration_ms": 20,
                }
            },
        },
    )
    merge_token_usage(
        dest,
        {
            "prompt_tokens": 8,
            "completion_tokens": 2,
            "total_tokens": 10,
            "calls": 1,
            "duration_ms": 15,
            "by_task": {
                "page_memory.tag": {
                    "prompt_tokens": 8,
                    "total_tokens": 10,
                    "calls": 1,
                    "duration_ms": 15,
                }
            },
        },
    )
    assert dest["total_tokens"] == 24
    assert dest["calls"] == 2
    assert dest["duration_ms"] == 35
    assert dest["by_task"]["document_agent.coarse_profile"]["calls"] == 1
    assert dest["by_task"]["page_memory.tag"]["total_tokens"] == 10


def test_record_provider_usage_writes_retrieval_ledger_not_parse_tracker() -> None:
    parse_tracker = init_token_tracker()
    parse_tracker["total_tokens"] = 4
    init_retrieval_ledger()
    try:
        record_provider_usage(
            {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
            model="router-model",
            task="retrieval.query_router",
            duration_ms=12,
        )
        assert get_current_token_tracker() is None
        ledger = get_current_retrieval_ledger()
        assert ledger is not None
        assert len(ledger.entries) == 1
        assert ledger.entries[0].task == "retrieval.query_router"
        assert ledger.entries[0].total_tokens == 8
        assert ledger.entries[0].duration_ms == 12
        assert ledger.total_tokens_or_none() == 8
    finally:
        cleanup_retrieval_ledger()
        cleanup_token_tracker()


def test_retrieval_ledger_unknown_tokens_keep_run_total_empty() -> None:
    init_retrieval_ledger()
    try:
        record_retrieval_call(
            None,
            model="cursor-model",
            task="retrieval.agent_explore",
            duration_ms=90,
        )
        ledger = get_current_retrieval_ledger()
        assert ledger is not None
        assert ledger.entries[0].total_tokens is None
        assert ledger.entries[0].duration_ms == 90
        assert ledger.total_tokens_or_none() is None
    finally:
        cleanup_retrieval_ledger()
