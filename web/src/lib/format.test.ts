import { describe, expect, it } from 'vitest'
import { ago, parseTime, portsHint } from './format'

describe('server times', () => {
  it('reads SQLite timestamps as UTC and ISO times as given', () => {
    expect(parseTime('2026-11-13 09:12:04')).toBe(Date.UTC(2026, 10, 13, 9, 12, 4))
    expect(parseTime('2026-11-13T09:12:04+01:00')).toBe(Date.UTC(2026, 10, 13, 8, 12, 4))
    expect(ago('2026-11-13 09:12:04', Date.UTC(2026, 10, 13, 9, 12, 30))).toBe('just now')
  })
})

describe('certificate ports', () => {
  it('words what the server says the certificate needs, not always port 80', () => {
    expect(portsHint([53])).toMatch(/^inbound UDP and TCP port 53/)
    expect(portsHint([80])).toBe('inbound TCP port 80')
    expect(portsHint([80, 53])).toContain('port 80 and inbound UDP and TCP port 53')
    expect(portsHint([])).toBe('')
  })
})
