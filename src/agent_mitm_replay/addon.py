"""mitmproxy addon: replay model messages and collect Codex native OTLP logs."""

import json
import hmac
import os
from collections import defaultdict, deque
from time import time_ns

from mitmproxy import ctx, http, io

from agent_mitm_replay.trace import codex_event, codex_log_records


def request_key(request: http.Request) -> tuple:
    """Match the model route and conversation structure, not generated text/IDs."""
    if request.method != "POST":
        return request.method, request.path
    body = json.loads(request.get_text())
    history = body.get("input", body.get("messages", []))
    shape = tuple((item.get("role"), item.get("type"), item.get("name"),
                   tuple(tool["function"]["name"] for tool in item.get("tool_calls", [])))
                  for item in history) if isinstance(history, list) else ("text",)
    return request.method, request.path, body["model"], bool(body.get("stream")), shape

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
        self.recorded = defaultdict(deque)
        self.audit = self.telemetry = self.capture = None
        self.requests = self.matched = self.misses = self.errors = self.total = 0
        self.telemetry_started = False
        self.model_spans = []

    def load(self, loader) -> None:
        loader.add_option("amr_mode", str, "record", "record or replay", choices=["record", "replay"])
        loader.add_option("amr_flows", str, "", "Recorded flows.mitm to replay")
        loader.add_option("amr_audit", str, "", "JSONL audit file")
        loader.add_option("amr_telemetry", str, "", "Codex OTLP log file")
        loader.add_option("amr_model_path", str, "", "Exact POST route for model inference")

    def configure(self, updated) -> None:
        if "amr_audit" in updated and ctx.options.amr_audit:
            self.audit = open(ctx.options.amr_audit, "w", encoding="utf-8")
        if "amr_telemetry" in updated and ctx.options.amr_telemetry:
            self.telemetry = open(ctx.options.amr_telemetry, "w", encoding="utf-8")
        if "amr_flows" in updated and ctx.options.amr_flows:
            if not self._replaying():
                self.capture = open(ctx.options.amr_flows, "wb")
                return
            for flow in io.read_flows_from_paths([ctx.options.amr_flows]):
                self.recorded[request_key(flow.request)].append(flow.response)
                self.total += 1

    def _replaying(self) -> bool:
        return ctx.options.amr_mode == "replay"

    def _log(self, event: str) -> None:
        self._write({"event": event, "turn": self.requests})

    def _write(self, record: dict) -> None:
        if self.audit is not None:
            self.audit.write(json.dumps(record) + "\n")
            self.audit.flush()

    def request(self, flow: http.HTTPFlow) -> None:
        token = os.getenv("AMR_TRIAL_TOKEN")
        if token and not hmac.compare_digest(flow.request.headers.get("authorization", ""), f"Bearer {token}"):
            flow.response = _error_response("unauthorized", "wrong trial token")
            return
        if self.telemetry is not None and flow.request.path == "/amr-logs":
            if not self.telemetry_started:
                self.telemetry.write('{"event": "otel"}\n')
                self.telemetry_started = True
            for record in codex_log_records(json.loads(flow.request.get_text())):
                if (event := codex_event(record)) is not None:
                    self.telemetry.write(json.dumps(event) + "\n")
            self.telemetry.flush()
            flow.response = http.Response.make(200, b"{}", {"content-type": "application/json"})
            return
        if flow.request.method != "POST" or flow.request.path != ctx.options.amr_model_path:
            # Codex probes optional billing metadata even with analytics disabled.
            # It is not inference and must not reach the provider or the cassette.
            flow.response = http.Response.make(404)
            return
        self.requests += 1
        flow.metadata["amr_start_ns"] = time_ns()
        if not self._replaying():
            if key := os.getenv("AMR_UPSTREAM_KEY"):
                flow.request.headers["authorization"] = f"Bearer {key}"
            return
        try:
            candidates = self.recorded.get(request_key(flow.request))
        except (KeyError, TypeError, ValueError):
            candidates = None
        if not candidates:
            self.misses += 1
            flow.response = _error_response("replay_miss", "no recorded response for this model turn")
            self._log("miss")
            return
        recorded = candidates.popleft()
        if _is_redirect(recorded) or recorded.status_code == 101:
            self.errors += 1
            kind = "unsafe_redirect" if _is_redirect(recorded) else "unsupported_websocket"
            flow.response = _error_response(kind, "the cassette holds a redirect or WebSocket upgrade")
            self._log("error")
            return
        flow.response = recorded.copy()
        flow.is_replay = "response"
        self.matched += 1
        self._log("hit")

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if not self._replaying() and flow.response.headers.get("content-type", "").startswith("text/event-stream"):
            flow.response.stream = True

    def response(self, flow: http.HTTPFlow) -> None:
        if "amr_start_ns" not in flow.metadata:
            return  # local telemetry, not a model exchange
        self.model_spans.append([flow.metadata["amr_start_ns"], time_ns()])
        if self._replaying():
            return
        self.total += 1
        if _is_redirect(flow.response) or flow.response.status_code == 101 or flow.response.status_code >= 400:
            self.errors += 1
            detail = {}
            if flow.response.headers.get("content-type", "").startswith("application/json"):
                try:
                    error = json.loads(flow.response.get_text()).get("error")
                    if isinstance(error, dict):
                        detail = {key: str(error[key])[:1000] for key in ("message", "type", "code")
                                  if key in error}
                except (ValueError, AttributeError):
                    pass  # Preserve the HTTP failure even if its body is malformed.
            self._write({"event": "error", "turn": self.requests,
                         "method": flow.request.method, "path": flow.request.path,
                         "status": flow.response.status_code, "upstream_error": detail})
            return
        if self.capture is not None:
            flow.request.headers.pop("authorization", None)
            io.FlowWriter(self.capture).add(flow)
        self._log("response")

    def error(self, flow: http.HTTPFlow) -> None:
        self.errors += 1
        self._log("error")

    def done(self) -> None:
        self._write({
            "event": "summary", "matched": self.matched, "misses": self.misses,
            "remaining": self.total - self.matched if self._replaying() else 0,
            "total": self.total, "requests": self.requests, "errors": self.errors,
            "model_spans": self.model_spans,
        })
        if self.audit is not None:
            self.audit.close()
            self.audit = None
        for handle in (self.capture, self.telemetry):
            if handle is not None:
                handle.close()

addons = [ReplayAddon()]
