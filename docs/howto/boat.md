# Boat for sbx

For an owner whose sandboxes run at [Boat](https://docs.boat.dev) (the `boat`
runtime) instead of on the server itself. sbx needs a Boat API key and a
domain (see [Cloudflare DNS](cloudflare.md)): Boat's sandboxes call the
server back at `app.D`. Boat's dashboard may differ a little from the
labels below.

## API key

1. Sign in at [boat.dev/dashboard](https://boat.dev/dashboard).
2. Open **API keys** and create a key with the scope **`full-sandbox`**. It
   starts with `boat_`. Copy it once.
3. Never paste it into a chat or onto a command line. Paste it in the
   onboarding page's **Sandboxes** step, or later in sbx's own pages
   ([below](#add-it-to-sbx)). sbx checks it with Boat and keeps it in its
   secret store.

If the key has an expiry, sbx warns 14 days before it, when Boat lets the key
look itself up; otherwise note the expiry yourself. Create a new key then and
add it again (below).

## Plan

sbx sets its limits to match your plan. As read from
Boat's [pricing](https://docs.boat.dev/pricing.md) on 2026-11-17:

| Plan | Per sandbox, at most | At once |
| --- | --- | --- |
| trial | 4 vCPUs, 8 GB | 2, stopped after 2 hours |
| `20` | 8 vCPUs, 16 GB, 111 GB disk | 100 |
| `100` | 16 vCPUs, 32 GB, 251 GB disk | 300 |
| `500` | 16 vCPUs, 32 GB, 251 GB disk | 1,000 |
| `2000` | 16 vCPUs, 32 GB, 251 GB disk | 2,000 |

On the onboarding page and with the CLI, sbx reads your plan from Boat when the
key is saved and sets these limits for you; nothing to choose. When you add
Boat later under **Settings**, choose your plan there.

**A trial key is refused at the key step.** The trial saves no snapshots,
and sbx's templates are saved snapshots, so a server on the trial could
never build them. When you save a key whose account is still on the trial,
sbx says so under the key field (the CLI exits with the same message) and
adds nothing: make the first payment in Boat's dashboard (Billing), then save
the key again. Plan on `20` or above. The
[reference](../reference.md#boat-boat) has every setting.

## Saved snapshots

sbx's templates are Boat saved snapshots: a server keeps two (`sbx-base` and
`default`) plus one per repository with a template of its own. Your plan keeps
some free (10 on `20`); each one above that is paid from your Boat wallet
($1.68 a month). Templates of other sbx servers on the same Boat account count
too, also ones from servers that are gone (each server names its templates
with a prefix of its own, `1a2b3c4d--sbx-base-…`).

When the free ones are taken and the wallet is empty, sbx says so before a
template build starts: delete the ones you don't need in Boat's dashboard
(**Snapshots**), or add money to the wallet; sbx builds the template again by
itself within half an hour, or build it now with
`sbx-admin jobs run template@boat:sbx-base`.

## Add it to sbx

During the onboarding, paste it in the onboarding page's **Sandboxes**
step: pick Boat and choose **Check key**
([the onboarding](setup.md#sandboxes)).
sbx checks the key with Boat, stores it and adds Boat.

Later, an agent may ask for the key on a **keys page**: it sends you a link
to your server (`https://app.<domain>/keys/…`). Sign in, paste the key into
its field and choose **Save keys**; the page shows when the server has
applied it. Each **How do I get this?** link leads back here.

![The keys page asking for a Boat API key and an Amp provider token](images/keys-page.png)

Other ways, with the key read from a file or stdin, never typed on a
command line:

- The web app: **Settings → Sandbox runtimes**, add Boat, paste the key and
  choose the plan.
- The `sbx` CLI, as an admin:
  `sbx runtimes add boat --secret BOAT_API_KEY < key-file` (the plan comes
  from the key; `--plan` sets another).
- On the server: `sbx-admin runtime add boat < key-file`.

`sbx runtimes test boat` then checks that Boat answers with the key and
names the plan. To replace a key: `sbx runtimes set boat --secret
BOAT_API_KEY < new-key-file`, or change it in Settings.

Untrusted public repositories can't be served while Boat is enabled: it has
no per-sandbox egress rules.
