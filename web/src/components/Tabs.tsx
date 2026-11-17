import * as T from '@radix-ui/react-toggle-group'
import type { ReactNode } from 'react'
import { NavLink } from 'react-router'
import { cn } from '@/lib/utils'

/** shadcn's line Tabs, for sections that are routes (each tab is a link, so it keeps its URL). */
export function NavTabs({ tabs, label }: { tabs: { to: string; label: ReactNode; end?: boolean; badge?: ReactNode }[]; label: string }) {
  return <nav aria-label={label} className="-mx-4 mb-6 overflow-x-auto border-b border-line px-4 sm:mx-0 sm:px-0">
    <div className="flex gap-1">
      {tabs.map(t => <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => cn(
        'relative inline-flex h-9 items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 text-[13px] outline-none transition-colors duration-150 ease-out',
        'focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-accent/25',
        'after:absolute after:inset-x-2 after:-bottom-px after:h-0.5 after:rounded-full after:bg-ink after:transition-opacity after:duration-150',
        isActive ? 'font-medium text-ink after:opacity-100' : 'text-muted hover:text-ink after:opacity-0',
      )}>{t.label}{t.badge}</NavLink>)}
    </div>
  </nav>
}

/** shadcn's ToggleGroup (single choice) styled as a segmented control. */
export function Segmented<T extends string>({ value, onChange, options, label }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode }[]; label: string
}) {
  return <T.Root type="single" value={value} onValueChange={v => { if (v) onChange(v as T) }} aria-label={label}
    className="inline-flex h-8 items-center rounded-lg bg-sunken p-[3px]">
    {options.map(o => <T.Item key={o.value} value={o.value} className={cn(
      'inline-flex h-full items-center rounded-md px-2.5 text-[12.5px] text-muted outline-none transition-[color,background-color,box-shadow] duration-150 ease-out',
      'hover:text-ink data-[state=on]:bg-panel data-[state=on]:font-medium data-[state=on]:text-ink data-[state=on]:shadow-sm',
      'focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-accent/25')}>{o.label}</T.Item>)}
  </T.Root>
}
