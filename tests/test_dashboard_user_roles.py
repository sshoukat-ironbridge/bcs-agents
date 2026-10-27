import pytest

from db.state_store import create_user


def test_create_user_requires_a_role_argument():
    with pytest.raises(TypeError):
        create_user("new@example.com", "hash")  # role omitted entirely


def test_create_user_rejects_unknown_role():
    with pytest.raises(ValueError, match="is not one of"):
        create_user("new@example.com", "hash", role="superuser")


def test_create_user_accepts_every_valid_role():
    from agents.casey.access import FULL_VISIBILITY_ROLES, RESTRICTED_ROLES
    for role in FULL_VISIBILITY_ROLES | RESTRICTED_ROLES:
        # No DB available in the test env -> returns None without raising,
        # which is what we're checking here (role validation happens
        # before the DB is touched).
        create_user(f"{role}@example.com", "hash", role=role)


def test_default_new_user_role_is_least_privileged():
    from agents.casey.access import DEFAULT_NEW_USER_ROLE, RESTRICTED_ROLES
    assert DEFAULT_NEW_USER_ROLE == "pm"
    assert DEFAULT_NEW_USER_ROLE in RESTRICTED_ROLES
