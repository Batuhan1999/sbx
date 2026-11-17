import { flexRender, getCoreRowModel, getFilteredRowModel, getSortedRowModel, useReactTable, type ColumnDef, type SortingState } from '@tanstack/react-table'
import { KeyRound, MoreHorizontal, Search, Trash2, UserPlus } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import { api } from '@/api/endpoints'
import { keys, useAction, useMe, useUsers } from '@/api/queries'
import type { PasswordLink, User, Users } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyCommand'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { ErrorState, QueryView, SkeletonRows } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Badge } from '@/components/ui/primitives'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { SortHeader, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ago, parseTime, plural } from '@/lib/format'
import { cn } from '@/lib/utils'
import { toast } from 'sonner'
import { emailError } from '@/lib/validation'

type Role = 'member' | 'admin'
const ROLES: { value: Role; title: string; description: string }[] = [
  { value: 'member', title: 'Member', description: 'Creates and uses their own sandboxes.' },
  { value: 'admin', title: 'Admin', description: 'Also manages settings, repositories and members.' },
]

type RowActions = { onLink: (l: PasswordLink) => void; onError: (e: unknown) => void }

/** Settings → People: the members table, invites and roles. */
export function People() {
  const users = useUsers()
  const [query, setQuery] = useState('')
  const [link, setLink] = useState<PasswordLink | null>(null)
  // One place for a row action's error: inside a row it would change the row's height.
  const [error, setError] = useState<unknown>(null)
  const actions = useMemo<RowActions>(() => ({ onLink: l => { setError(null); setLink(l) }, onError: setError }), [])
  const passwords = users.data?.login === 'password'
  return <div className="space-y-4">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="space-y-1">
        <h2 className="text-[16px] font-semibold tracking-tight">Members</h2>
        <p className="text-[13px] text-muted">
          {users.data?.login === 'oidc' ? 'Members sign in with your identity provider.'
            : users.data?.login === 'none' ? 'Sign-in is off: everyone who reaches this server is an admin.'
            : 'Invite people by link; each sets their own password.'}
        </p>
      </div>
      <InviteDialog onLink={actions.onLink} passwords={passwords} />
    </div>
    <div className="relative max-w-xs">
      <Search aria-hidden className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-faint" />
      <input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Search members" aria-label="Search members" className="h-9 pl-9" />
    </div>
    {error ? <div className="flex items-start gap-2"><ErrorState error={error} compact className="grow" />
      <Button size="sm" variant="ghost" onClick={() => setError(null)}>Dismiss</Button></div> : null}
    <QueryView query={users} loading={<div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={3} /></div>}>
      {data => <MembersTable data={data} query={query} actions={actions} />}
    </QueryView>
    <LinkDialog link={link} onClose={() => setLink(null)} />
  </div>
}

function MembersTable({ data, query, actions }: { data: Users; query: string; actions: RowActions }) {
  const me = useMe().data?.email
  const passwords = data.login === 'password'
  const [sorting, setSorting] = useState<SortingState>([])
  const columns = useMemo<ColumnDef<User>[]>(() => [
    { id: 'member', accessorKey: 'email', header: ({ column }) => <SortHeader column={column}>Member</SortHeader>,
      cell: ({ row: { original: u } }) => <MemberCell user={u} self={u.email === me} passwords={passwords} /> },
    { id: 'role', accessorFn: u => (u.admin ? 'admin' : 'member'), header: ({ column }) => <SortHeader column={column}>Role</SortHeader>,
      cell: ({ row: { original: u } }) => <RoleSelect user={u} actions={actions} /> },
    { id: 'active', accessorFn: u => (u.last_sign_in_at ? parseTime(u.last_sign_in_at) : 0), sortDescFirst: true,
      header: ({ column }) => <SortHeader column={column}>Last sign-in</SortHeader>,
      cell: ({ row: { original: u } }) => <span className="text-muted">{u.last_sign_in_at ? ago(u.last_sign_in_at) : '—'}</span> },
    { id: 'actions', header: () => <span className="sr-only">Actions</span>, enableSorting: false,
      cell: ({ row: { original: u } }) => <RowMenu user={u} self={u.email === me} passwords={passwords} actions={actions} /> },
  ], [me, passwords, actions])
  const table = useReactTable({
    data: data.users, columns, state: { sorting, globalFilter: query.trim() }, onSortingChange: setSorting,
    globalFilterFn: (row, _id, value: string) => row.original.email.includes(value.toLowerCase()),
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(), getFilteredRowModel: getFilteredRowModel(),
    getRowId: u => u.email,
  })
  const width: Record<string, string> = { member: '', role: 'w-32 px-2 sm:w-36 sm:px-4', active: 'hidden w-36 md:table-cell', actions: 'w-12 px-2 text-right' }
  const rows = table.getRowModel().rows
  return <Table>
    <TableHeader>
      {table.getHeaderGroups().map(g => <TableRow key={g.id} className="hover:bg-transparent">
        {g.headers.map(h => <TableHead key={h.id} className={width[h.column.id]}>{flexRender(h.column.columnDef.header, h.getContext())}</TableHead>)}
      </TableRow>)}
    </TableHeader>
    <TableBody>
      {rows.map(r => <TableRow key={r.id}>
        {r.getVisibleCells().map(c => <TableCell key={c.id} className={cn(width[c.column.id], c.column.id === 'member' && 'max-w-0')}>{flexRender(c.column.columnDef.cell, c.getContext())}</TableCell>)}
      </TableRow>)}
      {!rows.length && <TableRow className="hover:bg-transparent"><TableCell colSpan={4} className="h-24 text-center text-muted">No member matches “{query}”.</TableCell></TableRow>}
    </TableBody>
  </Table>
}

function Avatar({ email }: { email: string }) {
  const [name] = email.split('@')
  const parts = name.split(/[._-]+/).filter(Boolean)
  const initials = (parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2)).toUpperCase()
  return <span aria-hidden className="grid size-8 shrink-0 place-items-center rounded-full border border-line bg-sunken text-[11.5px] font-medium text-muted">{initials}</span>
}

function MemberCell({ user, self, passwords }: { user: User; self: boolean; passwords: boolean }) {
  const pending = passwords && !user.password
  return <div className="flex min-w-0 items-center gap-3">
    <Avatar email={user.email} />
    <div className="min-w-0">
      <div className="flex min-w-0 items-center gap-2">
        <span className="min-w-0 truncate font-medium">{user.email}</span>
        {self && <Badge className="shrink-0">You</Badge>}
        {pending && <Badge tone="warn" className="shrink-0">Pending</Badge>}
      </div>
      <div className="truncate text-[12px] text-muted">
        {pending ? 'Has not set a password yet' : user.last_sign_in_at ? `Signed in ${ago(user.last_sign_in_at)}` : 'Never signed in'}
        {user.tokens ? ` · ${plural(user.tokens, 'API token')}` : ''}
      </div>
    </div>
  </div>
}

function RoleSelect({ user, actions }: { user: User; actions: RowActions }) {
  const role = useAction((admin: boolean) => api.users.setAdmin(user.email, admin), keys.users, keys.settings, keys.me)
  return <Select value={user.admin ? 'admin' : 'member'} disabled={role.isPending}
    onValueChange={v => role.mutate(v === 'admin', {
      onSuccess: () => { actions.onError(null); toast.success(`${user.email} is now ${v === 'admin' ? 'an admin' : 'a member'}`) },
      onError: actions.onError })}>
    <SelectTrigger size="sm" aria-label={`Role of ${user.email}`}><SelectValue /></SelectTrigger>
    <SelectContent align="end" className="w-64">
      {ROLES.map(r => <SelectItem key={r.value} value={r.value} description={r.description}>{r.title}</SelectItem>)}
    </SelectContent>
  </Select>
}

function RowMenu({ user, self, passwords, actions }: { user: User; self: boolean; passwords: boolean; actions: RowActions }) {
  const newLink = useAction(() => api.users.link(user.email))
  const remove = useAction(() => api.users.remove(user.email).then(r => { toast.success(`Removed ${user.email}`); return r }), keys.users, keys.settings)
  const [removing, setRemoving] = useState(false)
  if (self && !passwords) return null
  return <>
    {/* Not modal: it opens the Remove dialog, and two modal layers can leave the page unclickable. */}
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={`Actions for ${user.email}`} className="data-[state=open]:bg-hover data-[state=open]:text-ink"><MoreHorizontal /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {passwords && !self && <DropdownMenuItem disabled={newLink.isPending} onSelect={() => newLink.mutate(undefined, { onSuccess: actions.onLink, onError: actions.onError })}>
          <KeyRound />{user.password ? 'Reset password' : 'New invite link'}
        </DropdownMenuItem>}
        {self && <DropdownMenuItem onSelect={() => location.assign('/app/cli')}><KeyRound />Change your password</DropdownMenuItem>}
        {!self && <>
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={() => setRemoving(true)}><Trash2 />Remove member</DropdownMenuItem>
        </>}
      </DropdownMenuContent>
    </DropdownMenu>
    {removing && <ConfirmDialog open onOpenChange={setRemoving} title={`Remove ${user.email}?`}
      description="They are signed out everywhere and their API tokens stop working." confirm="Remove member" onConfirm={() => remove.mutateAsync()} />}
  </>
}

function InviteDialog({ onLink, passwords }: { onLink: (l: PasswordLink) => void; passwords: boolean }) {
  const [open, setOpen] = useState(false)
  const roleId = useId()
  const add = useAction((v: { email: string; role: Role }) => api.users.add(v.email.trim().toLowerCase(), v.role === 'admin'), keys.users, keys.settings)
  const form = useAppForm({
    defaultValues: { email: '', role: 'member' as Role },
    onSubmit: async ({ value }) => {
      const result = await add.mutateAsync(value)
      form.reset(); setOpen(false)
      if (passwords) onLink(result)
    },
  })
  return <Dialog open={open} onOpenChange={o => { setOpen(o); if (!o) form.reset() }} title="Invite member"
    description={passwords ? 'You get a link to share. They set their own password with it.' : 'They can sign in with your identity provider right away.'}
    trigger={<Button variant="primary"><UserPlus />Invite member</Button>}>
    <AppForm form={form} className="space-y-4">
      <form.AppField name="email" validators={{ onSubmit: ({ value }) => emailError(value) }}>
        {field => <field.TextField label="Email" type="email" placeholder="name@example.com" autoComplete="off" autoFocus className="h-9" />}
      </form.AppField>
      <form.Field name="role">{field => <div>
        <label htmlFor={roleId} className="mb-1 block text-[12.5px] font-medium">Role</label>
        <Select value={field.state.value} onValueChange={v => field.handleChange(v as Role)}>
          <SelectTrigger id={roleId}><SelectValue /></SelectTrigger>
          <SelectContent>{ROLES.map(r => <SelectItem key={r.value} value={r.value} description={r.description}>{r.title}</SelectItem>)}</SelectContent>
        </Select>
      </div>}</form.Field>
      <div className="flex justify-end gap-2 pt-2">
        <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
        <SubmitButton form={form}>{passwords ? 'Create invite link' : 'Add member'}</SubmitButton>
      </div>
    </AppForm>
  </Dialog>
}

function LinkDialog({ link, onClose }: { link: PasswordLink | null; onClose: () => void }) {
  return <Dialog open={!!link} onOpenChange={o => { if (!o) onClose() }} title="Share the invite link"
    description={link ? <>Send it to <span className="font-medium text-ink">{link.email}</span>. It works once, for {plural(link.days, 'day')}, and is not shown again.</> : undefined}>
    {link && <div className="space-y-4">
      <div className="flex h-9 items-center gap-1 rounded-md border border-line bg-sunken pl-3 pr-1">
        <input readOnly value={link.link} aria-label="Invite link" onFocus={e => e.target.select()}
          className="h-full min-h-0 min-w-0 grow truncate border-0 bg-transparent p-0 font-mono text-[12px] shadow-none hover:border-0 focus-visible:shadow-none" />
        <CopyButton text={link.link} variant="ghost" label="Copy" />
      </div>
      <div className="flex justify-end"><Button variant="primary" onClick={onClose}>Done</Button></div>
    </div>}
  </Dialog>
}
