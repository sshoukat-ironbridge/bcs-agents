from agents.casey.access import visible_projects, can_view_project

PROJECTS = [
    {"client_name": "A", "pm_name": "Gustavo", "estimator_name": "Erik"},
    {"client_name": "B", "pm_name": "Carlos", "estimator_name": "Erik"},
    {"client_name": "C", "pm_name": "Gustavo", "estimator_name": "Dana"},
]


def test_admin_sees_everything():
    assert visible_projects("admin", "", PROJECTS) == PROJECTS


def test_leadership_sees_everything():
    assert visible_projects("leadership", "", PROJECTS) == PROJECTS


def test_cx_coordinator_sees_everything():
    assert visible_projects("cx_coordinator", "", PROJECTS) == PROJECTS


def test_pm_sees_only_own_projects():
    result = visible_projects("pm", "Gustavo", PROJECTS)
    assert [p["client_name"] for p in result] == ["A", "C"]


def test_estimator_sees_only_own_projects():
    result = visible_projects("estimator", "Erik", PROJECTS)
    assert [p["client_name"] for p in result] == ["A", "B"]


def test_pm_with_no_matches_sees_nothing():
    assert visible_projects("pm", "Nobody", PROJECTS) == []


def test_pm_with_empty_linked_name_sees_nothing():
    assert visible_projects("pm", "", PROJECTS) == []


def test_unknown_role_fails_closed():
    assert visible_projects("guest", "Gustavo", PROJECTS) == []


def test_can_view_project_true_for_own():
    assert can_view_project("pm", "Gustavo", PROJECTS[0]) is True


def test_can_view_project_false_for_others():
    assert can_view_project("pm", "Gustavo", PROJECTS[1]) is False


def test_can_view_project_true_for_admin():
    assert can_view_project("admin", "", PROJECTS[1]) is True


def test_visibility_field_none_for_full_visibility_roles():
    from agents.casey.access import visibility_field
    assert visibility_field("admin") is None
    assert visibility_field("leadership") is None
    assert visibility_field("cx_coordinator") is None


def test_visibility_field_for_restricted_roles():
    from agents.casey.access import visibility_field
    assert visibility_field("pm") == "pm_name"
    assert visibility_field("estimator") == "estimator_name"


def test_visibility_field_none_for_unknown_role():
    from agents.casey.access import visibility_field
    assert visibility_field("guest") is None
