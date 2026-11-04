"""Pure ASGI response filter: every JSON body the /api/v1 app sends goes through ``services._scrub.scrub``.

It sits on the app, not in the services, so a service (or a future router) that forgets to mask a free-form
field still cannot leak a credential. Bodies are small (list pages, a capped analysis state), so the response
is buffered, parsed, scrubbed and re-serialised; a body that is not valid JSON is passed through untouched."""
import json

from ..services._scrub import scrub


def _content_type(headers) -> str:
    for name, value in headers:
        if name.lower() == b"content-type":
            return value.decode("latin-1").lower()
    return ""


class ScrubMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        held_start = None
        chunks = []

        async def filtering_send(message):
            nonlocal held_start
            if message["type"] == "http.response.start" and "application/json" in _content_type(message["headers"]):
                held_start = message                    # wait for the whole body
                return
            if held_start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            try:
                body = json.dumps(scrub(json.loads(body)), ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8")
            except ValueError:
                pass                                    # not JSON after all: send it as it was
            headers = [(k, v) for k, v in held_start["headers"] if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
            await send({**held_start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, filtering_send)
