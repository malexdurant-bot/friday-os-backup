"""Optional temporary ingress: expose only signed Slack events, never the API."""
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/slack/events")
async def forward_slack(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            return JSONResponse(status_code=413, content={"error": "event_too_large"})
    headers = {name: request.headers[name] for name in (
        "x-slack-signature", "x-slack-request-timestamp", "content-type",
    ) if name in request.headers}
    if "x-slack-signature" not in headers or "x-slack-request-timestamp" not in headers:
        return JSONResponse(status_code=401, content={"error": "signature_required"})
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2.5) as client:
            reply = await client.post("http://127.0.0.1:8129/slack/events",
                                      content=bytes(body), headers=headers)
        return Response(content=reply.content, status_code=reply.status_code,
                        media_type="application/json")
    except httpx.HTTPError:
        return JSONResponse(status_code=503, content={"error": "runtime_unavailable"})
