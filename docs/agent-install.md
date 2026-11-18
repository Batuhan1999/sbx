# Install sbx and onboard a repository

This guide takes a coding agent (or a person) from a fresh server to a first
repository running in sandboxes. It ends with the
[acceptance checklist](#acceptance-checklist).

**How to work.** Check first, then decide. Ask the owner **once**, in one
message (step 2), for everything only they can give, and wait for the
answers before you do anything that depends on them. After that, the owner
gets two links from you: the setup link to create their account (step 4) and
one key page for every key (step 6). Don't stop for approval between routine
steps. Keep the current step visible and resolve a failed step before you
call it done.

**Where commands run.** Every command runs on your own machine. Commands for
the server go through `ssh HOST`, so each block can be pasted as it is, and
nothing depends on a variable or a `cd` from an earlier block.

**Placeholders.** `HOST` (what you pass to `ssh`, with sudo that asks no
password), `IP` (the server's public IP, step 1), `D` (the domain), `E` (the
email for certificate notices), `owner@example.com` (the owner, the first
admin), `KIND` (a runtime: `embed`, `e2b-cloud` or `boat`), `PLAN` (its plan).
In step 8, `dev-pg`, `temporal`, `10.0.0.5:7233`, `10.0.0.0/24` and
`owner/app` are examples.

**Secrets.** API keys, tokens, passwords and the setup link never go into the
chat, a command line, a recipe, a commit or a log. The owner types keys into
sbx's own key page (step 6), which stores each one where it belongs. The
setup link lives in a file under `~/.sbx-install` on your machine:

```sh
mkdir -m 700 -p ~/.sbx-install
```

## 1. Inspect the server

Download the latest release onto the server, check its checksum, and unpack
it to `~/sbx` there. The last line printed is the release's tag:

```sh
ssh HOST sh -s <<'EOF'
set -e
cd ~
v=$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/Batuhan1999/sbx/releases/latest)
v=${v##*/} url=https://github.com/Batuhan1999/sbx/releases/download
curl -fsSLO "$url/$v/sbx-$v.tar.gz"
curl -fsSL "$url/$v/sbx-$v.tar.gz.sha256" | sha256sum -c
tar -xzf "sbx-$v.tar.gz"
ln -sfn "sbx-$v" sbx
echo "$v"
EOF
```

`sha256sum -c` prints `sbx-v….tar.gz: OK`; anything else means a damaged
download: run it again. Copy that release's CLI (one Python 3 file, no
dependencies) to your machine, then check the host:

```sh
scp HOST:sbx/cli/sbx.py ~/.sbx-install/sbx.py
ssh HOST 'cd ~/sbx && sudo sh install.sh --check'
```

`--check` installs nothing. It prints the facts and the runtimes this host can
run, and exits 1 when sbx can't be installed here:

```text
os:         Ubuntu 24.04.1 LTS
memory:     15988 MiB
disk:       142 GiB free on /
/dev/kvm:   no; a VM without nested virtualization: enable it at the provider, or use bare metal
public IP:  203.0.113.10
port 80:    free
port 443:   free
sbx:        not installed
runtimes this host can run:
  embed      no: no /dev/kvm: use bare metal or a VM with nested virtualization enabled
  e2b-cloud  yes: needs an E2B API key and a domain pointing here
  boat       yes: needs a Boat API key (a paid plan: the trial saves no templates) and a domain pointing here
```

- **os.** Only Ubuntu 24.04. On anything else, stop and ask for one.
- **/dev/kvm.** E2B Embed runs sandboxes on this server and needs it; the line
  says why it is missing and how to fix it.
- **public IP.** Where DNS records point. `unknown` means no outbound HTTPS,
  which sbx needs.
- **ports.** Something other than sbx on 80 or 443 on a public address: ask
  the owner whether to stop it. Loopback listeners don't matter.
- **sbx: installed.** Running `install.sh` again upgrades it; use
  [operating the host](reference.md#operating-the-host) instead of this guide.

The server can't see its provider's firewall. Before the install nothing
listens, so from your machine `nc -vz -w 5 IP 443` says `refused` when the
port reaches the server and `timed out` when a firewall drops it. Check each
port the decisions below need; ask the owner to open a dropped one.

| Port | Needed for |
| --- | --- |
| TCP 22, 443 | always |
| TCP 80 | IP access (the IP certificate) |
| UDP and TCP 53 | a domain with `--dns delegated` |
| TCP 21000–21255 | IP access only: one port per preview |

## 2. Ask the owner, once

Work out what you can from step 1, then send **one message** with every
question below that applies, each with its how-to link, and wait for all the
answers. Ask nothing that `--check` already answered.

1. **Where sandboxes run** ([runtimes](reference.md#runtimes)).
   - `embed: yes`: E2B Embed on this server. Nothing to ask; each sandbox
     takes 4 GiB unless its recipe asks for more, and 12 GiB stays for the
     host. Ask about a provider only if the repositories need more room.
   - `embed: no`: say why (the line from `--check`) and ask which provider
     and plan. Several can be enabled; both run the same recipes.

     | | E2B Cloud ([how-to](howto/e2b.md)) | Boat ([how-to](howto/boat.md)) |
     |---|---|---|
     | New sandbox ready | about 1–2 s | about 5–6 s |
     | Pause and resume | keeps memory: processes continue | keeps the disk; sbx restarts the app on every resume, and after 10 minutes paused a resume is a cold boot |
     | Plans | Hobby: 1 h before an idle sandbox pauses, 8 vCPUs, 8 GiB, 10 at once. Pro: 24 h, 20 GiB disk, 100 at once | `20`: up to 8 vCPUs, 16 GiB, 100 at once; `100` and up: 16 vCPUs, 32 GiB. The trial saves no snapshots, which sbx's templates are: `20` or above |
     | Price | per second while a sandbox runs | per second while a sandbox runs (4 vCPUs, 8 GiB: about $0.04 an hour) |

     The owner creates the API key in the provider's dashboard (the how-to
     shows where) and keeps it ready for the key page; it never goes into the
     chat. Untrusted public repositories can't be served while Boat is
     enabled (it has no per-sandbox egress rules).
2. **The address.** A provider runtime and [endpoints](reference.md#endpoints)
   need a domain; Embed alone also works on the IP (a trusted IP certificate
   on port 80, previews on ports 21000–21255). For a domain ask for:
   - the domain `D` (usually a subdomain, `sbx.example.com`): the server
     answers at `app.D`, previews at `<name>.D`;
   - an email `E` for Let's Encrypt's expiry notices;
   - how the wildcard certificate is issued: `delegated` (no credential:
     records `D A IP`, `*.D A IP`, `_acme-challenge.D NS D.`, and port 53
     open), `cloudflare` (the A records and an API token that may edit the
     zone's DNS: [how-to](howto/cloudflare.md)), `route53` (the A records; the
     instance role on EC2) or `none` (self-signed, a quick test only: `D`
     = the IP with dashes plus `.sslip.io` needs no records; browsers warn and
     the desktop CLI refuses it, so steps 5 to 10 go through the
     [HTTP API](reference.md#http-api) with `curl -k`).
3. **The owner's email** for their admin account (`owner@example.com`).
4. **Which coding agents the team uses**: none, [Amp](howto/amp.md),
   [Claude Code](claude-code.md). Every template is built with the ones
   chosen; their keys can come later.
5. **The repository** (`owner/app`) and access to it: the GitHub App
   ([how-to](howto/github.md)); a public repository clones without it.

Once the owner says DNS records are in, check them before you go on:

```sh
dig +short app.D @1.1.1.1; dig +short anything.D @1.1.1.1   # both: the public IP
dig +short NS _acme-challenge.D @1.1.1.1                      # delegated: D.
```

## 3. Install

The same command in every case. It takes several minutes on a provider host
and longer with Embed. Start it detached, so a dropped connection can't stop
it, then check every minute or two until it prints an exit status:

```sh
ssh HOST 'cd ~/sbx && sudo rm -f /root/sbx-install.exit && sudo nohup sh -c "sh install.sh > /root/sbx-install.log 2>&1; echo \$? > /root/sbx-install.exit" < /dev/null > /dev/null 2>&1 &'
ssh HOST 'sudo grep -v "#t=" /root/sbx-install.log | tail -n 5; sudo cat /root/sbx-install.exit 2>/dev/null || echo running'
```

The `grep` keeps the setup link out of your output.

- **`0`**: done. `==> Done: finish the setup in your browser`; a provider host
  also says `No sandbox runtime yet` (the key page adds it). No template is
  built yet, on any host: templates build once the setup is finished, with the
  coding agents chosen (step 7).
- **Another number**: read the `FAILED: ...` line and
  `ssh HOST 'sudo grep -v "#t=" /root/sbx-install.log | tail -n 40'`, fix the
  cause and start the same command again: every step is safe to repeat.

## 4. The setup link: the owner creates their account

Fetch the setup link into a file without printing it. Each call makes a new
link and the previous one stops working:

```sh
ssh HOST sudo sbx-admin setup-link > ~/.sbx-install/setup-link
python3 ~/.sbx-install/sbx.py setup status --link-file ~/.sbx-install/setup-link --insecure
```

`--insecure` accepts the self-signed certificate the server uses until it has
an address. **First set the address** when it needs no credential, so the link
opens on the account step. Each line waits until HTTPS works (up to 15
minutes):

| Decision | Command |
| --- | --- |
| domain, `delegated` | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --insecure --domain D --email E` |
| domain, `route53` on EC2 | the same with `--dns route53` |
| IP only | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --insecure --ip` |
| quick test | the same as `delegated` with `--dns none` (keep `--insecure` on every command after it) |
| domain, `cloudflare` | none: the setup page's first screen asks for the domain and the token, and the owner pastes it there |

`address ready: https://app.D` means done; `Check the DNS records ...` names
the record to fix; a `waiting for the certificate` that runs out is usually a
closed port (53 for `delegated`, 80 for IP). Then fetch the link again, which
now names the trusted address:

```sh
ssh HOST sudo sbx-admin setup-link > ~/.sbx-install/setup-link
```

Send the owner the link (`cat ~/.sbx-install/setup-link` in a terminal of
their own, or yours if they trust it there; never in a shared log), with:
**"Create your account here."**, plus the owner's guide
[howto/setup.md](howto/setup.md#create-your-account), which shows the screen.
They create it with `owner@example.com` and a password of their own; that
signs them in. `setup status` then shows `admin`
as `done`.

## 5. Sign in the CLI as the owner

Right after the account exists, sign the desktop CLI in (macOS, with a trusted
certificate; the installer and `sbx login` work while the setup is still
open):

```sh
curl -fsSL https://app.D/install | sh    # IP only: https://IP/install
```

It ends with `sbx login`, which opens the browser: the owner signs in there
(if that was skipped, run `~/.local/bin/sbx login`). Below, `sbx` means
`~/.local/bin/sbx`; it now acts as the admin, so you finish the setup yourself
(step 7). **Without the desktop CLI** (Linux, or the quick test's
certificate), use the [HTTP API](reference.md#http-api) with an admin token in
a header file that holds nothing else:

```sh
(umask 077; ssh HOST sudo sbx-admin token create owner@example.com --label agent | sed 's/^/Authorization: Bearer /' > ~/.sbx-install/api-header)
curl -fsS -H @$HOME/.sbx-install/api-header https://app.D/api/me    # -k for the quick test
```

`sbx-admin token ls` and `token revoke ID` end it.

## 6. One key page for every key

Request every key the owner holds in **one** page: the runtime's API key, the
DNS token, an agent's token, a repository's variables. `sbx secrets request`
returns a URL on the web app that writes each key into the right store; the
owner, signed in, submits it there. With `--wait` it returns when all are
saved, non-zero when a key is refused or the request is cancelled. During the
setup it works with the setup link (`--link-file` or `SBX_SETUP_LINK`, for
`request`, `status`, `ls` and `cancel`), or as the signed-in admin:

```sh
sbx secrets request --wait --link-file ~/.sbx-install/setup-link \
  runtime:e2b-cloud "E2B API key" \
  connector:amp "Amp orb provider token"
```

Targets: `runtime:KIND` (`e2b-cloud`, `boat`; one not enabled yet is enabled
on its first plan), `dns:cloudflare`, `connector:KIND` (`amp`, `claude`),
`repo:OWNER/NAME:VAR` (`sbx secrets request --help` lists them). Send the
owner the URL it prints. Then `sbx setup status` shows the runtime `done`; set
the owner's plan if it is not the first one: `sbx runtimes set KIND --plan
PLAN`. Send the owner the guide's
[keys page section](howto/setup.md#paste-your-keys) with the URL the first
time: it shows the page and what happens after they save.

**No web app reachable** (the server isn't reachable from the owner's
browser): the owner writes the key into a file only they and you can read,
`(umask 077; pbpaste > ~/.sbx-install/e2b-key)` on macOS with the key copied,
or `(umask 077; read -rs k; printf '%s\n' "$k" > ~/.sbx-install/e2b-key)`
anywhere. Check it with `wc -c <` (never print it), hand it to sbx with
`python3 ~/.sbx-install/sbx.py setup runtime add KIND --link-file ~/.sbx-install/setup-link --plan PLAN --secret E2B_CLOUD_API_KEY < ~/.sbx-install/e2b-key`
(`BOAT_API_KEY` for Boat), and delete the file.

## 7. Coding agents, then finish: the first template build

Record the owner's answer from step 2, then finish. Nothing has built yet:
templates wait for this choice, so the first build already has every chosen
agent's layer and nothing rebuilds when its key arrives:

```sh
sbx setup agents amp claude --link-file ~/.sbx-install/setup-link   # or: sbx setup agents none
sbx setup status --link-file ~/.sbx-install/setup-link              # every step done
sbx setup finish                                                    # as the signed-in admin: no password
```

Without the desktop CLI: `curl -fsS -X POST -H @$HOME/.sbx-install/api-header
https://app.D/api/setup/complete`. The owner may press **Finish setup** on
the setup page instead; both work. `finish` runs the server's checks
(`The server needs attention` lists each failed one: fix it and run `finish`
again), then prints `set up: https://app.D; ... the templates build now`.
The base and default templates take about 10 minutes the first time:
`sbx templates` shows them; wait until `sbx-base` and `default` are `current`
on each runtime.

**An agent chosen later** (`sbx extensions set amp --secret provider_token < FILE`, a key page with `connector:amp`, or the
web app's Extensions page) changes every template: the answer says how many
rebuild on each runtime; existing sandboxes keep their templates and new ones
start from the old ones until the rebuilds finish. Add `--wait` to wait for
them (non-zero when one fails).

## 8. Optional: shared servers

Only when the repository needs a service on the server: a managed Postgres or
ClickHouse whose databases each sandbox clones, or a host on the owner's
private network. Hosted catalog packages (Neon, PlanetScale, ...) need none.

```sh
ssh HOST sh -s <<'EOF'
sudo sbx-admin server up dev-pg --kind postgres                         # or --kind clickhouse
sudo sbx-admin config set network '{"internal_cidrs": ["10.0.0.0/24"]}'  # only for a private host
sudo sbx-admin network apply                                            # Embed: before sandboxes use either
EOF
```

On Embed, sandboxes reach these directly. On E2B Cloud or Boat they reach them
only through an [endpoint](reference.md#endpoints): the service at
`<name>.D:443`, behind TLS with a client certificate per sandbox. **Ask the
owner before you add one**, naming the service and its address, and before
you apply a recipe whose `connections:` name a managed server with one:

```sh
sbx endpoints add --server dev-pg                # waits until it is on; non-zero with the reason
sbx endpoints add --name temporal --target 10.0.0.5:7233 --repo owner/app
```

Check with a real query from a sandbox (`sbx sandbox exec`), not only that it
starts.

## 9. The repository

1. **Access.** The owner connects GitHub on the **Extensions** page
   ([how-to](howto/github.md)) and selects the repository.
2. **Recipe.** In the repository's checkout, `sbx repo connect --agent`
   serves it, drafts `.sbx/` and starts Claude Code (or `--agent codex`) with
   the server's setup brief; without `--agent`, `sbx repo brief` prints the
   brief for you to follow (API: `GET /api/repos/OWNER/NAME/setup-brief`).
   Read it from the server, not its
   [template](../control/sbx_control/template_files/setup-brief.md). It goes
   from discovery to a verified first launch: services by
   [service selection](service-selection.md), a small
   [recipe](reference.md#recipe), sizes from the
   [resource guide](../control/sbx_control/template_files/resource-guide.md)
   within the runtime's limits (`sbx runtimes`). A secret the recipe needs and
   you can't generate goes on the key page (`repo:OWNER/NAME:VAR`, step 6).
3. **The recipe PR.** Once the recipe passes its first launch, the GitHub App
   opens the pull request that adds `.sbx/` to the repository. Report its link
   to the owner.
4. **Coding agents.** Start one session of each chosen agent to confirm its
   sandbox comes up ([Claude Code](claude-code.md), [Amp](amp-orbs.md)).
   Other agents reach sandboxes over SSH (`sbx ssh-config`) or with
   `sbx open NAME AGENT`.

## 10. Verify and report

```sh
ssh HOST sudo sbx-admin doctor                 # each part: ok / NO with the fix; exit 1 when one is NO
sbx runtimes test KIND                         # per enabled runtime; exit 1 on a NO
ssh HOST sudo sbx-admin runtime verify KIND    # create, command, file, pause/resume, preview, delete
curl -sS -o /dev/null -w '%{http_code}\n' https://app.D/   # a 200 or 30x, no TLS error
```

Fix every `NO` you can; one that needs the owner goes into the report as
theirs. `runtimes test` counts only once it reports `Base template: built` and
a reachability line. Then run the [acceptance checklist](#acceptance-checklist)
on each runtime the repository uses.

Report to the owner: the address, the runtimes and plans and the default, the
coding agents, any endpoints and who may use them; the recipe PR's link;
working preview links and the commands to start (`sbx up`), read logs
(`sbx logs`), change variables (`sbx env`) and delete (`sbx down`) a sandbox;
how to read a generated value (`echo '. /run/sbx/app-env; printenv NAME' |
sbx sandbox exec SANDBOX`, never the value itself); what you verified, with
the evidence, and what you did not; what is limited (a `--dns none`
certificate, a plan's limits) and what remains theirs (keys to rotate).

## Acceptance checklist

Every sandbox is a development environment of its own: it must be able to
migrate, seed and reset its data without changing another sandbox's data or
schema. Shared service infrastructure keeps this cheap, but the databases,
branches or namespaces on it belong to one sandbox each. This is the one
place that rule is stated; other docs link here.

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

Then check, with two disposable, named sandboxes A and B on each runtime the
repository uses (`sbx up check-a`, and `check-b`; a name is lowercase
letters, digits and dashes). Names are installation-wide: on each further
runtime, add its kind (`sbx up check-a-boat --runtime boat`, `check-b-boat`).
A recipe that passes on one runtime can fail on another.

- [ ] Both launch cleanly from the recipe and pass a useful app action.
- [ ] Migrate and seed both. Change or reset A's data; B still has its original
      data and schema.
- [ ] Restart A (`sbx sandbox restart check-a`), then pause and resume it
      (`sbx sandbox pause|resume check-a`, or `POST /api/sandboxes/check-a/pause` and `/resume`):
      its working data is still there. (On Boat sbx restarts the app on every
      resume, and a pause longer than 10 minutes makes it a cold boot: pause
      that long once to check the app starts again from the recipe.)
- [ ] On a provider runtime with an endpoint: a real query from a sandbox
      reaches the shared server through it.
- [ ] Delete A (`sbx down check-a`): B still works, and A's owned resources (its
      database, branch, role or namespace) are gone while the shared servers stay.
- [ ] No secret value appears in the recipe, the template, commits or logs.

Record what you verified and what you did not (for example optional providers
that were never configured).
