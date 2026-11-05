# Security

Cassettes contain prompts, terminal observations, file contents, model responses, and full request headers, including the provider API key (`Authorization: Bearer ...`). The runner creates cassette and run directories with mode `0700`. Keep cassettes out of public repositories, and remove the key before sharing one.

Replay mode is fail-closed by design: a request that does not match the cassette receives HTTP 400 and is never sent to the configured provider.

Recorded redirects are refused during replay with HTTP 400, and a redirect during recording fails the run, so the model client cannot follow a redirect out of the proxy during replay. The runner exempts loopback traffic from ambient HTTP proxies, keeping existing `NO_PROXY` and `no_proxy` entries. It does not check agent options: an option such as `api_base` or `llm_kwargs.base_url` sends model calls around the proxy. None of this replaces network isolation for task commands, verifiers, or an evaluation framework wired outside this runner.

Report security issues privately to the repository owner rather than opening an issue with credentials or cassette contents.
