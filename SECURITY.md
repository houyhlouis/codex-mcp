# Security

The [security model](docs/security.md) describes the local trust boundary, native permissions, human/automatic review, credential handling, and retained data. The service is intended for one trusted local OS user and approved project roots; it is not a multi-user or remote service.

## Reporting an issue

If private vulnerability reporting is enabled for the published repository, use that channel. Otherwise contact the repository maintainer privately before posting a report containing sensitive details. No maintainer email address or repository URL is configured in this source snapshot.

Include a minimal reproduction, affected version, platform/Codex version, and the boundary crossed. Redact credentials, personal information and project content. Do not upload a complete state directory, native authentication file or unredacted conversation history.

## Local incident handling

Stop the broker with the normal `stop` command if continued execution is unwanted. Retain only the local evidence needed to diagnose the issue and review it before sharing. Manage any affected credentials through their native provider's authorized process; the bridge does not rotate or replace credentials.
