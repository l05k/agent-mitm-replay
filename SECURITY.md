# Security

Cassettes contain prompts, terminal observations, file contents, and model responses. Keep them out of public repositories unless they have been reviewed and scrubbed.

Replay mode is fail-closed by design: a request that does not match the cassette receives HTTP 400 and is never sent to the configured provider.

The runner exempts loopback traffic from ambient HTTP proxies so model calls reach the local proxy. It does not check agent options: an option such as `api_base` or `llm_kwargs.base_url` sends model calls around the proxy. The provider origin must answer without redirects. None of this replaces network isolation for task commands, verifiers, or an evaluation framework wired outside this runner.

Report security issues privately to the repository owner rather than opening an issue with credentials or cassette contents.
