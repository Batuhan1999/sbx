# AWS role access

Two packages give a repository's sandboxes renewable access to a development
AWS role, so the app's own AWS SDK, CLI or secret loader works as it does on a
developer's machine:

- `aws-role` (connection): the role, its region and how the host authenticates
  to assume it. Saved once by an admin.
- `aws-profile` (provision): a named AWS profile in each sandbox whose
  credentials come from that role, refreshed by sbx.

Use them when the repository loads its development configuration from AWS
(Secrets Manager, Parameter Store) or calls AWS services while it runs. sbx
does not read or copy the app's secrets: the app's own loader fetches them with
the profile, at startup, as usual.

## 1. Prepare the role in AWS

1. Choose or create a development role with the permissions the app needs.
2. Create a dedicated IAM user for sbx with an access key (IAM → Users → the
   user → Security credentials → Create access key). Allow that user
   `sts:AssumeRole` on the role, and let the role's trust policy admit the
   user.

A role ARN alone is not enough, and neither is a copied personal SSO session:
both stop working without someone logging in.

## 2. Save the connection

In the web app, open **Extensions → Shared connections**, add an
**AWS development role**, and enter the access key ID, secret access key, role
ARN and region. Fill in an external ID only if the role's trust policy requires
one. Saving runs the check: it assumes the role once through STS and discards
the credentials. It never reads application secrets.

The same through the API, with the values in a request body rather than on a
command line:

```
POST /api/catalog/connections
{"package": "aws-role", "key": "dev-aws",
 "inputs": {"auth_method": "access_key", "access_key_id": "…", "secret_access_key": "…",
            "role_arn": "arn:aws:iam::123456789012:role/dev", "region": "eu-central-1"}}
```

The key pair is kept on the server and used only by the control
service. It never reaches a sandbox, a recipe or a template. To rotate it,
update the same connection; the new pair is checked before it is saved. The
role ARN and region identify the connection and cannot change while sandboxes
use it; make a new connection for another role.

**Other host identities.** With `auth_method: host_profile` and
`source_profile`, the control service assumes the role from a profile in its
own AWS configuration on the host (root's, in a standard install), for example
one backed by Roles Anywhere or another `credential_process`. An SSO profile
works until its login expires and then needs an interactive login on the host.
Temporary source keys (`ASIA…`) need `session_token` and expire, so they are
not suitable for unattended use.

## 3. Attach a profile to the repository

Use the profile name the repository's scripts or secret loader expect. In
`.sbx/sbx.yaml`:

```yaml
services:
  aws:
    uses: aws-profile
    connections: {role: dev-aws}
    with: {profile: dev}
```

or from a checkout, `sbx attach aws-profile --key aws --connection role=dev-aws profile=dev`,
or on the repository's **Packages** page in the web app.

The attachment reaches running sandboxes when they are prepared: run
`sbx sandbox prepare NAME` (or `POST /api/sandboxes/NAME/prepare`). That
reruns setup and start with the profile in place and keeps the sandbox's other
services, data and source edits. New sandboxes get it from the start.

Keep the repository's normal startup command. Do not set an "environment
loaded" flag to skip its secret loader, and do not bake an SSO cache into a
template. If the loader needs the AWS CLI, `.sbx/setup` installs it; SDK-only
apps need nothing extra.

## Inside the sandbox

Before setup runs, sbx writes into the sandbox's private runtime directory
(`/run/sbx/ext/`, not `~/.aws`):

- an AWS config with one profile per `aws-profile` attachment, whose
  `credential_process` is a small helper script;
- an empty shared-credentials file;
- `AWS_PROFILE`, `AWS_DEFAULT_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`,
  `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE`, `AWS_SDK_LOAD_CONFIG=1` and
  `AWS_EC2_METADATA_DISABLED=true`.

When the SDK needs credentials it runs the helper, which posts the
attachment's own token, in an `Authorization` header, to the control service's
`credentials` action over HTTPS (no redirects followed). The control service
assumes the role and returns standard `credential_process` JSON with an
expiry. Sessions last 15 minutes and are cached in the control service until
five minutes before they expire, so parallel processes share one. SDKs ask
again on their own before expiry.

Setup fails early, with **AWS connection required**, when:

- the role cannot be assumed (a missing role, a refused trust policy, a bad
  key, an expired host login): fix the connection, check it again, then
  prepare the sandbox;
- the app's env already sets static AWS credentials (`AWS_ACCESS_KEY_ID`,
  `AWS_SESSION_TOKEN`, a web identity or container credentials): remove them;
- two attachments use the same profile name, or `AWS_PROFILE` names a profile
  sbx does not manage;
- the control service is not reached at a plain HTTPS address.

Do not run the helper by hand in an agent session: its output is credentials.

Removing the attachment, changing its connection, or removing the repository's
trust stops refreshes at once. A session already issued stays valid in AWS
until it expires, at most 15 minutes. The runtime files are rewritten after a
cold resume, and `~/.aws` is removed before any reusable snapshot is taken.

## Development state is still per sandbox

The profile grants the role's permissions; it does not separate data. If the
app writes to buckets, queues, tables or secrets in AWS, give each sandbox its
own development resource or namespace and inject its identifiers, so that one
sandbox's migrations, seeds and resets never change another's. Access to a role
is not permission to copy production data.

AWS references:
[process credentials](https://docs.aws.amazon.com/sdkref/latest/guide/feature-process-credentials.html),
[AssumeRole](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html),
[Roles Anywhere credential helper](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/credential-helper.html).
