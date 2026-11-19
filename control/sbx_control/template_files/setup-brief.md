# Set up {repo} on {control}

You are in a checkout of `{repo}`, and the `sbx` CLI is signed in to
{control}. Your job: write the repository's `.sbx/` recipe so that
`sbx up NAME` starts the application its owner actually uses, with
development data of its own, then start one sandbox and give the owner its
preview link. Appended after the last step: worked examples (how real recipes
were made), the service selection guide and the resource guide.

## How to work

- Do the steps in order. Before a step, say what you are about to do; after it,
  run its check and show the result. Do not move on from a failed check.
- Do routine work without asking. Ask the owner only for a decision (product
  mode, starting data, a provider or plan that costs money) or for access, and
  wait for the answer before work that depends on it.
- Work through `sbx` and `ssh sbx-NAME` (run `sbx ssh-config` once so ssh
  knows the `sbx-` hosts). Where ssh doesn't work, `sbx sandbox exec NAME`
  runs a script from stdin over the API. When something fails, fix the recipe
  and retry the same command. Do not patch a sandbox by hand and call it done.
- Without the CLI, use the HTTP API with an API token
  (`Authorization: Bearer TOKEN`; `curl -k` for a self-signed certificate):

  | Command in this brief | HTTP API |
  | --- | --- |
  | `sbx repo status` | `GET /api/repos/{repo}`; the checklist: `GET /api/repos/{repo}/readiness` |
  | `sbx catalog show PACKAGE` | `GET /api/catalog/PACKAGE`; a slot's saved connections: `GET /api/catalog/KIND` (`connections`, for admins) |
  | `sbx attach PACKAGE --key KEY` | `POST /api/repos/{repo}/attachments` `{"package": "...", "key": "...", "inputs": {...}}` |
  | `sbx endpoints` | `GET /api/endpoints` |
  | `sbx recipe apply` | `PUT /api/repos/{repo}/recipe` `{"files": {".sbx/sbx.yaml": "...", ".sbx/setup": "..."}, "executable": [".sbx/setup"]}` |
  | `sbx recipe pr --status --json` | `GET /api/repos/{repo}/setup-pr` (`pull_request.url`; `record.problem` when sbx could not open it) |
  | `sbx recipe pr --yes` | `POST /api/repos/{repo}/setup-pr` `{"files": {...}, "executable": [...]}` |
  | `sbx up NAME` | `POST /api/sandboxes` `{"name": "NAME", "repo": "{repo}"}` (optional `"runtime"`); then `GET /api/sandboxes/NAME?wait=30` until `lifecycle.active` is false |
  | `sbx logs NAME`, why it failed | `GET /api/sandboxes/NAME/inspect?lines=200` (setup and process logs); `GET /api/sandboxes/NAME/detail` (`events`: each failure with its cause) |
  | `ssh sbx-NAME '...'`, `sbx sandbox exec NAME` | `POST /api/sandboxes/NAME/exec` `{"script": "...", "timeout": 120}` |
  | `sbx sandbox prepare\|restart\|pause\|resume NAME` | `POST /api/sandboxes/NAME/prepare` (or `/restart`, `/pause`, `/resume`) |
  | `sbx sandbox resources NAME` | `GET /api/sandboxes/NAME/resources?probe=true` |
  | `sbx down NAME` | `DELETE /api/sandboxes/NAME` |
- Never print, paste, commit or ask for a secret value in chat. The owner types
  values into `sbx env set` themselves.

## What the server knows

Catalog packages on this server: {services}

`{repo}` on this server:{env_now}

(Nothing after the colon means no values or connections are set yet.)

## 1. Check access

If `sbx` is not installed on this machine, install it from the server:
`curl -fsSL {control}/install | sh` (macOS). It ends with `sbx login`, which
opens the owner's browser: they are signed in there already, so it is one
click. Then run `sbx repo status`.

Check: `trust` is `trusted`. If it is empty (not served yet) and you are an
admin, go on: step 6's `sbx recipe apply` serves and trusts it. Otherwise stop
and tell the owner: an admin connects the repository in the web app first, and
an untrusted repository gets no services or secret values. Note `configured`:
whether the server already has a recipe for it.

## 2. Find out what the app needs

Read the README and docs, dependency manifests, Compose files, `.env.example`
and similar, ORM and migration config, seed and reset scripts, start scripts
and the wrappers around them, CI and deployment workflows. Write down:

- the product mode the owner runs and the command a developer starts it with.
  A lighter demo mode is not the same app; ask if the mode is unclear;
- every service it talks to, with the file that shows it;
- every environment variable it reads, and where each value comes from today:
  generated, a service's URL, an external account, or a secret loader.

If startup loads values through a secret manager (AWS Secrets Manager, Vault,
Doppler, ...), keep that loader and connect its identity; do not copy its
values into sbx. For AWS that is an `aws-role` connection plus the
`aws-profile` package.

Check: you have a list of services and a list of variables, each with evidence.

## 3. Choose services

Choose with the service selection guide appended below. Show the owner its
table before you change anything, and use the saved connections listed above
before asking for a new one. Never create a hosted account, pick a paid plan or
copy production data unless the owner says so. A managed server with an
endpoint (`sbx endpoints` lists them) gives every repository bound to it
access through that endpoint. Applying a recipe whose `connections:` names
that server (step 6) is the bind: ask the owner before you apply it.

Declare each service in `.sbx/sbx.yaml`:

```yaml
services:
  db:
    uses: postgres-clone          # the package
    connections: {server: dev-pg} # the slot, and the name of a saved connection
    with: {template: template0}   # the package's inputs
```

`dev-pg` stands for the name of a connection saved on this server: the list
above, or `sbx catalog show PACKAGE` (it lists the package's inputs, outputs,
the variables it sets and the saved connections each slot can take). With
one saved connection of the slot's kind, leave `connections:` out. A wrong
name stops the launch with an error that names the ones the server has.

A package input that is a secret goes to the server, never into the file:
`sbx attach PACKAGE --key db` asks for it hidden. If `sbx attach` reports
`needs_connection`, an admin adds that connection in the web app first.

Start each sandbox empty and let the repository's own migrations and seeds
fill it, unless the owner wants preloaded development data; that baseline is
prepared once on the shared server, never by `.sbx/setup`.

Check: the owner agreed to the table, and every provision package it names has
a connection.

## 4. Give every variable a source

Write each variable from step 2 into `env:` in `.sbx/sbx.yaml`:

| The value is | Write |
| --- | --- |
| A secret the app can generate | `SESSION_SECRET: ${random:32}` (also `${random:hex64}`, `${random:base64:32}`); stable per sandbox |
| A service output | `DATABASE_URL: ${services.db.url}` (`sbx catalog show PACKAGE` lists the outputs and the variables a package already sets) |
| A preview URL (origins, callbacks, CORS) | `${SBX_URL}`, or `${SBX_URL_<PREVIEW>}` for a named preview |
| Per-sandbox naming, such as a cookie prefix | `${SBX_NAME}` |
| A fixed setting | `NODE_ENV: development` |
| Something only the owner has (an API key, an account token) | declare it: list the name under `secrets:`, or use `${secret:NAME}` inside another value |

Declare every value the owner's chosen mode needs that sbx cannot generate or
provision. The declaration is how sbx knows a value is missing: `sbx repo
status` lists declared names without a value under `secrets_missing`, and the
web app flags them on the repository's Environment page. A needed value the
recipe never declares is invisible to that check and only fails at runtime.
Optional variables (an integration the owner doesn't use) stay out of the
recipe; list them in the hand-over (step 9).

```yaml
secrets: [STRIPE_SECRET_KEY, OPENAI_API_KEY]   # injected under their own names
env:
  SMTP_URL: smtp://mailer:${secret:SMTP_PASSWORD}@smtp.example.com:587
```

A `${secret:NAME}` without a value stops a launch; a name under `secrets:`
does not, the app just starts without it.

Check: every variable from step 2 has a source in `sbx.yaml`, and no value in
the file is a real secret.

## 5. Write the recipe

First read the appended worked example closest to this repository: its
decisions and pitfalls apply to apps like it.

```yaml
# .sbx/sbx.yaml
previews:
  "": 3000                      # the port that gets the main link
processes:
  web: npm run dev -- --host 0.0.0.0
health:
  - http://127.0.0.1:3000/
env:
  SESSION_SECRET: ${random:32}
```

- `.sbx/setup` installs or reconciles dependencies and runs the normal
  migrations and development seeds. It runs on create and on every prepare, so
  it must be executable and safe to repeat. sbx runs the recipe's scripts from
  its own copy (`/run/sbx/app`), not from the checkout, which has no `.sbx/`
  until the recipe is committed: start them with `cd "$SBX_WORKDIR"` (the
  checkout) rather than a path relative to the script, and keep `processes:`
  to commands that don't call files under `.sbx/`.
- `processes:` holds the repository's ordinary start commands. Bind web servers
  to `0.0.0.0`. Use `.sbx/start` only when a script is simpler; it starts its
  servers detached and returns.
- `health:` lists URLs that only answer when the app works. Without them a
  started app counts as unverified. Try each URL by hand inside a sandbox
  (`curl -i URL`) before you rely on it: a URL that never answers below 400
  holds the launch for the whole `health_timeout` (240 s by default).
- Leave out `template:` and `resources:` on the first pass. Sandboxes then get
  the default size (2 vCPU, 4 GiB RAM). Set `resources: {swap_mb: ...}` when
  setup is memory-hungry (a large `yarn install`, a native build), so every
  runtime gives it the same swap. Add other `resources:` only when
  measurements show the default is too small, and `template:` only when
  repeated setup dominates launch time (resource guide below). A size applies
  with or without `template:`, to sandboxes started after `sbx template build`.
- Add `lifecycle: {resume: restart}` only if the app's connections break
  after a pause.
- The recipe stays below 128 files and 1 MiB, with no symlinks.

The full format is in the
[reference](https://github.com/Batuhan1999/sbx/blob/main/docs/reference.md#recipe).

Check: `.sbx/setup` and any `.sbx/start` are executable, and running setup
twice in a row would leave the same state.

## 6. Put the recipe on the server

The server runs `sbx.yaml` from its own copy of the recipe: an operator copy,
else the one imported from the default branch. Your checkout's `sbx.yaml` is
not read until it gets there.

- If you are an admin on the server, run `sbx recipe apply` in the checkout.
  Repeat after every edit. Applying is what serves the repository and binds
  it to the managed servers its `connections:` name (step 3). Once a sandbox
  comes up healthy from the applied recipe (step 7), sbx commits it to the
  repository on its own: its GitHub App opens a draft pull request with
  `.sbx/`, and moves that pull request to the recipe that runs after every
  later healthy launch. You do not open it yourself.
- Otherwise, ask an admin to publish it: `sbx recipe pr --yes` in this
  checkout (publishing is an admin's). Once the owner merges the pull
  request, `sbx template build` imports the default branch's recipe.

Then run `sbx repo status` again. For each name under `secrets_missing`, ask
the owner to run `sbx env set NAME` (it asks for the value, hidden) or
`sbx env import FILE`.

Check: `configured` is `True`, `recipe_sha256` changed after your last apply,
and `secrets_missing` is empty or holds only values the owner chose to skip.

## 7. First launch

Run `sbx up check-a`. It returns with the preview links once health passes.

If it fails, `sbx up` prints the cause. Read `sbx logs check-a` and look inside
with `ssh sbx-check-a` (`/tmp/sbx-setup.log`, `/tmp/sbx-<process>.log`):

| The error says | Look at |
| --- | --- |
| `` `.sbx/setup` was killed (exit code 137: out of memory?) `` | `sudo dmesg \| grep -i oom` in the sandbox; then lower setup's parallelism or set `resources.swap_mb` / `memory_mb` (resource guide) |
| `` `.sbx/setup` exited with code N `` | the end of `/tmp/sbx-setup.log` |
| `the app did not come up: after N s, URL answered CODE` (or `did not answer`) | the process logs, and the health URL by hand inside the sandbox |
| `the app did not come up: a process of the app exited` | that process's log |

While the app starts, the operation holds the sandbox: `prepare` or `restart`
answers 409 "busy" until health passes or `health_timeout` runs out.

After a recipe edit, apply it again (step 6) and rerun setup and start on the
same sandbox, keeping its source edits, with `sbx sandbox prepare check-a`.
`sbx sandbox restart check-a` only restarts the processes. A changed service,
package or resource size needs a new sandbox.

Before changing `resources:`, read what the sandbox actually got with
`sbx sandbox resources check-a`.

If you applied the recipe, run `sbx recipe pr --status`. It prints the
recipe's pull request, or why sbx could not open it (the GitHub App is
missing, not installed on the repository, or may not push there) and exits 1.
sbx opens the pull request within seconds of the launch: "no recipe pull
request yet" means run it again shortly. Tell the owner a reason as printed:
an admin fixes the App's access, and the next healthy launch
(`sbx sandbox prepare check-a`) opens the pull request.

Check: `sbx up` returned with the preview links, which means the recipe's
health check passed. Open the preview link once yourself to see the app's
page, not an error. The owner judges the app from there.

## 8. Hand over

Run `sbx repo status` once more (an admin may also run `sbx recipe pr
--status`): the pull request must carry the recipe you verified
(`recipe_pr` without "the recipe changed since").

Tell the owner: `check-a`'s preview link, to open and judge the app; the
recipe's pull request link (or that the default branch has the recipe
already, or, if sbx could not open it, the reason it printed, and that the
files in `.sbx/` are theirs to commit); what you verified and what you did
not (optional providers left unconfigured, for example); the optional
variables the app reads that the recipe leaves out; and the everyday
commands: `sbx up NAME`, `sbx logs NAME`, `sbx env ls`, `sbx down NAME`
(`sbx down check-a` once they are done with it). Say that `check-a` keeps
running for now: it pauses by itself after two hours without use and wakes
when its link is opened; `sbx sandbox pause check-a` pauses it at once (on a
hosted runtime a running sandbox is billed). A value the recipe
generates, such as an admin password from `${random:32}`, is per sandbox;
give the command that reads it, not the value:
`echo '. /run/sbx/app-env; printenv NAME' | sbx sandbox exec SANDBOX`. When
a pull request is open, ask the owner to review and merge it: until then the
recipe lives only on this server. PR previews and a prebuilt template come
later, and only when asked.

Done means `check-a` is up and healthy, the owner has its preview link, and
the hand-over names the recipe's pull request link (or why there is none).
