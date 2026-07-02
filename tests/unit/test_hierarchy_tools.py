#!/usr/bin/env python3
"""Unit tests for hierarchy tools."""

import os
import sys
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENPROJECT_URL", "http://test.example.com")
os.environ.setdefault("OPENPROJECT_API_KEY", "test-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.tools.hierarchy import (
    list_work_package_children,
    remove_work_package_parent,
    set_work_package_parent,
)


async def test_list_work_package_children_empty():
    with patch("src.tools.hierarchy.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.list_work_package_children = AsyncMock(
            return_value={"_embedded": {"elements": []}, "total": 0}
        )
        mock_get_client.return_value = mock_client
        result = await list_work_package_children(10)
        assert "Work package #10 has no children." in result, (
            f"Expected no-children message, got: {result}"
        )
        print("✅ test_list_work_package_children_empty passed")


async def test_list_work_package_children_results():
    with patch("src.tools.hierarchy.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.list_work_package_children = AsyncMock(
            return_value={
                "_embedded": {
                    "elements": [
                        {
                            "id": 11,
                            "subject": "Child task 1",
                            "_embedded": {
                                "type": {"name": "Task"},
                                "status": {"name": "New"},
                                "priority": {"name": "Normal"},
                            },
                        },
                    ]
                },
                "total": 1,
            }
        )
        mock_get_client.return_value = mock_client
        result = await list_work_package_children(10)
        assert "Child task 1" in result, (
            f"Expected child subject in result, got: {result}"
        )
        assert "✅" in result, f"Expected success emoji, got: {result}"
        print("✅ test_list_work_package_children_results passed")


async def test_set_work_package_parent():
    with patch("src.tools.hierarchy.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.update_work_package = AsyncMock(
            return_value={"id": 20, "subject": "Child WP", "_embedded": {}}
        )
        mock_get_client.return_value = mock_client
        result = await set_work_package_parent(child_id=20, parent_id=10)
        assert "✅" in result, f"Expected success emoji, got: {result}"
        assert "20" in result, f"Expected child ID in result, got: {result}"
        assert "10" in result, f"Expected parent ID in result, got: {result}"
        print("✅ test_set_work_package_parent passed")


async def test_remove_work_package_parent():
    with patch("src.tools.hierarchy.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.update_work_package = AsyncMock(
            return_value={"id": 20, "subject": "WP", "_embedded": {}}
        )
        mock_get_client.return_value = mock_client
        result = await remove_work_package_parent(20)
        assert "✅" in result, f"Expected success emoji, got: {result}"
        assert "20" in result, f"Expected WP ID in result, got: {result}"
        print("✅ test_remove_work_package_parent passed")


async def test_list_work_package_children_rejects_non_multiple_offset():
    # offset=1 is not a page boundary: reject before any API call.
    with patch("src.tools.hierarchy.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_get_client.return_value = mock_client
        result = await list_work_package_children(42, offset=1, page_size=20)
        assert "multiple of page_size" in result
        mock_client.list_work_package_children.assert_not_called()
