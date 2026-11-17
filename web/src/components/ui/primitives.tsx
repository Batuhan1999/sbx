import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function Spinner({ className, label }: { className?: string; label?: string }) {
  return <span role={label ? 'status' : undefined} className="inline-flex items-center gap-2 text-muted">
    <svg viewBox="0 0 16 16" className={cn('size-4 animate-spin', className)} aria-hidden>
      <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeOpacity=".2" strokeWidth="2" />
      <path d="M14.5 8A6.5 6.5 0 0 0 8 1.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
    {label && <span className="text-[13px]">{label}</span>}
  </span>
}

export function Skeleton({ className }: { className?: string }) {
  return <span aria-hidden className={cn('block animate-pulse rounded bg-sunken', className)} />
}

const TONES = {
  neutral: 'bg-sunken text-muted border-line',
  accent: 'bg-accent-soft text-accent border-transparent',
  ok: 'bg-ok-soft text-ok border-transparent',
  warn: 'bg-warn-soft text-warn border-transparent',
  bad: 'bg-bad-soft text-bad border-transparent',
  info: 'bg-info-soft text-info border-transparent',
}
export type BadgeTone = keyof typeof TONES

export function Badge({ tone = 'neutral', className, children, ...props }: ComponentProps<'span'> & { tone?: BadgeTone }) {
  return <span className={cn('inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded-md border px-1.5 text-[11.5px] font-medium [&_svg]:size-3', TONES[tone], className)} {...props}>{children}</span>
}

export function Panel({ title, description, actions, children, className, bodyClassName, id }: {
  title?: ReactNode; description?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClassName?: string; id?: string
}) {
  return <section id={id} aria-labelledby={title && id ? `${id}-title` : undefined} className={cn('rounded-xl border border-line bg-panel shadow-[0_1px_2px_rgb(0_0_0/0.04)]', className)}>
    {(title || actions) && <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-line px-5 py-4">
      <div className="min-w-0 flex-1 basis-64">
        {title && <h2 id={id ? `${id}-title` : undefined} className="text-[14px] font-semibold leading-tight tracking-tight">{title}</h2>}
        {description && <p className="mt-1 text-[12.5px] text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>}
    <div className={cn('p-5', bodyClassName)}>{children}</div>
  </section>
}

export function Facts({ items, className }: { items: [ReactNode, ReactNode][]; className?: string }) {
  return <dl className={cn('grid grid-cols-1 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-[minmax(110px,max-content)_minmax(0,1fr)]', className)}>
    {items.map(([k, v], i) => <div key={i} className="contents">
      <dt className="text-muted">{k}</dt>
      <dd className="mb-1.5 min-w-0 break-words sm:mb-0">{v}</dd>
    </div>)}
  </dl>
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded border border-line bg-sunken px-1 py-px text-[11px] text-muted">{children}</kbd>
}

export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return <code className={cn('rounded bg-sunken px-1 py-px text-[12.5px]', className)}>{children}</code>
}
