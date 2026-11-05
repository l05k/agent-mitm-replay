"""mitmproxy addon that records or replays model calls (``mitmdump -s addon.py``).

Options, set by ``runner.py`` with ``--set``:

``amr_mode``   ``record`` or ``replay``.
``amr_flows``  The recorded ``flows.mitm`` (replay only; recording uses ``-w``).
``amr_audit``  JSONL file with one line per model call and a final summary.

Replay is a fixed response sequence: the first request gets the first recorded
response, the second the second, and so on. Method, URL and body are irrelevant;
each recorded response is served once, in cassette order. A request after the
last response is a *miss*: it gets a local HTTP 400 and never reaches the
network.

Redirects (3xx with ``Location``) count as errors, so the run fails. The
recorded redirect is consumed and the client gets a local 400 instead, so it
cannot follow the redirect to the provider. In record the redirect has already
reached the client; the failed run writes no manifest, so that cassette cannot
be replayed.

Audit lines: ``{"event": "response"|"hit"|"miss"|"error", "turn": n}``, then
``{"event": "summary", "matched", "misses", "remaining", "total", "requests",
"errors"}``. ``remaining`` counts recorded responses that were never served.
"""

import json
from collections import deque

from mitmproxy import ctx, http, io

REDIRECTS = {301, 302, 303, 307, 308}

def _is_redirect(response: http.Response) -> bool:
    """The model client would follow it, leaving the proxy."""
    return response.status_code in REDIRECTS and "location" in response.headers

def _error_response(error_type: str, message: str) -> http.Response:
    return http.Response.make(
        400, json.dumps({"error": {"type": error_type, "message": message}}), {"content-type": "application/json"}
    )

class ReplayAddon:
    def __init__(self) -> None:
        self.recorded: deque[http.Response] = deque()
        self.audit = None
        self.requests = self.matched = self.misses = self.errors = self.total = 0

    def load(self, loader) -> None:
        loader.add_option("amr_mode", str, "record", "record or replay", choices=["record", "replay"])
        loader.add_option("amr_flows", str, "", "Recorded flows.mitm to replay")
        loader.add_option("amr_audit", str, "", "JSONL audit file")

    def configure(self, updated) -> None:
        if "amr_audit" in updated and ctx.options.amr_audit:
            self.audit = open(ctx.options.amr_audit, "w", encoding="utf-8")
        if "amr_flows" in updated and ctx.options.amr_mode == "replay":
            flows = [
                flow for flow in io.read_flows_from_paths([ctx.options.amr_flows])
                if isinstance(flow, http.HTTPFlow) and flow.response is not None
            ]
            self.recorded = deque(flow.response for flow in flows)
            self.total = len(self.recorded)

    def _replaying(self) -> bool:
        return ctx.options.amr_mode == "replay"

    def _log(self, event: str) -> None:
        self._write({"event": event, "turn": self.requests})

    def _write(self, record: dict) -> None:
        if self.audit is not None:
            self.audit.write(json.dumps(record) + "\n")
            self.audit.flush()

    def request(self, flow: http.HTTPFlow) -> None:
        self.requests += 1
        if not self._replaying():
            return
        if not self.recorded:
            self.misses += 1
            flow.response = _error_response(
                "replay_miss", f"the cassette has no response left for {flow.request.method} {flow.request.url}"
            )
            self._log("miss")
            return
        recorded = self.recorded.popleft()
        if _is_redirect(recorded):
            self.errors += 1
            flow.response = _error_response("unsafe_redirect", "the cassette holds a redirect; refusing to serve it")
            self._log("error")
            return
        flow.response = recorded.copy()
        flow.is_replay = "response"
        self.matched += 1
        self._log("hit")

    def response(self, flow: http.HTTPFlow) -> None:
        if self._replaying():
            return
        self.total += 1
        if _is_redirect(flow.response):
            # The client may already be following it; fail the recording so it is never replayed.
            self.errors += 1
            self._log("error")
            return
        self._log("response")

    def error(self, flow: http.HTTPFlow) -> None:
        self.errors += 1
        self._log("error")

    def done(self) -> None:
        self._write({
            "event": "summary", "matched": self.matched, "misses": self.misses,
            "remaining": len(self.recorded) if self._replaying() else 0,
            "total": self.total, "requests": self.requests, "errors": self.errors,
        })
        if self.audit is not None:
            self.audit.close()
            self.audit = None

addons = [ReplayAddon()]
