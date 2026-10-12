# Security

Cassettes contain prompts, terminal observations, file contents, and model responses. Keep them out of public repositories unless they have been reviewed and scrubbed.

Replay mode is fail-closed by design: a request that does not match the cassette receives HTTP 400 and is never sent to the configured provider.

Redirecting responses are refused during recording and replay before the client can follow them. The Harbor runner rejects model-routing overrides, validates restored manifest options, and exempts loopback traffic from ambient HTTP proxies. These safeguards do not replace network isolation for task commands, verifiers, or an evaluation framework wired outside this runner.

Report security issues privately to the repository owner rather than opening an issue with credentials or cassette contents.
