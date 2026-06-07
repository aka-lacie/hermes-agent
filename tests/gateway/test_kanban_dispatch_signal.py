from gateway import kanban_dispatch_signal as signal


def test_kanban_dispatch_nudge_invokes_registered_callbacks():
    calls = []

    def callback(payload):
        calls.append(payload)

    signal.register_kanban_dispatch_nudge(callback)
    try:
        signal.nudge_kanban_dispatch(task_id="t_123", session_id="sess-1")
    finally:
        signal.unregister_kanban_dispatch_nudge(callback)

    assert calls == [{"task_id": "t_123", "session_id": "sess-1"}]


def test_kanban_dispatch_nudge_is_best_effort():
    calls = []

    def failing(_payload):
        raise RuntimeError("boom")

    def callback(payload):
        calls.append(payload)

    signal.register_kanban_dispatch_nudge(failing)
    signal.register_kanban_dispatch_nudge(callback)
    try:
        signal.nudge_kanban_dispatch(task_id="t_456")
    finally:
        signal.unregister_kanban_dispatch_nudge(failing)
        signal.unregister_kanban_dispatch_nudge(callback)

    assert calls == [{"task_id": "t_456"}]
