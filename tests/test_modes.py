from coding_agent.modes import MODES, get_mode, mode_names


def test_mode_names():
    assert mode_names() == ["default", "plan", "code", "review"]


def test_get_mode_default():
    assert get_mode("default") == MODES["default"]
    assert "file and shell tools" in get_mode("default")


def test_get_mode_plan_differs():
    plan = get_mode("plan")
    assert "planning" in plan.lower()
    assert plan != get_mode("default")


def test_get_mode_code():
    code = get_mode("code")
    assert "coding" in code.lower()


def test_get_mode_review():
    review = get_mode("review")
    assert "review" in review.lower()


def test_get_mode_unknown_falls_back_to_default():
    assert get_mode("nope") == MODES["default"]
