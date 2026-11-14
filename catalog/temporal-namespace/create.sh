#!/bin/sh
# The sandbox's namespace; one that exists already is fine.
set -eu
address=${INPUT_ADDRESS:-${TEMPORAL_SERVER_ADDRESS:?connect a temporal-server}}
namespace=$(python3 ./sbx_provider.py name)
if ! out=$(temporal operator namespace create --address "$address" --namespace "$namespace" \
    --retention "$INPUT_RETENTION" 2>&1); then
  case $out in
    *NamespaceAlreadyExists*|*"already exists"*) ;;
    *) echo "$out" >&2; exit 1 ;;
  esac
fi
printf 'address=%s\nnamespace=%s\n' "$address" "$namespace" >> "$SBX_OUTPUT"
