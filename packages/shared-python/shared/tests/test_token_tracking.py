from concurrent.futures import ThreadPoolExecutor

from shared.services.ai.token_tracking import (
    bind_token_tracker,
    cleanup_token_tracker,
    get_current_token_tracker,
    get_current_token_tracker_root_id,
    init_token_tracker,
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
