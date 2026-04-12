from state_graph import StateGraph


def test_state_graph_action_key_ignores_audit_fields():
    g = StateGraph()

    act_with_audit = {"actions": [{"action": "click", "element_id": 1, "text": "", "used_element_id": 99}]}
    act_plain = {"actions": [{"action": "click", "element_id": 1, "text": ""}]}

    g.record_transition("A", "B", act_with_audit, touch=True)
    g.record_transition("A", "B", act_plain, touch=True)

    assert len(g.edges) == 1
    e = g.get_edge("A", "B", act_plain)
    assert e is not None
    assert e.count == 2

