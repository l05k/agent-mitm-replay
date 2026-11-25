# Security

Run trusted tasks, images and agent packages. Harbor downloads and installs
the pinned agent CLI during agent setup, so its environment needs access to
nvm, Node.js and npm even for an offline model replay. The local proxy binds
the Docker bridge IP and authenticates each trial with an ephemeral token. It
substitutes the provider bearer key during capture on the host and strips
authorization from saved model requests. Offline it has no provider
credential. These measures are not sandboxing; they do not restrict task,
verifier or installer network access.

Cassettes and diagnostics contain prompts, file contents and tool/model
outputs, which can themselves contain sensitive data. Use private directories
and review before sharing. Do not commit cassettes, local inputs, credentials
or raw diagnostics.

Report security issues privately to the repository owner.
