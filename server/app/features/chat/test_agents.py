from app.features.chat.agents import select_agents


def test_inspiration_agent_selected_for_method_inspire_prompt() -> None:
    agents = select_agents("what can i make research on especially on the method inspire me.")
    keys = [agent.key for agent in agents]
    assert "inspiration" in keys
