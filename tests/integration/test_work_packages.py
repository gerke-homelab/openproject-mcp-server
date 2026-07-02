"""Integration tests: work packages."""

import json

import pytest

from src.client import OpenProjectClient
from tests.integration.conftest import extract_elements

pytestmark = pytest.mark.integration


async def test_list_work_packages(client: OpenProjectClient, project_id: int) -> None:
    result = await client.get_work_packages(project_id=project_id, page_size=5)
    assert isinstance(extract_elements(result), list)


async def test_create_get_update_delete(
    client: OpenProjectClient, project_id: int, wp_type_id: int
) -> None:
    result = await client.create_work_package(
        {"project": project_id, "subject": "integration-test-crud", "type": wp_type_id}
    )
    wp_id = result.get("id")
    assert wp_id, f"No id in create response: {result}"

    try:
        fetched = await client.get_work_package(wp_id)
        assert fetched["id"] == wp_id
        assert fetched["subject"] == "integration-test-crud"

        updated = await client.update_work_package(
            wp_id, {"description": "integration test update"}
        )
        assert updated.get("id") == wp_id
    finally:
        await client.delete_work_package(wp_id)


async def test_search_work_packages(
    client: OpenProjectClient, project_id: int, wp_type_id: int
) -> None:
    result = await client.create_work_package(
        {
            "project": project_id,
            "subject": "search-target-unique-xyz",
            "type": wp_type_id,
        }
    )
    wp_id = result["id"]
    try:
        filters = json.dumps(
            [
                {
                    "subjectOrId": {
                        "operator": "**",
                        "values": ["search-target-unique-xyz"],
                    }
                }
            ]
        )
        found = await client.get_work_packages(filters=filters)
        ids = [e["id"] for e in extract_elements(found)]
        assert wp_id in ids, f"Created WP {wp_id} not found in search results: {ids}"
    finally:
        await client.delete_work_package(wp_id)


async def test_pagination_offset_traverses_all_pages(
    client: OpenProjectClient, project_id: int, wp_type_id: int
) -> None:
    """Offset paging must reach rows beyond the first page (regression: WP #1011).

    OpenProject's `offset` query param is a 1-based page number; the client
    translates the caller's row offset into it. Before the fix, `offset=page_size`
    was sent verbatim, asked for page N, and returned zero results — stranding
    every row past the first page. This creates 3 WPs and walks them with
    page_size=2 (two pages), asserting the second page is non-empty and every
    created WP is reachable exactly once.
    """
    marker = "pgtraverse-probe-wp1011"
    page_size = 2
    created: list[int] = []
    try:
        for i in range(3):
            res = await client.create_work_package(
                {
                    "project": project_id,
                    "subject": f"{marker}-{i}",
                    "type": wp_type_id,
                }
            )
            created.append(res["id"])

        filters = json.dumps([{"subjectOrId": {"operator": "**", "values": [marker]}}])

        # Walk every page via row-offset stepping and union the IDs seen.
        seen: list[int] = []
        second_page_nonempty = False
        offset = 0
        while True:
            page = await client.get_work_packages(
                project_id=project_id,
                filters=filters,
                offset=offset,
                page_size=page_size,
            )
            elements = extract_elements(page)
            if offset == page_size:  # the exact offset that WP #1011 broke
                second_page_nonempty = bool(elements)
            seen.extend(e["id"] for e in elements)
            if offset + page_size >= page.get("total", 0):
                break
            offset += page_size

        assert second_page_nonempty, (
            "offset=page_size returned zero results — WP #1011 regression"
        )
        for wp_id in created:
            assert wp_id in seen, f"WP {wp_id} unreachable via offset paging: {seen}"
        assert len(seen) == len(set(seen)), f"duplicate IDs across pages: {seen}"
    finally:
        for wp_id in created:
            try:
                await client.delete_work_package(wp_id)
            except Exception:
                pass  # best-effort cleanup; don't mask assertion failures


async def test_list_types(client: OpenProjectClient, project_id: int) -> None:
    result = await client.get_types(project_id)
    types = extract_elements(result)
    assert types, "Expected at least one WP type"
    assert all("id" in t and "name" in t for t in types)


async def test_list_statuses(client: OpenProjectClient) -> None:
    result = await client.get_statuses()
    statuses = extract_elements(result)
    assert statuses, "Expected at least one status"
    assert all("id" in s and "name" in s for s in statuses)


async def test_list_priorities(client: OpenProjectClient) -> None:
    result = await client.get_priorities()
    priorities = extract_elements(result)
    assert priorities, "Expected at least one priority"
    assert all("id" in p and "name" in p for p in priorities)


async def test_add_comment_and_activities(
    client: OpenProjectClient, fresh_wp: int
) -> None:
    await client.add_work_package_comment(fresh_wp, "integration test comment")
    result = await client.get_work_package_activities(fresh_wp)
    activities = result.get("_embedded", {}).get("elements", [])
    assert any(
        "integration test comment" in str(a.get("comment", {}).get("raw", ""))
        for a in activities
    ), f"Comment not found in activities: {activities}"


async def test_assign_and_unassign(
    client: OpenProjectClient, fresh_wp: int, current_user_id: int
) -> None:
    # Use an available assignee from the WP's project rather than the current user,
    # since the current user may not have an assignable role in this project.
    available = await client.get_available_assignees(fresh_wp)
    assignees = available.get("_embedded", {}).get("elements", [])
    if not assignees:
        pytest.skip("No assignable users for this work package")

    assignee_id = assignees[0]["id"]
    updated = await client.update_work_package(fresh_wp, {"assignee_id": assignee_id})
    assignee_href = updated.get("_links", {}).get("assignee", {}).get("href", "")
    assert str(assignee_id) in assignee_href, (
        f"Expected user {assignee_id} as assignee, got href: {assignee_href}"
    )

    unassigned = await client.update_work_package(fresh_wp, {"assignee_id": None})
    assignee_href_after = (
        unassigned.get("_links", {}).get("assignee", {}).get("href") or ""
    )
    assert not assignee_href_after or assignee_href_after.endswith("null"), (
        f"Expected null assignee after unassign, got: {assignee_href_after}"
    )


async def test_list_overdue(client: OpenProjectClient, project_id: int) -> None:
    result = await client.get_work_packages(
        project_id=project_id,
        filters=json.dumps([{"dueDate": {"operator": "<t-", "values": ["0"]}}]),
    )
    assert isinstance(extract_elements(result), list)


async def test_list_due_soon(client: OpenProjectClient, project_id: int) -> None:
    result = await client.get_work_packages(
        project_id=project_id,
        filters=json.dumps([{"dueDate": {"operator": "w", "values": []}}]),
    )
    assert isinstance(extract_elements(result), list)


async def test_list_unassigned(client: OpenProjectClient, project_id: int) -> None:
    result = await client.get_work_packages(
        project_id=project_id,
        filters=json.dumps([{"assignee": {"operator": "!*", "values": []}}]),
    )
    assert isinstance(extract_elements(result), list)


async def test_list_recently_created(
    client: OpenProjectClient, project_id: int
) -> None:
    result = await client.get_work_packages(
        project_id=project_id,
        filters=json.dumps([{"createdAt": {"operator": "w", "values": []}}]),
    )
    assert isinstance(extract_elements(result), list)


async def test_list_high_priority(client: OpenProjectClient, project_id: int) -> None:
    # Discover the "High" or equivalent priority ID dynamically
    priorities_result = await client.get_priorities()
    priorities = extract_elements(priorities_result)
    high = next((p for p in priorities if "high" in p.get("name", "").lower()), None)
    if not high:
        pytest.skip("No 'high' priority configured on this instance")
    result = await client.get_work_packages(
        project_id=project_id,
        filters=json.dumps(
            [{"priority": {"operator": "=", "values": [str(high["id"])]}}]
        ),
    )
    assert isinstance(extract_elements(result), list)


async def test_list_nearly_complete(client: OpenProjectClient, project_id: int) -> None:
    result = await client.get_work_packages(
        project_id=project_id,
        filters=json.dumps([{"percentageDone": {"operator": ">=", "values": ["80"]}}]),
    )
    assert isinstance(extract_elements(result), list)


async def test_custom_fields(
    client: OpenProjectClient, fresh_wp: int, seed_wp_id: int | None
) -> None:
    """Write + read each supported custom field type.

    Uses fresh_wp (newly created each run). Custom fields must be enabled
    on the project — see scripts/setup_test_project.py click-ops checklist.
    If custom fields are not enabled, assertions will fail with 'field not present';
    the test will still indicate which fields are missing.
    """
    import datetime

    today = datetime.date.today().isoformat()

    # Use fresh_wp as the target; seed_wp_id is available if tests need a
    # persistent WP to check between runs.
    target_wp = fresh_wp

    cases: list[tuple[str, object, object]] = [
        ("customField2", "cf2-test-string", "cf2-test-string"),
        (
            "customField3",
            {"format": "markdown", "raw": "cf3 text"},
            None,
        ),  # checked below
        ("customField4", True, True),
        ("customField5", today, today),
        ("customField6", 3.14, 3.14),
        ("customField8", 42, 42),
        ("customField9", "https://example.com", "https://example.com"),
        ("customField11", "cf11-test", "cf11-test"),
        ("customField15", {"format": "markdown", "raw": "cf15 text"}, None),
    ]

    for cf_key, write_val, expected in cases:
        await client.update_work_package(
            target_wp, {"custom_fields": {cf_key: write_val}}
        )
        result = await client.get_work_package(target_wp)
        actual = result.get(cf_key)
        if expected is None:
            # Formattable: check raw text is present
            raw = actual.get("raw", "") if isinstance(actual, dict) else str(actual)
            assert "text" in raw, f"{cf_key}: expected text in raw, got {actual!r}"
        else:
            assert actual == expected, (
                f"{cf_key}: expected {expected!r}, got {actual!r}"
            )


async def test_long_text_custom_field_bare_string(
    client: OpenProjectClient, project_id: int, wp_type_id: int
) -> None:
    """A bare string written to a Formattable (long-text) custom field must be
    auto-wrapped from the form schema — on both create and update.

    Regression: previously a bare string on create was forwarded as-is (no
    existing value to infer the type from), and OpenProject silently dropped
    the write. The fix detects the field type from the form schema.
    """
    # CREATE: bare string into an empty long-text field (customField3).
    created = await client.create_work_package(
        {
            "project": project_id,
            "subject": "longtext-bare-string-create",
            "type": wp_type_id,
            "custom_fields": {"customField3": "created via bare string"},
        }
    )
    wp_id = created["id"]
    try:
        cf = created.get("customField3")
        assert isinstance(cf, dict), f"expected wrapped dict, got {cf!r}"
        assert cf.get("raw") == "created via bare string", cf

        # UPDATE: bare string overwriting the existing long-text value.
        await client.update_work_package(
            wp_id, {"custom_fields": {"customField3": "updated via bare string"}}
        )
        refreshed = await client.get_work_package(wp_id)
        cf = refreshed.get("customField3")
        assert isinstance(cf, dict), f"expected wrapped dict, got {cf!r}"
        assert cf.get("raw") == "updated via bare string", cf
    finally:
        await client.delete_work_package(wp_id)

    # UPDATE an initially-EMPTY long-text field: the schema must be fetched from
    # the optimistic-locked /form endpoint (which 409s without lockVersion), so
    # this exercises that the lockVersion is threaded through correctly.
    blank = await client.create_work_package(
        {
            "project": project_id,
            "subject": "longtext-bare-string-update-empty",
            "type": wp_type_id,
        }
    )
    blank_id = blank["id"]
    try:
        await client.update_work_package(
            blank_id,
            {"custom_fields": {"customField3": "set on empty field via bare string"}},
        )
        refreshed = await client.get_work_package(blank_id)
        cf = refreshed.get("customField3")
        assert isinstance(cf, dict), f"expected wrapped dict, got {cf!r}"
        assert cf.get("raw") == "set on empty field via bare string", cf
    finally:
        await client.delete_work_package(blank_id)
