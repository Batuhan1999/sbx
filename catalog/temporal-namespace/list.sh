#!/bin/sh
set -eu
address=${INPUT_ADDRESS:-${TEMPORAL_SERVER_ADDRESS:?connect a temporal-server}}
namespaces=$(temporal operator namespace list --address "$address" -o json)
printf '%s' "$namespaces" | python3 -c '
import json, sys
import sbx_provider as sbx

def names(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "name" and sbx.owned(child):
                yield child
            else:
                yield from names(child)
    elif isinstance(value, list):
        for child in value:
            yield from names(child)
print("\n".join(sorted(set(names(json.load(sys.stdin))))))
'
