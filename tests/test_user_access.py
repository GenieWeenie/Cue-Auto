from __future__ import annotations

from cue_agent.security.user_access import UserAccessStore, has_permission, is_approver


def test_user_access_store_upsert_and_role_management():
    store = UserAccessStore(":memory:")
    created = store.upsert_user("u1", username="alice", display_name="Alice")
    assert created["user_id"] == "u1"
    assert created["role"] == "user"

    updated = store.set_role("u1", "operator", actor_user_id="admin-1")
    assert updated["role"] == "operator"
    assert store.get_user("u1")["role"] == "operator"  # type: ignore[index]

    rows = store.list_users(limit=10)
    assert len(rows) == 1
    assert rows[0]["username"] == "alice"

    assert store.delete_user("u1") is True
    assert store.get_user("u1") is None


def test_role_permissions_and_approver_roles():
    assert has_permission("admin", "users.manage") is True
    assert has_permission("user", "tasks.manage") is True
    assert has_permission("readonly", "tasks.manage") is False
    assert has_permission("readonly", "tasks.view") is True
    assert has_permission("user", "skills.marketplace.view") is True
    assert has_permission("user", "skills.marketplace.manage") is False
    assert has_permission("operator", "skills.marketplace.manage") is True

    assert is_approver("admin") is True
    assert is_approver("operator") is True
    assert is_approver("user") is False
