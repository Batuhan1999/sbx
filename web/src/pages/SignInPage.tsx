import { api } from '@/api/endpoints'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { emailError } from '@/lib/validation'

export function AuthFrame({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return <main className="flex min-h-dvh items-center justify-center px-4 py-12">
    <div className="w-full max-w-[360px]">
      <div className="mb-6 flex items-center gap-2">
        <span aria-hidden className="grid size-7 place-items-center rounded-md bg-ink text-[12px] font-bold text-bg">sb</span>
        <span className="font-semibold">sbx</span>
      </div>
      <h1 className="text-[20px] font-semibold tracking-tight">{title}</h1>
      {description && <p className="mt-1 text-[13.5px] text-muted">{description}</p>}
      <div className="mt-6 rounded-xl border border-line bg-panel p-5 shadow-sm">{children}</div>
    </div>
  </main>
}

export default function SignInPage() {
  const form = useAppForm({
    defaultValues: { email: '', password: '' },
    onSubmit: async ({ value }) => {
      const result = await api.signIn(value.email.trim(), value.password)
      form.reset({ email: value.email, password: '' })
      location.assign(result.next || '/app/repos')
      await new Promise(() => {}) // stay busy while the browser navigates
    },
  })
  return <AuthFrame title="Sign in" description="With the email and password for this server.">
    <AppForm form={form} className="space-y-4">
      <form.AppField name="email" validators={{ onChange: ({ value }) => emailError(value) }}>
        {field => <field.TextField label="Email" type="email" autoComplete="username" autoFocus />}
      </form.AppField>
      <form.AppField name="password" validators={{ onChange: ({ value }) => (value ? undefined : 'Enter your password.') }}>
        {field => <field.TextField label="Password" type="password" autoComplete="current-password" />}
      </form.AppField>
      <SubmitButton form={form} className="w-full">Sign in</SubmitButton>
    </AppForm>
  </AuthFrame>
}
