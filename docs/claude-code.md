# Claude Code

The Claude Code connector makes the sbx server the runners of a Claude Code
[self-hosted environment](https://code.claude.com/docs/en/self-hosted-environments).
A cloud session started on that environment (from claude.ai/code, the desktop or
mobile app, or `claude --cloud`) runs in a sandbox of its own: it gets the same
checkout, per-sandbox services, injected env, recipe setup, health checks and
previews as a sandbox started with `sbx up`. The connector code is in
`control/sbx_control/connectors/claude_code/`; how connectors plug in is
described in [extensions](extensions.md#agent-connectors).

## Requirements

- A Claude Team or Enterprise organization with self-hosted environments turned
  on. Anthropic offers them as a public beta, off by default: an organization
  Owner turns on **Allow self-hosted environments** on the **Cloud
  environments** admin page, which needs cloud sessions enabled. Check
  [Anthropic's availability notes](https://code.claude.com/docs/en/self-hosted-environments#availability-and-limitations)
  for the current plans and exclusions.
- The repository is onboarded in sbx and passes the
  [acceptance checklist](agent-install.md#acceptance-checklist). Sessions run
  only on trusted repositories with a recipe.
- Sessions count against each developer's own Claude Code usage, and the
  conversation still goes to Anthropic; the checkout, services and files stay
  on the server.

## What the connector contributes

- **Template layer**: the pinned `claude` CLI in every template. `sbx open NAME
  claude` uses it with the developer's own login.
- **A host service**, `claude-orchestrator`, that runs Anthropic's runner
  orchestrator. It polls Anthropic for queued sessions over outbound HTTPS; no
  public port is opened.
- **A sandbox per session**: the orchestrator's spawn hook posts each order to
  the control service on its loopback interface. sbx creates the session's
  sandbox (named `s-` and a hash of the session), owned by the account that
  started it, on the session's repository and branch, then starts `claude
  self-hosted-runner --capacity 1` in it. A session that comes back later gets
  the same sandbox, resumed if it was paused.
- **The session works where the app runs**: the runner's checkout is the
  sandbox's own, so edits reach the running app. The session's `CLAUDE.md`
  names the preview links, logs and restart command, followed by the
  repository's `.sbx/AGENTS.md`.
- **Credentials**: the runner gets only the session's own token. Developers'
  tokens from `sbx open` and the variables in `sandboxes.agent_hidden` are left
  out of its environment.
- **Keeper policy**: when the session ends (its runner was released after being
  idle, or the session was archived or deleted) and nobody is using the sandbox,
  it pauses. The next message or preview request resumes it.

## Set it up

1. Onboard the repository in sbx first and pass the acceptance checklist. Claude
   Code uses the same recipe, services and variables; there is no separate
   configuration for them.
2. In claude.ai's admin settings, on **Cloud environments**, create a
   self-hosted environment and copy its **environment key**. It is shown once.
   Anthropic's [quickstart](https://code.claude.com/docs/en/self-hosted-environments-quickstart)
   walks through the claude.ai side; skip its runner steps, sbx runs those.
3. In sbx **Extensions**, on **Claude Code**, paste the key as the
   environment secret and save; saving turns it on and **Disconnect** turns it
   off. The key goes into the secret store, and saving installs the pinned CLI on the host and starts `claude-orchestrator`.
   On the host you can do the same without putting the key in a command line:

   ```sh
   read -r -s CLAUDE_KEY
   printf '%s\n' "$CLAUDE_KEY" | sbx-admin connect claude environment_secret=-
   unset CLAUDE_KEY
   ```

4. Press **Test** on the card. It checks the secret, the CLI version and that
   the orchestrator answers; the orchestrator can take up to 10 minutes to
   start after the secret is first set.
5. Start a cloud session, choose the self-hosted environment and one of the
   repositories sbx serves. Its sandbox appears on the **Sandboxes** page with
   the origin "Claude Code session".

## Limits and settings

- A session on a repository that is not trusted or has no recipe is refused,
  and the message names the repositories this environment serves.
- When the server has no room for another sandbox, the order is refused as
  retryable and the reason reaches the session.
- Each session's sandbox counts against the server's sandbox capacity like any
  other; archive or delete sessions you no longer need.

The connector's settings, changed with
`sbx-admin config set claude '{"release_idle_session_min": 60}'`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `version` | pinned per release | The `claude` CLI version on the host and in templates. |
| `release_idle_session_min` | 120 | Minutes a session's runner waits idle before releasing it (the sandbox then pauses). |
| `kill_session_after_min` | 480 | Longest a single session may run. |
| `hook_concurrency` | 4 | Orders the orchestrator hands to sbx at once. |
| `expected_spawn_seconds` | 150 | How long the orchestrator expects a sandbox to take to start. |

## Inspect and clean up

```sh
journalctl -u claude-orchestrator -n 50   # the orchestrator and its spawn hook
```

In a session's sandbox, the runner logs to `~/.cache/claude-runner.log`. Use
the ordinary sandbox views, API and `sbx sandbox` commands to inspect setup,
restart the app or change env. Disconnecting removes the environment secret and
stops the orchestrator; existing sandboxes stay until you delete them.
