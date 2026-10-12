# Security

Cassettes contain prompts, terminal observations, file contents, and model responses. Keep them out of public repositories unless they have been reviewed and scrubbed.

Replay mode is fail-closed by design: a request that does not match the cassette receives HTTP 400 and is never sent to the configured provider.

Report security issues privately to the repository owner rather than opening an issue with credentials or cassette contents.
