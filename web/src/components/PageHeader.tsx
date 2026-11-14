import type { ReactNode } from 'react'
import { Link } from 'react-router'
import { ChevronRight } from 'lucide-react'

export function PageHeader({ title, description, actions, crumbs, meta }: {
  title: ReactNode; description?: ReactNode; actions?: ReactNode; crumbs?: { label: string; to: string }[]; meta?: ReactNode
}) {
  return <header className="mb-6">
    {crumbs && <nav aria-label="Breadcrumb" className="mb-2 flex items-center gap-1 text-[12.5px] text-muted">
      {crumbs.map(c => <span key={c.to} className="inline-flex items-center gap-1"><Link to={c.to} className="hover:text-ink">{c.label}</Link><ChevronRight className="size-3.5 text-faint" /></span>)}
    </nav>}
    <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        <h1 className="break-words text-[20px] font-semibold tracking-[-0.01em]">{title}</h1>
        {description && <p className="mt-1 max-w-[70ch] text-[13.5px] text-muted">{description}</p>}
        {meta && <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[12.5px] text-muted">{meta}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  </header>
}
