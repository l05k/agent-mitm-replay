"""Record and replay model HTTP responses for Harbor agents."""

import json
import hmac
import os
import re
import time
import gzip
import zlib
from collections import defaultdict, deque

from mitmproxy import ctx, http, io



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



def _complete_sse(response: http.Response, path: str) -> bool:
    """Require a dispatched terminal SSE event, not just an HTTP 200."""
    if not 200 <= response.status_code < 300:
        return False
    if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
        return False
    try:
        raw = response.raw_content or b""
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding in {"gzip", "x-gzip"}:
            # mitmproxy's decoder tolerates missing gzip trailers.
            body = gzip.decompress(raw)
        elif encoding == "deflate":
            try:
                body = zlib.decompress(raw)
            except zlib.error:
                body = zlib.decompress(raw, -zlib.MAX_WBITS)
        elif encoding == "identity":
            body = raw
        else:
            return False  # Do not recover encodings we cannot strictly validate.
    except (ValueError, OSError, EOFError, zlib.error):
        return False
    data = []
    complete = False
    lines = re.split(rb"\r\n|\r|\n", body)
    for line in lines[:-1]:
        if not line:
            if data:
                payload = b"\n".join(data)
                complete = payload == b"[DONE]"
                if path.endswith("/responses"):
                    try:
                        complete = json.loads(payload).get("type") == "response.completed"
                    except (ValueError, AttributeError):
                        complete = False
                data = []
        elif line.startswith(b"data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(b" ") else value)
    return complete and not data and not lines[-1]


def _error_response(error_type: str, message: str) -> http.Response:
    return http.Response.make(
        400, json.dumps({"error": {"type": error_type, "message": message}}), {"content-type": "application/json"}
    )

class ReplayAddon:
    def __init__(self) -> None:
        self.recorded = defaultdict(deque)
        self.audit = self.capture = None
        self.requests = self.matched = self.misses = self.errors = self.total = 0
        self.streams: dict[str, bytearray] = {}
        self.tool_log = None

    def load(self, loader) -> None:
        loader.add_option("amr_mode", str, "record", "record or replay", choices=["record", "replay"])
        loader.add_option("amr_flows", str, "", "Recorded flows.mitm to replay")
        loader.add_option("amr_audit", str, "", "JSONL audit file")
        loader.add_option("amr_model_path", str, "", "Exact POST route for model inference")
        loader.add_option("amr_codex_tools", str, "", "Local Codex native tool timing JSONL")

    def configure(self, updated) -> None:
        if "amr_codex_tools" in updated and ctx.options.amr_codex_tools:
            self.tool_log = open(ctx.options.amr_codex_tools, "w", encoding="utf-8")
            self.tool_log.write('{"event":"otel"}\n')
            self.tool_log.flush()
        if "amr_audit" in updated and ctx.options.amr_audit:
            self.audit = open(ctx.options.amr_audit, "w", encoding="utf-8")
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
        if self.tool_log is not None and flow.request.method == "POST" and flow.request.path == "/amr-logs":
            # Native telemetry is local-only; never forward or count it as a
            # model exchange, including during offline replay.
            flow.response = _error_response("invalid_telemetry", "invalid native tool timing payload")
            payload = json.loads(flow.request.get_text())
            for resource in payload.get("resourceLogs", []):
                for scope in resource.get("scopeLogs", []):
                    for record in scope.get("logRecords", []):
                        attrs = {item["key"]: next(iter(item["value"].values()))
                                 for item in record.get("attributes", [])}
                        if attrs.get("event.name") != "codex.tool_result":
                            continue
                        seq = attrs.get("tool_result_seq")
                        event = {"seq": int(seq) if seq is not None else None, "id": attrs["call_id"],
                                 "duration_ns": int(attrs["duration_ms"]) * 1_000_000}
                        self.tool_log.write(json.dumps(event) + "\n")
            self.tool_log.flush()
            flow.response = http.Response.make(200, b"{}", {"content-type": "application/json"})
            return
        if flow.request.method != "POST" or flow.request.path != ctx.options.amr_model_path:
            # Codex probes optional billing metadata even with analytics disabled.
            # It is not inference and must not reach the provider or the cassette.
            flow.response = http.Response.make(404)
            return
        self.requests += 1
        flow.metadata["amr_turn"] = self.requests
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
        if (not self._replaying() and "amr_turn" in flow.metadata
                and flow.response.headers.get("content-type", "").lower().startswith("text/event-stream")):
            body = self.streams[flow.id] = bytearray()

            def capture(chunk: bytes) -> bytes:
                # mitmproxy only assigns raw_content at HTTP EOF. A client can
                # cancel after the SSE terminal event but before that EOF.
                body.extend(chunk)
                return chunk

            flow.response.stream = capture

    def response(self, flow: http.HTTPFlow) -> None:
        self.streams.pop(flow.id, None)
        if flow.metadata.get("amr_recorded"):
            return
        if flow.request.method != "POST" or flow.request.path != ctx.options.amr_model_path:
            return
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
        flow.metadata["amr_recorded"] = True
        if self.capture is not None:
            flow.request.headers.pop("authorization", None)
            io.FlowWriter(self.capture).add(flow)
        self._log("response")

    def error(self, flow: http.HTTPFlow) -> None:
        body = self.streams.pop(flow.id, None)
        if "amr_turn" not in flow.metadata or flow.metadata.get("amr_recorded"):
            return
        message = flow.error.msg if flow.error else ""
        # HTTP/2 uses the same CANCEL text for both peers, so it cannot prove
        # a normal client cancellation. Keep ambiguous resets failing.
        client_closed = message == "Client disconnected."
        if client_closed and flow.response is not None:
            candidate = flow.response.copy()
            if body is not None:
                candidate.raw_content = bytes(body)
            if _complete_sse(candidate, flow.request.path):
                if self._replaying():
                    if flow.is_replay == "response":
                        return  # Already counted as a hit; the full body was served.
                else:
                    # Persist the bytes seen by the stream hook, never an empty
                    # or stale body from the aborted HTTP response.
                    candidate.headers.pop("transfer-encoding", None)
                    candidate.headers["content-length"] = str(len(candidate.raw_content or b""))
                    candidate.timestamp_end = time.time()
                    flow.response = candidate
                    flow.error = None
                    self.response(flow)
                    return
        self.errors += 1
        self._log("error")

    def done(self) -> None:
        self._write({
            "event": "summary", "matched": self.matched, "misses": self.misses,
            "remaining": self.total - self.matched if self._replaying() else 0,
            "total": self.total, "requests": self.requests, "errors": self.errors,
        })
        if self.audit is not None:
            self.audit.close()
            self.audit = None
        if self.capture is not None:
            self.capture.close()
        if self.tool_log is not None:
            self.tool_log.close()
            self.tool_log = None

addons = [ReplayAddon()]
