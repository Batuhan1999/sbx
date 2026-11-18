import { Blocks, Boxes, Database, FolderGit2, LogOut, Menu, Monitor, Moon, Settings, Sun, Terminal } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router'
import { useMe } from '@/api/queries'
import { ErrorState } from '@/components/states'
import { useTheme, type Theme } from '@/components/ThemeProvider'
import { Drawer } from '@/components/ui/dialog'
import { Kbd, Skeleton } from '@/components/ui/primitives'
import { cn } from '@/lib/utils'
import { UpdateNotice } from './UpdateNotice'

const NAV = [
  { to: '/app/repos', label: 'Repositories', icon: FolderGit2, key: 'r' },
  { to: '/app/sandboxes', label: 'Sandboxes', icon: Boxes, key: 's' },
  { to: '/app/services', label: 'Services', icon: Database, key: 'v', admin: true },
  { to: '/app/extensions', label: 'Extensions', icon: Blocks, key: 'e', admin: true },
  { to: '/app/settings', label: 'Settings', icon: Settings, key: 't', admin: true },
]

function Brand() {
  return <div className="flex items-center gap-2 px-2">
    <span aria-hidden className="grid size-6 place-items-center rounded-md bg-ink text-[11px] font-bold tracking-tight text-bg">sb</span>
    <span className="text-[14px] font-semibold tracking-tight">sbx</span>
  </div>
}

function NavItem({ to, icon: Icon, label, hint, onClick }: { to: string; icon: React.ComponentType<{ className?: string }>; label: string; hint?: ReactNode; onClick?: () => void }) {
  return <NavLink to={to} onClick={onClick} className={({ isActive }) => cn(
    'group flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] transition-colors',
    isActive ? 'bg-hover font-medium text-ink' : 'text-muted hover:bg-hover hover:text-ink',
  )}>
    <Icon className="size-4 shrink-0" />
    <span className="grow">{label}</span>
    {hint && <span className="hidden opacity-0 transition-opacity group-hover:opacity-100 lg:inline">{hint}</span>}
  </NavLink>
}

function ThemeSwitch() {
  const { theme, setTheme } = useTheme()
  const options: { value: Theme; icon: typeof Sun; label: string }[] = [
    { value: 'light', icon: Sun, label: 'Light' }, { value: 'system', icon: Monitor, label: 'System' }, { value: 'dark', icon: Moon, label: 'Dark' },
  ]
  return <div role="radiogroup" aria-label="Theme" className="flex rounded-md border border-line p-0.5">
    {options.map(o => <button key={o.value} role="radio" aria-checked={theme === o.value} aria-label={o.label} title={o.label}
      onClick={() => setTheme(o.value)}
      className={cn('grid h-6 grow place-items-center rounded-[5px] text-muted transition-colors hover:text-ink', theme === o.value && 'bg-hover text-ink')}>
      <o.icon className="size-3.5" />
    </button>)}
  </div>
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const me = useMe()
  const admin = !!me.data?.admin
  return <div className="flex h-full flex-col gap-4 px-3 py-4">
    <Brand />
    <nav aria-label="Main" className="flex flex-col gap-0.5">
      {NAV.filter(n => !n.admin || admin).map(n => <NavItem key={n.to} to={n.to} icon={n.icon} label={n.label} onClick={onNavigate}
        hint={<><Kbd>g</Kbd> <Kbd>{n.key}</Kbd></>} />)}
    </nav>
    <div className="mt-auto flex flex-col gap-3">
      <NavItem to="/app/cli" icon={Terminal} label="CLI & access" onClick={onNavigate} />
      <ThemeSwitch />
      {admin && <UpdateNotice />}
      <div className="flex items-center gap-2 border-t border-line px-1 pt-3">
        {me.data ? <div className="min-w-0 grow">
          <div className="truncate text-[12.5px] font-medium" title={me.data.email}>{me.data.email}</div>
          <div className="text-[11.5px] text-muted">{me.data.admin ? 'Admin' : 'Member'} · {me.data.version}</div>
        </div> : <div className="grow space-y-1.5"><Skeleton className="h-3 w-32" /><Skeleton className="h-3 w-16" /></div>}
        {me.data?.login !== 'none' && <a href="/logout" className="grid size-7 place-items-center rounded-md text-muted hover:bg-hover hover:text-ink" aria-label="Sign out" title="Sign out"><LogOut className="size-4" /></a>}
      </div>
    </div>
  </div>
}

/** g then a letter jumps to a section, outside text fields. */
function useGoShortcuts() {
  const navigate = useNavigate()
  useEffect(() => {
    let armed = 0
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (e.metaKey || e.ctrlKey || e.altKey || t.closest('input, textarea, select, [contenteditable="true"]')) return
      if (e.key === 'g') { armed = Date.now(); return }
      if (Date.now() - armed < 1200) {
        const hit = NAV.find(n => n.key === e.key)
        if (hit) { e.preventDefault(); navigate(hit.to) }
        armed = 0
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])
}

export function Shell() {
  const [open, setOpen] = useState(false)
  const me = useMe()
  const location = useLocation()
  useGoShortcuts()
  useEffect(() => { document.getElementById('main')?.focus({ preventScroll: true }) }, [location.pathname])
  return <div className="min-h-dvh lg:grid lg:grid-cols-[232px_minmax(0,1fr)]">
    <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-md focus:bg-panel focus:px-3 focus:py-2 focus:shadow">Skip to content</a>
    <aside className="sticky top-0 hidden h-dvh border-r border-line bg-panel lg:block"><Sidebar /></aside>
    <header className="sticky top-0 z-30 flex h-12 items-center gap-2 border-b border-line bg-panel/90 px-3 backdrop-blur lg:hidden">
      <button onClick={() => setOpen(true)} className="grid size-8 place-items-center rounded-md text-muted hover:bg-hover hover:text-ink" aria-label="Open navigation"><Menu className="size-[18px]" /></button>
      <Brand />
    </header>
    <Drawer open={open} onOpenChange={setOpen} label="Navigation"><Sidebar onNavigate={() => setOpen(false)} /></Drawer>
    <main id="main" tabIndex={-1} className="min-w-0 outline-none">
      <div className="mx-auto w-full max-w-[1180px] px-4 py-6 sm:px-6 lg:px-10 lg:py-8">
        {me.error && !me.data ? <ErrorState error={me.error} retry={() => me.refetch()} /> : <Outlet />}
      </div>
    </main>
  </div>
}
