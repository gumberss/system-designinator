from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

SERVICE_URLS = {
    "identity": "IDENTITY_URL",
    "destination": "DESTINATION_URL",
    "flights": "FLIGHT_URL",
    "hotels": "HOTEL_URL",
    "activities": "ACTIVITIES_URL",
    "booking": "BOOKING_URL",
    "search": "SEARCH_URL",
    "ai": "AI_GATEWAY_URL",
}
FORWARDED_HEADERS = {
    "authorization",
    "content-type",
    "accept",
    "idempotency-key",
    "x-admin-token",
    "last-event-id",
    "mcp-method",
    "mcp-name",
    "mcp-protocol-version",
    "mcp-session-id",
}

app = FastAPI(title="Marketplace API Gateway", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "gateway", "status": "ok"}


@app.api_route(
    "/api/{service}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def route_request(service: str, path: str, request: Request) -> Response:
    environment_key = SERVICE_URLS.get(service)
    if environment_key is None:
        raise HTTPException(status_code=404, detail="Unknown marketplace service")
    base_url = os.getenv(environment_key)
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail=f"Service route {service!r} is not configured",
        )

    forwarded_headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in FORWARDED_HEADERS
    }
    upstream_path = path
    if path == "search" and service in {"flights", "hotels", "activities"}:
        upstream_path = f"{service}/{path}"
    upstream_url = f"{base_url.rstrip('/')}/{upstream_path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"
    try:
        timeout = httpx.Timeout(60.0 if service == "ai" else 8.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.request(
                request.method,
                upstream_url,
                content=await request.body(),
                headers=forwarded_headers,
            )
    except httpx.TimeoutException as error:
        raise HTTPException(
            status_code=504,
            detail="Marketplace service timed out",
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=503,
            detail="Marketplace service is unavailable",
        ) from error

    response_headers = {}
    content_type = upstream.headers.get("content-type")
    if content_type:
        response_headers["content-type"] = content_type
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )
