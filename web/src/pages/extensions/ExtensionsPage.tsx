import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Github } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Navigate, useLocation } from 'react-router'
import { toast } from 'sonner'
import { api } from '@/api/endpoints'
import { keys, useAction, useConnections, useMe } from '@/api/queries'
import type { Check, Connection } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { PageHeader } from '@/components/PageHeader'
import { ErrorState, SkeletonRows } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { DropdownMenuItem, DropdownMenuSeparator } from '@/components/ui/dropdown-menu'
import { Checks, ConnectionForm, formFields } from './ConnectionCard'
import { ConnectorIcon, Group, Row, RowMenu } from './parts'

/** The coding agents, each with its state, and GitHub. Shared services have their own page (Services). */
export default function ExtensionsPage() {
  const me = useMe()
  if (me.data && !me.data.admin) return <Navigate to="/app/services" replace />
  return <><PageHeader title="Extensions" /><AdminExtensions /></>
}

function AdminExtensions() {
  const connections = useConnections(true)
  const { hash } = useLocation()
  useEffect(() => {
    if (hash && connections.data) document.getElementById(hash.slice(1))?.scrollIntoView({ block: 'start' })
  }, [hash, connections.data])
  const list = connections.data ?? []
  const agents = list.filter(c => c.group === 'agent')
  const github = list.find(c => c.kind === 'github')
  return <div className="max-w-4xl space-y-8">
    {connections.error && <ErrorState error={connections.error} retry={() => connections.refetch()} />}
    <Group id="agents" title="Coding agents">
      <div className="overflow-hidden rounded-lg border border-line bg-panel">
        {connections.isPending ? <SkeletonRows rows={3} />
          : <ul className="divide-y divide-line">{agents.map(c => <ConnectorRow key={c.kind} c={c} />)}</ul>}
      </div>
    </Group>
    {github && <Group id="github-h" title="GitHub">
      <div className="overflow-hidden rounded-lg border border-line bg-panel">
        <ul><ConnectorRow c={github} /></ul>
      </div>
    </Group>}
  </div>
}

/** A coding agent or GitHub with its state: ready (no badge), needs its key (chosen in the setup, already in every
 * template), needs attention, or not set up; one button for what comes next, the rest in its menu. */
function ConnectorRow({ c }: { c: Connection }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [checks, setChecks] = useState<Check[] | null>(null)
  const test = useAction(async () => setChecks((await api.connections.test(c.kind)).checks), keys.connections)
  const editable = formFields(c).length > 0 && c.actions.includes('connect')
  const waiting = c.state === 'not_connected' && c.facts.chosen === true
  const unset = !waiting && (c.state === 'not_connected' || c.state === 'off')
  const ready = !waiting && !unset
  const installUrl = typeof c.facts.install_url === 'string' ? c.facts.install_url : ''
  const badge = waiting ? <StateBadge tone="warn" label="Needs its key" /> : unset ? <StateBadge tone="idle" label="Not set up" />
    : c.state === 'attention' ? <StateBadge tone="warn" label="Needs attention" />
    : checks?.some(x => !x.ok) ? <StateBadge tone="bad" label="Test failed" /> : null
  const setUp = unset && (editable || c.kind === 'github')
  const hasMenu = ready && ((c.actions.includes('test')) || editable || !!installUrl || c.actions.includes('disconnect'))
  return <Row id={c.kind} icon={<ConnectorIcon kind={c.kind} />} title={c.title} badge={badge}
    line={ready && c.summary ? c.summary : undefined}
    actions={<>
      {waiting && editable && <Button size="sm" variant="primary" onClick={() => setEditing(true)}>Add key</Button>}
      {setUp && <Button size="sm" onClick={() => setEditing(true)}>Set up</Button>}
      {test.isPending && <span className="text-[12px] text-muted">Testing…</span>}
      {hasMenu && <RowMenu label={`Actions for ${c.title}`}>
        {c.actions.includes('test') && <DropdownMenuItem onSelect={() => test.mutate()}>Test</DropdownMenuItem>}
        {editable && <DropdownMenuItem onSelect={() => setEditing(true)}>Edit</DropdownMenuItem>}
        {installUrl && <DropdownMenuItem onSelect={() => window.open(installUrl, '_blank', 'noreferrer')}>Manage on GitHub<ArrowUpRight /></DropdownMenuItem>}
        {c.actions.includes('disconnect') && <><DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={() => setDisconnecting(true)}>Disconnect</DropdownMenuItem></>}
      </RowMenu>}
    </>}>
    {(checks || test.error) && <div className="mt-2 space-y-2 pl-11">
      {checks && <Checks checks={checks} failedFirst />}
      <ErrorState error={test.error} compact />
    </div>}
    {(editable || c.kind === 'github') && <Dialog open={editing} onOpenChange={setEditing} title={c.title} wide={formFields(c).filter(f => !f.advanced).length > 2}>
      {c.kind === 'github' && unset ? <GitHubAdd onDone={() => setEditing(false)} /> : <ConnectionForm c={c} onDone={() => setEditing(false)} />}
    </Dialog>}
    {disconnecting && <ConfirmDialog open onOpenChange={setDisconnecting} title={`Disconnect ${c.title}?`}
      description="Its saved credentials are removed from this server." confirm="Disconnect"
      onConfirm={async () => {
        const result = await api.connections.disconnect(c.kind)
        if (result.rebuilds && Object.keys(result.rebuilds.runtimes).length) toast(`${c.title} disconnected`, { description: result.rebuilds.message })
        await qc.invalidateQueries({ queryKey: keys.connections })
      }} />}
  </Row>
}


/** GitHub's Add: create the App on GitHub (its manifest fills every value; the organization optional). */
function GitHubAdd({ onDone }: { onDone: () => void }) {
  const start = useAction((owner: string) => api.connections.githubManifest(owner, '/app/extensions'))
  const form = useAppForm({
    defaultValues: { owner: '' },
    onSubmit: async ({ value }) => {
      const { action_url, manifest } = await start.mutateAsync(value.owner.trim())
      // GitHub's App manifest flow: the browser posts a form to GitHub, which shows the App for confirmation.
      const f = document.createElement('form')
      f.method = 'post'
      f.action = action_url
      const input = document.createElement('input')
      input.type = 'hidden'
      input.name = 'manifest'
      input.value = manifest
      f.append(input)
      document.body.append(f)
      f.submit()
    },
  })
  return <AppForm form={form} className="space-y-4">
    <form.AppField name="owner" validators={{ onChange: ({ value }) => value && !/^[A-Za-z0-9-]+$/.test(value) ? 'Use a GitHub organization name.' : undefined }}>
      {field => <field.TextField label="GitHub organization" optional placeholder="Your account" autoComplete="off" spellCheck={false} />}
    </form.AppField>
    <div className="flex justify-end gap-2">
      <Button variant="ghost" onClick={onDone}>Cancel</Button>
      <SubmitButton form={form}><Github />Create on GitHub</SubmitButton>
    </div>
  </AppForm>
}

