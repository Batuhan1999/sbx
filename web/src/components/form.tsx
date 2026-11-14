// Every form that takes input uses TanStack Form through this module.
import { createFormHook, createFormHookContexts, useStore, type AnyFormApi } from '@tanstack/react-form'
import { Eye, EyeOff } from 'lucide-react'
import { useId, useState, type ComponentProps, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { ErrorState } from './states'
import { Button, type ButtonProps } from './ui/button'

const { fieldContext, formContext, useFieldContext } = createFormHookContexts()

type ControlProps = {
  label: ReactNode
  help?: ReactNode
  className?: string
  children?: ReactNode
  optional?: boolean
} & Omit<ComponentProps<'input'>, 'value' | 'onChange' | 'onBlur' | 'name' | 'children'> & { rows?: number }

function useFieldErrors() {
  const field = useFieldContext<unknown>()
  const attempted = useStore(field.form.store, s => s.submissionAttempts > 0)
  const shown = field.state.meta.isBlurred || attempted
  const errors = shown ? field.state.meta.errors.filter(Boolean).map(String) : []
  return [...new Set(errors)].join(' ')
}

function Control({ label, help, className, children, optional, kind = 'input', rows, ...props }: ControlProps & { kind?: 'input' | 'select' | 'textarea' }) {
  const field = useFieldContext<string>()
  const id = useId()
  const [visible, setVisible] = useState(false)
  const error = useFieldErrors()
  const common = {
    id,
    name: field.name,
    value: field.state.value ?? '',
    onBlur: field.handleBlur,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => field.handleChange(e.target.value),
    'aria-invalid': !!error,
    'aria-describedby': error || help ? `${id}-hint` : undefined,
    className: cn(kind === 'input' && props.type === 'password' && 'pr-9', className),
  }
  const password = kind === 'input' && props.type === 'password'
  return <div className="min-w-0">
    <label htmlFor={id} className="mb-1 flex items-baseline gap-1.5 text-[12.5px] font-medium">{label}{optional && <span className="font-normal text-faint">optional</span>}</label>
    {kind === 'select' ? <select {...common} disabled={props.disabled}>{children}</select>
      : kind === 'textarea' ? <textarea {...common} rows={rows ?? 6} placeholder={props.placeholder} disabled={props.disabled} spellCheck={false} />
      : <div className="relative">
        <input {...props} {...common} type={password && visible ? 'text' : props.type} />
        {password && <button type="button" className="absolute right-1 top-1/2 grid size-7 -translate-y-1/2 place-items-center rounded text-muted hover:text-ink"
          aria-label={visible ? 'Hide value' : 'Show value'} aria-pressed={visible} onClick={() => setVisible(v => !v)}>
          {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </button>}
      </div>}
    {(error || help) && <p id={`${id}-hint`} aria-live="polite" className={cn('mt-1 text-[12px]', error ? 'text-bad' : 'text-muted')}>{error || help}</p>}
  </div>
}

function TextField(props: ControlProps) { return <Control {...props} /> }
function SelectField(props: ControlProps) { return <Control {...props} kind="select" /> }
function TextareaField(props: ControlProps) { return <Control {...props} kind="textarea" /> }
function CheckboxField({ label, help }: { label: ReactNode; help?: ReactNode }) {
  const field = useFieldContext<boolean>()
  const id = useId()
  return <div className="flex items-start gap-2.5">
    <input id={id} type="checkbox" className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]" name={field.name} checked={!!field.state.value}
      onBlur={field.handleBlur} onChange={e => field.handleChange(e.target.checked)} aria-describedby={help ? `${id}-hint` : undefined} />
    <label htmlFor={id} className="text-[13px]">{label}{help && <span id={`${id}-hint`} className="block text-[12px] text-muted">{help}</span>}</label>
  </div>
}

export const { useAppForm } = createFormHook({ fieldContext, formContext, fieldComponents: { TextField, SelectField, TextareaField, CheckboxField }, formComponents: {} })
export { useStore }

export function AppForm({ form, children, ...props }: Omit<ComponentProps<'form'>, 'onSubmit'> & { form: AnyFormApi }) {
  const [error, setError] = useState<unknown>(null)
  const busy = useStore(form.store, s => s.isSubmitting)
  return <form {...props} noValidate aria-busy={busy} onSubmit={async e => {
    e.preventDefault()
    e.stopPropagation()
    setError(null)
    const element = e.currentTarget
    try { await form.handleSubmit() } catch (x) { setError(x) }
    requestAnimationFrame(() => element.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus())
  }}>
    {children}
    {!!error && <ErrorState error={error} compact className="mt-3" />}
  </form>
}

export function SubmitButton({ form, busy, ...props }: Omit<ButtonProps, 'form'> & { form: AnyFormApi }) {
  const submitting = useStore(form.store, s => s.isSubmitting)
  return <Button variant="primary" {...props} type="submit" busy={busy || submitting} />
}
