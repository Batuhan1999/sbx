# Install sbx and onboard a repository

This guide is for a coding agent (or a person) taking a fresh server to a
first repository running in sandboxes. The recommended path splits the work:

1. **You install** sbx on the server (steps 1 and 2) and hand the owner the
   onboarding link the installer prints last (step 3).
2. **The owner does the setup page** in their browser: domain, sandboxes,
   account, GitHub ([the owner's guide](howto/setup.md)).
3. **You connect the repository** with the server's setup brief (step 4): the
   recipe, one sandbox, its preview link.

A server that must be set up with no browser at all (automation, a lab) takes
the [CLI path](#without-a-browser-automation) instead of step 3.

**Where commands run.** Every command runs on your own machine. Commands for
the server go through `ssh HOST`, so each block can be pasted as it is, and
nothing depends on a variable or a `cd` from an earlier block.

**Placeholders.** `HOST` (what you pass to `ssh`, with sudo that asks no
password), `IP` (the server's public IP, step 1), `D` (the domain the owner
chooses; the server answers at `app.D`), `owner@example.com` (the owner, the
first admin), `KIND` (a runtime: `embed`, `e2b-cloud` or `boat`). In step 5,
`dev-pg`, `temporal`, `10.0.0.5:7233`, `10.0.0.0/24` and `owner/app` are
examples.

**Secrets.** API keys, tokens, passwords and the onboarding link never go into
a shared chat, a command line, a recipe, a commit or a log. The owner types
keys into sbx's own pages; on the CLI path, secrets come on stdin from a file
only the owner and you can read.

## 1. Inspect the server

Download the latest release onto the server, check its checksum, and unpack
it to `~/sbx` there. The last line printed is the release's tag:

```sh
ssh HOST sh -s <<'SH'
set -e
cd ~
v=$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/Batuhan1999/sbx/releases/latest)
v=${v##*/} url=https://github.com/Batuhan1999/sbx/releases/download
curl -fsSLO "$url/$v/sbx-$v.tar.gz"
curl -fsSL "$url/$v/sbx-$v.tar.gz.sha256" | sha256sum -c
tar -xzf "sbx-$v.tar.gz"
ln -sfn "sbx-$v" sbx
echo "$v"
SH
```

`sha256sum -c` prints `sbx-v….tar.gz: OK`; anything else means a damaged
download: run it again. Then check the host:

```sh
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
  says why it is missing and how to fix it. Without it, the owner picks E2B
  Cloud or Boat on the setup page and needs that provider's API key
  ([E2B](howto/e2b.md#api-key), [Boat](howto/boat.md#api-key)); tell them
  before they start.
- **public IP.** Where the DNS records point. `unknown` means no outbound
  HTTPS, which sbx needs.
- **ports.** Something other than sbx on 80 or 443 on a public address: ask
  the owner whether to stop it. Loopback listeners don't matter.
- **sbx: installed.** Running `install.sh` again upgrades it; use
  [operating the host](reference.md#operating-the-host) instead of this guide.

The server can't see its provider's firewall. Before the install nothing
listens, so from your machine `nc -vz -w 5 IP 443` says `refused` when the
port reaches the server and `timed out` when a firewall drops it. Ask the
owner to open a dropped one:

| Port | Needed for |
| --- | --- |
| TCP 22, 443 | always |
| TCP 80 | the installer's trusted certificate for the IP, so the onboarding link opens without a browser warning |
| UDP and TCP 53 | the domain's certificate: the server answers its DNS check itself |
| TCP 21000–21255 | IP-only access (CLI path) only: one port per preview |

## 2. Install

The same command in every case. It takes several minutes on a provider host
and longer with Embed. Start it detached, so a dropped connection can't stop
it, then check every minute or two until it prints an exit status:

```sh
ssh HOST 'cd ~/sbx && sudo rm -f /root/sbx-install.exit && sudo nohup sh -c "sh install.sh > /root/sbx-install.log 2>&1; echo \$? > /root/sbx-install.exit" < /dev/null > /dev/null 2>&1 &'
ssh HOST 'sudo grep -v "#t=" /root/sbx-install.log | tail -n 8; sudo cat /root/sbx-install.exit 2>/dev/null || echo running'
```

The log is root's alone; the `grep` keeps the onboarding link out of your
output.

- **`0`**: done. Before its link, the installer says how the first visit's
  HTTPS went (`==> HTTPS for the first visit`): `ok: trusted certificate for
  IP` (Let's Encrypt over TCP 80), or `self-signed for now: ...` with the
  reason (no public IP, port 80 in use, or Certbot's error: usually TCP 80
  closed in a cloud firewall). Self-signed means the owner's browser warns
  once, until the domain's certificate is issued. A provider host also says
  `No sandbox runtime yet`: the setup page's **Sandboxes** step adds one.
- **Another number**: read the `FAILED: ...` line and
  `ssh HOST 'sudo grep -v "#t=" /root/sbx-install.log | tail -n 40'`, fix the
  cause and start the same command again: every step is safe to repeat.

## 3. Hand the owner the onboarding link

The installer's last line is the onboarding link,
`==> Open the onboarding: https://IP/app/setup#t=TOKEN`, with a one-time
token. Running the installer again during the setup prints the same link, and
so does `sudo sbx-admin setup-link` on the server (`--new` replaces it, and
the old one stops working).

Give the owner that link, with the [owner's guide](howto/setup.md), in your
conversation with them: they open it and do the rest. It is a one-time
credential for this setup, so never put it in a shared channel, an issue, a
commit or a log; once the setup finishes it stops working. The page then
takes them through:

- **Domain**: they choose `D` and add `D A IP`, `*.D A IP` and
  `_acme-challenge.D NS D.`; the page checks the records and that the server
  answers on port 53 from outside. A blocked port 53 means allowing TCP and
  UDP 53 in the provider's firewall. Where port 53 can't open at all, set the
  address on the [CLI path](#2-the-address) with a DNS API token, then the
  owner goes on from **Sandboxes**.
- **Sandboxes**: Embed on a `/dev/kvm` host, shown as chosen; otherwise E2B
  Cloud or Boat with the API key alone. Templates start building here.
- **Account**: their email and password; it finishes the setup.
- **GitHub**: the sbx GitHub App, or **Skip for now**.

It ends in the control plane with **Your server is ready**. Wait for the owner
to say so.

## 4. The repository

The owner can start this from the web app: **Connect a repository**, pick it,
**Copy prompt**, and paste the prompt to you in a checkout of the repository.
The prompt is the server's setup brief; `sbx repo brief` in the checkout
prints the same text (API: `GET /api/repos/OWNER/NAME/setup-brief`).

The brief expects the desktop CLI (macOS) signed in as the owner. Install it
from the server:

```sh
curl -fsSL https://app.D/install | sh
```

It ends with `sbx login`, which opens the owner's browser: one click, since
they are signed in there (if that was skipped, run `~/.local/bin/sbx login`).
Then, in the checkout:

```sh
sbx repo brief             # the setup brief; or: sbx repo connect --agent
```

Follow the brief from the server, not its
[template](../control/sbx_control/template_files/setup-brief.md). It goes from
discovery to one healthy sandbox: services by
[service selection](service-selection.md), a small
[recipe](reference.md#recipe), sizes from the
[resource guide](../control/sbx_control/template_files/resource-guide.md)
within the runtime's limits (`sbx runtimes`). A value only the owner has goes
in with `sbx env set NAME` (typed by the owner, hidden) or a key page
(`sbx secrets request repo:OWNER/NAME:VAR "LABEL"`, once the repository is
connected). It ends with `sbx up check-a` and its preview link for the owner.
Once that sandbox is healthy, the GitHub App opens the pull request that adds
`.sbx/` to the repository; without the App (a public repository cloned
anonymously) `sbx repo status` says why there is none under `recipe_pr`.

`sbx repo connect --agent` serves the repository and starts Claude Code (or
`--agent codex`) with the same brief.

## 5. Optional: shared servers

Only when the repository needs a service on the server: a managed Postgres or
ClickHouse whose databases each sandbox clones, or a host on the owner's
private network. Hosted catalog packages (Neon, PlanetScale, ...) need none.

```sh
ssh HOST sh -s <<'SH'
sudo sbx-admin server up dev-pg --kind postgres                         # or --kind clickhouse
sudo sbx-admin config set network '{"internal_cidrs": ["10.0.0.0/24"]}'  # only for a private host
sudo sbx-admin network apply                                            # Embed: before sandboxes use either
SH
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

## 6. Verify and report

```sh
ssh HOST sudo sbx-admin doctor                 # each part: ok / NO with the fix; exit 1 when one is NO
sbx runtimes test KIND                         # per enabled runtime; exit 1 on a NO
sbx templates --wait                           # step k of n per template; non-zero when one fails
curl -sS -o /dev/null -w '%{http_code}\n' https://app.D/   # a 200 or 30x, no TLS error
```

Fix every `NO` you can; one that needs the owner goes into the report as
theirs. Report to the owner: `check-a`'s preview link, the recipe pull
request's link (or why there is none), the everyday commands (`sbx up`,
`sbx logs`, `sbx env`, `sbx down`), what you verified, with the evidence, and
what you did not.

## Without a browser (automation)

The whole setup through the CLI, for automation and test servers: the same
steps as the setup page, through the onboarding link. Do steps 1 and 2 first.
Copy the release's CLI (one Python 3 file, no dependencies) and the link into
files only you can read; the link is a credential, read from a file
(`--link-file`) or `SBX_SETUP_LINK`, never the command line:

```sh
mkdir -m 700 -p ~/.sbx-install
scp HOST:sbx/cli/sbx.py ~/.sbx-install/sbx.py
(umask 077; ssh HOST 'sudo sbx-admin setup-link' > ~/.sbx-install/setup-link)
python3 ~/.sbx-install/sbx.py setup status --link-file ~/.sbx-install/setup-link
```

When the installer said `self-signed for now`, add `--insecure` to every
`sbx.py` command until the address is ready: it accepts the self-signed
certificate. Every command that waits takes `--timeout SECONDS` and exits
non-zero when it passes.

### 1. The owner's secrets

Each secret comes on stdin, one line each, from a file only the owner and you
can read: the owner writes it with `(umask 077; pbpaste > FILE)` on macOS
with the value copied, or `(umask 077; read -rs k; printf '%s\n' "$k" > FILE)`
anywhere. Check it with `wc -c < FILE` (never print it) and delete it once
used.

### 2. The address

Each line waits until HTTPS works (default 900 s; it notes its progress at
least every 30 seconds):

| Decision | Command |
| --- | --- |
| domain (the setup page's way: records `D A IP`, `*.D A IP`, `_acme-challenge.D NS D.`; TCP and UDP 53 open) | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --domain D` |
| domain, port 53 can't open: Cloudflare (the two `A` records, no `NS`; [token](howto/cloudflare.md#api-token)) | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --domain D --dns cloudflare --secret cloudflare_api_token < cloudflare-token-file` |
| domain, port 53 can't open: Route 53 (the two `A` records; the instance role on EC2) | the same as the first line with `--dns route53` |
| IP only (Embed only; TCP 80 and 21000–21255 open) | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --ip` |
| quick test (`D` = the IP with dashes plus `.sslip.io`, no records) | the domain line with `--dns none`; browsers warn and the desktop CLI refuses it, so use the HTTP API with `curl -k` after it and keep `--insecure` |

`--email` is optional: a contact for the Let's Encrypt account, which sends no
expiry emails. `address ready: https://app.D` means done. `Check the DNS
records ...` names each check that failed and its fix: a missing record, or
another record at `_acme-challenge.D` to remove. The wait prints the
certificate's error as soon as an issuance fails: `port 53: no public resolver
... reached this server` means a firewall drops port 53 (ask the owner to
allow TCP and UDP 53, or use a token); anything else is Let's Encrypt's own
error. Once fixed, run the same command again: it retries. A wait for an IP
that runs out is usually a closed port 80.

The link now names the trusted address; fetch it again (same token):

```sh
(umask 077; ssh HOST 'sudo sbx-admin setup-link' > ~/.sbx-install/setup-link)
```

### 3. Sandbox runtime

A `/dev/kvm` host already has Embed. Otherwise add a provider with its API key
on stdin (`BOAT_API_KEY` for Boat):

```sh
python3 ~/.sbx-install/sbx.py setup runtime available --link-file ~/.sbx-install/setup-link
python3 ~/.sbx-install/sbx.py setup runtime add e2b-cloud --link-file ~/.sbx-install/setup-link --secret E2B_CLOUD_API_KEY < e2b-key-file
```

Without `--plan`, Boat takes the plan its key's account is on (a trial key is
refused: the trial saves no templates) and E2B Cloud takes Hobby's limits;
`--plan pro` sets E2B Pro. It waits through the server's restart (default
600 s). Templates start building now; follow them with
`python3 ~/.sbx-install/sbx.py templates --wait --link-file ~/.sbx-install/setup-link`
(default 1800 s).

### 4. Account and finish

The owner's own account, with their password on stdin:

```sh
python3 ~/.sbx-install/sbx.py setup admin owner@example.com --link-file ~/.sbx-install/setup-link < password-file
```

Then finish, either signed in (the desktop CLI: `curl -fsSL https://app.D/install | sh`
ends with `sbx login`, which works while the setup is open) or with the
owner's password again:

```sh
sbx setup finish --link-file ~/.sbx-install/setup-link
python3 ~/.sbx-install/sbx.py setup finish --link-file ~/.sbx-install/setup-link --admin owner@example.com < password-file
```

`finish` runs the server's checks (`The server needs attention` lists each
failed one: fix it and run `finish` again), then prints `set up:
https://app.D; ...`. The link stops working. GitHub is then connected under
**Extensions** ([how-to](howto/github.md)), and the repository follows
[step 4](#4-the-repository).

**Without the desktop CLI** (Linux, or the quick test's certificate), use the
[HTTP API](reference.md#http-api) with an admin token in a header file that
holds nothing else:

```sh
(umask 077; ssh HOST sudo sbx-admin token create owner@example.com --label agent | sed 's/^/Authorization: Bearer /' > ~/.sbx-install/api-header)
curl -fsS -H @$HOME/.sbx-install/api-header https://app.D/api/me    # -k for the quick test
```

`sbx-admin token ls` and `token revoke ID` end it.

## Acceptance checklist

This checklist verifies changes to sbx itself (lifecycle, services, runtimes,
recipes in [examples](../examples/)) on a test server. Onboarding a
repository does not run it: that ends at one healthy sandbox and its preview
link ([step 4](#4-the-repository)). Run it for a repository too when its
owner wants the isolation between sandboxes proven.

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
