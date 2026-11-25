# Monetization thesis (open core)

**Viability: 7/10.** Teams are rolling coding agents out across many repos, and the cost they
fear is not broken builds. They fear a codebase that still passes its tests while it gets harder
to change each week. Code-health gates are already a paid category: CodeScene and SonarQube sell
quality gates and dashboards to engineering orgs. Neither was checked for pricing or agent features
this session, so treat that as **unverified context, not evidence**. What narrowpy adds is being
agent-native. It fails only on what *this change* made worse, it runs as the agent's own Stop hook,
and it refuses edits to its own gate.

No customer has been asked yet. Validation comes first (see the end).

## The boundary

| Open source (MIT, this repo) | Paid (hosted / enterprise) |
|---|---|
| All lints, the typer, the CLI | **Org dashboard**: structural debt per repo over time, and which change (and which agent or session) added or removed it |
| Baseline ratchet, `--base`, SARIF, GitHub annotations | **PR app**: a check run with inline findings plus an auto-drawn before/after diagram, with no CI setup |
| Claude Code hooks, MCP server | **Policy**: org-wide thresholds and lints, required owners for baseline changes, signed baselines (the tamper check, made central) |
| Single repo, local | **Cross-repo**: monorepo and multi-service graphs, cycles across packages that each look clean |
| Python | **More languages** on the same engine (TypeScript next, then Go) |
| | **Agent analytics**: regressions per agent, model or prompt template, so platform teams can see which setup produces rot |

The line is simple: **one developer and one repo are free forever. Seeing across repos, people and
agents is paid.** The CLI never phones home.

## Why the hooks are already there

- Finding keys and metrics are stable and structural (no line numbers), so a server can track a
  finding across commits without re-deriving it. That is the core of the dashboard.
- `--format json` and SARIF already carry everything a PR app needs.
- The Stop hook knows the session id and the base commit, which is exactly the "which agent session
  did this" join that agent analytics needs.

## Pricing hypothesis (untested)

- Team: per active contributor (human or agent identity) per month, for the PR app and dashboard.
- Enterprise: self-hosted, SSO, policy, audit export.

## What would change the score

- **Up:** 3+ teams running the Stop hook for two weeks and keeping it on. Inbound asks for TypeScript.
- **Down:** people install it and turn it off after a day (too noisy). Track this with a voluntary
  `narrowpy feedback` link before building anything paid.

## Next validation steps (no money required)

1. Measure noise: run on 20 popular Python repos and hand-label a random sample of findings. Publish the precision.
2. Post the live transcript (agent hides the cycle, the gate catches it, the agent fixes it properly) where agent users gather.
3. Ask 5 platform/devex teams one question: "Would you pay to see which agent sessions made your architecture worse?"
