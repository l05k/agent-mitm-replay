import { appendFileSync } from 'node:fs';

const log = '/logs/agent/openclaw-tools.jsonl';
const emit = (event) => appendFileSync(log, `${JSON.stringify(event)}\n`, { mode: 0o600 });

export default {
  id: 'amr-tool-trace',
  name: 'Benchmark tool trace',
  configSchema: { type: 'object', additionalProperties: false },
  register(api) {
    emit({ event: 'ready' });
    api.on('before_tool_call', (event, context) => {
      emit({ event: 'start', run: event.runId ?? context.runId ?? null,
        id: event.toolCallId ?? context.toolCallId ?? null, name: event.toolName });
    });
    api.on('after_tool_call', (event, context) => {
      const end_ms = Date.now();
      emit({ event: 'end', run: event.runId ?? context.runId ?? null,
        id: event.toolCallId ?? context.toolCallId ?? null, name: event.toolName,
        arguments: event.params, result: event.result ?? null, error: event.error ?? null,
        end_ms, duration_ms: event.durationMs ?? null });
    });
  },
};
