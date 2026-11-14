import type { Lifecycle, Sandbox } from '@/api/types'
import { sandboxStatus, type Tone } from '@/lib/status'
import { cn } from '@/lib/utils'

const TONE: Record<Tone, { dot: string; text: string; bg: string }> = {
  ok: { dot: 'bg-ok', text: 'text-ok', bg: 'bg-ok-soft' },
  warn: { dot: 'bg-warn', text: 'text-warn', bg: 'bg-warn-soft' },
  bad: { dot: 'bg-bad', text: 'text-bad', bg: 'bg-bad-soft' },
  progress: { dot: 'bg-info', text: 'text-info', bg: 'bg-info-soft' },
  idle: { dot: 'bg-faint', text: 'text-muted', bg: 'bg-sunken' },
}

export function ToneDot({ tone, pulse, className }: { tone: Tone; pulse?: boolean; className?: string }) {
  return <span aria-hidden className={cn('inline-block size-1.5 shrink-0 rounded-full', TONE[tone].dot, pulse && 'pulse-dot', className)} />
}

export function StatusBadge({ sandbox, showDetail = false, className }: {
  sandbox: Pick<Sandbox, 'status' | 'vm' | 'error'> & { lifecycle?: Lifecycle | null }; showDetail?: boolean; className?: string
}) {
  const v = sandboxStatus(sandbox)
  const t = TONE[v.tone]
  return <span className={cn('inline-flex min-w-0 items-center gap-1.5 text-[12.5px]', className)} title={v.detail}>
    <span className={cn('inline-flex h-5 shrink-0 items-center gap-1.5 rounded-full px-2 font-medium', t.bg, t.text)}>
      <ToneDot tone={v.tone} pulse={v.busy} />{v.label}
    </span>
    {showDetail && v.detail && <span className="truncate text-muted">{v.detail}</span>}
  </span>
}

export function StateBadge({ tone, label, pulse }: { tone: Tone; label: string; pulse?: boolean }) {
  const t = TONE[tone]
  return <span className={cn('inline-flex h-5 shrink-0 items-center gap-1.5 rounded-full px-2 text-[12px] font-medium', t.bg, t.text)}>
    <ToneDot tone={tone} pulse={pulse} />{label}
  </span>
}
