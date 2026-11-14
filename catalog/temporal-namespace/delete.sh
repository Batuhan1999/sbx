#!/bin/sh
# Temporal deletes a namespace's data in the background; one that is gone
# already is fine.
set -eu
address=${INPUT_ADDRESS:-${TEMPORAL_SERVER_ADDRESS:?connect a temporal-server}}
namespace=$(python3 ./sbx_provider.py name)
if ! out=$(temporal operator namespace delete --address "$address" --namespace "$namespace" --yes 2>&1); then
  case $out in
    *NamespaceNotFound*|*"namespace "*"not found"*|*"Namespace "*"not found"*) ;;
    *) echo "$out" >&2; exit 1 ;;
  esac
fi
