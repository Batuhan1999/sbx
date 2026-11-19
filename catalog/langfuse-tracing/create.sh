#!/bin/sh
# Nothing is made per sandbox: the connected server's project keys, as outputs.
set -eu
printf 'host=%s\npublic_key=%s\nsecret_key=%s\n' "${LANGFUSE_SERVER_URL:?connect a langfuse-server}" \
  "$LANGFUSE_SERVER_PUBLIC_KEY" "$LANGFUSE_SERVER_SECRET_KEY" >> "$SBX_OUTPUT"
