#!/usr/bin/env python3
"""Write runtime configuration only; never print app values or commit them."""
import json
import os
from pathlib import Path

keys = [key for key in os.environ if key.startswith(('MAPLE_', 'CLICKHOUSE_', 'TINYBIRD_', 'ELECTRIC_', 'INGEST_', 'VITE_'))]
keys += ['SD_INTERNAL_TOKEN', 'INTERNAL_SERVICE_TOKEN', 'DATABASE_URL', 'TURBO_TELEMETRY_DISABLED']
path = Path('.env.local')
text = ''.join(f'{key}={json.dumps(os.environ[key])}\n' for key in sorted(set(keys)) if key in os.environ)
if not path.exists() or path.read_text() != text:
    path.write_text(text)
path.chmod(0o600)
collector = f'''receivers:
  otlp:
    protocols:
      grpc: {{endpoint: 127.0.0.1:4317}}
      http: {{endpoint: 127.0.0.1:4318}}
processors:
  batch: {{timeout: 1s}}
exporters:
  maple:
    endpoint: {json.dumps(os.environ['CLICKHOUSE_URL'])}
    database: {json.dumps(os.environ['CLICKHOUSE_DATABASE'])}
    username: {json.dumps(os.environ['CLICKHOUSE_USER'])}
    password: ${{env:CLICKHOUSE_PASSWORD}}
    org_id: default
extensions:
  health_check: {{endpoint: 127.0.0.1:13133}}
service:
  extensions: [health_check]
  pipelines:
    traces: {{receivers: [otlp], processors: [batch], exporters: [maple]}}
    logs: {{receivers: [otlp], processors: [batch], exporters: [maple]}}
    metrics: {{receivers: [otlp], processors: [batch], exporters: [maple]}}
'''
Path('/tmp/maple-sbx-collector.yaml').write_text(collector)
