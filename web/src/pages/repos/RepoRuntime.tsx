import { api } from '@/api/endpoints'
import { keys, useAction, useRepoInfo, useRuntimeChoice } from '@/api/queries'
import type { Runtimes } from '@/api/types'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { QueryView } from '@/components/states'
import { Panel } from '@/components/ui/primitives'
import { toast } from 'sonner'

/** Admins: where the repository's new sandboxes run. Shown only when more than one runtime is enabled. */
export function RepoRuntime({ repo }: { repo: string }) {
  const choice = useRuntimeChoice()
  const info = useRepoInfo(repo, !!choice)
  const save = useAction((runtime: string) => api.repos.setRuntime(repo, runtime || null), keys.repoInfo(repo), keys.template(repo), keys.readiness(repo))
  if (!choice) return null
  return <Panel title="Runtime" description="Where new sandboxes of this repository run, unless one is started on another. Existing sandboxes stay where they are; to move one, delete it and create it again.">
    <QueryView query={info} inline>{d => <RuntimeForm key={d.runtime ?? ''} current={d.runtime ?? ''} choice={choice} save={save} />}</QueryView>
  </Panel>
}

function RuntimeForm({ current, choice, save }: { current: string; choice: Runtimes; save: { mutateAsync: (runtime: string) => Promise<unknown> } }) {
  const form = useAppForm({ defaultValues: { runtime: current }, onSubmit: async ({ value }) => { await save.mutateAsync(value.runtime); toast.success('Runtime saved') } })
  const fallback = choice.runtimes.find(r => r.kind === choice.default)?.title ?? choice.default
  return <AppForm form={form} className="space-y-3">
    <form.AppField name="runtime">{field => <field.SelectField label="Runtime">
      <option value="">Installation default ({fallback})</option>
      {choice.runtimes.map(r => <option key={r.kind} value={r.kind}>{r.title}</option>)}
    </field.SelectField>}</form.AppField>
    <div className="flex items-center justify-end gap-3"><SubmitButton form={form}>Save</SubmitButton></div>
  </AppForm>
}
