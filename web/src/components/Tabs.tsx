import type { ReactNode } from 'react'
import { NavLink } from 'react-router'
import { cn } from '@/lib/utils'

export function NavTabs({ tabs, label }: { tabs: { to: string; label: ReactNode; end?: boolean; badge?: ReactNode }[]; label: string }) {
  return <nav aria-label={label} className="-mx-4 mb-6 overflow-x-auto border-b border-line px-4 sm:mx-0 sm:px-0">
    <div className="flex gap-1">
      {tabs.map(t => <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => cn(
        'relative inline-flex h-9 items-center gap-1.5 whitespace-nowrap px-2.5 text-[13px] transition-colors',
        isActive ? 'font-medium text-ink after:absolute after:inset-x-2 after:-bottom-px after:h-0.5 after:rounded-full after:bg-ink' : 'text-muted hover:text-ink',
      )}>{t.label}{t.badge}</NavLink>)}
    </div>
  </nav>
}

export function Segmented<T extends string>({ value, onChange, options, label }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode }[]; label: string
}) {
  return <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-line bg-sunken p-0.5">
    {options.map(o => <button key={o.value} type="button" role="radio" aria-checked={value === o.value} onClick={() => onChange(o.value)}
      className={cn('h-6 rounded-[5px] px-2.5 text-[12.5px] transition-colors', value === o.value ? 'bg-panel font-medium text-ink shadow-sm' : 'text-muted hover:text-ink')}>
      {o.label}
    </button>)}
  </div>
}
