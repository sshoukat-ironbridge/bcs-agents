"""Role-based visibility filter for Casey/dashboard data.

Pure function over plain dicts; no DB access. Roles come from
dashboard_users.role (db/state_store.py, Step 7j): admin, leadership,
cx_coordinator, pm, estimator.
"""

FULL_VISIBILITY_ROLES = {"admin", "leadership", "cx_coordinator"}
RESTRICTED_ROLES = {"pm", "estimator"}

# The safe default for any UI/script that creates a new dashboard user and
# doesn't otherwise decide a role: least privileged, not full access.
DEFAULT_NEW_USER_ROLE = "pm"

# Maps a restricted role to the project field it's matched against.
_LINKED_FIELD_BY_ROLE = {
    "pm": "pm_name",
    "estimator": "estimator_name",
}


def visible_projects(role: str, linked_name: str, projects: list[dict]) -> list[dict]:
    """Filters a list of project dicts (each with pm_name/estimator_name
    keys, e.g. casey_active_jobs rows) by what the given role may see.

    admin, leadership, cx_coordinator: see everything, unchanged order.
    pm: only projects whose pm_name matches linked_name.
    estimator: only projects whose estimator_name matches linked_name.
    Any other/unknown role: no visibility (safest default — fail closed).
    """
    if role in FULL_VISIBILITY_ROLES:
        return list(projects)

    field = _LINKED_FIELD_BY_ROLE.get(role)
    if field is None or not linked_name:
        return []

    return [p for p in projects if (p.get(field) or "").strip() == linked_name.strip()]


def can_view_project(role: str, linked_name: str, project: dict) -> bool:
    """Single-project version of visible_projects(), for a detail page."""
    return bool(visible_projects(role, linked_name, [project]))


def visibility_field(role: str) -> "str | None":
    """The project-dict/row field a restricted role is matched against
    ("pm_name" or "estimator_name"), or None for a full-visibility role
    or an unknown one. Lets a SQL query layer (dashboard/casey_queries.py)
    build its own WHERE clause from the same role->field mapping
    visible_projects() uses, without duplicating it."""
    if role in FULL_VISIBILITY_ROLES:
        return None
    return _LINKED_FIELD_BY_ROLE.get(role)
