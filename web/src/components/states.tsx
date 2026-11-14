import { AlertTriangle, RotateCw } from 'lucide-react'
import type { ReactNode } from 'react'
import { describeError } from '@/api/client'
import { cn } from '@/lib/utils'
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

export function ErrorState({ error, retry, compact, className }: { error: unknown; retry?: () => void; compact?: boolean; className?: string }) {
  if (!error) return null
  const { message, checks, status } = describeError(error)
  return <div role="alert" className={cn('rounded-lg border border-bad/25 bg-bad-soft text-[13px]', compact ? 'px-3 py-2' : 'px-4 py-3', className)}>
    <div className="flex items-start gap-2.5">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-bad" />
      <div className="min-w-0 grow">
        <p className="break-words text-bad">{message}{status && status >= 500 ? <span className="text-bad/70"> (HTTP {status})</span> : null}</p>
        {checks.length > 0 && <ul className="mt-2 space-y-1">
          {checks.map(c => <li key={c.name} className="text-ink"><span className="font-medium">{c.name}</span>: <span className="text-muted">{c.detail}</span>{c.fix && <span className="block text-[12px] text-muted">Fix: {c.fix}</span>}</li>)}
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
