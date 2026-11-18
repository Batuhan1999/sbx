# Write a catalog package for {what}

You are in a folder where `./{name}/` will become a package in the catalog of
the sbx server at {control}. The `sbx` CLI is signed in there as an admin.
Your job: write the package, push it, test it on the server until it passes
twice in a row, and leave it ready for repositories to use.

## How to work

- Do the steps in order; run each step's check and show the result.
- Ask the owner only for access you lack (a provider account, a server to test
  against) and wait for it. They enter credentials themselves, in the web app
  or at a hidden prompt.
- Never print a credential, write one into the package, or put one on a
  command line you show.

## 1. Choose the shape

A package is a folder with a `package.yaml` and scripts. It has one of two
kinds:

| Kind | What it is | Examples |
| --- | --- | --- |
| `connection` | A server or account an admin saves once under a key, with a `check`. No scripts. | `postgres-server`, `neon-project`, `aws-role` |
| `provision` | A resource per sandbox, made on a connection: `create` makes it, `delete` removes it, `list` finds it. | `postgres-clone`, `neon-branch`, `aws-profile` |

A per-sandbox service usually needs both. Run `sbx catalog ls`: reuse a
connection package that fits, and write a new one only when none does. Then
read the closest shipped provision package:

- `sbx catalog show postgres-clone --files`: a database copied on a server you
  run, in shell;
- `sbx catalog show neon-branch --files`: a branch made through a hosted
  provider's API, in Python with the shared helper.

Check: you can name the connection kind the package requires and the example
you start from.

## 2. Scaffold

```sh
sbx catalog new {name} --from EXAMPLE
```

`--from` copies the example's files (including `sbx_provider.py`, the shared
helper) with its name replaced. `--from` is required: start from the closest
package `sbx catalog ls` lists.

Check: `./{name}/package.yaml` exists.

## 3. Write package.yaml

```yaml
kind: provision
name: {name}
title: Human title
description: >
  One paragraph for people: what a sandbox gets, where it comes from, how long
  it takes. Its first sentence is the summary the web app shows.
requires:
  server:                         # a slot, filled with a saved connection
    kind: postgres-server         # the connection package it needs
    expose:
      url: PG_ADMIN_URL           # the connection's url input arrives as $PG_ADMIN_URL
inputs:
  template:
    label: Template database
    required: true
    help: The database each sandbox's copy starts from.
  mode:
    label: Start with
    default: schema
    choices: { schema: Empty tables, data: A copy of the template's data }
    help: Whether each sandbox's database gets the template's rows too.
outputs:                          # what create writes to $SBX_OUTPUT
  url:
    sensitive: true
  database: {}
env:                              # what the sandbox's app gets
  DATABASE_URL: ${outputs.url}
runs:
  create: create.sh
  delete: delete.sh
  list: list.sh
timeout: 5m
```

| Key | Kinds | Meaning |
| --- | --- | --- |
| `kind`, `name`, `title`, `description`, `docs` | all | Identity and help, written for people: the title names what you get (the web app shows the kind itself, so no "(per sandbox)"), and the description names other packages by what they give, not by their package name. The name is lowercase letters, digits, `-` and `_`. `docs` is the provider's page on getting the key or account the inputs ask for. |
| `logo` | all | An `.svg` file in the folder (at most 32 KiB): the provider's official mark, in its version for light backgrounds. The web app shows it small on a white tile in both themes; without one it shows a generic icon. A copy made with `--from` keeps the example's logo: replace it, or remove the file and the key. |
| `inputs` | all | `NAME: {label, required, default, secret, help, choices, type, min, max, advanced, hidden}`. Scripts get `INPUT_<NAME>`; `secret: true` values are never shown. `label` is the field's name in the web app (else the name in words); `help` is one short fragment, only where the label is not enough. `choices` lists the only values allowed, as `value: label` (or a list of values), and the web app offers them as a list; the default must be one. `type` is `text` (the default), `number` or `integer` (with optional `min` and `max`), `boolean` (`"true"`/`"false"`, a switch; a default of either implies it), `email` or `url` (http or https); the server refuses other values. `advanced: true` folds a rarely changed input under More options; `hidden: true` leaves an operator's input out of the web app's forms. |
| `check` | connection | Shell text run with the inputs (exit 0 means usable), or a `.py` file whose `check(values)` returns an error message or `""`. |
| `connect` | connection | A `.py` file whose `connect(before, given)` returns the inputs to save on an update. |
| `identity` | connection | Inputs that name the endpoint; they cannot change while resources use the connection. |
| `requires` | provision | `SLOT: {kind, expose}`: the connection package it runs on, and in `expose` which of that connection's inputs the scripts see, under which variable names. |
| `runs` | provision | `create` (required), `delete`, `list`: a script each, or one script for all. |
| `outputs` | provision | `NAME: {sensitive}`. Recipes read them as `${services.KEY.NAME}`. |
| `env` | provision | Variables for the app, from `${outputs.NAME}`. |
| `timeout` | provision | Per script: `30s`, `5m`, `1h` (default `5m`). |
| `hooks` | provision | `pause: SCRIPT`, run like `runs` after the sandbox paused (for example to close idle sessions). |
| `runtime`, `actions` | provision | Python run inside the control service: files written into the sandbox, and host-side functions it may call with the token the platform issues each resource (`SBX_ACTION_TOKEN`, its `token` output). Installation packages only; see `aws-profile`. |
| `template` | provision | What the templates of repositories that attach it install once: `apt` packages, `files` (`/abs/path: text` or `{content, mode}`), `root` and `user` commands. No credentials; changing it rebuilds those templates. |
| `live` | provision | `true`: attaching it reaches existing sandboxes when they are prepared. |
| `scrub` | provision | Paths removed before a reusable snapshot is taken. |

Check: every input has a `label` where its name does not read as words, and
`help` only where the label can't say what to enter or where to get it; every
input with a fixed set of values has `choices`, every number or URL its `type`,
an operator-only input `hidden: true` (the API and CLI set it); every output
`create` writes is declared, and
every value that is a credential is `secret` (inputs) or `sensitive` (outputs).

## 4. Write the scripts

What a script gets, and nothing else from the host:

- its action (`create`, `delete`, `list`) as the first argument and in
  `SBX_ACTION`;
- `INPUT_<NAME>` for each input, and the required connections' fields under the
  names `expose` gives them;
- `SBX_ID` (the resource ID), `SBX_NAME` (the sandbox), `SBX_REPO`,
  `SBX_SERVICE` (the attachment's key), `SBX_OUTPUT`, `PATH` and `HOME`;
- for `create` and `delete`, `SBX_RESOURCE_STATUS` (`creating` or `ready`) and
  `SBX_PREVIOUS_OUTPUT`, a JSON object of what an earlier `create` wrote for
  this resource. Treat it as secret;
- for a package with `actions`, `SBX_ACTION_TOKEN`, the token its sandbox calls
  them with (the platform issues and keeps it; don't write it as an output).

Installation packages run on the host from a private copy of the folder. The
host has `python3` (standard library), `curl`, `jq` and `openssl`. A command
your scripts or `check` need beyond those goes in `host_tools`, its name
mapped to the root shell command that installs it (as `postgres-server` does
for `psql`); the installer installs it on the next install or upgrade.

Rules:

1. **Name what you make after the resource ID**: `sbx-<id>`, or `sbx_<id>` with
   every `-` turned into `_` where names cannot hold hyphens (SQL identifiers).
   `python3 ./sbx_provider.py name -` (or `_`) prints it. `SBX_ID` is opaque,
   stable for this resource and different for every attachment; never derive
   names from `SBX_NAME`.
2. **`create` is idempotent.** Run twice for the same resource, it ends in the
   same state and writes the same outputs: look before you make
   (`IF NOT EXISTS`), and reuse credentials from `SBX_PREVIOUS_OUTPUT` instead
   of resetting them. Verify that the provider's IDs still match before
   returning saved credentials.
3. **`create` appends `key=value` lines to `$SBX_OUTPUT`**, one per declared
   output. Lines written before a failure are kept, sealed, for the retry. For
   an asynchronous provider, write the job ID first, poll that job on retry, and
   write connection values only after the provider confirms completion.
4. **`delete` succeeds when there is nothing to delete**, and never removes a
   same-named resource that replaced the recorded one.
5. **`list` prints the names of the resources it can see**, one per line. It
   is discovery only: sbx deletes only resources it recorded for sandboxes
   that are gone, never by prefix.
6. **Never touch what you were given**: the template database, the parent
   branch, the account itself.
7. **Fail loudly**: `set -eu` in shell, one line on stderr, exit 1. The
   platform shows the last lines. Never print a credential, including in
   error messages.

The shared helper `sbx_provider.py` implements rules 1 to 3 and 7 for Python
scripts: `resource_name()`, `previous()`, `recorded()`, `write_outputs()`,
`fetch()` (errors name only the HTTP status), `all_pages()`, `wait()` and
`main(run)`. The `neon-branch` example shows them in use.

Check: each script is executable and starts with `set -eu` (shell) or uses
`sbx_provider.main` (Python).

## 5. Push and test

```sh
sbx catalog push ./{name}
sbx catalog test {name} --input template=TEMPLATE --connection server=CONNECTION_KEY
```

`push` uploads the folder as the installation's version of the package; the
shipped catalog is untouched. `test` runs, for a throwaway sandbox on the
server: `create`, `create` again (its outputs must not change), `list` (it must
name the resource) and `delete`, and prints each step with the non-sensitive
outputs. Fix what fails, push, and test again.

A connection package's test runs its `check`. Its inputs are usually
credentials, so do not pass them with `--input`: ask the owner to add a
connection with it in the web app, which runs the check before saving.

Check: `sbx catalog test {name}` prints `passed` twice in a row.

## 6. Use it in a repository

In a repository's `.sbx/sbx.yaml`:

```yaml
services:
  db:
    uses: {name}
    connections:
      server: CONNECTION_KEY
    with:
      template: TEMPLATE
```

or, from that repository's checkout,
`sbx attach {name} --key db --connection server=CONNECTION_KEY template=TEMPLATE`.
Start a sandbox with `sbx up NAME`: the resource exists, its variables reach
the app, and `sbx down NAME` removes it. That is the real proof.

## Done

- `sbx catalog test {name}` passes twice in a row.
- `package.yaml` has a title, a description of what a sandbox gets, and help on
  every input.
- You told the owner which connection the package requires and how to add it.
