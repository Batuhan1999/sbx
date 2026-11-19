# Install sbx and onboard a repository

This guide is for a coding agent (or a person) taking a fresh server to a
first repository running in sandboxes. The recommended path splits the work:

1. **You install** sbx on the server (steps 1 and 2) and give the owner the
   onboarding page's address, which the installer prints last (step 3).
2. **The owner does the onboarding page** in their browser: account, domain,
   sandboxes, GitHub ([the owner's guide](howto/setup.md)).
3. **You connect the repository** with the server's setup brief (step 4): the
   recipe, one sandbox, its preview link.

A server that must be set up with no browser at all (automation, a lab) takes
the [CLI path](#without-a-browser-automation) instead of step 3.

**Where commands run.** Every command runs on your own machine. Commands for
the server go through `ssh HOST`, so each block can be pasted as it is, and
nothing depends on a variable or a `cd` from an earlier block; the CLI path's
commands alone read `SBX_CONTROL`, which that path sets.

**Placeholders.** `HOST` (what you pass to `ssh`, with sudo that asks no
password), `IP` (the server's public IP, step 1), `D` (the domain the owner
chooses; the server answers at `app.D`), `owner@example.com` (the owner, the
first admin), `KIND` (a runtime: `embed`, `e2b-cloud` or `boat`). In step 5,
`dev-pg`, `temporal`, `10.0.0.5:7233`, `10.0.0.0/24` and `owner/app` are
examples.

**Secrets.** API keys, tokens and passwords never go into a shared chat, a
command line, a recipe, a commit or a log. The owner types keys into sbx's
own pages; on the CLI path, secrets come on stdin from a file only the owner
and you can read.

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
  Cloud or Boat on the onboarding page and needs that provider's API key
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
| TCP 80 | the installer's trusted certificate for the IP, so the onboarding page opens without a browser warning |
| UDP and TCP 53 | the domain's certificate: the server answers its DNS check itself |
| TCP 21000–21255 | IP-only access (CLI path) only: one port per preview |

## 2. Install

The same command in every case. It takes several minutes on a provider host
and longer with Embed. Start it detached, so a dropped connection can't stop
it, then check every minute or two until it prints an exit status:

```sh
ssh HOST 'cd ~/sbx && sudo rm -f /root/sbx-install.exit && sudo nohup sh -c "sh install.sh > /root/sbx-install.log 2>&1; echo \$? > /root/sbx-install.exit" < /dev/null > /dev/null 2>&1 &'
ssh HOST 'sudo tail -n 8 /root/sbx-install.log; sudo cat /root/sbx-install.exit 2>/dev/null || echo running'
```

- **`0`**: done. Before its last line, the installer says how the first
  visit's HTTPS went (`==> HTTPS for the first visit`): `ok: trusted
  certificate for IP` (Let's Encrypt over TCP 80), or `self-signed for
  now: ...` with the
  reason (no public IP, port 80 in use, or Certbot's error: usually TCP 80
  closed in a cloud firewall). Self-signed means the owner's browser warns
  once, until the domain's certificate is issued. A provider host also says
  `No sandbox runtime yet`: the onboarding page's **Sandboxes** step adds
  one.
- **Another number**: read the `FAILED: ...` line and
  `ssh HOST 'sudo tail -n 40 /root/sbx-install.log'`, fix the cause and start
  the same command again: every step is safe to repeat.

## 3. Give the owner the onboarding address

The installer's last line is the onboarding page's address,
`==> Open the onboarding: https://IP/onboarding`. It is an address, not a
credential: give it to the owner as it is, with the
[owner's guide](howto/setup.md). Running the installer again during the
onboarding prints the address again.

The page takes them through:

- **Account**: their email and password. They are the first admin, signed in
  for the steps that follow.
- **Domain**: they choose `D` and add `D A IP`, `*.D A IP` and
  `_acme-challenge.D NS D.`; the page checks the records and that the server
  answers on port 53 from outside, waits for the certificate and moves itself
  to `https://app.D/onboarding`. A blocked port 53 means allowing TCP and
  UDP 53 in the provider's firewall. Where port 53 can't open at all, set the
  address on the [CLI path](#3-the-address) with a DNS API token; the owner
  then opens `https://app.D/onboarding`, signs in when asked, and goes on
  from **Sandboxes**.
- **Sandboxes**: Embed on a `/dev/kvm` host, shown as chosen; otherwise E2B
  Cloud or Boat with the API key alone. Templates start building here.
- **GitHub**: the sbx GitHub App, or **Skip for now**. Either one opens the
  control plane on **Your server is ready**, a summary; choosing what's next
  there finishes the onboarding.

Wait for the owner to say they are through.

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
`sbx logs`, `sbx env`, `sbx down`), that `check-a` keeps running until it
pauses by itself after two hours without use (`sbx sandbox pause check-a`
pauses it at once), what you verified, with the evidence, and what you did
not.

## Without a browser (automation)

The whole onboarding through the CLI, for automation and test servers: the
same steps as the onboarding page, in its order. Do steps 1 and 2 first. Copy
the release's CLI (one Python 3 file, no dependencies) into a directory only
you can read, and name the server: the installer's last line without
`/onboarding`.

```sh
mkdir -m 700 -p ~/.sbx-install
scp HOST:sbx/cli/sbx.py ~/.sbx-install/sbx.py
export SBX_CONTROL=https://IP
```

Every `sbx.py` command reads the server from `SBX_CONTROL`: set it in each
shell you run one in. The first command makes the owner's account and writes
an API token for it to the file `--token-file` names; every later command
signs in with that file (`SBX_TOKEN_FILE` names it too). The token is that
admin's credential: it stays in the directory only you can read, never on a
command line, in a chat or in a log.

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

### 2. The account

The owner's own account, with their password on stdin. It needs no
credential: until the account exists, whoever asks first can create it, so
run it right after the install:

```sh
python3 ~/.sbx-install/sbx.py setup admin owner@example.com --token-file ~/.sbx-install/token < password-file
python3 ~/.sbx-install/sbx.py setup status --token-file ~/.sbx-install/token
```

The owner is the first admin, and `~/.sbx-install/token` (mode 600) holds
their API token. `status` lists each step, done or to do.

A server that already has an admin refuses `setup admin`. When the owner made
the account on the onboarding page, take a token for it from the server
instead:

```sh
(umask 077; ssh HOST sudo sbx-admin token create owner@example.com --label agent > ~/.sbx-install/token)
```

### 3. The address

Each line waits until HTTPS works (default 900 s; it notes its progress at
least every 30 seconds):

| Decision | Command |
| --- | --- |
| domain (the onboarding page's way: records `D A IP`, `*.D A IP`, `_acme-challenge.D NS D.`; TCP and UDP 53 open) | `python3 ~/.sbx-install/sbx.py setup address --token-file ~/.sbx-install/token --domain D` |
| domain, port 53 can't open: Cloudflare (the two `A` records, no `NS`; [token](howto/cloudflare.md#api-token)) | `python3 ~/.sbx-install/sbx.py setup address --token-file ~/.sbx-install/token --domain D --dns cloudflare --secret cloudflare_api_token < cloudflare-token-file` |
| domain, port 53 can't open: Route 53 (the two `A` records; the instance role on EC2) | the same as the first line with `--dns route53` |
| IP only (Embed only; TCP 80 and 21000–21255 open) | `python3 ~/.sbx-install/sbx.py setup address --token-file ~/.sbx-install/token --ip` |
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

With a domain, the server answers at `app.D` from here on. Name it for the
commands that follow (IP only: `SBX_CONTROL` stays as it is):

```sh
export SBX_CONTROL=https://app.D
```

### 4. Sandbox runtime

A `/dev/kvm` host already has Embed. Otherwise add a provider with its API key
on stdin (`BOAT_API_KEY` for Boat):

```sh
python3 ~/.sbx-install/sbx.py setup runtime available --token-file ~/.sbx-install/token
python3 ~/.sbx-install/sbx.py setup runtime add e2b-cloud --token-file ~/.sbx-install/token --secret E2B_CLOUD_API_KEY < e2b-key-file
```

Without `--plan`, Boat takes the plan its key's account is on (a trial key is
refused: the trial saves no templates) and E2B Cloud takes Hobby's limits;
`--plan pro` sets E2B Pro. It waits through the server's restart (default
600 s). Templates start building now; follow them with
`python3 ~/.sbx-install/sbx.py templates --wait --token-file ~/.sbx-install/token`
(default 1800 s).

### 5. Finish

```sh
python3 ~/.sbx-install/sbx.py setup finish --token-file ~/.sbx-install/token
```

`finish` needs the account, the address and a runtime, and names the first
one missing; it runs no checks (each step checked its part when it was done,
and `sbx-admin doctor` runs them all). It prints the server's address. From then on the onboarding page's address leads to the control
plane. GitHub is connected under **Extensions** ([how-to](howto/github.md)),
and the repository follows [step 4](#4-the-repository).

On macOS, the desktop CLI (`curl -fsSL https://app.D/install | sh`, which
ends with `sbx login`) works as soon as the account exists, during the
onboarding too: signed in, `sbx setup finish` needs no token file.

**Without the desktop CLI** (Linux, or the quick test's certificate), use the
[HTTP API](reference.md#http-api) with the token in a header file that holds
nothing else:

```sh
(umask 077; sed 's/^/Authorization: Bearer /' ~/.sbx-install/token > ~/.sbx-install/api-header)
curl -fsS -H @$HOME/.sbx-install/api-header https://app.D/api/me    # -k for the quick test
```

`ssh HOST sudo sbx-admin token ls` lists the tokens, and `token revoke ID`
ends one.

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
