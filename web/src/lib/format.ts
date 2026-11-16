const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 31_536_000], ['month', 2_592_000], ['week', 604_800], ['day', 86_400], ['hour', 3_600], ['minute', 60],
]
const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })

/** A server time: SQLite's current_timestamp ("2026-11-13 09:12:04") is UTC without saying so. */
export function parseTime(value: string): number {
  return Date.parse(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(value) ? value.replace(' ', 'T') + 'Z' : value)
}

export function ago(value: string | null | undefined, now = Date.now()): string {
  if (!value) return '—'
  const t = parseTime(value)
  if (Number.isNaN(t)) return value
  const seconds = Math.round((t - now) / 1000)
  if (Math.abs(seconds) < 60) return 'just now'
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit)
  }
  return 'just now'
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const t = parseTime(value)
  return Number.isNaN(t) ? value : new Date(t).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function memory(mb: number | null | undefined): string {
  if (mb == null) return '—'
  return mb >= 1024 ? `${+(mb / 1024).toFixed(1)} GB` : `${mb} MB`
}

export function plural(n: number, one: string, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`
}

export function host(url: string): string {
  try { return new URL(url).host } catch { return url }
}

/** What a certificate needs open, in words, from the server's needs_ports
 * (settings.Domain.needs_ports): 80 for an IP's, 53 for delegated DNS. */
export function portsHint(ports: number[] = []): string {
  return ports.map(p => (p === 53 ? 'inbound UDP and TCP port 53 (it answers the _acme-challenge DNS queries)' : `inbound TCP port ${p}`)).join(' and ')
}

/** A runtime's limits in words, exact (a rounded maximum would let a size just above it look allowed); "" when it states none. */
export function limitsText(limits: { timeout_seconds?: number; cpu?: number; memory_mb?: number; disk_mb?: number; sandboxes?: number } = {}): string {
  const each = [
    limits.cpu != null && plural(limits.cpu, 'vCPU'),
    limits.memory_mb != null && `${limits.memory_mb} MB memory`,
    limits.disk_mb != null && `${limits.disk_mb} MB disk`,
    limits.timeout_seconds != null && `${duration(limits.timeout_seconds)} before it pauses`,
  ].filter(Boolean)
  const parts = [
    limits.sandboxes != null && `up to ${limits.sandboxes} running at once`,
    each.length > 0 && `each at most ${each.join(', ')}`,
  ].filter(Boolean) as string[]
  const text = parts.join('; ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function duration(seconds: number): string {
  if (seconds % 86_400 === 0) return plural(seconds / 86_400, 'day')
  if (seconds % 3_600 === 0) return plural(seconds / 3_600, 'hour')
  if (seconds % 60 === 0) return plural(seconds / 60, 'minute')
  return plural(seconds, 'second')
}
