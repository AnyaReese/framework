from workflow import WorkflowRunner


class _DummyAppium:
    pass


class _DummyGPT:
    pass


class _DummyQuestionnaires:
    def open_gaps(self):
        return []

    def progress_score(self):
        return 0.0


def test_pop_stack_marks_explored_using_stack_edge_action():
    r = WorkflowRunner(appium=_DummyAppium(), gpt=_DummyGPT(), questionnaires=_DummyQuestionnaires())

    r.dfs_stack = ["A"]
    r.dfs_via = [None]

    r._enter_state(from_sig="A", to_sig="B", via_action="a01")
    assert r.dfs_stack == ["A", "B"]
    assert r.dfs_via == [None, "a01"]

    r._pop_stack_to("A", mark_explored=True)
    assert r.dfs_stack == ["A"]
    assert r.dfs_via == [None]
    assert "a01" in r.explored_actions.get("A", set())

    r._enter_state(from_sig="A", to_sig="C", via_action="a02")
    r._enter_state(from_sig="C", to_sig="B", via_action="cB")
    assert r.dfs_stack == ["A", "C", "B"]
    assert r.dfs_via == [None, "a02", "cB"]

    r._pop_stack_to("C", mark_explored=True)
    assert r.dfs_stack == ["A", "C"]
    assert r.dfs_via == [None, "a02"]
    assert "cB" in r.explored_actions.get("C", set())


def test_reconcile_stack_records_implicit_observation_when_sig_mismatch():
    r = WorkflowRunner(appium=_DummyAppium(), gpt=_DummyGPT(), questionnaires=_DummyQuestionnaires())

    # Pretend the graph/runner last recorded state was "A", but caller switches current sig to "B"
    # without recording an edge or observation.
    r.recent_states = ["A"]
    r.dfs_stack = ["A"]
    r.dfs_via = [None]

    r._reconcile_stack_on_external_move("B", record_observation=False)

    assert r.graph.has_state("B")
    assert r.recent_states[-1] == "B"
    assert r.dfs_stack[-1] == "B"
    assert len(r.dfs_stack) == len(r.dfs_via)

