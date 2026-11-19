# Lizard for sbx

For an owner whose sandboxes run at [Lizard](https://lizard.build) (the
`lizard` runtime) instead of on the server itself. sbx needs a Lizard API
key, a Lizard project and a domain (see [Cloudflare DNS](cloudflare.md)):
Lizard's sandboxes call the server back at `app.D`. Lizard's dashboard may
differ a little from the labels below.

sbx's Lizard runtime was written from Lizard's SDK and documentation and has
not run against a live Lizard account yet. Run `sbx-admin runtime verify
lizard` on the server after adding it; it takes a throwaway sandbox through
every step and says which one fails.

## API key

1. Sign in at [lizard.build](https://lizard.build) and create a **project**
   for sbx's sandboxes (they are billed to it), or pick one.
2. Create an **API key**. It starts with `liz_`. A key limited to that
   project is enough, and is the safer choice. Copy it once.
3. Never paste it into a chat or onto a command line. Paste it on the keys
   page your agent sends you ([below](#add-it-to-sbx)). sbx checks it with
   Lizard and keeps it in its secret store.

When the key sees more than one project, tell your agent which one
(`LIZARD_PROJECT`: its ID, slug or name). The region is `us-east-1` unless
you name another (`LIZARD_REGION`).

## Plan

Tell your agent the plan, so sbx sets its limits to match. As read from
Lizard's [limits](https://lizard.build/docs/platform/limits/) on 2026-11-19:

| Plan | Running at once | A sandbox lives |
| --- | --- | --- |
| trial | 1 Small, 2 Mini | 1 hour, then Lizard deletes it |
| `pro` | 40 vCPUs | until deleted |
| `pro-5x` | 220 vCPUs | until deleted |
| `pro-20x` | 1,200 vCPUs | until deleted |

The trial deletes every sandbox an hour after it starts, paused or not, so
nothing kept in a sandbox survives there: use it to try the connection, and
plan on `pro` or above.

Every sandbox of an installation has one size on Lizard, Small (2 vCPUs,
4 GB, 20 GB disk) unless you ask for a larger one; a larger size costs more
for every sandbox. A paused sandbox and each template cost their disk space
until deleted. The [reference](../reference.md#lizard-lizard) has every
setting.

## Add it to sbx

Your agent usually asks for the key on a **keys page**: it sends you a link
to your server (`https://app.<domain>/keys/…`). Sign in, paste the key into
its field and choose **Save keys**. sbx checks the key with Lizard, stores it
and adds Lizard; the page shows when the server has applied it. Each **How
do I get this?** link leads back here.

Other ways, with the key read from a file or stdin, never typed on a
command line:

- The web app: **Settings → Sandbox runtimes**, add Lizard, paste the key,
  name the project and choose the plan. The first-run wizard offers the same.
- The `sbx` CLI, as an admin:
  `sbx runtimes add lizard --plan pro --secret LIZARD_API_KEY < key-file`,
  with `--set LIZARD_PROJECT=my-project` when the key sees several.
- On the server: `sbx-admin runtime add lizard < key-file`.

`sbx runtimes test lizard` then checks that Lizard answers with the key,
finds the project and names the plan. To replace a key: `sbx runtimes set
lizard --secret LIZARD_API_KEY < new-key-file`, or change it in Settings.

Untrusted public repositories can't be served while Lizard is enabled: it
has no per-sandbox egress rules.
