# Beam for sbx

For an owner whose sandboxes run at [Beam](https://docs.beam.cloud/v2/sandbox/overview)
(the `beam` runtime) instead of on the server itself. sbx needs a Beam API
token and a domain (see [Cloudflare DNS](cloudflare.md)): Beam's sandboxes
call the server back at `app.D`. Beam's dashboard may differ a little from
the labels below.

**Not yet run against a live Beam account.** sbx's Beam runtime was written
from Beam's documentation, its published API description and its SDK's
source, and tested against a stand-in for Beam's API. Before relying on it,
run the [check with a real token](#check-it-with-a-real-token) below and
report what fails.

## API token

1. Sign in at [platform.beam.cloud](https://platform.beam.cloud) and open
   **Settings → API keys**
   ([platform.beam.cloud/settings/api-keys](https://platform.beam.cloud/settings/api-keys)).
2. Create a token and copy it once. It is a workspace token: it can start
   and stop anything in that Beam workspace, so use a workspace made for sbx
   if the account also runs other things.
3. Never paste it into a chat or onto a command line. Paste it on the keys
   page your agent sends you ([below](#add-it-to-sbx)). sbx checks it with
   Beam and keeps it in its secret store.

Beam bills per second of CPU and memory a sandbox uses, from the workspace's
credits ([pricing](https://www.beam.cloud/pricing)); it publishes no number
for sandboxes at once. sbx's own limits for Beam start at 8 vCPUs and 16 GiB
per sandbox (Beam's Shared CPU tier), 10 sandboxes at once and 24 hours a
sandbox; change them in Settings or with `sbx runtimes set beam`.

## What Beam cannot do

Read this before choosing Beam: it differs from E2B Cloud and Boat.

- **No pause.** Beam runs a sandbox or deletes it. A sandbox whose timeout
  passes is deleted with its files, where the other runtimes pause it.
  `sbx pause` on a Beam sandbox is refused with that reason. Keep work in
  git, or snapshot the sandbox before it goes.
- **Preview URLs at Beam are public.** sbx's own preview addresses stay
  behind its login as always, but each port sbx opens also has an address at
  Beam (`https://<id>-<port>.app.beam.cloud`) that anyone who learns it can
  open without signing in. sbx never shows that address; an app that prints
  its own host name (in a redirect or an absolute link) does. Do not serve
  anything on a Beam sandbox that must stay private.
- **No per-sandbox egress rules**, so untrusted public repositories can't be
  served while Beam is enabled, and **no route to this server's private
  network**: managed servers are reached through
  [endpoints](../reference.md#endpoints).
- **Raw port addresses** (`<port>-<sandbox id>.D`) do not fit a host name
  for Beam's long sandbox ids; use the named previews.

## Add it to sbx

Your agent usually asks for the token on a **keys page**: it sends you a
link to your server (`https://app.<domain>/keys/…`). Sign in, paste the
token into its field and choose **Save keys**. sbx checks the token with
Beam, stores it and adds Beam. Each **How do I get this?** link leads back
here.

Other ways, with the token read from a file or stdin, never typed on a
command line:

- The web app: **Settings → Sandbox runtimes**, add Beam and paste the
  token. The first-run wizard offers the same.
- The `sbx` CLI, as an admin:
  `sbx runtimes add beam --secret BEAM_API_KEY < token-file`.
- On the server: `sbx-admin runtime add beam < token-file`.

`sbx runtimes test beam` then checks that Beam answers with the token and
names the workspace. To replace a token: `sbx runtimes set beam --secret
BEAM_API_KEY < new-token-file`, or change it in Settings.

## Check it with a real token

Run these once, in order, after adding the token; each names what it proves.
Stop at the first that fails and send its output (it contains no secret).

1. `sbx runtimes test beam`: the token is accepted (`Beam API: answers;
   workspace …`).
2. `sbx-admin jobs run template@beam:base`, then `sbx-admin jobs`: the base
   template builds. This is the first real use of Beam's image import, of a
   sandbox from a stub, of commands and file uploads, and of saving a
   sandbox's filesystem as an image.
3. `sbx-admin runtime verify beam`: a sandbox from the base template, a
   command, a file written and read, a preview port reached through the
   gate, and the delete. It reports "pause and resume: Beam has no pause"
   and passes without them.
4. `sbx up check-a-beam --runtime beam` on a real repository, open its
   preview, then `sbx rm check-a-beam`.
5. Restart sbx-control (`systemctl restart sbx-control`) with a Beam sandbox
   running, then `sbx ls`: the sandbox is still listed with its name (its
   metadata is read back from the sandbox).
6. Let a throwaway sandbox pass its timeout: `sbx ls` no longer lists it.
