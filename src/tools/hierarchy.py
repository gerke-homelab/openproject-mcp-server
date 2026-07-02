"""Work package hierarchy management tools (parent-child relationships)."""

from src.server import get_client
from src.tool_registry import tracked_tool
from src.utils.formatting import (
    format_error,
    format_success,
    format_work_package_list,
    validate_pagination,
)


@tracked_tool(
    tags={
        "write",
        "hierarchy",
        "hierarchy-write",
        "set_work_package_parent",
    }
)
async def set_work_package_parent(child_id: int, parent_id: int) -> str:
    """Set a work package as child of another (create parent-child relationship).

    Args:
        child_id: ID of the child work package
        parent_id: ID of the parent work package

    Returns:
        Success message confirming the relationship
    """
    try:
        client = get_client()

        # Update child to set parent
        data = {"parent_id": parent_id}
        result = await client.update_work_package(child_id, data)

        text = format_success(
            f"Work package #{child_id} is now child of #{parent_id}\n\n"
        )
        text += f"**Child**: {result.get('subject', 'Unknown')}\n"

        embedded = result.get("_embedded", {})
        if "parent" in embedded:
            text += f"**Parent**: {embedded['parent'].get('subject', 'Unknown')}\n"

        return text

    except Exception as e:
        return format_error(f"Failed to set parent: {e!s}")


@tracked_tool(
    tags={
        "write",
        "hierarchy",
        "hierarchy-write",
        "remove_work_package_parent",
    }
)
async def remove_work_package_parent(work_package_id: int) -> str:
    """Remove parent from a work package (break parent-child relationship).

    Args:
        work_package_id: ID of the work package to remove parent from

    Returns:
        Success message
    """
    try:
        client = get_client()

        # Update to remove parent (set to null)
        data = {"parent_id": None}
        await client.update_work_package(work_package_id, data)

        return format_success(f"Removed parent from work package #{work_package_id}")

    except Exception as e:
        return format_error(f"Failed to remove parent: {e!s}")


@tracked_tool(
    tags={
        "read",
        "hierarchy",
        "hierarchy-read",
        "list_work_package_children",
    }
)
async def list_work_package_children(
    work_package_id: int, offset: int = 0, page_size: int = 20
) -> str:
    """List all children of a work package.

    Args:
        work_package_id: ID of the parent work package
        offset: Starting index for pagination (default: 0)
        page_size: Number of results per page (default: 20, max: 100)

    Returns:
        List of child work packages
    """
    try:
        if err := validate_pagination(offset, page_size):
            return err

        client = get_client()

        result = await client.list_work_package_children(
            parent_id=work_package_id, offset=offset, page_size=page_size
        )

        children = result.get("_embedded", {}).get("elements", [])
        total = result.get("total", len(children))

        if not children:
            return f"Work package #{work_package_id} has no children."

        text = f"✅ **Children of Work Package #{work_package_id}:**\n\n"
        text += format_work_package_list(children)

        # Add pagination info (children is non-empty here; guard the next-page hint
        # so we don't advertise an offset that would return an empty page).
        if total > page_size:
            start = offset + 1
            end = offset + len(children)
            text += f"\n📄 **Pagination**: Showing {start}-{end} of {total} total\n"
            if end < total:
                text += f"   Use `offset={offset + page_size}` to see next page\n"

        return text

    except Exception as e:
        return format_error(f"Failed to list children: {e!s}")
