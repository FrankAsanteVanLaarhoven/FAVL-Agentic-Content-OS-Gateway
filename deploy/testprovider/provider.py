"""Development-only provider for M1.3 evidence.

Not part of the platform. Started only under the `dev` compose profile. It
exists so adapter behaviour is proven against a real socket rather than a
mock: timeouts, oversized responses, redirects and webhook signatures all
behave differently in practice than they do against a stub.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SIGNING_SECRET = os.getenv("WEBHOOK_SIGNING_KEY", "dev-signing-key")
DELIVERIES: list[dict] = []


def expected_signature(event_id: str, timestamp: str, body: bytes) -> str:
    signed = b".".join([event_id.encode(), timestamp.encode(), body])
    return "v1=" + hmac.new(SIGNING_SECRET.encode(), signed, hashlib.sha256).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        print("provider:", fmt % args, flush=True)

    def _send(self, code: int, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", f"provider-{int(time.time() * 1000)}")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def do_GET(self):  # noqa: N802
        route = urlparse(self.path)
        query = parse_qs(route.query)

        if route.path in ("/health/live", "/health"):
            return self._send(200, {"status": "live"})
        if route.path == "/large":
            size = int(query.get("bytes", ["4000000"])[0])
            return self._send(200, b'{"pad":"' + b"x" * size + b'"}')
        if route.path == "/deliveries":
            return self._send(200, {"count": len(DELIVERIES), "deliveries": DELIVERIES})
        if route.path == "/redirect":
            # Points at cloud metadata: the guard must refuse the hop.
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        route = urlparse(self.path)
        query = parse_qs(route.query)
        body = self._read_body()

        if route.path == "/slow":
            time.sleep(float(query.get("seconds", ["30"])[0]))
            return self._send(200, {"slept": True})

        if route.path == "/fail":
            return self._send(int(query.get("code", ["500"])[0]), {"error": "provider failure"})

        if route.path == "/webhook":
            event_id = self.headers.get("X-FAVL-Event-ID", "")
            timestamp = self.headers.get("X-FAVL-Timestamp", "")
            provided = self.headers.get("X-FAVL-Signature", "")
            attempt = self.headers.get("X-FAVL-Delivery-Attempt", "")
            valid = hmac.compare_digest(
                expected_signature(event_id, timestamp, body), provided
            )
            DELIVERIES.append(
                {
                    "event_id": event_id,
                    "timestamp": timestamp,
                    "attempt": attempt,
                    "signature_valid": valid,
                    "has_authorization_header": "authorization" in {
                        k.lower() for k in self.headers.keys()
                    },
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            if not valid:
                return self._send(401, {"error": "invalid signature"})
            return self._send(200, {"received": True, "event_id": event_id})

        if route.path in ("/echo", "/extract"):
            try:
                parsed = json.loads(body) if body else {}
            except ValueError:
                parsed = {"raw": body.decode("utf-8", "replace")}
            return self._send(
                200,
                {
                    "echoed": parsed,
                    "idempotency_key": self.headers.get("X-FAVL-Idempotency-Key"),
                    "invocation_id": self.headers.get("X-FAVL-Invocation-ID"),
                    "saw_authorization": "authorization"
                    in {k.lower() for k in self.headers.keys()},
                },
            )

        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "9099"))
    print(f"provider: listening on {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
