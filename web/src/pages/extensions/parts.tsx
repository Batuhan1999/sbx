import { Bot, Github, MoreHorizontal, Plus } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'

export function Group({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return <section aria-labelledby={`${id}-h`} className="space-y-3">
    <h2 id={`${id}-h`} className="text-[15px] font-semibold">{title}</h2>
    {children}
  </section>
}

export function ConnectorIcon({ kind }: { kind: string }) {
  return <span className="grid size-8 shrink-0 place-items-center rounded-md border border-line bg-sunken text-muted">
    {kind === 'github' ? <Github className="size-4" /> : <Bot className="size-4" />}
  </span>
}

/** Something that can be added: its icon, name, one line on what it is, and Add. */
export function Tile({ id, icon, title, line, onAdd }: { id?: string; icon: ReactNode; title: string; line?: string; onAdd?: () => void }) {
  return <li id={id} className="flex min-w-0 scroll-mt-16 items-center gap-3 rounded-lg border border-line bg-panel p-3">
    {icon}
    <div className="min-w-0 grow">
      <p className="font-medium leading-snug">{title}</p>
      {line && <p className="line-clamp-2 text-[12.5px] text-muted">{line}</p>}
    </div>
    {onAdd && <Button size="sm" onClick={onAdd} aria-label={`Add ${title}`}><Plus />Add</Button>}
  </li>
}

/** A row's overflow menu. */
export function RowMenu({ label, children }: { label: string; children: ReactNode }) {
  // Not modal: an item opens a dialog, and two modal layers can leave the page unclickable.
  return <DropdownMenu modal={false}>
    <DropdownMenuTrigger asChild>
      <Button size="icon-sm" variant="ghost" aria-label={label} className="data-[state=open]:bg-hover"><MoreHorizontal /></Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end">{children}</DropdownMenuContent>
  </DropdownMenu>
}

export function Row({ id, icon, title, aside, badge, line, actions, children }: {
  id?: string; icon: ReactNode; title: ReactNode; aside?: ReactNode; badge?: ReactNode; line?: ReactNode; actions?: ReactNode; children?: ReactNode
}) {
  return <li id={id} className="scroll-mt-16 px-4 py-3">
    <div className="flex items-center gap-3">
      {icon}
      <div className="min-w-0 grow">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="font-medium">{title}</span>{aside}{badge}</div>
        {line && <p className="mt-0.5 break-words text-[12.5px] text-muted">{line}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">{actions}</div>
    </div>
    {children}
  </li>
}
