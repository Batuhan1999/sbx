import { Link } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useCatalog, useSharedConnections } from '@/api/queries'
import type { Attachment, CatalogPackage, PackageInput } from '@/api/types'
import { PackageHeading, PackageInputControl, packageInputError } from '@/components/PackageLogo'
import { AppForm, MoreOptions, SECRET_KEPT, SubmitButton, useAppForm, useStore } from '@/components/form'
import { EmptyState, ErrorState, QueryView } from '@/components/states'
import { Button } from '@/components/ui/button'
import { article } from '@/lib/packages'
import { keyError, required } from '@/lib/validation'
import { toast } from 'sonner'

/** A package's settings for one repository: new, or an attachment it already has. */
export function EntryForm(props: { repo: string; name: string; attachment?: Attachment; onDone: () => void; onBack?: () => void; onCancel?: () => void; bare?: boolean }) {
  // The catalog is loaded already: the form shows at once instead of after its own request.
  const catalog = useCatalog()
  return <QueryView query={catalog}>{list => {
    const found = list.find(p => p.name === props.name)
    return found ? <AttachForm {...props} entry={found} /> : <EmptyState title="This package isn’t in the catalog" />
  }}</QueryView>
}

/** Secret inputs are write-only. */
export function AttachForm({ repo, entry, attachment, onDone, onBack, onCancel, bare }: {
  repo: string; entry: CatalogPackage; attachment?: Attachment; onDone: () => void; onBack?: () => void; onCancel?: () => void
  /** Without the package's heading, where the page around it shows the package already. */
  bare?: boolean
}) {
  const catalog = useCatalog()
  const shared = useSharedConnections(Object.keys(entry.requires).length > 0)
  const attach = useAction((v: { key: string; inputs: Record<string, string>; connections: Record<string, string> }) => api.attachments.attach(repo, {
    package: entry.name, key: v.key, inputs: Object.fromEntries(Object.entries(v.inputs).filter(([n, x]) => x !== '' || !entry.inputs[n]?.secret)),
    connections: Object.fromEntries(Object.entries(v.connections).filter(([, x]) => x)), check: true,
  }), keys.env(repo), keys.readiness(repo), keys.repos, keys.catalog)
  const inputs = Object.entries(entry.inputs).filter(([, i]) => !i.hidden)
  const form = useAppForm({
    defaultValues: {
      key: attachment?.key ?? entry.name.split('/').pop()!.replace(/[^a-z0-9_-]/g, '-'),
      inputs: Object.fromEntries(inputs.map(([n, i]) => [n, i.secret ? '' : attachment?.inputs[n] ?? i.default ?? ''])) as Record<string, string>,
      connections: Object.fromEntries(Object.keys(entry.requires).map(slot => [slot, attachment?.connections[slot] ?? ''])) as Record<string, string>,
    },
    onSubmit: async ({ value }) => { await attach.mutateAsync(value); toast.success(`${entry.title} saved`); onDone() },
  })
  const submitting = useStore(form.store, s => s.isSubmitting)
  const titleOf = (kind: string) => catalog.data?.find(p => p.name === kind)?.title ?? kind
  const main = inputs.filter(([, i]) => !i.advanced)
  const advanced = inputs.filter(([, i]) => i.advanced)
  const renderInput = ([n, i]: [string, PackageInput]) => {
    const set = attachment?.secret_inputs[n]
    return <form.AppField key={n} name={`inputs.${n}`} validators={{ onChange: ({ value }) => (i.required && !set && !i.default && !attachment?.inputs[n] ? required(value ?? '') : undefined) ?? packageInputError(i, value) }}>
      {field => <PackageInputControl field={field} name={n} input={i} placeholder={i.secret && set ? SECRET_KEPT : i.default ?? ''}
        note={!i.agent && i.secret ? 'Hidden from the coding agent.' : undefined} />}
    </form.AppField>
  }
  return <AppForm form={form} className="space-y-4">
    {!attachment && !bare && <PackageHeading logo={entry.logo} title={entry.title} description={entry.description} docs={entry.docs} docsLabel={`${entry.title} docs`} />}
    {Object.entries(entry.requires).map(([slot, need]) => {
      const title = titleOf(need.kind)
      const options = (shared.data ?? []).filter(s => s.package === need.kind)
      if (shared.error && !shared.data) return <div key={slot} className="text-[12.5px]">
        <p className="mb-1 font-medium">{title}</p>
        <ErrorState error={shared.error} compact retry={() => shared.refetch()} />
      </div>
      if (shared.isSuccess && options.length === 0) return <div key={slot} className="text-[12.5px]">
        <p className="mb-1 font-medium">{title}</p>
        <p className="text-muted">None connected yet. <Link className="text-accent hover:underline" to="/app/extensions" state={{ connect: need.kind }}>Connect {article(title)}</Link> first.</p>
      </div>
      // One connection to choose from: the server takes it when none is named, so there is nothing to pick.
      const current = attachment?.connections[slot] ?? ''
      if (shared.isSuccess && options.length === 1 && (current === '' || current === options[0].key)) return null
      return <form.AppField key={slot} name={`connections.${slot}`}>
        {field => <field.SelectField label={title}>
          <option value="">Choose…</option>
          {options.map(s => <option key={s.key} value={s.key}>{s.key}</option>)}
        </field.SelectField>}
      </form.AppField>
    })}
    {main.map(renderInput)}
    {(advanced.length > 0 || !attachment) && <MoreOptions open={advanced.some(([n, i]) => !i.secret && (attachment?.inputs[n] ?? '') !== '' && attachment?.inputs[n] !== i.default)}>
      {advanced.map(renderInput)}
      {!attachment && <form.AppField name="key" validators={{ onChange: ({ value }) => keyError(value) }}>
        {field => <field.TextField label="Name in the recipe" className="font-mono" autoComplete="off" spellCheck={false} />}
      </form.AppField>}
    </MoreOptions>}
    <div className="flex justify-between gap-2">
      {onBack ? <Button variant="ghost" disabled={submitting} onClick={onBack}>Back</Button> : <span />}
      <div className="flex gap-2"><Button variant="ghost" onClick={onCancel ?? onDone}>Cancel</Button><SubmitButton form={form}>{attachment ? 'Save' : 'Add'}</SubmitButton></div>
    </div>
  </AppForm>
}
