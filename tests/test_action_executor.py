from action_cls import ActionStep, ActionType, AndroidActionExecutor


def test_android_action_executor_executes_click_and_input(monkeypatch):
    taps = []
    typed = []

    class DummyClient:
        def tap(self, x, y):
            taps.append((x, y))

        def type_text(self, text):
            typed.append(text)

    action = AndroidActionExecutor(DummyClient())
    vid_map = {
        1: {"absolute_frame": {"x": 0, "y": 0, "width": 20, "height": 20}, "enabled": True},
        2: {"absolute_frame": {"x": 0, "y": 0, "width": 10, "height": 10}, "enabled": True},
    }
    assert action.execute(ActionStep(action=ActionType.CLICK, element_id=1), vid_map)
    assert action.execute(ActionStep(action=ActionType.CLICK, element_id=1), vid_map)
    assert action.execute(ActionStep(action=ActionType.INPUT, element_id=2, text="abc"), vid_map)

    assert taps.count((10, 10)) == 2
    assert (5, 5) in taps
    assert typed[-1] == "abc"
