# Documentation

Start with the [root README](../README.md) for what SideCode is and how to get it running.
These documents go deeper.

| Document | Read it when you want to |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Understand the components, the data flow, the block timeline, how a terminal session is attached through the Claude channel or the shared Codex daemon, where state lives, and why the boundaries fall where they do |
| [DEPLOY.md](DEPLOY.md) | Deploy or operate the gateway: every `.env` variable, TLS, upgrades, backups, speech-to-text, push, troubleshooting |
| [CLIENT.md](CLIENT.md) | Install, run or debug the device daemon: the installer, `rc-client` commands, services, agent discovery, terminal mirroring, the `claude` shim, the shared Codex daemon and attached sessions |
| [WEB.md](WEB.md) | Build, run or change the browser app, including its mock gateway |
| [DESIGN.md](DESIGN.md) | Change the interface: the screens, composer semantics, approval rules, status vocabulary, design tokens |
| [VALIDATION.md](VALIDATION.md) | Run the checks every change needs, and the end-to-end smoke flow before a release |

The normative wire contract lives outside `docs/`, next to its schema and fixtures:
[`protocol/PROTOCOL.md`](../protocol/PROTOCOL.md). Each component also carries a README with its own
layout and commands: [`gateway/`](../gateway/README.md), [`client/`](../client/README.md),
[`web/`](../web/README.md), [`protocol/`](../protocol/README.md).
