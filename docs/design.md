# Design and limitations

## Architecture

The project has two parts:

- `addon.py`: a mitmproxy addon that records and replays HTTP traffic. It has no Harbor dependency.
- `runner.py`: starts the proxy and Harbor, sets Harbor's launch options, and validates Harbor's results.

Harbor needs no source patches. The CLI currently supports Harbor Terminus-2. To use the addon with another evaluation framework, route all of its model traffic through the proxy and validate its trial results yourself.

## Request matching

- The replay key is the HTTP method, URL, and raw request body. Headers are not part of the key.
- Each recorded response is used at most once. Requests with the same key are answered in recording order (FIFO). The order across different keys is not checked.
- A request with no remaining match gets HTTP 400. It is never recorded on the fly or forwarded to the provider.
- Replay requests are fully buffered before matching. Request streaming is only enabled while recording, because it can open an upstream connection before the matching hook runs.

The addon refuses settings that would loosen the key or allow a response to be reused: the `server_replay_*` options `ignore_content`, `ignore_host`, `ignore_port`, `ignore_params`, `ignore_payload_params`, `reuse`, and `nopop`, as well as mitmproxy's own `server_replay` addon.

## Safety checks

| Risk | Check |
| --- | --- |
| Model client follows a redirect out of the proxy | Redirect responses are replaced with HTTP 400 `unsafe_redirect` while recording; cassettes containing redirects are rejected on replay. |
| Agent options point the model client elsewhere | Routing options are rejected in agent options, `llm_kwargs`, `llm_call_kwargs`, and the restored manifest. See [Agent options](usage.md#agent-options). |
| Ambient HTTP proxy intercepts the loopback hop | Loopback addresses are added to `NO_PROXY` and `no_proxy`. |
| Harbor exits 0 after a failed trial | The runner checks Harbor's `result.json` files, not its exit code. |
| A hidden prompt blocks the run | Harbor runs with `--yes` and closed stdin. |

## Limitations

- **One cassette per trial.** Use one cassette and one proxy process per Harbor trial. Do not share a cassette between concurrent trials.
- **No general network isolation.** The runner disables Harbor telemetry and LiteLLM's price-map download. It does not block network access from task commands or verifiers. Use the task's network policy if you need full isolation.
- **Verifier model calls are not covered.** If the verifier calls an LLM, point it at its own replay service or disable it for a fully offline run.
- **No stream timing.** Streamed response bytes are stored, but the original timing between events is not reproduced.
- **Only the `litellm` backend** and OpenAI-compatible Chat Completions models (`openai/...`) are supported.
