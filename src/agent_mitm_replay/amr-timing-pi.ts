import { appendFileSync } from "node:fs";

const LOG = "/logs/agent/amr-timing.jsonl";

const emit = (event: Record<string, unknown>): void => {
  appendFileSync(LOG, `${JSON.stringify(event)}\n`, { mode: 0o600 });
};

const field = (event: unknown, name: string): unknown => {
  if (typeof event !== "object" || event === null || !(name in event)) {
    return undefined;
  }
  return (event as Record<string, unknown>)[name];
};

const callId = (event: unknown): string | undefined => {
  const value = field(event, "toolCallId");
  return typeof value === "string" && value.length > 0 ? value : undefined;
};

const toolName = (event: unknown): string | null => {
  const value = field(event, "toolName");
  return typeof value === "string" ? value : null;
};

export default function (pi: { on: (name: string, handler: (event: unknown) => void) => void }): void {
  emit({ event: "ready", agent: "pi" });
  const started = new Map<string, bigint>();

  pi.on("tool_execution_start", (event) => {
    const id = callId(event);
    if (id === undefined) {
      emit({ event: "error", detail: "tool_execution_start has no toolCallId" });
      return;
    }
    if (started.has(id)) {
      emit({ event: "error", id, detail: "duplicate tool_execution_start" });
      return;
    }
    started.set(id, process.hrtime.bigint());
  });

  pi.on("tool_execution_end", (event) => {
    const id = callId(event);
    if (id === undefined) {
      emit({ event: "error", detail: "tool_execution_end has no toolCallId" });
      return;
    }
    const begin = started.get(id);
    if (begin === undefined) {
      emit({ event: "error", id, detail: "tool_execution_end without start" });
      return;
    }
    started.delete(id);
    emit({
      event: "tool",
      id,
      name: toolName(event),
      start_ns: begin.toString(),
      duration_ns: (process.hrtime.bigint() - begin).toString(),
      is_error: field(event, "isError") === true,
    });
  });
}
