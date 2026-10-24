"""Interpret approved-run evidence, never infer failure/success from laptop silence."""

import time

TERMINAL = {"completed", "failed", "cancelled", "deferred_offline", "unknown"}


def apply_event(state, event):
    occurred = event.get("source_epoch", 0)
    if not isinstance(occurred, (int, float)):
        return state
    # A delayed terminal transition must still close its own run, not a newer run.
    if event.get("event_type") in {"run_finished", "worker_outcome", "monitoring_stopped"}:
        result = event.get("outcome")
        preserves_known = (event.get("event_type") == "monitoring_stopped" and result == "unknown"
                           and state.get("outcome") in {"completed", "failed", "cancelled"})
        if result in TERMINAL and occurred >= state.get("terminal_at", 0) and not preserves_known:
            state.update(outcome=result, terminal_at=occurred)
    state["at"] = max(occurred, state.get("at", 0))
    kind = event.get("event_type")
    if kind == "approved":
        state.setdefault("approved_at", occurred)
    elif kind == "approved_request" and event.get("request_id"):
        requests = state.setdefault("requests", [])
        if event["request_id"] not in requests:
            requests.append(event["request_id"])
        if event.get("entity"):
            state.setdefault("entities", {})[event["request_id"]] = event["entity"]
    elif kind in {"worker_started", "worker_launched"}:
        state["started_at"] = min(occurred, state.get("started_at", occurred))
    elif kind == "heartbeat" and occurred >= state.get("heartbeat_at", 0):
        state["heartbeat_at"] = occurred
        state["worker_present"] = event.get("worker_present", False)
        if event.get("progress_at"):
            state["progress_at"] = event["progress_at"]
            state["stage"] = event.get("stage")
            state["entity"] = event.get("entity")
            state["request_id"] = event.get("request_id")
            state["progress_value"] = event.get("progress_value")
    elif kind in {"item_failed", "worker_start_failed", "supervisor_error"}:
        if kind == "item_failed":
            state["failed_items"] = state.get("failed_items", 0) + 1
        if occurred >= state.get("last_error_at", 0):
            state["last_error_at"] = occurred
            state["last_error"] = event.get("message", event.get("error_code", "item failure"))
    return state


def evaluate(state, now=None):
    now = time.time() if now is None else now
    result = state.get("outcome")
    if result == "completed":
        return "healthy", "Existing worker recorded successful completion."
    if result == "cancelled":
        return "cancelled", "The user stopped/cancelled this approved run."
    if result == "failed":
        return "unhealthy", (f"Approved run failed. Evidence: {state.get('last_error', 'worker exit without completion')}. "
                             "Inspect correlated logs and approve a retry after fixing the cause.")
    if result in {"deferred_offline", "unknown"}:
        return "unknown", "Run deferred or outcome unavailable; no automatic retry or failure inference."
    heartbeat = state.get("heartbeat_at", 0)
    if now - heartbeat > 180:
        return "unknown", "Laptop telemetry is absent; it may be asleep/offline or monitoring has stopped."
    if state.get("failed_items", 0):
        return "unhealthy", (f"{state['failed_items']} item failures in the approved run. "
                             f"Evidence: {state.get('last_error', 'see worker log')}.")
    if not state.get("started_at") and now - state.get("approved_at", now) > 300:
        return "unhealthy", "Approved launch has not started after five minutes while telemetry is fresh."
    progress = state.get("progress_at", state.get("started_at", now))
    deadline = 1800 if state.get("stage") == "model_load" else 900
    if now - progress > deadline and state.get("worker_present"):
        return "unhealthy", (f"Suspected stall in {state.get('stage', 'startup')}; "
                             f"no progress for {int((now-progress)/60)} minutes. "
                             f"Last counter: {state.get('progress_value')}. Check worker/connectivity.")
    return "healthy", f"Approved run progressing; stage={state.get('stage', 'starting')}."
