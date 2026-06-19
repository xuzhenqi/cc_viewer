"""FastAPI proxy that dumps every request and forwards it to the upstream endpoint.

Key design choices:
- Catch-all routes across HTTP methods so any Anthropic API path is captured.
- Streaming passthrough using httpx + FastAPI StreamingResponse, so SSE/streaming
  responses work end-to-end (Claude Code streams nearly every response).
- Headers forwarded verbatim, except hop-by-hop headers (host, content-length, ...).
- Per-request atomic dump to data/ before forwarding, so we never lose a request
  even if upstream fails.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .dump import dump_request


HOP_BY_HOP = frozenset({
    "host", "content-length", "connection", "transfer-encoding",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "upgrade",
})


class ProxyState:
    def __init__(self) -> None:
        self.upstream_url: str | None = None
        self.client: httpx.AsyncClient | None = None
        self._counter_lock = threading.Lock()
        self._counter = 0

    def configure(self, upstream: str) -> None:
        self.upstream_url = upstream
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def next_counter(self) -> int:
        with self._counter_lock:
            self._counter += 1
            return self._counter


state = ProxyState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await state.aclose()


app = FastAPI(title="Claude Code Capture Proxy", lifespan=lifespan)


async def _handle(request: Request, full_path: str) -> Response:
    if state.upstream_url is None or state.client is None:
        return JSONResponse({"error": "upstream not configured"}, status_code=500)

    body_bytes = await request.body()

    incoming_headers = {k: v for k, v in request.headers.items()}
    n = state.next_counter()
    dump_path = dump_request(
        counter=n,
        method=request.method,
        path=full_path,
        query=request.url.query,
        headers=incoming_headers,
        body_bytes=body_bytes,
        upstream_url=state.upstream_url,
    )
    print(f"[{n:05d}] {request.method} /{full_path} -> {dump_path.name}", flush=True)

    upstream_url = state.upstream_url.rstrip("/") + request.url.path
    if request.url.query:
        upstream_url += "?" + request.url.query

    forward_headers = {
        k: v for k, v in incoming_headers.items() if k.lower() not in HOP_BY_HOP
    }

    try:
        upstream_req = state.client.build_request(
            method=request.method,
            url=upstream_url,
            headers=forward_headers,
            content=body_bytes,
        )
        upstream_resp = await state.client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        return JSONResponse(
            {"error": f"upstream error: {e!r}"},
            status_code=502,
        )

    response_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP
    }

    async def stream_body():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()

    if upstream_resp.is_stream_consumed:
        body = await upstream_resp.aread()
        return Response(content=body, status_code=upstream_resp.status_code, headers=response_headers)

    return StreamingResponse(
        stream_body(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
    )


@app.post("/{full_path:path}")
@app.get("/{full_path:path}")
@app.put("/{full_path:path}")
@app.delete("/{full_path:path}")
@app.patch("/{full_path:path}")
async def proxy(full_path: str, request: Request):
    return await _handle(request, full_path)


@app.api_route("/", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_root(request: Request):
    return await _handle(request, "")