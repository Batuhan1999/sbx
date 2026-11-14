import { useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'
import { useNavigate } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useMe, useRepos } from '@/api/queries'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
import { nameFromBranch, sandboxNameError } from '@/lib/validation'

/** How long to wait for a fast refusal (bad name, repo not served) before showing the sandbox while it is created. */
const EARLY_ERROR_MS = 2500

export function NewSandboxForm({ repo: fixedRepo, branch: initialBranch = '', onCancel }: { repo?: string; branch?: string; onCancel?: () => void }) {
  const me = useMe()
  const repos = useRepos()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const choices = useMemo(() => {
    const served = new Set<string>()
    for (const r of repos.data ?? []) if (r.trust) served.add(r.repo)
    for (const r of me.data?.repos ?? []) if (!r.includes('*')) served.add(r.toLowerCase())
    return [...served].sort()
  }, [repos.data, me.data])
  const form = useAppForm({
    defaultValues: { repo: fixedRepo ?? '', branch: initialBranch, name: nameFromBranch(initialBranch) },
    onSubmit: async ({ value }) => {
      const body = { name: value.name, branch: value.branch.trim() || undefined, repo: value.repo || choices[0] || undefined, public_keys: '' }
      const created = api.sandboxes.create(body).finally(() => qc.invalidateQueries({ queryKey: keys.sandboxes }))
      const early = await Promise.race([created.then(() => 'done' as const), new Promise<'slow'>(r => setTimeout(() => r('slow'), EARLY_ERROR_MS))])
      created.catch(() => { /* shown on the sandbox page as its lifecycle error */ })
      if (early === 'slow' || early === 'done') navigate(`/app/sandboxes/${encodeURIComponent(value.name)}`)
    },
  })
  const repo = useStore(form.store, s => s.values.repo)
  return <AppForm form={form} className="space-y-4">
    {!fixedRepo && <form.AppField name="repo">{field => <field.SelectField label="Repository">
      {choices.length === 0 && <option value="">No repositories yet</option>}
      {choices.map(r => <option key={r} value={r}>{r}</option>)}
    </field.SelectField>}</form.AppField>}
    <form.AppField name="branch" listeners={{ onChange: ({ value, fieldApi }) => {
      const name = fieldApi.form.getFieldValue('name')
      if (!fieldApi.form.getFieldMeta('name')?.isDirty || name === '') fieldApi.form.setFieldValue('name', nameFromBranch(value), { dontUpdateMeta: true })
    } }}>
      {field => <field.TextField label="Branch" optional placeholder="default branch" autoComplete="off" spellCheck={false} help="Checked out from GitHub. Empty uses the repository's default branch." />}
    </form.AppField>
    <form.AppField name="name" validators={{ onChange: ({ value }) => sandboxNameError(value) }}>
      {field => <field.TextField label="Name" autoComplete="off" spellCheck={false} placeholder="feature-login" help="Lowercase letters, digits and hyphens. Also its preview hostname." />}
    </form.AppField>
    <p className="text-[12.5px] text-muted">The sandbox runs {repo || fixedRepo || choices[0] || 'the repository'}'s recipe: services, setup, then the app. You can follow each step on its page.</p>
    <div className="flex justify-end gap-2 pt-1">
      {onCancel && <button type="button" className="h-8 rounded-md px-3 text-[13px] text-muted hover:bg-hover hover:text-ink" onClick={onCancel}>Cancel</button>}
      <SubmitButton form={form} disabled={!fixedRepo && choices.length === 0}>Create sandbox</SubmitButton>
    </div>
  </AppForm>
}
