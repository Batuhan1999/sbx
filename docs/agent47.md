# Agent47

The Agent47 connector puts [Agent47](https://pypi.org/project/agent47/), a
Python command-line coding agent, into the sandboxes of an installation, with
the model provider key the installation saves for it. The connector code is
`control/sbx_control/connectors/agent47.py`; how connectors plug in is
described in [extensions](extensions.md#agent-connectors).

Agent47 is a plain CLI. It has no interface through which a server could
start its runs (nothing like Claude Code's self-hosted runners or Amp's orb
providers), so there are no "Agent47 sessions" in sbx: people and agents run
it in a sandbox over SSH, like Codex.

It is off until an admin turns it on. Agent47 calls itself a controlled
closed beta that a human must supervise; read its
[README](https://pypi.org/project/agent47/) before enrolling a repository.

## Turning it on

From the web app (**Extensions → Agent47**), the CLI or the API; all three
save the same fields:

| Field | Meaning |
| --- | --- |
| `provider` | The model provider Agent47 calls: `openrouter` (default), `openai`, `gemini`, `deepseek` or `nvidia`. |
| `api_key` | Optional secret: the installation's API key at that provider. |
| `model` | Optional: the provider's name for the model (`AGENT_MODEL`); empty leaves Agent47's default. |
| `version` | The version of `agent47` on PyPI that templates install (no form field; CLI and API only). |
| `enabled` | `false` turns it off and keeps the key; saving anything else turns it on. |

```bash
# The key is read from stdin, never from the command line.
printf '%s' "$KEY" | sbx extensions set agent47 --set provider=openrouter --secret api_key --wait
sbx extensions set agent47 --set model=qwen/qwen3-coder
sbx extensions set agent47 --set version=0.1.0 --wait     # another pinned release
sbx extensions set agent47 --set enabled=false --wait     # off; the key stays
sbx extensions disconnect agent47 --wait                  # off, and the key is deleted
```

```bash
curl -X PUT https://app.example.com/api/connections/agent47 \
  -H "Authorization: Bearer $SBX_TOKEN" -H 'Content-Type: application/json' \
  -d @values.json      # {"values": {"provider": "openrouter", "api_key": "..."}}
```

Turning it on or off, or changing `version`, changes what every template
holds: the answer's `rebuilds` says how many templates rebuild on each
runtime, and `--wait` waits for them
([API](reference.md#http-api)). Sandboxes that exist keep the template they
started from, so Agent47 is in sandboxes created after the rebuild. A changed
key, provider or model rebuilds nothing; it reaches a running sandbox the
next time sbx writes its runtime (prepare, an env change, a cold boot).

A key belongs to its provider: changing `provider` while a key is saved is
refused unless the new provider's key comes with it.

## What the connector contributes

- **Template layer** (when on): `python3-venv`, the pinned package in a
  virtualenv of its own, `/opt/sbx-agent47`, so its dependencies never meet
  the app's, and its two commands in `/usr/local/bin`: `code-agent` (the
  automation CLI) and `agent47` (the interactive terminal). The build ends
  with `code-agent --version` naming the pinned version.
- **Runtime layer** (sandboxes of trusted repositories): one file in the
  sandbox's tmpfs, `/run/sbx/ext/agent47/env`, with `AGENT_PROVIDER`,
  `AGENT_MODEL` when set, and the saved key under the name Agent47 reads for
  that provider (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
  `DEEPSEEK_API_KEY`, `NVIDIA_API_KEY`).
- **The key stays Agent47's.** Only the two commands read that file: the key
  is in no template, recipe or log, and not in the sandbox's env, so the
  app's processes and the other agents never get it. Each line applies only
  where the shell has no value of its own, so a repository's secret, the
  per-sandbox `OPENROUTER_API_KEY` of the
  [`openrouter-key`](../catalog/openrouter-key/) package with its own credit
  limit, or a key a developer exports in their shell wins over the
  installation's.
- **Keeper**: a running Agent47 process counts as use, so a run left in
  `tmux` keeps its sandbox awake.
- **Snapshots**: `~/.config/agent47` and the checkout's `.code-agent/` are
  removed from reusable snapshots.

Without a saved key the connector only installs the CLI: each developer
exports their own key in the sandbox (`export OPENROUTER_API_KEY=...`).

## Using it

```bash
sbx open NAME ssh                       # a shell in the checkout
code-agent doctor                       # Agent47's own diagnostics
code-agent run --dry-run "Find the riskiest defect in this repository"
code-agent run "Fix the failing parser tests"
```

Without a terminal (an agent driving the sandbox, a script), `run-json`
emits newline-delimited JSON events and takes its approvals as flags:

```bash
ssh sbx-NAME 'cd ~/app && code-agent run-json --approve-all "Fix the failing parser tests"'
```

Things to know:

- **`code-agent`, not bare `agent47`.** The interactive `agent47` terminal
  refuses to start a model session without a Google sign-in and a key in the
  operating system's credential manager, which a sandbox does not have.
  `code-agent run` and `run-json` read the key from the environment and need
  neither.
- **No `--sandbox`.** Agent47's own sandbox mode needs Docker or Podman; the
  sbx sandbox is the isolation boundary here.
- **`.code-agent/`.** Agent47 keeps its run history and databases in
  `.code-agent/` in the directory it runs in. Add it to the repository's
  `.gitignore`.
- **The checkout's `.env`.** Agent47 reads `.env` in the directory it runs
  in for its own settings (`AGENT_*`, `*_API_KEY`); the environment, and so
  the installation's values, win over it.
- **Approvals.** `code-agent run` asks before risky actions on a terminal.
  `--approve-all` approves every tool request: use it only where the
  sandbox's secrets and services may be touched by whatever the model
  decides to run.
- **`sbx open NAME <agent>`** starts agents that have a per-developer login
  (`claude`, `codex`); Agent47 has none, so open a shell and run it.

## Limits

- Nothing here was run against a real sandbox runtime or a real provider
  key when the connector was written: the template build, `code-agent
  --version` and a model call are the first things to check on an
  installation ([acceptance](#acceptance)).
- Only the five providers above. Agent47's `compatible` provider (any
  OpenAI-compatible endpoint) needs `COMPATIBLE_BASE_URL` and
  `COMPATIBLE_API_KEY`: set them as the repository's own env and secret and
  export `AGENT_PROVIDER=compatible`.
- The package's dependencies are not pinned: a template build installs the
  pinned `agent47` with whatever versions of its dependencies PyPI resolves
  that day.
- The installation's key is one key for every trusted repository's
  sandboxes, with no per-sandbox spending limit; a repository that attaches
  `openrouter-key` gets a capped key per sandbox instead.

## Acceptance

On an installation with a trusted repository, with a disposable sandbox:

1. `sbx extensions set agent47 --secret api_key --wait` (key on stdin) exits
   0 and `sbx extensions ls` shows `agent47  connected`.
2. `sbx up NAME`, then in `sbx open NAME ssh`: `code-agent --version` prints
   the pinned version, and `env | grep OPENROUTER_API_KEY` prints nothing
   (the saved key is not in the shell's env).
3. `code-agent doctor` reports the provider and a usable key;
   `code-agent run --dry-run "Describe this repository"` gets an answer from
   the model.
4. Start a long `code-agent run` in `tmux`, detach and close SSH: the
   sandbox is still running after the idle time that would pause it.
5. `sbx extensions disconnect agent47 --wait`; a new sandbox has no
   `code-agent`.
