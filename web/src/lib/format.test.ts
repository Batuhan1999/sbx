import { describe, expect, it } from 'vitest'
import { ago, limitsText, parseTime, planSummary, portsHint, runLength } from './format'

describe('a plan in one line', () => {
  it('says each limit in people’s units', () => {
    expect(planSummary({ timeout_seconds: 3600, cpu: 8, memory_mb: 8192, disk_mb: 10240, sandboxes: 10 }))
      .toBe('1 h runs · 8 vCPU · 8 GB RAM · 10 GB disk · 10 at once')
    expect(planSummary({ memory_mb: 512 })).toBe('0.5 GB RAM')
    expect(runLength(5400)).toBe('1 h 30 min')
    expect(runLength(300)).toBe('5 min')
    expect(runLength(3599)).toBe('1 h')          // rounded once: never "60 min"
    expect(runLength(7170)).toBe('2 h')
    expect(planSummary({})).toBe('')
  })
})

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

describe('runtime limits', () => {
  it('words the limits a runtime states, exactly, and nothing for the ones it does not', () => {
    expect(limitsText({ sandboxes: 20, cpu: 8, memory_mb: 8192, disk_mb: 10240, timeout_seconds: 3600 }))
      .toBe('Up to 20 running at once; each at most 8 vCPUs, 8192 MB memory, 10240 MB disk, 1 hour before it pauses')
    expect(limitsText({ cpu: 1 })).toBe('Each at most 1 vCPU')
    expect(limitsText({})).toBe('')
  })
})
