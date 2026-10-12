"""mitmproxy addon that audits recorded flows and replays them offline.

The addon is loaded by ``mitmdump -s addon.py`` and driven by three options:

``hrr_mode``
    ``record``: mitmdump records the flows itself (``-w flows.mitm``); the
    addon only writes one audit line per completed response.
    ``replay``: the addon loads the recorded flows, answers every request from
    the cassette and never lets a request reach the network. A request without
    a recorded response is answered with a synthetic 400.

``hrr_flows``
    Absolute path of the recorded ``flows.mitm`` file (required in replay).

``hrr_audit``
    Absolute path of the JSONL audit file (required in both modes).

Every audit line is flushed immediately and carries ``event``,
``response_index``, ``request_sha256``, ``response_sha256``, ``status_code``
and ``is_replay``:

``response``
    record: one line per completed response; ``response_index`` is the index of
    that response in the recorded file, counting only flows that have one.
``hit``
    replay: the request was served from the cassette; ``response_index`` is the
    index of the consumed record in the cassette file order (same numbering as
    above) and the hashes are the ones of the recorded request and response.
``miss``
    replay: no record left for this request, answered with 400.
``error``
    the flow failed; the line carries an extra ``error`` string.
``summary``
    written by ``done()`` with ``matched``, ``misses``, ``remaining``,
    ``total``, ``requests`` and ``errors``.

Matching and consumption are borrowed from mitmproxy's ``ServerPlayback`` so
that a recorded response is consumed exactly once, in recording order, per
request hash bucket. The ``server_replay_*`` options that drive that matching
come from mitmproxy's ``serverplayback`` addon (part of the default addon set),
and the built-in ``server_replay`` machinery itself stays off: this addon owns
the request hook, so a miss can never fall through to the network.
"""

import hashlib
import json
import logging
import os
from typing import IO
from typing import Any
from typing import Optional

from mitmproxy import ctx
from mitmproxy import exceptions
from mitmproxy import http
from mitmproxy import io as mitmproxy_io
from mitmproxy.addons.serverplayback import ServerPlayback

logger = logging.getLogger(__name__)

MODE_RECORD = "record"
MODE_REPLAY = "replay"

# ``ServerPlayback._hash()`` and ``ServerPlayback.next_flow()`` read the
# ``server_replay_*`` options. mitmproxy's ``serverplayback`` addon registers
# them in every stock mitmdump run, so this addon does not touch them -- it only
# insists that they stay strict: no response reuse, bodies matched as recorded.


def _sha256(data: bytes | None) -> str:
    """SHA-256 of a raw message body; a missing body hashes as an empty one."""
    return hashlib.sha256(data if data is not None else b"").hexdigest()


def _raw_content(message: http.Message | None) -> bytes | None:
    return message.raw_content if message is not None else None


def _request_line(flow: http.HTTPFlow) -> str:
    request = flow.request
    if request is None:
        return "<no request>"
    return f"{request.method} {request.url}"


def _error_response(status_code: int, error_type: str, message: str) -> http.Response:
    body = json.dumps({"error": {"type": error_type, "message": message}})
    return http.Response.make(
        status_code, body.encode("utf-8"), {"content-type": "application/json"}
    )


class ReplayAddon:
    """Record audit and offline replay for Harbor model calls."""

    name = "harbor_mitm_replay"

    def __init__(self) -> None:
        self._mode = MODE_RECORD
        self._audit: IO[str] | None = None
        self._audit_path: str | None = None
        self._audit_missing_logged = False
        self._playback: ServerPlayback | None = None
        self._flows_path: str | None = None
        self._flows: list[http.HTTPFlow] = []
        self._flow_indices: dict[int, int] = {}
        self._requests = 0
        self._matched = 0
        self._misses = 0
        self._errors = 0
        self._total = 0

    def load(self, loader) -> None:
        loader.add_option(
            "hrr_mode",
            str,
            MODE_RECORD,
            "harbor_mitm_replay operation mode: 'record' audits the responses "
            "written by mitmdump -w, 'replay' serves every request from the "
            "recorded flows without ever contacting the network.",
            choices=[MODE_RECORD, MODE_REPLAY],
        )
        loader.add_option(
            "hrr_flows",
            Optional[str],
            None,
            "Absolute path of the recorded flows.mitm file. Required when hrr_mode=replay.",
        )
        loader.add_option(
            "hrr_audit",
            Optional[str],
            None,
            "Absolute path of the JSONL audit file written by the addon. Required in both modes.",
        )

    def configure(self, updated) -> None:
        mode = ctx.options.hrr_mode
        if mode not in (MODE_RECORD, MODE_REPLAY):
            raise exceptions.OptionsError(
                f"hrr_mode must be {MODE_RECORD!r} or {MODE_REPLAY!r}, got {mode!r}"
            )
        if mode == MODE_REPLAY:
            # Latched before anything else is validated: once a run asked for
            # replay it must never fall back to forwarding requests upstream,
            # even if the cassette turned out to be broken.
            self._mode = MODE_REPLAY

        audit_path = ctx.options.hrr_audit
        if not audit_path:
            raise exceptions.OptionsError(
                "hrr_audit must be set to the absolute path of the JSONL audit file"
            )
        if not os.path.isabs(audit_path):
            raise exceptions.OptionsError(
                f"hrr_audit must be an absolute path, got {audit_path!r}"
            )

        if self._mode == MODE_REPLAY:
            self._require_strict_matching()
            flows_path = ctx.options.hrr_flows
            if not flows_path:
                raise exceptions.OptionsError(
                    "hrr_flows must be set to the absolute path of the recorded "
                    "flows.mitm file when hrr_mode=replay"
                )
            if not os.path.isabs(flows_path):
                raise exceptions.OptionsError(
                    f"hrr_flows must be an absolute path, got {flows_path!r}"
                )
            if self._playback is None or self._flows_path != flows_path:
                self._load_flows(flows_path)

        self._open_audit(audit_path)

    def _require_strict_matching(self) -> None:
        """Refuse options that would make replay match or consume differently."""
        broken = []
        if ctx.options.server_replay_reuse:
            broken.append("server_replay_reuse")
        if ctx.options.server_replay_nopop:
            broken.append("server_replay_nopop")
        if ctx.options.server_replay_ignore_content:
            broken.append("server_replay_ignore_content")
        if getattr(ctx.options, "stream_large_bodies", None):
            broken.append("stream_large_bodies")
        if getattr(ctx.options, "connection_strategy", "lazy") != "lazy":
            broken.append("connection_strategy must be lazy")
        if getattr(ctx.options, "server_replay", None):
            broken.append("server_replay")
        if broken:
            raise exceptions.OptionsError(
                "hrr_mode=replay needs mitmproxy's built-in server replay to "
                "stay off and its matching to stay strict; unset "
                + ", ".join(broken)
            )

    def _load_flows(self, flows_path: str) -> None:
        flows: list[http.HTTPFlow] = []
        try:
            with open(flows_path, "rb") as fp:
                for flow in mitmproxy_io.FlowReader(fp).stream():
                    if isinstance(flow, http.HTTPFlow) and flow.response is not None:
                        flows.append(flow)
        except (OSError, exceptions.FlowReadException) as exc:
            raise exceptions.OptionsError(
                f"hrr_flows: cannot read {flows_path}: {exc}"
            ) from exc
        if not flows:
            raise exceptions.OptionsError(
                f"hrr_flows: {flows_path} contains no HTTP flow with a response"
            )

        playback = ServerPlayback()
        playback.load_flows(flows)

        self._playback = playback
        self._flows_path = flows_path
        # Keep the flows alive and map them by identity: next_flow() hands back
        # the very objects loaded here, this map locates them in the cassette.
        self._flows = flows
        self._flow_indices = {id(flow): index for index, flow in enumerate(flows)}
        self._total = len(flows)
        self._matched = 0
        self._misses = 0

    def _replay_active(self) -> bool:
        """Whether requests must be served from the cassette.

        The mode is sticky for the lifetime of the process: a request that
        arrives before ``configure()`` ran still sees hrr_mode=replay, and a
        later mode change can never turn a replay run into a forwarding one.
        """
        if self._mode != MODE_REPLAY:
            if getattr(ctx.options, "hrr_mode", None) == MODE_REPLAY:
                self._mode = MODE_REPLAY
        return self._mode == MODE_REPLAY

    def request(self, flow: http.HTTPFlow) -> None:
        self._requests += 1
        if not self._replay_active():
            return
        try:
            self._serve_from_cassette(flow)
        except Exception:
            # A request hook that returns without a response would be forwarded
            # to the recording upstream. Fail closed instead.
            logger.exception(
                "harbor_mitm_replay: replay failed for %s", _request_line(flow)
            )
            self._errors += 1
            flow.response = _error_response(
                503,
                "replay_failed",
                "harbor_mitm_replay failed to serve this request from the cassette",
            )
            self._write_audit(
                {
                    "event": "error",
                    "response_index": None,
                    "request_sha256": _sha256(_raw_content(flow.request)),
                    "response_sha256": None,
                    "status_code": 503,
                    "is_replay": False,
                    "error": "replay_failed",
                }
            )

    def _serve_from_cassette(self, flow: http.HTTPFlow) -> None:
        playback = self._playback
        if playback is None:
            logger.error(
                "harbor_mitm_replay: hrr_mode=replay without a loaded cassette, "
                "refusing %s",
                _request_line(flow),
            )
            self._errors += 1
            flow.response = _error_response(
                503,
                "replay_not_configured",
                "harbor_mitm_replay is not configured for replay",
            )
            self._write_audit(
                {
                    "event": "error",
                    "response_index": None,
                    "request_sha256": _sha256(_raw_content(flow.request)),
                    "response_sha256": None,
                    "status_code": 503,
                    "is_replay": False,
                    "error": "replay_not_configured",
                }
            )
            return

        recorded = playback.next_flow(flow)
        if recorded is not None and recorded.response is not None:
            response = recorded.response.copy()
            # No refresh() and no waiting: the recorded response is replayed
            # verbatim, the original latency is not reproduced.
            flow.response = response
            flow.is_replay = "response"
            self._matched += 1
            self._write_audit(
                {
                    "event": "hit",
                    "response_index": self._flow_indices.get(id(recorded)),
                    "request_sha256": _sha256(_raw_content(recorded.request)),
                    "response_sha256": _sha256(_raw_content(recorded.response)),
                    "status_code": response.status_code,
                    "is_replay": True,
                }
            )
            return

        self._misses += 1
        logger.warning(
            "harbor_mitm_replay: no recorded response for %s", _request_line(flow)
        )
        flow.response = _error_response(
            400,
            "replay_miss",
            f"no recorded response for {_request_line(flow)}",
        )
        self._write_audit(
            {
                "event": "miss",
                "response_index": None,
                "request_sha256": _sha256(_raw_content(flow.request)),
                "response_sha256": None,
                "status_code": 400,
                "is_replay": False,
            }
        )

    def response(self, flow: http.HTTPFlow) -> None:
        response = flow.response
        if response is None or self._replay_active():
            return
        # _total doubles as the running index of recorded responses: it counts
        # the responses written to flows.mitm, in the order mitmdump wrote them.
        self._write_audit(
            {
                "event": "response",
                "response_index": self._total,
                "request_sha256": _sha256(_raw_content(flow.request)),
                "response_sha256": _sha256(_raw_content(response)),
                "status_code": response.status_code,
                "is_replay": bool(flow.is_replay),
            }
        )
        self._total += 1

    def error(self, flow: http.HTTPFlow) -> None:
        self._errors += 1
        response = flow.response
        self._write_audit(
            {
                "event": "error",
                "response_index": None,
                "request_sha256": _sha256(_raw_content(flow.request)),
                "response_sha256": _sha256(_raw_content(response)) if response else None,
                "status_code": response.status_code if response else None,
                "is_replay": bool(flow.is_replay),
                "error": str(flow.error) if flow.error else "",
            }
        )

    def done(self) -> None:
        if self._audit is None:
            return
        remaining = 0
        if self._replay_active() and self._playback is not None:
            remaining = self._playback.count()
        summary = {
            "event": "summary",
            "matched": self._matched,
            "misses": self._misses,
            "remaining": remaining,
            "total": self._total,
            "requests": self._requests,
            "errors": self._errors,
        }
        self._write_audit(summary)
        logger.info("harbor_mitm_replay: %s", json.dumps(summary))
        audit, self._audit = self._audit, None
        self._audit_path = None
        audit.close()

    def _open_audit(self, path: str) -> None:
        if self._audit is not None and self._audit_path == path:
            return
        if self._audit is not None:
            audit, self._audit = self._audit, None
            audit.close()
        try:
            self._audit = open(path, "w", encoding="utf-8")
        except OSError as exc:
            raise exceptions.OptionsError(
                f"hrr_audit: cannot open {path}: {exc}"
            ) from exc
        self._audit_path = path
        self._audit_missing_logged = False

    def _write_audit(self, record: dict[str, Any]) -> None:
        audit = self._audit
        if audit is None:
            if not self._audit_missing_logged:
                logger.error(
                    "harbor_mitm_replay: no audit file is open, dropping audit records"
                )
                self._audit_missing_logged = True
            return
        audit.write(json.dumps(record) + "\n")
        audit.flush()


addons = [ReplayAddon()]
