from app.features.chat.agents import parse_intent_classification, select_agents


def test_inspiration_agent_selected_for_method_inspire_prompt() -> None:
    agents = select_agents("what can i make research on especially on the method inspire me.")
    keys = [agent.key for agent in agents]
    assert "inspiration" in keys


def test_select_agents_uses_classifier_output() -> None:
    classification = parse_intent_classification(
        '{"primary_intent":"suggestion","intents":["suggestion"],"agent_keys":["suggestion","evaluation"],"confidence":0.91}'
    )
    agents = select_agents("different natural wording", classification=classification)
    assert [agent.key for agent in agents] == ["suggestion", "evaluation"]


def test_classifier_normalization_keeps_evaluation() -> None:
    classification = parse_intent_classification('{"primary_intent":"inspiration","agent_keys":["inspiration"]}')
    assert classification is not None
    assert classification.agent_keys == ("inspiration", "evaluation")
