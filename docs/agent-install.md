# Install sbx and onboard a repository

This is the one onboarding guide, for a person or a coding agent. It goes from a
fresh server to a useful first repository and ends in the
[acceptance checklist](#acceptance-checklist).

Start from the owner's repository and server access. Read the [README](../README.md)
and `install.sh` before changing the server. Do not assume a domain, GitHub App,
external account or existing recipe; ask only for what you cannot discover.

Work through the steps in order and keep the current step visible. For each step,
say what you are setting up, do it through the exposed CLI/API, check the result,
then move on. Do not stop for approval between routine steps. Ask a focused
question when the owner must choose a dataset, supply access, or complete an
interactive login, and wait for the answer before dependent work. Resolve a
failed step before presenting it as done.

## Steps

1. **Server.** Check Ubuntu 24.04, `/dev/kvm`, memory, disk and network access
   against the [host requirements](../README.md#host-requirements). Explain any
   prerequisite only the owner can fix. Download the latest release's archive
   (or the release the owner names), check its `.sha256` and run
   `sh install.sh` from the unpacked directory as root, as the
   [README](../README.md#install) shows; keep its progress visible. A git
   checkout needs its web app built first (Node 22).
2. **Control plane.** Open the printed setup link. Guide the owner through IP
   access or a domain and the admin login; let them type credentials themselves.
   With a domain, check the DNS records and ports the host requirements list
   (the `_acme-challenge` NS record and port 53 for `--dns delegated`). The
   admin login is a shared password that admits the admins only, and whoever
   holds it can sign in as any of them; for a team, suggest OIDC with
   `allowed_emails`.
   Confirm the control service, runtime and secret store are ready
   (`sbx-admin doctor`, `sbx-admin store status`).
3. **Repository access.** Connect GitHub on the web app's **Extensions** page
   and let the owner select a repository. A public repository URL works
   without a GitHub App.
4. **Desktop CLI.** On the machine with a checkout of the repository, install
   `sbx` from the server's **CLI & access** page and run `sbx login`. The
   desktop CLI runs on macOS only; on Linux or in CI, use the
   [HTTP API](reference.md#http-api) with a token the owner makes on the host
   (`sbx-admin token create EMAIL --label LABEL`, shown once).
5. **Set up the repository.** In the checkout, `sbx repo connect --agent`
   starts Claude Code (or `--agent codex`) with the server's setup brief.
   Without that agent installed, it prints the brief for another agent to
   follow (the API serves it at
   `GET /api/repos/OWNER/NAME/setup-brief`). The
   [setup brief](../control/sbx_control/template_files/setup-brief.md) is the
   one procedure from discovery to a verified first launch: it chooses services
   with [service selection](service-selection.md), declares every variable,
   writes a small [recipe](reference.md#recipe), sizes it with the
   [resource guide](../control/sbx_control/template_files/resource-guide.md)
   and launches it. For preloaded development data, see
   [development baselines](reference.md#development-baselines).
6. **Acceptance.** Run the [acceptance checklist](#acceptance-checklist).
7. **Coding agents.** Ask the owner which coding agents the team uses. For
   Claude Code cloud sessions, follow [Claude Code](claude-code.md); for Amp
   threads, follow [Amp](amp-orbs.md). Each needs a key only the owner can copy
   (from claude.ai, or from Amp's settings) and paste on the **Extensions**
   page; never ask for it in chat. Check that the card shows the connector as
   connected and start one session or thread to confirm its sandbox comes up.
   Other agents need nothing here: they reach sandboxes over SSH (`sbx
   ssh-config`) or with `sbx open NAME AGENT`.
8. **Handoff.** Give the owner working preview links and the commands to start
   another sandbox (`sbx up`), read logs (`sbx logs`), change variables
   (`sbx env`) and delete a sandbox (`sbx down`). Offer a setup PR
   (`sbx recipe pr`), PR previews or a prebuilt template once the basic launch
   works.

Do not put private values in chat, recipes, commits, templates or logs. Tell the
owner what succeeded, what is limited, and which actions remain theirs.

## Acceptance checklist

Every sandbox is a development environment of its own: it must be able to
migrate, seed and reset its data without changing another sandbox's data or
schema. Shared service infrastructure keeps this cheap; the databases, branches
or namespaces on it are per sandbox. This is the one place that rule is stated;
other docs link here.

Before you start, make sure the recipe gives each sandbox:

- its own database, branch or namespace, provisioned before `.sbx/setup` runs,
  with a login that may run the repository's normal migrations (schema changes
  included);
- setup that runs the normal migrations and development seeds and is safe to
  repeat;
- SQLite files, uploads and other app state in its own filesystem or in a
  resource of its own;
- a template, if any, with tools and dependencies only: no credentials and no
  other sandbox's data.

Then check, with two disposable, named sandboxes A and B:

- [ ] Both launch cleanly from the recipe and pass a useful app action.
- [ ] Migrate and seed both. Change or reset A's data; B still has its original
      data and schema.
- [ ] Restart A (`sbx sandbox restart A`), then pause and resume it
      (`sbx sandbox pause|resume A`, or `POST /api/sandboxes/A/pause` and `/resume`):
      its working data is still there.
- [ ] Delete A (`sbx down A`): B still works, and A's owned resources (its
      database, branch, role or namespace) are gone while the shared servers stay.
- [ ] No secret value appears in the recipe, the template, commits or logs.

Record what you verified and what you did not (for example optional providers
that were never configured).
