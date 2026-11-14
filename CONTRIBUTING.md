# Contributing

Thanks for helping with sbx. The [README](README.md) explains the product and
[development](docs/development.md) how to run the checks. Coding agents also
follow [AGENTS.md](AGENTS.md).

## Reporting a bug

Tell us:

- the sbx revision, host OS and how you installed it;
- the command, API call or page that failed;
- what you expected and what happened;
- logs, with credentials and private values removed.

A public repository or a small recipe that reproduces the problem is the most
useful thing you can send. If an app does not start, say whether the sandbox
reached `workspace_ready`, which app action fails, which external services the
app needs, and whether a fresh second sandbox fails the same way.

## Proposing a change

- Solve one concrete problem per pull request, reproduced through the CLI or API
  where possible.
- Put the fix in the right layer: lifecycle in the core, agent- or
  provider-specific behavior in its [extension](docs/extensions.md), app setup
  in the recipe. An ordinary command beats a new abstraction or setting.
- Add tests for behavior that can break, and run the checks.
- For lifecycle or service changes, run the
  [acceptance checklist](docs/agent-install.md#acceptance-checklist).
- Edit the web app in `web/src` and commit its rebuilt assets.
- Leave `vendor/e2b-embed/` untouched; runtime configuration goes in `embed/`.
  Keep third-party notices intact.
- Update the docs when commands or behavior change.

The pull request template asks for the problem, the change and how you verified
it, including what you could not test.

## License

Contributions to sbx's own code are under its [MIT license](LICENSE). Vendored
components keep their own licenses ([notices](THIRD_PARTY_NOTICES.md)).
