#!/usr/bin/env python3
"""
Unit tests for News Tools - validates all 5 news management tools.

This script tests:
1. Pydantic input validation
2. Formatting functions
3. Tool functionality (without actual API calls)
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_pydantic_models():
    """Test Pydantic input validation for news tools."""
    from pydantic import ValidationError

    from src.tools.news import CreateNewsInput, UpdateNewsInput

    # Test 1.1: Valid CreateNewsInput
    valid_input = CreateNewsInput(
        project_id=1,
        title="Test News",
        summary="This is a test summary",
        description="# Test\n\nThis is a test description",
    )
    assert valid_input.project_id == 1
    assert valid_input.title == "Test News"

    # Test 1.2: Invalid CreateNewsInput - missing required fields
    with pytest.raises(ValidationError):
        CreateNewsInput(
            project_id=1,
            title="Test",
            # Missing summary and description
        )

    # Test 1.3: Invalid CreateNewsInput - title too long
    with pytest.raises(ValidationError):
        CreateNewsInput(
            project_id=1,
            title="A" * 256,  # More than 255 chars
            summary="Test",
            description="Test",
        )

    # Test 1.4: Invalid CreateNewsInput - invalid project_id
    with pytest.raises(ValidationError):
        CreateNewsInput(
            project_id=0,  # Must be > 0
            title="Test",
            summary="Test",
            description="Test",
        )

    # Test 1.5: Valid UpdateNewsInput
    valid_input = UpdateNewsInput(news_id=5, title="Updated Title")
    assert valid_input.news_id == 5
    assert valid_input.title == "Updated Title"
    assert valid_input.summary is None

    # Test 1.6: Valid UpdateNewsInput - partial update
    valid_input = UpdateNewsInput(news_id=10, description="New description only")
    assert valid_input.news_id == 10
    assert valid_input.title is None
    assert valid_input.description == "New description only"


def test_formatting_functions():
    """Test news formatting functions."""
    from src.utils.formatting import format_news_detail, format_news_list

    # Test 2.1: format_news_list with empty list
    result = format_news_list([])
    assert "No news entries found" in result

    # Test 2.2: format_news_list with single item
    test_news = [
        {
            "id": 1,
            "title": "Test News Entry",
            "summary": "This is a test summary",
            "createdAt": "2025-12-11T10:30:00.000Z",
            "_links": {
                "project": {"title": "Test Project"},
                "author": {"title": "John Doe"},
            },
        }
    ]
    result = format_news_list(test_news)
    assert "📰 News List" in result
    assert "Test News Entry" in result
    assert "Test Project" in result
    assert "John Doe" in result
    assert "2025-12-11" in result

    # Test 2.3: format_news_list with multiple items
    test_news = [
        {
            "id": 1,
            "title": "News 1",
            "summary": "Summary 1",
            "createdAt": "2025-12-11T10:00:00.000Z",
            "_links": {
                "project": {"title": "Project A"},
                "author": {"title": "User 1"},
            },
        },
        {
            "id": 2,
            "title": "News 2",
            "summary": "Summary 2",
            "createdAt": "2025-12-10T15:00:00.000Z",
            "_links": {
                "project": {"title": "Project B"},
                "author": {"title": "User 2"},
            },
        },
    ]
    result = format_news_list(test_news)
    assert "2 items" in result
    assert "News 1" in result
    assert "News 2" in result

    # Test 2.4: format_news_list with long summary
    test_news = [
        {
            "id": 1,
            "title": "Test",
            "summary": "A" * 200,  # Very long summary
            "createdAt": "2025-12-11T10:00:00.000Z",
            "_links": {
                "project": {"title": "Project"},
                "author": {"title": "User"},
            },
        }
    ]
    result = format_news_list(test_news)
    assert "..." in result  # Should be truncated

    # Test 2.5: format_news_detail
    test_news_item = {
        "id": 5,
        "title": "Detailed News Entry",
        "summary": "Short summary here",
        "description": {"raw": "# Main Content\n\nThis is the full content"},
        "createdAt": "2025-12-11T10:30:45.123Z",
        "_links": {
            "self": {"href": "/api/v3/news/5"},
            "project": {"href": "/api/v3/projects/1", "title": "My Project"},
            "author": {"href": "/api/v3/users/2", "title": "Jane Smith"},
        },
    }
    result = format_news_detail(test_news_item)
    assert "📰 News Entry #5" in result
    assert "Detailed News Entry" in result
    assert "My Project" in result
    assert "Jane Smith" in result
    assert "2025-12-11" in result
    assert "Main Content" in result


async def test_tools_with_mocks():
    """Test news tools with mocked API calls."""
    from src.tools.news import (
        CreateNewsInput,
        UpdateNewsInput,
        create_news,
        delete_news,
        get_news,
        list_news,
        update_news,
    )

    # Test 3.1: list_news with mock
    with patch("src.tools.news.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.get_news = AsyncMock(
            return_value={
                "_embedded": {
                    "elements": [
                        {
                            "id": 1,
                            "title": "Test News",
                            "summary": "Summary",
                            "createdAt": "2025-12-11T10:00:00.000Z",
                            "_links": {
                                "project": {"title": "Project"},
                                "author": {"title": "User"},
                            },
                        }
                    ]
                },
                "total": 1,
            }
        )
        mock_get_client.return_value = mock_client

        result = await list_news(project_id=1, page_size=10)
        assert "📰 News List" in result
        assert "Test News" in result

    # Test 3.2: list_news with no results
    with patch("src.tools.news.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.get_news = AsyncMock(
            return_value={"_embedded": {"elements": []}, "total": 0}
        )
        mock_get_client.return_value = mock_client

        result = await list_news()
        assert "No news entries found" in result or "✅" in result

    # Test 3.3: create_news with mock
    with patch("src.tools.news.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.create_news = AsyncMock(
            return_value={
                "id": 10,
                "title": "New News Entry",
                "summary": "Summary",
                "description": {"raw": "Content"},
            }
        )
        mock_get_client.return_value = mock_client

        input_data = CreateNewsInput(
            project_id=1,
            title="New News Entry",
            summary="Summary",
            description="Content",
        )
        result = await create_news(input_data)
        assert "✅" in result
        assert "10" in result

    # Test 3.4: get_news with mock
    with patch("src.tools.news.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.get_news_item = AsyncMock(
            return_value={
                "id": 5,
                "title": "Detailed News",
                "summary": "Summary",
                "description": {"raw": "Content"},
                "createdAt": "2025-12-11T10:00:00.000Z",
                "_links": {
                    "project": {"title": "Project"},
                    "author": {"title": "User"},
                },
            }
        )
        mock_get_client.return_value = mock_client

        result = await get_news(news_id=5)
        assert "📰 News Entry #5" in result
        assert "Detailed News" in result

    # Test 3.5: update_news with mock
    with patch("src.tools.news.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.update_news = AsyncMock(
            return_value={
                "id": 5,
                "title": "Updated Title",
                "summary": "Updated Summary",
            }
        )
        mock_get_client.return_value = mock_client

        input_data = UpdateNewsInput(news_id=5, title="Updated Title")
        result = await update_news(input_data)
        assert "✅" in result
        assert "5" in result

    # Test 3.6: delete_news with mock
    with patch("src.tools.news.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.delete_news = AsyncMock(return_value=True)
        mock_get_client.return_value = mock_client

        result = await delete_news(news_id=5)
        assert "✅" in result
        assert "5" in result
        assert "deleted" in result.lower()

    # Test 3.7: Error handling
    with patch("src.tools.news.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.get_news = AsyncMock(side_effect=Exception("API Error"))
        mock_get_client.return_value = mock_client

        result = await list_news()
        assert "❌" in result
        assert "Failed" in result or "Error" in result


async def test_list_news_rejects_non_multiple_offset():
    # offset=1 is not a page boundary: reject before any API call.
    from src.tools.news import list_news

    with patch("src.tools.news.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_get_client.return_value = mock_client
        result = await list_news(offset=1, page_size=20)
        assert "multiple of page_size" in result
        mock_client.get_news.assert_not_called()
