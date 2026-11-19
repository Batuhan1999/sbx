import { AlertTriangle, RotateCw } from 'lucide-react'
import type { ReactNode } from 'react'
import { describeError } from '@/api/client'
import { cn } from '@/lib/utils'
import { DocsLink } from './PackageLogo'
import { Button } from './ui/button'
import { Skeleton, Spinner } from './ui/primitives'

export function EmptyState({ icon: Icon, title, children, action, className }: {
  icon?: React.ComponentType<{ className?: string }>; title: ReactNode; children?: ReactNode; action?: ReactNode; className?: string
}) {
  return <div className={cn('flex flex-col items-center px-6 py-12 text-center', className)}>
    {Icon && <span className="mb-3 grid size-9 place-items-center rounded-lg border border-line bg-sunken text-muted"><Icon className="size-[18px]" /></span>}
    <p className="font-medium">{title}</p>
    {children && <div className="mt-1 max-w-[52ch] text-[13px] text-muted">{children}</div>}
    {action && <div className="mt-4 flex flex-wrap justify-center gap-2">{action}</div>}
  </div>
}

const TECHNICAL = /:\/\/|Error\(|Exception|Traceback|Caused by|ConnectionPool|Max retries|errno|host=/i

/** Where a message's detail starts: its first ": ", "; " or " (". A plain aside in parentheses belongs to the
 * sentence ("the sandbox runtime (E2B Embed) failed: …" is cut after "failed", not after "runtime"). */
function detailStart(t: string): number {
  const marks = /: |; | \(/g
  for (let m = marks.exec(t); m; m = marks.exec(t)) {
    const close = m[0] === ' (' ? t.indexOf(')', m.index) : -1
    if (close < 0 || TECHNICAL.test(t.slice(m.index, close + 1))) return m.index
    marks.lastIndex = close + 1
  }
  return -1
}

/** A message, and its technical part (addresses, exception text) apart: what is wrong reads first, the rest is
 * behind Details. A message without technical detail comes back whole, with no detail. */
export function splitDetail(text: string): { head: string; detail: string } {
  const t = (text ?? '').trim()
  if (!TECHNICAL.test(t) && t.length <= 160) return { head: t, detail: '' }
  const cut = detailStart(t)
  if (cut <= 0 || TECHNICAL.test(t.slice(0, cut))) return { head: t.length > 160 ? `${t.slice(0, 120).trim()}…` : t, detail: t }
  return { head: t.slice(0, cut), detail: t.slice(cut).replace(/^[:;]\s*|^\s*/, '') }
}

/** Text that may carry technical detail: its head, and the detail under a Details disclosure. */
export function DetailText({ text, className }: { text: string; className?: string }) {
  const { head, detail } = splitDetail(text)
  return <span className={className}>{head}{detail && <details className="mt-1 text-[12px] [&>summary]:cursor-pointer">
    <summary className="w-fit text-muted hover:text-ink">Details</summary>
    <span className="mt-1 block break-all font-mono text-[11.5px] text-muted">{detail}</span>
  </details>}</span>
}

export function ErrorState({ error, retry, compact, className }: { error: unknown; retry?: () => void; compact?: boolean; className?: string }) {
  if (!error) return null
  const { message, checks, status } = describeError(error)
  return <div role="alert" className={cn('rounded-lg border border-bad/25 bg-bad-soft text-[13px]', compact ? 'px-3 py-2' : 'px-4 py-3', className)}>
    <div className="flex items-start gap-2.5">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-bad" />
      <div className="min-w-0 grow">
        <div className="break-words text-bad"><DetailText text={message} />{status && status >= 500 ? <span className="text-bad/70"> (HTTP {status})</span> : null}</div>
        {checks.length > 0 && <ul className="mt-2 space-y-1">
          {checks.map(c => <li key={c.name} className="text-ink"><span className="font-medium">{c.name}</span>: <DetailText className="text-muted" text={c.detail} />{c.fix && <span className="block text-[12px] text-muted">Fix: {c.fix}</span>}{c.help_url && <span className="block"><DocsLink href={c.help_url}>How to set it up</DocsLink></span>}</li>)}
        </ul>}
      </div>
      {retry && <Button size="sm" variant="ghost" onClick={retry} className="-my-0.5"><RotateCw />Retry</Button>}
    </div>
  </div>
}

export function Loading({ label = 'Loading…', className }: { label?: string; className?: string }) {
  return <div className={cn('flex items-center justify-center py-12', className)}><Spinner label={label} /></div>
}

export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return <div className={cn('divide-y divide-line', className)} aria-busy="true" aria-label="Loading">
    {Array.from({ length: rows }, (_, i) => <div key={i} className="flex items-center gap-4 px-4 py-3.5">
      <Skeleton className="h-3.5 w-40" /><Skeleton className="h-3.5 w-24" /><Skeleton className="ml-auto h-3.5 w-16" />
    </div>)}
  </div>
}

export function QueryView<T>({ query, children, loading, inline }: {
  query: { data: T | undefined; error: unknown; isPending: boolean; refetch: () => unknown }
  children: (data: T) => ReactNode; loading?: ReactNode; inline?: boolean
}) {
  if (query.data !== undefined) return <>{children(query.data)}</>
  if (query.error) return <ErrorState error={query.error} retry={() => query.refetch()} compact={inline} />
  return <>{loading ?? <Loading />}</>
}
