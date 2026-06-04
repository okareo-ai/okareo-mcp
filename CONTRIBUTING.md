# Contributing to okareo-mcp

Thanks for your interest in the Okareo MCP server.

## How this repository works

This repository is a **curated public mirror**. The canonical source of truth is
maintained privately by the Okareo team, and this repo is published as periodic,
provably-clean release snapshots. Commits here are made by Okareo's publishing
automation, so **pull requests cannot be merged directly into this repository** —
a merge here would be overwritten by the next published snapshot.

That doesn't mean we don't want your input. We do — here's how it works in
practice.

## Reporting bugs and requesting features

Please open a [GitHub Issue](https://github.com/okareo-ai/okareo-mcp/issues).
Helpful details:

- what you expected versus what happened;
- your copilot/client and the MCP transport you used (remote HTTP vs. local
  stdio);
- minimal steps to reproduce.

**Security issues:** please do not open a public issue. Email security@okareo.com
instead.

## Proposing changes (pull requests)

We do consider community pull requests. Because of the mirror model described
above, the workflow is:

1. **Open an issue first** (or comment on an existing one) describing the change,
   so we can confirm direction before you invest effort.
2. **Open your PR against `main`.** A maintainer will review it here and discuss
   it with you.
3. **If accepted**, an Okareo maintainer ports the change into the internal
   source of truth. It then lands in this repository through the next release
   snapshot. We will credit you for the contribution, and your PR will be closed
   (not merged) once the change ships — ideally with a link to the published
   commit.

This is more indirect than a typical merge, but it's the only way to keep the
public tree provably clean while still accepting outside help.

### Developer Certificate of Origin (sign-off)

By contributing, you certify the
[Developer Certificate of Origin](https://developercertificate.org/): that you
wrote the contribution, or otherwise have the right to submit it under this
project's license. To certify it, add a sign-off line to each commit:

```
git commit -s
```

which appends `Signed-off-by: Your Name <you@example.com>` using your
`git config` identity. PRs without sign-off may be asked to add it before review.

## License

Contributions are accepted under the [Apache License, Version 2.0](LICENSE).
Okareo names and logos are trademarks and are not licensed under Apache 2.0 — see
[TRADEMARK.md](TRADEMARK.md).
