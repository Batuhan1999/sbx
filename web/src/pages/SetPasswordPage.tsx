import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '@/api/endpoints'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { ErrorState, Loading } from '@/components/states'
import { AnchorButton } from '@/components/ui/button'
import { passwordError } from '@/lib/validation'
import { AuthFrame } from './SignInPage'

/** The link's token is in the fragment (#t=...), which never reaches a server log; it leaves the address bar at once. */
function takeToken(): string {
  const token = new URLSearchParams(location.hash.slice(1)).get('t') ?? ''
  if (token) history.replaceState(null, '', location.pathname)
  return /^[A-Za-z0-9_-]+$/.test(token) ? token : ''
}

export default function SetPasswordPage() {
  const [token] = useState(takeToken)
  const link = useQuery({ queryKey: ['password-link', token], queryFn: () => api.passwordLink(token), enabled: !!token, retry: false, staleTime: Infinity })
  const form = useAppForm({
    defaultValues: { password: '', confirm: '' },
    onSubmit: async ({ value }) => {
      const result = await api.setPassword(token, value.password)
      form.reset()
      location.assign(link.data?.login === 'password' ? result.next : '/auth/login?next=%2Fapp%2Frepos')
      await new Promise(() => {}) // stay busy while the browser navigates
    },
  })
  if (!token) return <AuthFrame title="Set your password"><ErrorState error="This page needs the link an admin gave you." compact /></AuthFrame>
  if (link.isPending) return <AuthFrame title="Set your password"><Loading /></AuthFrame>
  if (link.error) return <AuthFrame title="Set your password">
    <div className="space-y-4"><ErrorState error={link.error} compact /><AnchorButton href="/auth/login" className="w-full">Go to sign-in</AnchorButton></div>
  </AuthFrame>
  return <AuthFrame title="Set your password" description={`For ${link.data.email} on this server.`}>
    <AppForm form={form} className="space-y-4">
      <input type="email" autoComplete="username" value={link.data.email} readOnly hidden />
      <form.AppField name="password" validators={{ onChange: ({ value }) => passwordError(value) }}>
        {field => <field.TextField label="New password" type="password" autoComplete="new-password" autoFocus help="At least 8 characters." />}
      </form.AppField>
      <form.AppField name="confirm" validators={{ onChangeListenTo: ['password'], onChange: ({ value, fieldApi }) => !value ? 'Repeat the password.' : value !== fieldApi.form.getFieldValue('password') ? 'The passwords differ.' : undefined }}>
        {field => <field.TextField label="Repeat password" type="password" autoComplete="new-password" />}
      </form.AppField>
      <SubmitButton form={form} className="w-full">Set password and sign in</SubmitButton>
    </AppForm>
  </AuthFrame>
}
