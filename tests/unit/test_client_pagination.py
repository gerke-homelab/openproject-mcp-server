"""Regression tests for offset→page pagination translation (WP #1011).

OpenProject's API v3 ``offset`` query parameter is a **1-based page number**, not
a row index (https://www.openproject.org/docs/api/collections). Our tools expose
``offset`` as a starting *row* index (0, page_size, 2*page_size, …), so the client
must translate row offset → page number before hitting the API. Before the fix,
``offset=page_size`` was sent verbatim and asked the API for page N — returning
zero results for any result set with more than one page.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.client import OpenProjectClient, _offset_to_page
from src.utils.formatting import validate_pagination


def _client() -> OpenProjectClient:
    return OpenProjectClient(base_url="https://op.test", api_key="k")


def _endpoint_of(req_mock) -> str:
    """Extract the endpoint string from a mocked ``_request(method, endpoint, ...)``."""
    return req_mock.call_args.args[1]


@pytest.mark.parametrize(
    ("offset", "page_size", "expected_page"),
    [
        (0, 20, 1),  # first page
        (20, 20, 2),  # second page — the exact failure in WP #1011
        (40, 20, 3),
        (0, 100, 1),
        (100, 100, 2),
    ],
)
def test_offset_to_page(offset, page_size, expected_page):
    assert _offset_to_page(offset, page_size) == expected_page


@pytest.mark.parametrize("offset", [-1, -20, -21, -100])
def test_offset_to_page_never_below_one(offset):
    # A negative offset floor-divides below 1; the helper must clamp to page 1
    # so the API never receives offset=0 or a negative page number.
    assert _offset_to_page(offset, 20) >= 1


def test_offset_to_page_none_offset_stays_none():
    assert _offset_to_page(None, 20) is None


# --- validate_pagination (shared row-offset contract) --------------------------


@pytest.mark.parametrize("offset", [0, 20, 40, 100])
def test_validate_pagination_accepts_page_boundaries(offset):
    assert validate_pagination(offset, 20) is None


@pytest.mark.parametrize("offset", [1, 5, 19, 21])
def test_validate_pagination_rejects_non_multiple_offset(offset):
    err = validate_pagination(offset, 20)
    assert err is not None
    assert "multiple of page_size" in err
    assert err.startswith("❌")


def test_validate_pagination_rejects_negative_offset():
    err = validate_pagination(-1, 20)
    assert err is not None and "offset must be >= 0" in err


@pytest.mark.parametrize("page_size", [0, -1, 101])
def test_validate_pagination_rejects_bad_page_size(page_size):
    err = validate_pagination(0, page_size)
    assert err is not None and "page_size must be between 1 and 100" in err


def test_offset_to_page_without_page_size_treats_offset_as_page():
    # No page size to divide by: fall back to a valid 1-based page number.
    assert _offset_to_page(0, None) == 1
    assert _offset_to_page(3, None) == 3


@pytest.mark.asyncio
async def test_get_work_packages_translates_second_page_offset():
    client = _client()
    mock = {"_embedded": {"elements": []}, "total": 0}
    with patch.object(client, "_request", new=AsyncMock(return_value=mock)) as req:
        await client.get_work_packages(offset=20, page_size=20)
        endpoint = _endpoint_of(req)
        assert "offset=2" in endpoint  # page 2, not row 20
        assert "offset=20" not in endpoint
        assert "pageSize=20" in endpoint


@pytest.mark.asyncio
async def test_get_work_packages_first_page_is_page_one():
    client = _client()
    mock = {"_embedded": {"elements": []}, "total": 0}
    with patch.object(client, "_request", new=AsyncMock(return_value=mock)) as req:
        await client.get_work_packages(offset=0, page_size=20)
        assert "offset=1" in _endpoint_of(req)


@pytest.mark.asyncio
async def test_list_work_package_children_translates_offset():
    client = _client()
    mock = {"_embedded": {"elements": []}, "total": 0}
    with patch.object(client, "_request", new=AsyncMock(return_value=mock)) as req:
        await client.list_work_package_children(42, offset=20, page_size=20)
        assert "offset=2" in _endpoint_of(req)


@pytest.mark.asyncio
async def test_get_news_translates_offset():
    client = _client()
    mock = {"_embedded": {"elements": []}, "total": 0}
    with patch.object(client, "_request", new=AsyncMock(return_value=mock)) as req:
        await client.get_news(offset=20, page_size=20)
        assert "offset=2" in _endpoint_of(req)
