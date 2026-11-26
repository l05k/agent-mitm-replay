import { appendFileSync } from "node:fs";

const LOG = "/logs/agent/amr-timing.jsonl";

const emit = (event) => {
  appendFileSync(LOG, `${JSON.stringify(event)}\n`, { mode: 0o600 });
};

export default {
  id: "amr-timing",
  name: "Native tool timing",
  configSchema: { type: "object", additionalProperties: false },
  register(api) {
    emit({ event: "ready", agent: "openclaw" });
    api.on("after_tool_call", (event, context) => {
      const id = event?.toolCallId ?? context?.toolCallId;
      if (typeof id !== "string" || id.length === 0) {
        emit({ event: "error", detail: "after_tool_call has no toolCallId" });
        return;
      }
      const duration = event?.durationMs;
      if (typeof duration !== "number" || !Number.isFinite(duration) || duration < 0) {
        emit({
          event: "error",
          id,
          detail: "after_tool_call has no native durationMs",
        });
        return;
      }
      emit({
        event: "tool",
        id,
        name: event?.toolName ?? context?.toolName ?? null,
        duration_ns: String(Math.round(duration * 1e6)),
        error: event?.error ?? null,
      });
    });
  },
};
