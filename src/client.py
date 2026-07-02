"""
OpenProject API Client

A comprehensive async client for OpenProject API v3 with proxy support.
"""

import asyncio
import base64
import json
import logging
import os
import re
import ssl
from typing import Any, ClassVar
from urllib.parse import quote

import aiohttp

# Configure logging
logger = logging.getLogger(__name__)

# Version information
__version__ = "3.0.0"

# OpenProject custom-field properties are always named customField<N> (e.g.
# customField12). We restrict the custom_fields dict to this pattern before
# merging so a stray/typo'd key (e.g. "subject", "lockVersion") can't silently
# overwrite a real top-level work-package property.
_CUSTOM_FIELD_KEY = re.compile(r"^customField\d+$")


def _offset_to_page(offset: int | None, page_size: int | None) -> int | None:
    """Translate a caller-supplied row offset into OpenProject's page number.

    OpenProject API v3's ``offset`` query parameter is a **1-based page number**,
    not a row index (see https://www.openproject.org/docs/api/collections — "offset
    (page number for offset-based pagination)"). Our tools, however, expose
    ``offset`` as a starting *row* index (0, page_size, 2*page_size, …). Passing
    that value straight through made ``offset=page_size`` request page N and return
    zero results for any multi-page result set (WP #1011).

    Returns ``None`` when ``offset`` is ``None`` so callers can omit the parameter.
    """
    if offset is None:
        return None
    if not page_size or page_size < 1:
        # No page size to divide by — treat the value as an already-1-based page.
        return offset if offset >= 1 else 1
    # Clamp to a valid 1-based page: a negative offset floor-divides below 1.
    return max(1, (offset // page_size) + 1)


def _merge_custom_fields(
    payload: dict,
    custom_fields: dict | None,
    reference: dict | None = None,
    schema: dict | None = None,
) -> None:
    """Merge validated custom-field values into a work-package payload in place.

    Each key must match ``customField<N>``; anything else raises ValueError so
    the caller fails loudly rather than injecting an arbitrary top-level field.

    OpenProject represents long-text/markdown custom fields (type "Formattable")
    as ``{"raw": "...", "html": "..."}`` objects rather than bare strings.
    Passing a bare string for a Formattable field is silently ignored by the API
    (the write succeeds but the value is discarded).  This function auto-wraps
    bare strings for Formattable fields, detecting the field type from (in order):

    1. ``schema`` — the form/WP schema, where the field entry has
       ``{"type": "Formattable"}``.  Authoritative even when the field has no
       current value (e.g. on create), so this is the reliable signal.
    2. ``reference`` — the current WP or form payload, where an existing
       ``{"raw": ...}`` value reveals the field is Formattable.  A fallback for
       when no schema is available.

    Callers may also pass the value pre-wrapped as ``{"raw": "..."}`` and it
    will be forwarded as-is.

    Args:
        payload: The request body being built (mutated in place).
        custom_fields: Caller-supplied ``{customField<N>: value}`` dict.
        reference: Optional dict (current WP or form payload) used to detect
            Formattable fields by an existing ``{"raw": ...}`` value.
        schema: Optional WP/form schema dict (``_embedded.schema``) used to
            detect Formattable fields by their declared ``type``.  Preferred
            over ``reference`` because it works even with no current value.
    """
    if not custom_fields:
        return
    for cf_name, cf_value in custom_fields.items():
        if not _CUSTOM_FIELD_KEY.match(str(cf_name)):
            raise ValueError(
                f"Invalid custom field key {cf_name!r}: must match "
                f"'customField<N>' (e.g. 'customField12'). Refusing to set an "
                f"arbitrary top-level property."
            )
        # Auto-wrap bare strings for Formattable (long-text/markdown) fields.
        if isinstance(cf_value, str):
            schema_entry = (schema or {}).get(cf_name)
            schema_type = (
                schema_entry.get("type") if isinstance(schema_entry, dict) else None
            )
            ref_value = (reference or {}).get(cf_name)
            if schema_type == "Formattable":
                cf_value = {"raw": cf_value}
            elif isinstance(ref_value, dict) and "raw" in ref_value:
                cf_value = {"raw": cf_value}
            elif schema_type is None and (ref_value is None and reference is not None):
                # Field type not in schema and no current value to infer from —
                # forward as-is.
                logger.debug(
                    "custom field %s type undetermined (not in schema, null "
                    "reference); forwarding bare string. If this is a long-text "
                    "field, pass {'raw': <value>} explicitly.",
                    cf_name,
                )
        payload[cf_name] = cf_value


def _verify_custom_fields(
    response: dict,
    custom_fields: dict | None,
    operation: str = "write",
) -> None:
    """Raise if a custom field value was silently dropped by OpenProject.

    Compares the values we intended to write against what came back in the
    response.  Only checks fields the caller actually supplied.

    Args:
        response: The API response dict (work package after create/update).
        custom_fields: The ``{customField<N>: value}`` dict we tried to write.
        operation: Label for the error message ("create" or "update").

    Raises:
        ValueError: If a supplied field is present in the response but empty,
            indicating the write was silently dropped by OpenProject.
            Fields absent from the response entirely emit a warning instead
            of raising, since absence may reflect a schema/type mismatch
            rather than a write failure.
    """
    if not custom_fields:
        return
    dropped = []
    for cf_name, intended in custom_fields.items():
        actual = response.get(cf_name)
        if actual is None:
            # Field not present in response at all — possibly not enabled for
            # this type/project, or a schema mismatch.  Warn but don't fail
            # hard since it may be intentional (field disabled after creation).
            logger.warning(
                "custom field %s not present in %s response; "
                "it may not be enabled for this work-package type/project.",
                cf_name,
                operation,
            )
            continue
        # Determine the intended non-empty value for comparison.
        if isinstance(intended, dict):
            intended_raw = intended.get("raw", "")
        else:
            intended_raw = str(intended) if intended is not None else ""
        if not intended_raw:
            continue  # caller wrote empty — nothing to verify
        # Determine what the response reports.
        if isinstance(actual, dict):
            actual_raw = actual.get("raw", "")
        else:
            actual_raw = str(actual) if actual is not None else ""
        if not actual_raw:
            dropped.append(
                f"{cf_name}: intended {intended_raw!r} but response shows empty value "
                f"(OpenProject silently dropped the write — check that the field "
                f"format matches the value shape and that the field is enabled for "
                f"this work-package type and project)."
            )
    if dropped:
        raise ValueError(
            f"custom field {operation} verification failed — " + "; ".join(dropped)
        )


class OpenProjectClient:
    """Client for the OpenProject API v3 with optional proxy support"""

    # Total attempts for a single request before giving up (includes the first
    # try). Retries apply only to transient failures (429 / 5xx / network).
    _MAX_RETRIES: ClassVar[int] = 4
    # Base for exponential backoff (seconds): 1, 2, 4, ... per attempt.
    _BACKOFF_BASE: ClassVar[float] = 1.0

    def __init__(
        self,
        base_url: str,
        api_key: str,
        proxy: str | None = None,
        ca_bundle: str | None = None,
    ):
        """
        Initialize the OpenProject client.

        Args:
            base_url: The base URL of the OpenProject instance
            api_key: API key for authentication
            proxy: Optional HTTP proxy URL
            ca_bundle: Optional path to a CA bundle (PEM) used to verify the
                OpenProject TLS cert. Needed when the instance serves a cert from
                a private CA (e.g. an internal step-ca) that is not in the system
                trust store. Falls back to the OPENPROJECT_CA_BUNDLE env var.
                When unset, the system default trust store is used.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.proxy = proxy
        self.ca_bundle = ca_bundle or os.getenv("OPENPROJECT_CA_BUNDLE") or None

        # Build the SSL context once. If a CA bundle is configured, trust that
        # CA explicitly (additive to the system roots); otherwise use defaults.
        self.ssl_context = ssl.create_default_context()
        if self.ca_bundle:
            self.ssl_context.load_verify_locations(cafile=self.ca_bundle)

        # Setup headers with Basic Auth
        self.headers = {
            "Authorization": f"Basic {self._encode_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"OpenProject-MCP/{__version__}",
        }

        logger.info(f"OpenProject Client initialized for: {self.base_url}")
        if self.proxy:
            logger.info(f"Using proxy: {self.proxy}")
        if self.ca_bundle:
            logger.info(f"Using custom CA bundle: {self.ca_bundle}")

    def _encode_api_key(self) -> str:
        """Encode API key for Basic Auth"""
        credentials = f"apikey:{self.api_key}"
        return base64.b64encode(credentials.encode()).decode()

    @classmethod
    def _retry_delay(cls, attempt: int, headers: Any | None) -> float:
        """Compute the backoff delay for a retry attempt (0-indexed).

        Honors a ``Retry-After`` header (seconds form) when present — the server's
        instruction wins over our exponential backoff. Otherwise returns
        ``_BACKOFF_BASE * 2**attempt``.
        """
        if headers is not None:
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except (TypeError, ValueError):
                    pass  # Non-numeric (HTTP-date form) — fall back to backoff.
        backoff: float = cls._BACKOFF_BASE * (2**attempt)
        return backoff

    async def _request(
        self, method: str, endpoint: str, data: dict | None = None
    ) -> dict:
        """
        Execute an API request.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            data: Optional request body data

        Returns:
            Dict: Response data from the API

        Raises:
            Exception: If the request fails
        """
        url = f"{self.base_url}/api/v3{endpoint}"

        logger.debug(f"API Request: {method} {url}")
        if data:
            logger.debug(f"Request body: {json.dumps(data, indent=2)}")

        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session:
            # Bounded retry on transient failures: 429 (rate limit, honoring
            # Retry-After) and 5xx (server). 4xx other than 429 are caller
            # errors and are NOT retried. Exponential backoff between attempts.
            last_error: str | None = None
            for attempt in range(self._MAX_RETRIES):
                try:
                    request_params: dict[str, Any] = {
                        "method": method,
                        "url": url,
                        "headers": self.headers,
                        "json": data,
                    }
                    if self.proxy:
                        request_params["proxy"] = self.proxy

                    async with session.request(**request_params) as response:
                        response_text = await response.text()
                        logger.debug(f"Response status: {response.status}")

                        # Retry on rate-limit / server errors.
                        if response.status == 429 or response.status >= 500:
                            last_error = self._format_error_message(
                                response.status, response_text
                            )
                            if attempt < self._MAX_RETRIES - 1:
                                delay = self._retry_delay(attempt, response.headers)
                                logger.warning(
                                    f"{response.status} on {method} {url}; "
                                    f"retry {attempt + 1}/{self._MAX_RETRIES - 1} in {delay:.1f}s"
                                )
                                await asyncio.sleep(delay)
                                continue
                            raise Exception(last_error)

                        try:
                            response_json: dict[str, Any] = (
                                json.loads(response_text) if response_text else {}
                            )
                        except json.JSONDecodeError:
                            logger.error(
                                f"Invalid JSON response: {response_text[:200]}..."
                            )
                            response_json = {}

                        # Non-retryable client errors (4xx other than 429).
                        if response.status >= 400:
                            raise Exception(
                                self._format_error_message(
                                    response.status, response_text
                                )
                            )

                        return response_json

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    # Network-level error: retry the transient ones too. aiohttp's
                    # ClientTimeout surfaces as asyncio.TimeoutError (NOT a
                    # ClientError subclass), so it must be caught explicitly —
                    # a request timeout is exactly the transient failure retry is
                    # meant to cover. (3.11+: asyncio.TimeoutError == TimeoutError.)
                    err_label = (
                        "Timeout"
                        if isinstance(e, asyncio.TimeoutError)
                        else "Network error"
                    )
                    last_error = f"{err_label} accessing {url}: {e!s}"
                    if attempt < self._MAX_RETRIES - 1:
                        delay = self._retry_delay(attempt, None)
                        logger.warning(
                            f"{err_label} on {method} {url}; "
                            f"retry {attempt + 1}/{self._MAX_RETRIES - 1} in {delay:.1f}s: {e}"
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error(f"{err_label}: {e!s}")
                    raise Exception(last_error) from None

            # Loop exhausted without returning (all attempts were retryable failures).
            raise Exception(last_error or f"Request to {url} failed after retries")

    def _format_error_message(self, status: int, response_text: str) -> str:
        """Format error message based on HTTP status code"""
        base_msg = f"API Error {status}: {response_text}"

        error_hints = {
            401: "Authentication failed. Please check your API key.",
            403: "Access denied. The user lacks required permissions.",
            404: "Resource not found. Please verify the URL and resource exists.",
            407: "Proxy authentication required.",
            500: "Internal server error. Please try again later.",
            502: "Bad gateway. The server or proxy is not responding correctly.",
            503: "Service unavailable. The server might be under maintenance.",
        }

        if status in error_hints:
            base_msg += f"\n\n{error_hints[status]}"

        return base_msg

    async def test_connection(self) -> dict:
        """Test the API connection and authentication"""
        logger.info("Testing API connection...")
        return await self._request("GET", "")

    async def get_projects(self, filters: str | None = None) -> dict:
        """
        Retrieve all projects.

        Args:
            filters: Optional JSON-encoded filter string

        Returns:
            Dict: API response containing projects
        """
        endpoint = "/projects"
        if filters:
            encoded_filters = quote(filters)
            endpoint += f"?filters={encoded_filters}"

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_work_packages(
        self,
        project_id: int | None = None,
        filters: str | None = None,
        offset: int | None = None,
        page_size: int | None = None,
    ) -> dict:
        """
        Retrieve work packages.

        Args:
            project_id: Optional project ID to filter by
            filters: Optional JSON-encoded filter string
            offset: Optional starting index for pagination
            page_size: Optional number of results per page

        Returns:
            Dict: API response containing work packages
        """
        if project_id:
            endpoint = f"/projects/{project_id}/work_packages"
        else:
            endpoint = "/work_packages"

        # Build query parameters
        query_params = []
        if filters:
            encoded_filters = quote(filters)
            query_params.append(f"filters={encoded_filters}")
        # OpenProject's `offset` param is a 1-based page number, not a row index.
        page = _offset_to_page(offset, page_size)
        if page is not None:
            query_params.append(f"offset={page}")
        if page_size is not None:
            query_params.append(f"pageSize={page_size}")

        if query_params:
            endpoint += "?" + "&".join(query_params)

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def create_work_package(self, data: dict) -> dict:
        """
        Create a new work package.

        Args:
            data: Work package data including project, subject, type, etc.

        Returns:
            Dict: Created work package data
        """
        # Prepare initial payload for form
        form_payload: dict[str, Any] = {"_links": {}}

        # Set required links
        if "project" in data:
            form_payload["_links"]["project"] = {
                "href": f"/api/v3/projects/{data['project']}"
            }
        if "type" in data:
            form_payload["_links"]["type"] = {"href": f"/api/v3/types/{data['type']}"}

        # Set subject if provided
        if "subject" in data:
            form_payload["subject"] = data["subject"]

        # Get form with initial payload
        form = await self._request("POST", "/work_packages/form", form_payload)

        # Use form payload and add additional fields
        payload = form.get("payload", form_payload)
        payload["lockVersion"] = form.get("lockVersion", 0)

        # Add optional fields
        if "description" in data:
            payload["description"] = {"raw": data["description"]}
        if "priority_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["priority"] = {
                "href": f"/api/v3/priorities/{data['priority_id']}"
            }
        if "assignee_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["assignee"] = {
                "href": f"/api/v3/users/{data['assignee_id']}"
            }
        if "version_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["version"] = {
                "href": f"/api/v3/versions/{data['version_id']}"
            }

        # Add date fields (ISO 8601 format: YYYY-MM-DD)
        if "startDate" in data:
            payload["startDate"] = data["startDate"]
        if "dueDate" in data:
            payload["dueDate"] = data["dueDate"]
        if "date" in data:
            payload["date"] = data["date"]

        # Custom fields: merge validated customField<N> keys as top-level payload
        # entries.  Pass the form schema so Formattable fields are auto-wrapped
        # correctly — on create the payload has no existing values to infer from,
        # so the schema's declared type is the only reliable signal.
        _merge_custom_fields(
            payload,
            data.get("custom_fields"),
            reference=payload,
            schema=form.get("_embedded", {}).get("schema", {}),
        )

        # Create work package
        result = await self._request("POST", "/work_packages", payload)
        _verify_custom_fields(result, data.get("custom_fields"), "create")
        return result

    async def get_types(self, project_id: int | None = None) -> dict:
        """
        Retrieve available work package types.

        Args:
            project_id: Optional project ID to filter types by

        Returns:
            Dict: API response containing types
        """
        if project_id:
            endpoint = f"/projects/{project_id}/types"
        else:
            endpoint = "/types"

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_users(self, filters: str | None = None) -> dict:
        """
        Retrieve users.

        Args:
            filters: Optional JSON-encoded filter string

        Returns:
            Dict: API response containing users
        """
        endpoint = "/users"
        if filters:
            encoded_filters = quote(filters)
            endpoint += f"?filters={encoded_filters}"

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_user(self, user_id: int) -> dict:
        """
        Retrieve a specific user by ID.

        Args:
            user_id: The user ID

        Returns:
            Dict: User data
        """
        return await self._request("GET", f"/users/{user_id}")

    async def get_memberships(
        self, project_id: int | None = None, user_id: int | None = None
    ) -> dict:
        """
        Retrieve memberships.

        Args:
            project_id: Optional project ID to filter memberships by project
            user_id: Optional user ID to filter memberships by user

        Returns:
            Dict: API response containing memberships
        """
        endpoint = "/memberships"

        # Use filters instead of path-based filtering for better compatibility
        filters = []
        if project_id:
            filters.append({"project": {"operator": "=", "values": [project_id]}})
        if user_id:
            filters.append({"principal": {"operator": "=", "values": [str(user_id)]}})

        if filters:
            filter_string = quote(json.dumps(filters))
            endpoint += f"?filters={filter_string}"

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_statuses(self) -> dict:
        """
        Retrieve available work package statuses.

        Returns:
            Dict: API response containing statuses
        """
        result = await self._request("GET", "/statuses")

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_priorities(self) -> dict:
        """
        Retrieve available work package priorities.

        Returns:
            Dict: API response containing priorities
        """
        result = await self._request("GET", "/priorities")

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_work_package(self, work_package_id: int) -> dict:
        """
        Retrieve a specific work package by ID.

        Args:
            work_package_id: The work package ID

        Returns:
            Dict: Work package data
        """
        return await self._request("GET", f"/work_packages/{work_package_id}")

    async def get_allowed_status_transitions(
        self, work_package_id: int, lock_version: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the statuses this work package may legally transition to NOW.

        OpenProject enforces a configurable workflow: from a given status, only
        certain target statuses are valid for the work package's type and the
        acting user's role. The authoritative allowed set comes from the work
        package's update form — we ask OpenProject rather than reimplement the
        workflow rules.

        Args:
            work_package_id: The work package ID.
            lock_version: Current lockVersion. Fetched if not supplied. The
                /form endpoint is optimistic-locked and 409s without it.

        Returns:
            List of ``{"id": int, "name": str}`` allowed target statuses
            (includes the current status, which OpenProject lists as allowed).
        """
        if lock_version is None:
            current = await self.get_work_package(work_package_id)
            lock_version = current.get("lockVersion", 0)

        form = await self._request(
            "POST",
            f"/work_packages/{work_package_id}/form",
            {"lockVersion": lock_version},
        )
        status_schema = form.get("_embedded", {}).get("schema", {}).get("status", {})
        allowed = status_schema.get("_embedded", {}).get("allowedValues", [])
        return [
            {"id": v.get("id"), "name": v.get("name")}
            for v in allowed
            if v.get("id") is not None
        ]

    async def update_work_package(self, work_package_id: int, data: dict) -> dict:
        """
        Update an existing work package.

        Args:
            work_package_id: The work package ID
            data: Update data including fields to modify

        Returns:
            Dict: Updated work package data
        """
        # First get current work package to get lock version
        current_wp = await self.get_work_package(work_package_id)
        lock_version = current_wp.get("lockVersion", 0)

        # Workflow-aware status transition guard: if the caller is changing the
        # status, confirm the target is a legal transition from the current
        # status (OpenProject enforces a per-type/role workflow). A blind
        # status_id poke to a disallowed value otherwise fails with an opaque
        # 422; here we fail early with the list of valid options. Skip the check
        # when validate_status_transition is False (e.g. trusted bulk loads).
        if data.get("status_id") is not None and data.get(
            "validate_status_transition", True
        ):
            current_status_href = (
                current_wp.get("_links", {}).get("status", {}).get("href", "")
            )
            current_status_id = (
                int(current_status_href.rstrip("/").split("/")[-1])
                if current_status_href
                else None
            )
            target_status_id = int(data["status_id"])
            if target_status_id != current_status_id:
                allowed = await self.get_allowed_status_transitions(
                    work_package_id, lock_version
                )
                allowed_ids = {s["id"] for s in allowed}
                if target_status_id not in allowed_ids:
                    options = ", ".join(f"{s['name']} (id {s['id']})" for s in allowed)
                    raise Exception(
                        f"Status transition to id {target_status_id} is not allowed "
                        f"from the current status by the workflow. "
                        f"Allowed transitions: {options or 'none'}."
                    )

        # Prepare payload with lock version
        payload = {"lockVersion": lock_version}

        # Add fields to update
        if "subject" in data:
            payload["subject"] = data["subject"]
        if "description" in data:
            payload["description"] = {"raw": data["description"]}
        if "type_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["type"] = {"href": f"/api/v3/types/{data['type_id']}"}
        if "status_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["status"] = {
                "href": f"/api/v3/statuses/{data['status_id']}"
            }
        if "priority_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["priority"] = {
                "href": f"/api/v3/priorities/{data['priority_id']}"
            }
        if "assignee_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["assignee"] = {
                "href": f"/api/v3/users/{data['assignee_id']}"
            }
        if "version_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["version"] = {
                "href": f"/api/v3/versions/{data['version_id']}"
            }
        if "percentage_done" in data:
            payload["percentageDone"] = data["percentage_done"]
        if "parent_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            if data["parent_id"] is None:
                # Remove parent
                payload["_links"]["parent"] = {"href": None}
            else:
                # Set parent
                payload["_links"]["parent"] = {
                    "href": f"/api/v3/work_packages/{data['parent_id']}"
                }

        # Add date fields (ISO 8601 format: YYYY-MM-DD)
        if "startDate" in data:
            payload["startDate"] = data["startDate"]
        if "dueDate" in data:
            payload["dueDate"] = data["dueDate"]
        if "date" in data:
            payload["date"] = data["date"]

        # Custom fields: merge validated customField<N> keys as top-level entries.
        # Fetch the WP form schema so Formattable fields are auto-wrapped by their
        # declared type — current_wp only reveals the type when the field already
        # has a {"raw": ...} value, so an empty long-text field would otherwise be
        # forwarded as a bare string and silently dropped.
        cf_schema: dict = {}
        if data.get("custom_fields"):
            try:
                # The /form endpoint is optimistic-locked and 409s without the
                # current lockVersion, so pass it through.
                wp_form = await self._request(
                    "POST",
                    f"/work_packages/{work_package_id}/form",
                    {"lockVersion": lock_version},
                )
                cf_schema = wp_form.get("_embedded", {}).get("schema", {})
            except Exception as e:
                logger.debug(
                    "could not fetch WP %s form schema for custom-field typing: %s",
                    work_package_id,
                    e,
                )
        _merge_custom_fields(
            payload,
            data.get("custom_fields"),
            reference=current_wp,
            schema=cf_schema,
        )

        result = await self._request(
            "PATCH", f"/work_packages/{work_package_id}", payload
        )
        _verify_custom_fields(result, data.get("custom_fields"), "update")
        return result

    async def delete_work_package(self, work_package_id: int) -> bool:
        """
        Delete a work package.

        Args:
            work_package_id: The work package ID

        Returns:
            bool: True if successful
        """
        await self._request("DELETE", f"/work_packages/{work_package_id}")
        return True

    async def add_work_package_comment(
        self, work_package_id: int, comment: str, internal: bool = False
    ) -> dict:
        """
        Add a comment/activity to a work package.

        Args:
            work_package_id: The work package ID
            comment: Comment text (supports markdown)
            internal: Whether the comment is internal (visible only to team members)

        Returns:
            Dict: API response containing the created activity
        """
        payload: dict[str, Any] = {"comment": {"format": "markdown", "raw": comment}}

        if internal:
            payload["internal"] = internal

        return await self._request(
            "POST", f"/work_packages/{work_package_id}/activities", payload
        )

    async def get_work_package_activities(self, work_package_id: int) -> dict:
        """
        Retrieve activities (comments, changes) for a work package.

        Args:
            work_package_id: The work package ID

        Returns:
            Dict: API response containing activities
        """
        result = await self._request(
            "GET", f"/work_packages/{work_package_id}/activities"
        )

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_time_entries(self, filters: str | None = None) -> dict:
        """
        Retrieve time entries.

        Args:
            filters: Optional JSON-encoded filter string

        Returns:
            Dict: API response containing time entries
        """
        endpoint = "/time_entries"
        if filters:
            encoded_filters = quote(filters)
            endpoint += f"?filters={encoded_filters}"

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def create_time_entry(self, data: dict) -> dict:
        """
        Create a new time entry.

        Args:
            data: Time entry data including work package, hours, etc.

        Returns:
            Dict: Created time entry data
        """
        # Prepare payload
        payload: dict[str, Any] = {}

        # Set required fields
        if "work_package_id" in data:
            payload["_links"] = {
                "workPackage": {
                    "href": f"/api/v3/work_packages/{data['work_package_id']}"
                }
            }
        if "hours" in data:
            payload["hours"] = f"PT{data['hours']}H"
        if "spent_on" in data:
            payload["spentOn"] = data["spent_on"]
        if "comment" in data:
            payload["comment"] = {"raw": data["comment"]}
        if "activity_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["activity"] = {
                "href": f"/api/v3/time_entries/activities/{data['activity_id']}"
            }

        return await self._request("POST", "/time_entries", payload)

    async def update_time_entry(self, time_entry_id: int, data: dict) -> dict:
        """
        Update an existing time entry.

        Args:
            time_entry_id: The time entry ID
            data: Update data including fields to modify

        Returns:
            Dict: Updated time entry data
        """
        # First get current time entry to get lock version
        current_te = await self._request("GET", f"/time_entries/{time_entry_id}")

        # Prepare payload with lock version
        payload = {"lockVersion": current_te.get("lockVersion", 0)}

        # Add fields to update
        if "hours" in data:
            payload["hours"] = f"PT{data['hours']}H"
        if "spent_on" in data:
            payload["spentOn"] = data["spent_on"]
        if "comment" in data:
            payload["comment"] = {"raw": data["comment"]}
        if "activity_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["activity"] = {
                "href": f"/api/v3/time_entries/activities/{data['activity_id']}"
            }

        return await self._request("PATCH", f"/time_entries/{time_entry_id}", payload)

    async def delete_time_entry(self, time_entry_id: int) -> bool:
        """
        Delete a time entry.

        Args:
            time_entry_id: The time entry ID

        Returns:
            bool: True if successful
        """
        await self._request("DELETE", f"/time_entries/{time_entry_id}")
        return True

    async def get_time_entry_activities(
        self, work_package_id: int | None = None
    ) -> dict:
        """Retrieve available time entry activities via the form schema.

        There is no dedicated collection endpoint (GET /time_entries/activities
        does not exist in v3). Activities are discovered via the create-form's
        allowedValues, which requires a work package context to be populated.
        If no work_package_id is provided, the first work package found is used.
        """
        payload: dict = {}
        wp_id = work_package_id
        if wp_id is None:
            # allowedValues is empty without a WP context — fetch any WP
            wps = await self._request("GET", "/work_packages?pageSize=1")
            elements = wps.get("_embedded", {}).get("elements", [])
            if elements:
                wp_id = elements[0].get("id")
        if wp_id is not None:
            payload["_links"] = {
                "workPackage": {"href": f"/api/v3/work_packages/{wp_id}"}
            }
        form = await self._request("POST", "/time_entries/form", payload)
        raw_allowed = (
            form.get("_embedded", {})
            .get("schema", {})
            .get("activity", {})
            .get("_embedded", {})
            .get("allowedValues", [])
        )
        # allowedValues may be link objects {href, title} rather than {id, name}.
        # Normalize to {id, name} so downstream formatting is consistent.
        elements = []
        for item in raw_allowed:
            if "id" in item:
                elements.append(item)
            else:
                href = item.get("href", "")
                item_id = href.rstrip("/").split("/")[-1] if href else None
                name = item.get("title") or item.get("name", "Unknown")
                elements.append({"id": item_id, "name": name, **item})
        result: dict = {"_embedded": {"elements": elements}}

        return result

    async def get_versions(self, project_id: int | None = None) -> dict:
        """
        Retrieve project versions.

        Args:
            project_id: Optional project ID to filter versions by project

        Returns:
            Dict: API response containing versions
        """
        if project_id:
            endpoint = f"/projects/{project_id}/versions"
        else:
            endpoint = "/versions"

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def create_version(self, project_id: int, data: dict) -> dict:
        """
        Create a new project version.

        Args:
            project_id: The project ID
            data: Version data including name, description, etc.

        Returns:
            Dict: Created version data
        """
        # Prepare payload
        payload = {
            "_links": {"definingProject": {"href": f"/api/v3/projects/{project_id}"}}
        }

        # Set required fields
        if "name" in data:
            payload["name"] = data["name"]
        if "description" in data:
            payload["description"] = {"raw": data["description"]}
        if "start_date" in data:
            payload["startDate"] = data["start_date"]
        if "end_date" in data:
            payload["endDate"] = data["end_date"]
        if "status" in data:
            payload["status"] = data["status"]

        return await self._request("POST", "/versions", payload)

    async def check_permissions(self) -> dict:
        """
        Check user permissions and capabilities.

        Returns:
            Dict: User information including permissions
        """
        try:
            # Get current user info which includes permissions
            return await self._request("GET", "/users/me")
        except Exception as e:
            logger.error(f"Failed to check permissions: {e}")
            return {}

    async def create_project(self, data: dict) -> dict:
        """
        Create a new project.

        Args:
            data: Project data including name, identifier, description, etc.

        Returns:
            Dict: Created project data
        """
        # Prepare payload
        payload = {}

        # Set required fields
        if "name" in data:
            payload["name"] = data["name"]
        if "identifier" in data:
            payload["identifier"] = data["identifier"]
        if "description" in data:
            payload["description"] = {"raw": data["description"]}
        if "public" in data:
            payload["public"] = data["public"]
        if "status" in data:
            payload["status"] = data["status"]
        if "parent_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["parent"] = {
                "href": f"/api/v3/projects/{data['parent_id']}"
            }

        return await self._request("POST", "/projects", payload)

    async def update_project(self, project_id: int, data: dict) -> dict:
        """
        Update an existing project.

        Args:
            project_id: The project ID
            data: Update data including fields to modify

        Returns:
            Dict: Updated project data
        """
        # First get current project to get lock version if needed
        try:
            current_project = await self.get_project(project_id)
            lock_version = current_project.get("lockVersion", 0)
        except Exception:
            lock_version = 0

        # Prepare payload with lock version
        payload = {"lockVersion": lock_version}

        # Add fields to update
        if "name" in data:
            payload["name"] = data["name"]
        if "identifier" in data:
            payload["identifier"] = data["identifier"]
        if "description" in data:
            payload["description"] = {"raw": data["description"]}
        if "public" in data:
            payload["public"] = data["public"]
        if "status" in data:
            payload["status"] = data["status"]
        if "parent_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["parent"] = {
                "href": f"/api/v3/projects/{data['parent_id']}"
            }

        return await self._request("PATCH", f"/projects/{project_id}", payload)

    async def delete_project(self, project_id: int) -> bool:
        """
        Delete a project.

        Args:
            project_id: The project ID

        Returns:
            bool: True if successful
        """
        await self._request("DELETE", f"/projects/{project_id}")
        return True

    async def get_project(self, project_id: int) -> dict:
        """
        Retrieve a specific project by ID.

        Args:
            project_id: The project ID

        Returns:
            Dict: Project data
        """
        return await self._request("GET", f"/projects/{project_id}")

    async def get_subprojects(self, parent_id: int) -> dict:
        """
        Retrieve direct subprojects of a parent project.

        Args:
            parent_id: The parent project ID

        Returns:
            Dict: API response containing direct child projects
        """
        # Use parent_id filter for direct children only
        filters = json.dumps(
            [{"parent_id": {"operator": "=", "values": [str(parent_id)]}}]
        )
        return await self.get_projects(filters)

    async def get_roles(self) -> dict:
        """
        Retrieve available roles.

        Returns:
            Dict: API response containing roles
        """
        result = await self._request("GET", "/roles")

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_role(self, role_id: int) -> dict:
        """
        Retrieve a specific role by ID.

        Args:
            role_id: The role ID

        Returns:
            Dict: Role data
        """
        return await self._request("GET", f"/roles/{role_id}")

    async def create_membership(self, data: dict) -> dict:
        """
        Create a new membership.

        Args:
            data: Membership data including project, user/group, and roles

        Returns:
            Dict: Created membership data
        """
        # Prepare payload
        payload: dict[str, Any] = {"_links": {}}

        # Set required fields
        if "project_id" in data:
            payload["_links"]["project"] = {
                "href": f"/api/v3/projects/{data['project_id']}"
            }
        if "user_id" in data:
            payload["_links"]["principal"] = {
                "href": f"/api/v3/users/{data['user_id']}"
            }
        elif "group_id" in data:
            payload["_links"]["principal"] = {
                "href": f"/api/v3/groups/{data['group_id']}"
            }
        if "role_ids" in data:
            payload["_links"]["roles"] = [
                {"href": f"/api/v3/roles/{role_id}"} for role_id in data["role_ids"]
            ]
        elif "role_id" in data:
            payload["_links"]["roles"] = [{"href": f"/api/v3/roles/{data['role_id']}"}]
        if "notification_message" in data:
            payload["notificationMessage"] = {"raw": data["notification_message"]}

        return await self._request("POST", "/memberships", payload)

    async def update_membership(self, membership_id: int, data: dict) -> dict:
        """
        Update an existing membership.

        Args:
            membership_id: The membership ID
            data: Update data including fields to modify

        Returns:
            Dict: Updated membership data
        """
        # First get current membership to get lock version if needed
        try:
            current_membership = await self.get_membership(membership_id)
            lock_version = current_membership.get("lockVersion", 0)
        except Exception:
            lock_version = 0

        # Prepare payload with lock version
        payload = {"lockVersion": lock_version}

        # Add fields to update
        if "role_ids" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["roles"] = [
                {"href": f"/api/v3/roles/{role_id}"} for role_id in data["role_ids"]
            ]
        elif "role_id" in data:
            if "_links" not in payload:
                payload["_links"] = {}
            payload["_links"]["roles"] = [{"href": f"/api/v3/roles/{data['role_id']}"}]
        if "notification_message" in data:
            payload["notificationMessage"] = {"raw": data["notification_message"]}

        return await self._request("PATCH", f"/memberships/{membership_id}", payload)

    async def delete_membership(self, membership_id: int) -> bool:
        """
        Delete a membership.

        Args:
            membership_id: The membership ID

        Returns:
            bool: True if successful
        """
        await self._request("DELETE", f"/memberships/{membership_id}")
        return True

    async def get_membership(self, membership_id: int) -> dict:
        """
        Retrieve a specific membership by ID.

        Args:
            membership_id: The membership ID

        Returns:
            Dict: Membership data
        """
        return await self._request("GET", f"/memberships/{membership_id}")

    async def list_work_package_children(
        self,
        parent_id: int,
        include_descendants: bool = False,
        offset: int | None = None,
        page_size: int | None = None,
    ) -> dict:
        """
        List all child work packages of a parent.

        Args:
            parent_id: The parent work package ID
            include_descendants: If True, includes grandchildren and below
            offset: Optional starting index for pagination
            page_size: Optional number of results per page

        Returns:
            Dict: API response containing child work packages
        """
        if include_descendants:
            # Use descendants filter to get all levels
            filters = json.dumps(
                [{"descendantsOf": {"operator": "=", "values": [str(parent_id)]}}]
            )
        else:
            # Use parent filter to get direct children only
            filters = json.dumps(
                [{"parent": {"operator": "=", "values": [str(parent_id)]}}]
            )

        # Build query parameters
        query_params = [f"filters={quote(filters)}"]
        # OpenProject's `offset` param is a 1-based page number, not a row index.
        page = _offset_to_page(offset, page_size)
        if page is not None:
            query_params.append(f"offset={page}")
        if page_size is not None:
            query_params.append(f"pageSize={page_size}")

        endpoint = "/work_packages?" + "&".join(query_params)
        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def create_work_package_relation(self, data: dict) -> dict:
        """
        Create a relationship between work packages.

        Args:
            data: Relation data including from_id, to_id, type, lag, description

        Returns:
            Dict: Created relation data
        """
        from_id = data.get("from_id")
        if not from_id:
            raise ValueError("from_id is required")

        # Prepare payload according to OpenProject API v3 spec
        payload: dict[str, Any] = {"_links": {}}

        # Set required fields
        if "to_id" in data:
            payload["_links"]["to"] = {"href": f"/api/v3/work_packages/{data['to_id']}"}
        if "type" in data:
            payload["type"] = data["type"]
        if "lag" in data:
            payload["lag"] = data["lag"]
        if "description" in data:
            payload["description"] = data["description"]

        # POST to /api/v3/work_packages/{id}/relations
        return await self._request(
            "POST", f"/work_packages/{from_id}/relations", payload
        )

    async def list_work_package_relations(self, filters: str | None = None) -> dict:
        """
        List work package relations.

        Args:
            filters: Optional JSON-encoded filter string

        Returns:
            Dict: API response containing relations
        """
        endpoint = "/relations"
        if filters:
            encoded_filters = quote(filters)
            endpoint += f"?filters={encoded_filters}"

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def update_work_package_relation(self, relation_id: int, data: dict) -> dict:
        """
        Update an existing work package relation.

        Args:
            relation_id: The relation ID
            data: Update data including fields to modify

        Returns:
            Dict: Updated relation data
        """
        # First get current relation to get lock version if needed
        try:
            current_relation = await self.get_work_package_relation(relation_id)
            lock_version = current_relation.get("lockVersion", 0)
        except Exception:
            lock_version = 0

        # Prepare payload with lock version
        payload = {"lockVersion": lock_version}

        # Add fields to update
        if "relation_type" in data:
            payload["type"] = data["relation_type"]
        if "lag" in data:
            payload["lag"] = data["lag"]
        if "description" in data:
            payload["description"] = data["description"]

        return await self._request("PATCH", f"/relations/{relation_id}", payload)

    async def delete_work_package_relation(self, relation_id: int) -> bool:
        """
        Delete a work package relation.

        Args:
            relation_id: The relation ID

        Returns:
            bool: True if successful
        """
        await self._request("DELETE", f"/relations/{relation_id}")
        return True

    async def get_work_package_relation(self, relation_id: int) -> dict:
        """
        Retrieve a specific work package relation by ID.

        Args:
            relation_id: The relation ID

        Returns:
            Dict: Relation data
        """
        return await self._request("GET", f"/relations/{relation_id}")

    # ============================================================
    # News API Methods
    # ============================================================

    async def get_news(
        self,
        filters: str | None = None,
        sort_by: str | None = None,
        offset: int | None = None,
        page_size: int | None = None,
    ) -> dict:
        """
        Retrieve news entries with filtering and pagination.

        Args:
            filters: Optional JSON-encoded filter string (e.g., project_id filter)
            sort_by: Optional JSON-encoded sort criteria (e.g., [["created_at", "asc"]])
            offset: Optional starting index for pagination
            page_size: Optional number of results per page

        Returns:
            Dict: API response containing news entries
        """
        endpoint = "/news"

        # Build query parameters
        query_params = []
        if filters:
            encoded_filters = quote(filters)
            query_params.append(f"filters={encoded_filters}")
        if sort_by:
            encoded_sort = quote(sort_by)
            query_params.append(f"sortBy={encoded_sort}")
        # OpenProject's `offset` param is a 1-based page number, not a row index.
        page = _offset_to_page(offset, page_size)
        if page is not None:
            query_params.append(f"offset={page}")
        if page_size is not None:
            query_params.append(f"pageSize={page_size}")

        if query_params:
            endpoint += "?" + "&".join(query_params)

        result = await self._request("GET", endpoint)

        # Ensure proper response structure
        if "_embedded" not in result:
            result["_embedded"] = {"elements": []}
        elif "elements" not in result.get("_embedded", {}):
            result["_embedded"]["elements"] = []

        return result

    async def get_news_item(self, news_id: int) -> dict:
        """
        Retrieve a specific news entry by ID.

        Args:
            news_id: The news ID

        Returns:
            Dict: News entry data
        """
        return await self._request("GET", f"/news/{news_id}")

    async def create_news(self, data: dict) -> dict:
        """
        Create a new news entry.

        Args:
            data: News data including:
                - project (int): Project ID (required)
                - title (str): News headline (required)
                - summary (str): Short summary (required)
                - description (str): Main body content, supports markdown (required)

        Returns:
            Dict: Created news entry data
        """
        # Prepare payload
        payload = {}

        # Set required fields
        if "title" in data:
            payload["title"] = data["title"]
        if "summary" in data:
            payload["summary"] = data["summary"]
        if "description" in data:
            payload["description"] = {"raw": data["description"]}

        # Set project link (required)
        if "project" in data:
            payload["_links"] = {
                "project": {"href": f"/api/v3/projects/{data['project']}"}
            }

        return await self._request("POST", "/news", payload)

    async def update_news(self, news_id: int, data: dict) -> dict:
        """
        Update an existing news entry.

        Args:
            news_id: The news ID
            data: Update data including fields to modify:
                - title (str): New headline (optional)
                - summary (str): New summary (optional)
                - description (str): New content, supports markdown (optional)

        Returns:
            Dict: Updated news entry data
        """
        # First get current news to get lock version
        current_news = await self.get_news_item(news_id)

        # Prepare payload with lock version
        payload = {"lockVersion": current_news.get("lockVersion", 0)}

        # Add fields to update
        if "title" in data:
            payload["title"] = data["title"]
        if "summary" in data:
            payload["summary"] = data["summary"]
        if "description" in data:
            payload["description"] = {"raw": data["description"]}

        return await self._request("PATCH", f"/news/{news_id}", payload)

    async def delete_news(self, news_id: int) -> bool:
        """
        Delete a news entry.

        Args:
            news_id: The news ID

        Returns:
            bool: True if successful
        """
        await self._request("DELETE", f"/news/{news_id}")
        return True

    async def get_wiki_page_by_id(self, wiki_page_id: int) -> dict:
        """Get a wiki page by integer ID.

        The OpenProject v3 API wiki support is currently a stub: only this
        single GET endpoint is available.  List, create, update, and delete
        operations are not exposed via the API.
        """
        return await self._request("GET", f"/wiki_pages/{wiki_page_id}")

    async def get_groups(self) -> dict:
        """List all groups in the OpenProject instance."""
        return await self._request("GET", "/groups")

    async def get_group(self, group_id: int) -> dict:
        """Get a specific group by ID."""
        return await self._request("GET", f"/groups/{group_id}")

    async def get_notifications(
        self,
        filters: str | None = None,
        page_size: int = 20,
    ) -> dict:
        """List notifications for the current API user."""
        query_params = [f"pageSize={page_size}"]
        if filters:
            query_params.append(f"filters={quote(filters)}")
        endpoint = "/notifications?" + "&".join(query_params)
        return await self._request("GET", endpoint)

    async def mark_notification_read(self, notification_id: int) -> bool:
        """Mark a single notification as read."""
        await self._request("POST", f"/notifications/{notification_id}/read_ian")
        return True

    async def mark_all_notifications_read(self) -> bool:
        """Mark all notifications as read for the current user."""
        await self._request("POST", "/notifications/read_ian")
        return True

    async def _upload_request(
        self,
        endpoint: str,
        file_bytes: bytes,
        filename: str,
        content_type: str,
    ) -> dict:
        """Upload a file via multipart form-data POST.

        OpenProject v3 attachment upload requires a two-part multipart body:
          - 'metadata': JSON part with {"fileName": "...", "contentType": "..."}
          - 'file': binary part with the actual file bytes

        Replicates the retry/backoff logic from _request.
        """
        import aiohttp as _aiohttp

        url = f"{self.base_url}/api/v3{endpoint}"
        connector = _aiohttp.TCPConnector(ssl=self.ssl_context)
        timeout = _aiohttp.ClientTimeout(total=60)

        last_error: str | None = None
        async with _aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session:
            for attempt in range(self._MAX_RETRIES):
                try:
                    form = _aiohttp.FormData()
                    form.add_field(
                        "metadata",
                        json.dumps({"fileName": filename, "contentType": content_type}),
                        content_type="application/json",
                    )
                    form.add_field(
                        "file",
                        file_bytes,
                        filename=filename,
                        content_type=content_type,
                    )
                    # Strip Content-Type from headers so aiohttp sets multipart boundary
                    upload_headers = {
                        k: v
                        for k, v in self.headers.items()
                        if k.lower() != "content-type"
                    }
                    request_params: dict = {
                        "method": "POST",
                        "url": url,
                        "headers": upload_headers,
                        "data": form,
                    }
                    if self.proxy:
                        request_params["proxy"] = self.proxy

                    async with session.request(**request_params) as response:
                        response_text = await response.text()
                        if response.status == 429 or response.status >= 500:
                            last_error = self._format_error_message(
                                response.status, response_text
                            )
                            if attempt < self._MAX_RETRIES - 1:
                                delay = self._retry_delay(attempt, response.headers)
                                await asyncio.sleep(delay)
                                continue
                            raise Exception(last_error)
                        try:
                            response_json: dict[str, Any] = (
                                json.loads(response_text) if response_text else {}
                            )
                        except json.JSONDecodeError:
                            response_json = {}
                        if response.status >= 400:
                            raise Exception(
                                self._format_error_message(
                                    response.status, response_text
                                )
                            )
                        return response_json
                except (_aiohttp.ClientError, asyncio.TimeoutError) as e:
                    err_label = (
                        "Timeout"
                        if isinstance(e, asyncio.TimeoutError)
                        else "Network error"
                    )
                    last_error = f"{err_label} during upload to {url}: {e!s}"
                    if attempt < self._MAX_RETRIES - 1:
                        delay = self._retry_delay(attempt, None)
                        await asyncio.sleep(delay)
                        continue
                    raise Exception(last_error) from None
        raise Exception(last_error or f"Upload to {url} failed after retries")

    _ATTACHMENT_CONTAINERS: ClassVar[frozenset[str]] = frozenset(
        {"work_packages", "wiki_pages", "projects"}
    )

    async def upload_attachment(
        self,
        container_type: str,
        container_id: int,
        file_bytes: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> dict:
        """Upload a file attachment to a work package, wiki page, or project.

        Args:
            container_type: One of 'work_packages', 'wiki_pages', 'projects'
            container_id: ID of the container resource
            file_bytes: Raw file bytes
            filename: Original filename (used for Content-Disposition)
            content_type: MIME type (default: application/octet-stream)
        """
        if container_type not in self._ATTACHMENT_CONTAINERS:
            raise ValueError(
                f"container_type must be one of {self._ATTACHMENT_CONTAINERS}, got '{container_type}'"
            )
        endpoint = f"/{container_type}/{container_id}/attachments"
        return await self._upload_request(endpoint, file_bytes, filename, content_type)

    async def get_attachment(self, attachment_id: int) -> dict:
        """Get metadata for an attachment by ID."""
        return await self._request("GET", f"/attachments/{attachment_id}")

    async def delete_attachment(self, attachment_id: int) -> bool:
        """Delete an attachment by ID."""
        await self._request("DELETE", f"/attachments/{attachment_id}")
        return True

    async def list_attachments(self, container_type: str, container_id: int) -> dict:
        """List attachments for a work package, wiki page, or project."""
        return await self._request(
            "GET", f"/{container_type}/{container_id}/attachments"
        )

    async def get_watchers(self, work_package_id: int) -> dict:
        """List users watching a work package."""
        return await self._request("GET", f"/work_packages/{work_package_id}/watchers")

    async def get_available_watchers(self, work_package_id: int) -> dict:
        """List users eligible to watch a work package (project members)."""
        return await self._request(
            "GET", f"/work_packages/{work_package_id}/available_watchers"
        )

    async def add_watcher(self, work_package_id: int, user_id: int) -> bool:
        """Add a user as a watcher on a work package."""
        await self._request(
            "POST",
            f"/work_packages/{work_package_id}/watchers",
            {"_links": {"user": {"href": f"/api/v3/users/{user_id}"}}},
        )
        return True

    async def remove_watcher(self, work_package_id: int, user_id: int) -> bool:
        """Remove a user from the watcher list of a work package."""
        await self._request(
            "DELETE", f"/work_packages/{work_package_id}/watchers/{user_id}"
        )
        return True

    async def get_activity(self, activity_id: int) -> dict:
        """Get a single work package activity (comment or change) by ID."""
        return await self._request("GET", f"/activities/{activity_id}")

    async def update_activity(
        self, activity_id: int, comment: str, internal: bool = False
    ) -> dict:
        """Edit the comment on an activity.

        The API expects comment as a plain string (not a formattable dict),
        even though it returns comment as {"format": "markdown", "raw": "..."}.
        """
        return await self._request(
            "PATCH",
            f"/activities/{activity_id}",
            {"comment": comment, "internal": internal},
        )

    async def get_available_assignees(self, work_package_id: int) -> dict:
        """List users eligible to be assigned to a work package."""
        return await self._request(
            "GET", f"/work_packages/{work_package_id}/available_assignees"
        )

    async def get_reminders(self, work_package_id: int) -> dict:
        """List reminders set on a work package for the current user."""
        return await self._request("GET", f"/work_packages/{work_package_id}/reminders")

    async def create_reminder(
        self, work_package_id: int, remind_at: str, note: str | None = None
    ) -> dict:
        """Create a reminder on a work package.

        Args:
            work_package_id: Work package ID
            remind_at: ISO 8601 datetime string (e.g. '2026-06-25T09:00:00Z')
            note: Optional reminder note
        """
        payload: dict = {"remindAt": remind_at}
        if note is not None:
            payload["note"] = note
        return await self._request(
            "POST", f"/work_packages/{work_package_id}/reminders", payload
        )

    async def get_queries(self, project_id: int | None = None) -> dict:
        """List saved queries, optionally scoped to a project."""
        if project_id is not None:
            filters = quote(
                json.dumps(
                    [{"project_id": {"operator": "=", "values": [str(project_id)]}}]
                )
            )
            return await self._request("GET", f"/queries?filters={filters}")
        return await self._request("GET", "/queries")

    async def get_query(self, query_id: int) -> dict:
        """Get a saved query by ID."""
        return await self._request("GET", f"/queries/{query_id}")

    async def get_default_query(self, project_id: int | None = None) -> dict:
        """Get the default query (global or project-scoped)."""
        if project_id is not None:
            return await self._request("GET", f"/projects/{project_id}/queries/default")
        return await self._request("GET", "/queries/default")

    async def create_query(self, data: dict) -> dict:
        """Create a new saved query. `data` is the full HAL payload."""
        return await self._request("POST", "/queries", data)

    async def update_query(self, query_id: int, data: dict) -> dict:
        """Update a saved query. `data` is the partial HAL payload."""
        return await self._request("PATCH", f"/queries/{query_id}", data)

    async def delete_query(self, query_id: int) -> bool:
        """Delete a saved query by ID."""
        await self._request("DELETE", f"/queries/{query_id}")
        return True

    async def star_query(self, query_id: int) -> dict:
        """Star a query (pin to top of the list)."""
        return await self._request("PATCH", f"/queries/{query_id}/star")

    async def unstar_query(self, query_id: int) -> dict:
        """Unstar a query."""
        return await self._request("PATCH", f"/queries/{query_id}/unstar")

    async def get_version(self, version_id: int) -> dict:
        """Get a single version by ID."""
        return await self._request("GET", f"/versions/{version_id}")

    async def update_version(self, version_id: int, data: dict) -> dict:
        """Update a version. Auto-fetches lockVersion."""
        current = await self.get_version(version_id)
        lock_version = current.get("lockVersion", 0)
        payload: dict = {"lockVersion": lock_version}
        if "name" in data:
            payload["name"] = data["name"]
        if "description" in data:
            payload["description"] = {"raw": data["description"]}
        if "start_date" in data:
            payload["startDate"] = data["start_date"]
        if "due_date" in data:
            payload["endDate"] = data["due_date"]
        if "status" in data:
            payload["status"] = data["status"]
        return await self._request("PATCH", f"/versions/{version_id}", payload)

    async def delete_version(self, version_id: int) -> bool:
        """Delete a version by ID. Fails if work packages are assigned."""
        await self._request("DELETE", f"/versions/{version_id}")
        return True

    async def list_custom_actions(self) -> dict:
        """List all custom actions defined in the OpenProject instance."""
        return await self._request("GET", "/custom_actions")

    async def get_custom_action(self, action_id: int) -> dict:
        """Get a single custom action by ID."""
        return await self._request("GET", f"/custom_actions/{action_id}")

    async def execute_custom_action(self, action_id: int, work_package_id: int) -> dict:
        """Execute a custom action against a work package. Auto-fetches lockVersion."""
        current = await self.get_work_package(work_package_id)
        lock_version = current.get("lockVersion", 0)
        payload = {
            "_links": {
                "workPackage": {"href": f"/api/v3/work_packages/{work_package_id}"}
            },
            "lockVersion": lock_version,
        }
        return await self._request(
            "POST", f"/custom_actions/{action_id}/execute", payload
        )

    async def create_user(self, data: dict) -> dict:
        """Create a new user (admin only).

        Some OpenProject instances require the password field; others do not
        (you'll get a 422 if yours does and you omit it). When provided, the
        password works for web login immediately. The v3 API does not support
        basic-auth with username/password — API authentication always uses tokens.
        """
        payload = {
            "login": data["login"],
            "firstName": data["first_name"],
            "lastName": data["last_name"],
            "email": data["email"],
            "status": data.get("status", "active"),
        }
        if data.get("admin") is not None:
            payload["admin"] = data["admin"]
        if data.get("password"):
            payload["password"] = data["password"]
        return await self._request("POST", "/users", payload)

    async def update_user(self, user_id: int, data: dict) -> dict:
        """Update an existing user (admin only). Auto-fetches lockVersion."""
        current = await self.get_user(user_id)
        lock_version = current.get("lockVersion", 0)
        payload: dict = {"lockVersion": lock_version}
        if "first_name" in data:
            payload["firstName"] = data["first_name"]
        if "last_name" in data:
            payload["lastName"] = data["last_name"]
        if "email" in data:
            payload["email"] = data["email"]
        if "status" in data:
            payload["status"] = data["status"]
        if "admin" in data:
            payload["admin"] = data["admin"]
        return await self._request("PATCH", f"/users/{user_id}", payload)

    async def get_principals(self, project_id: int | None = None) -> dict:
        """List principals (users + groups + placeholder users).

        Optionally scoped to a project's available assignees context.
        """
        if project_id is not None:
            from urllib.parse import quote

            filters = quote(
                json.dumps([{"member": {"operator": "=", "values": [str(project_id)]}}])
            )
            return await self._request("GET", f"/principals?filters={filters}")
        return await self._request("GET", "/principals")

    async def get_placeholder_users(self, filters: str | None = None) -> dict:
        """List placeholder users."""
        if filters:
            from urllib.parse import quote as _quote

            return await self._request(
                "GET", f"/placeholder_users?filters={_quote(filters)}"
            )
        return await self._request("GET", "/placeholder_users")

    async def get_placeholder_user(self, placeholder_id: int) -> dict:
        """Get a single placeholder user by ID."""
        return await self._request("GET", f"/placeholder_users/{placeholder_id}")

    async def create_placeholder_user(self, name: str) -> dict:
        """Create a placeholder user. Only requires a name."""
        return await self._request("POST", "/placeholder_users", {"name": name})

    async def update_placeholder_user(self, placeholder_id: int, name: str) -> dict:
        """Update a placeholder user's name."""
        current = await self.get_placeholder_user(placeholder_id)
        lock_version = current.get("lockVersion", 0)
        return await self._request(
            "PATCH",
            f"/placeholder_users/{placeholder_id}",
            {"name": name, "lockVersion": lock_version},
        )

    async def delete_placeholder_user(self, placeholder_id: int) -> bool:
        """Delete a placeholder user (admin only)."""
        await self._request("DELETE", f"/placeholder_users/{placeholder_id}")
        return True

    async def get_my_preferences(self) -> dict:
        """Get the authenticated user's notification and UI preferences."""
        return await self._request("GET", "/my_preferences")

    async def update_my_preferences(self, data: dict) -> dict:
        """Update the authenticated user's preferences."""
        payload: dict = {}
        for field in (
            "timeZone",
            "wantsNewsletters",
            "commentSortDescending",
            "warnOnLeavingUnsaved",
            "autoHidePopups",
            "pauseReminders",
        ):
            if field in data:
                payload[field] = data[field]
        if "daily_reminders" in data:
            payload["dailyReminders"] = data["daily_reminders"]
        if "immediate_reminders" in data:
            payload["immediateReminders"] = data["immediate_reminders"]
        return await self._request("PATCH", "/my_preferences", payload)

    # ── Phase F: documents ──────────────────────────────────────────────────

    async def get_documents(self, filters: str | None = None) -> dict:
        """List documents. No POST endpoint exists in v3 (creation is form-only)."""
        if filters:
            from urllib.parse import quote as _q

            return await self._request("GET", f"/documents?filters={_q(filters)}")
        return await self._request("GET", "/documents")

    async def get_document(self, document_id: int) -> dict:
        """Get a document by ID."""
        return await self._request("GET", f"/documents/{document_id}")

    async def update_document(self, document_id: int, data: dict) -> dict:
        """Update a document's title or description."""
        current = await self.get_document(document_id)
        lock_version = current.get("lockVersion", 0)
        payload: dict = {"lockVersion": lock_version}
        if "title" in data:
            payload["title"] = data["title"]
        if "description" in data:
            payload["description"] = {"raw": data["description"]}
        return await self._request("PATCH", f"/documents/{document_id}", payload)

    # ── Phase F: storages ───────────────────────────────────────────────────

    async def get_storages(self) -> dict:
        """List configured file storages (OneDrive, Nextcloud, etc.)."""
        return await self._request("GET", "/storages")

    async def get_storage(self, storage_id: int) -> dict:
        """Get a single file storage by ID."""
        return await self._request("GET", f"/storages/{storage_id}")

    async def get_project_storages(self, project_id: int | None = None) -> dict:
        """List project-storage links (which storages are linked to which projects)."""
        if project_id is not None:
            from urllib.parse import quote as _q

            filters = _q(
                json.dumps(
                    [{"projectId": {"operator": "=", "values": [str(project_id)]}}]
                )
            )
            return await self._request("GET", f"/project_storages?filters={filters}")
        return await self._request("GET", "/project_storages")

    async def get_project_storage(self, project_storage_id: int) -> dict:
        """Get a single project-storage link by ID."""
        return await self._request("GET", f"/project_storages/{project_storage_id}")

    # ── Phase F: file links ─────────────────────────────────────────────────

    async def get_work_package_file_links(self, work_package_id: int) -> dict:
        """List file links attached to a work package."""
        return await self._request(
            "GET", f"/work_packages/{work_package_id}/file_links"
        )

    async def get_file_link(self, file_link_id: int) -> dict:
        """Get a single file link by ID."""
        return await self._request("GET", f"/file_links/{file_link_id}")

    async def create_file_links(
        self, work_package_id: int, storage_id: int, file_infos: list[dict]
    ) -> dict:
        """Create file links on a work package.

        Each item in file_infos should have: originId (str), name (str),
        mimeType (str), createdAt (ISO str), lastModifiedAt (ISO str),
        fileSize (int), originData (dict).
        """
        elements = [
            {
                "_type": "FileLink",
                "originData": f.get("originData", {}),
                "storageUrl": f.get("storageUrl", ""),
                "_links": {
                    "storageUrl": {"href": f.get("storageUrl", "")},
                    "storage": {"href": f"/api/v3/storages/{storage_id}"},
                },
                **{k: v for k, v in f.items() if k not in ("originData", "storageUrl")},
            }
            for f in file_infos
        ]
        payload = {"_type": "Collection", "_embedded": {"elements": elements}}
        return await self._request(
            "POST", f"/work_packages/{work_package_id}/file_links", payload
        )

    async def delete_file_link(self, file_link_id: int) -> bool:
        """Delete a file link by ID."""
        await self._request("DELETE", f"/file_links/{file_link_id}")
        return True

    # ── Phase G: categories ─────────────────────────────────────────────────

    async def get_project_categories(self, project_id: int) -> dict:
        """List categories for a project."""
        return await self._request("GET", f"/projects/{project_id}/categories")

    async def get_category(self, category_id: int) -> dict:
        """Get a single category by ID."""
        return await self._request("GET", f"/categories/{category_id}")

    # ── Phase G: views ──────────────────────────────────────────────────────

    async def get_views(self) -> dict:
        """List all saved views."""
        return await self._request("GET", "/views")

    async def get_view(self, view_id: int) -> dict:
        """Get a single view by ID."""
        return await self._request("GET", f"/views/{view_id}")
