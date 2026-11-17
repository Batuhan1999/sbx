import { flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type ColumnDef, type SortingState } from '@tanstack/react-table'
import { ArrowUpRight, Bot, GitBranch, GitPullRequest, User } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import type { Sandbox } from '@/api/types'
import { RuntimeLabel } from '@/components/RuntimeLabel'
import { StatusBadge } from '@/components/StatusBadge'
import { SortHeader, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ago, host, parseTime } from '@/lib/format'
import { sandboxStatus } from '@/lib/status'
import { cn } from '@/lib/utils'

const ORIGIN_ICON = { pr: GitPullRequest, session: Bot, person: User }

export function mainPreview(s: Pick<Sandbox, 'urls'>): string | undefined {
  return s.urls.web ?? Object.values(s.urls).find(u => /^https?:/.test(u))
}

/** Where each column shows: the repository and origin need room. */
const HIDE: Record<string, string> = { app: 'hidden lg:table-cell', origin: 'hidden md:table-cell' }

function columns(showRepo: boolean): ColumnDef<Sandbox>[] {
  return [
    {
      id: 'name', accessorKey: 'name',
      header: ({ column }) => <SortHeader column={column}>Sandbox</SortHeader>,
      cell: ({ row: { original: s } }) => <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-1.5">
          <Link to={`/app/sandboxes/${encodeURIComponent(s.name)}`} className="truncate font-medium after:absolute after:inset-0 hover:underline">{s.name}</Link>
          <RuntimeLabel kind={s.runtime} />
        </div>
        <div className="mt-0.5 flex min-w-0 items-center gap-1 text-[12px] text-muted"><GitBranch className="size-3 shrink-0" /><span className="truncate">{s.branch || 'default branch'}</span></div>
      </div>,
    },
    ...(showRepo ? [{
      id: 'app', accessorFn: (s: Sandbox) => s.app ?? '',
      header: ({ column }) => <SortHeader column={column}>Repository</SortHeader>,
      cell: ({ row: { original: s } }) => <span className="block truncate text-muted">{s.app || '—'}</span>,
    } satisfies ColumnDef<Sandbox>] : []),
    {
      id: 'status', accessorFn: s => sandboxStatus(s).key, header: 'Status', enableSorting: false,
      cell: ({ row: { original: s } }) => {
        const status = sandboxStatus(s)
        return <div className="flex min-w-0 flex-col items-start gap-0.5">
          <StatusBadge sandbox={s} />
          {(status.busy || status.key === 'failed') && status.detail && <span className="max-w-full truncate text-[12px] text-muted" title={status.detail}>{status.detail}</span>}
        </div>
      },
    },
    {
      id: 'origin', accessorFn: s => s.origin?.label ?? '', header: 'Origin', enableSorting: false,
      cell: ({ row: { original: s } }) => {
        const Icon = ORIGIN_ICON[s.origin?.kind] ?? User
        return <div className="flex min-w-0 items-center gap-1.5 text-[12.5px] text-muted">
          <Icon className="size-3.5 shrink-0" /><span className="truncate">{s.origin?.label}{s.origin?.kind === 'person' && s.owner ? ` · ${s.owner.split('@')[0]}` : ''}</span>
        </div>
      },
    },
    {
      id: 'created', accessorFn: s => (s.created_at ? parseTime(s.created_at) : 0), sortDescFirst: true,
      header: ({ column }) => <div className="flex justify-end"><SortHeader column={column} className="ml-0 -mr-2">Created</SortHeader></div>,
      cell: ({ row: { original: s } }) => {
        const preview = mainPreview(s)
        return <div className="flex flex-col items-end gap-0.5 text-[12.5px] text-muted">
          <span title={s.created_at ?? undefined}>{ago(s.created_at)}</span>
          {preview && sandboxStatus(s).key !== 'failed' && <a href={preview} target="_blank" rel="noreferrer" className="relative z-10 inline-flex items-center gap-0.5 text-ink hover:underline md:hidden" title={preview}>{host(preview)}<ArrowUpRight className="size-3" /></a>}
        </div>
      },
    },
  ]
}

export function SandboxTable({ sandboxes, showRepo = true }: { sandboxes: Sandbox[]; showRepo?: boolean }) {
  const [sorting, setSorting] = useState<SortingState>([])
  const cols = useMemo(() => columns(showRepo), [showRepo])
  const table = useReactTable({
    data: sandboxes, columns: cols, state: { sorting }, onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(), getRowId: s => s.name,
  })
  return <Table aria-label="Sandboxes" className="table-fixed">
    <TableHeader>
      {table.getHeaderGroups().map(group => <TableRow key={group.id} className="hover:bg-transparent">
        {group.headers.map(h => <TableHead key={h.id} className={cn(HIDE[h.column.id], h.column.id === 'created' && 'w-28 text-right', h.column.id === 'status' && 'w-40', h.column.id === 'name' && 'w-[40%] md:w-auto')}>
          {h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}
        </TableHead>)}
      </TableRow>)}
    </TableHeader>
    <TableBody>
      {table.getRowModel().rows.map(row => <TableRow key={row.id} className="relative">
        {row.getVisibleCells().map(cell => <TableCell key={cell.id} className={cn('min-w-0', HIDE[cell.column.id])}>
          {flexRender(cell.column.columnDef.cell, cell.getContext())}
        </TableCell>)}
      </TableRow>)}
    </TableBody>
  </Table>
}
