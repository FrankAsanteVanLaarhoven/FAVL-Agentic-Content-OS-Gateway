"""Guarded outbound HTTP.

Every outbound request from an adapter goes through here. Redirects are
followed manually so each hop is revalidated — httpx's own redirect handling
would resolve and connect to the new location without any of these checks,
which is a standard SSRF bypass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .ssrf import OutboundPolicy, SSRFBlocked, validate_url

logger = logging.getLogger(__name__)

# Never forwarded to a provider, whatever the caller supplied.
STRIPPED_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "host",
    }
)


class ResponseTooLarge(Exception):
    pass


class ContentTypeRejected(Exception):
    pass


class TooManyRedirects(Exception):
    pass


@dataclass
class OutboundResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]
    final_url: str
    provider_request_id: str | None
    redirects: int


def sanitise_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Drop hop-by-hop headers and anything carrying inbound credentials.

    Automatic credential forwarding is how an internal token ends up at a
    third-party provider.
    """
    if not headers:
        return {}
    return {
        k: v for k, v in headers.items() if k.lower() not in STRIPPED_REQUEST_HEADERS
    }


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read the body, aborting as soon as the cap is passed.

    Content-Length is advisory; a provider can lie or stream indefinitely, so
    the cap is enforced on bytes actually received.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(f"response exceeded {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    policy: OutboundPolicy,
    *,
    json_body: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    connect_timeout: float = 5.0,
    read_timeout: float = 10.0,
    total_timeout: float = 15.0,
) -> OutboundResponse:
    """Perform one guarded request, revalidating every redirect hop."""
    safe_headers = sanitise_headers(headers)
    current_url = url
    redirects = 0

    while True:
        target = validate_url(current_url, policy)

        timeout = httpx.Timeout(
            total_timeout, connect=connect_timeout, read=read_timeout
        )
        send_headers = dict(safe_headers)
        # Host and SNI carry the real name; the connection goes to the address
        # already validated, closing the DNS rebinding window.
        send_headers["Host"] = (
            target.host if target.port in (80, 443) else f"{target.host}:{target.port}"
        )

        response = await client.request(
            method,
            target.pinned_url,
            headers=send_headers,
            json=json_body,
            content=content,
            timeout=timeout,
            follow_redirects=False,
            extensions={"sni_hostname": target.host},
        )

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise SSRFBlocked("redirect_without_location", target.host)
            redirects += 1
            if redirects > policy.max_redirects:
                raise TooManyRedirects(f"exceeded {policy.max_redirects} redirects")
            current_url = str(httpx.URL(current_url).join(location))
            logger.info(
                "outbound.redirect from=%s to=%s hop=%d",
                target.host,
                httpx.URL(current_url).host,
                redirects,
            )
            continue

        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type and content_type not in policy.allowed_content_types:
            await response.aclose()
            raise ContentTypeRejected(content_type)

        try:
            body = await _read_capped(response, policy.max_response_bytes)
        finally:
            await response.aclose()

        return OutboundResponse(
            status_code=response.status_code,
            body=body,
            headers=dict(response.headers),
            final_url=current_url,
            provider_request_id=(
                response.headers.get("x-request-id")
                or response.headers.get("x-correlation-id")
            ),
            redirects=redirects,
        )
