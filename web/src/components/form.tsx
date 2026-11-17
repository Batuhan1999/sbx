// Every form that takes input uses TanStack Form through this module.
import { createFormHook, createFormHookContexts, useStore, type AnyFormApi } from '@tanstack/react-form'
import { Eye, EyeOff } from 'lucide-react'
import { Children, Fragment, isValidElement, useId, useState, type ComponentProps, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { ErrorState } from './states'
import { Button, type ButtonProps } from './ui/button'
import { Checkbox } from './ui/checkbox'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select'
import { Switch } from './ui/switch'

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

function Control({ label, help, className, optional, kind = 'input', rows, ...props }: ControlProps & { kind?: 'input' | 'textarea' }) {
  const field = useFieldContext<string>()
  const id = useId()
  const [visible, setVisible] = useState(false)
  const error = useFieldErrors()
  const common = {
    id,
    name: field.name,
    value: field.state.value ?? '',
    onBlur: field.handleBlur,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => field.handleChange(e.target.value),
    'aria-invalid': !!error,
    'aria-describedby': error || help ? `${id}-hint` : undefined,
    className: cn(kind === 'input' && props.type === 'password' && 'pr-9', className),
  }
  const password = kind === 'input' && props.type === 'password'
  return <div className="min-w-0">
    <label htmlFor={id} className="mb-1 flex items-baseline gap-1.5 text-[12.5px] font-medium">{label}{optional && <span className="font-normal text-faint">optional</span>}</label>
    {kind === 'textarea' ? <textarea {...common} rows={rows ?? 6} placeholder={props.placeholder} disabled={props.disabled} spellCheck={false} />
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
function TextareaField(props: ControlProps) { return <Control {...props} kind="textarea" /> }

// Radix Select keeps '' for "nothing chosen"; an option whose value is '' (a default) goes through this instead.
const EMPTY = '__empty__'
type Option = { value: string; label: ReactNode; disabled?: boolean }

/** The <option> elements a form passes, read into Select items (fragments and arrays flattened). */
function options(children: ReactNode): Option[] {
  return Children.toArray(children).flatMap(child => {
    if (!isValidElement<{ value?: string | number; children?: ReactNode; disabled?: boolean }>(child)) return []
    if (child.type === Fragment) return options(child.props.children)
    const value = child.props.value ?? String(child.props.children ?? '')
    return [{ value: String(value), label: child.props.children, disabled: child.props.disabled }]
  })
}

/** shadcn's Select for a text value; its choices are <option> children, as with a native select. */
function SelectField({ label, help, className, children, optional, disabled }: ControlProps) {
  const field = useFieldContext<string>()
  const id = useId()
  const error = useFieldErrors()
  const items = options(children)
  return <div className="min-w-0">
    <label htmlFor={id} className="mb-1 flex items-baseline gap-1.5 text-[12.5px] font-medium">{label}{optional && <span className="font-normal text-faint">optional</span>}</label>
    <Select name={field.name} value={field.state.value === '' ? EMPTY : field.state.value ?? EMPTY} disabled={disabled}
      onValueChange={v => { field.handleChange(v === EMPTY ? '' : v); field.handleBlur() }}>
      <SelectTrigger id={id} className={className} aria-invalid={!!error} aria-describedby={error || help ? `${id}-hint` : undefined}><SelectValue /></SelectTrigger>
      <SelectContent>{items.map(o => <SelectItem key={o.value} value={o.value === '' ? EMPTY : o.value} disabled={o.disabled}>{o.label}</SelectItem>)}</SelectContent>
    </Select>
    {(error || help) && <p id={`${id}-hint`} aria-live="polite" className={cn('mt-1 text-[12px]', error ? 'text-bad' : 'text-muted')}>{error || help}</p>}
  </div>
}

/** A label beside a control, with help under it. */
function Choice({ id, label, help, children }: { id: string; label: ReactNode; help?: ReactNode; children: ReactNode }) {
  return <div className="flex items-start gap-2.5">
    <span className="mt-0.5">{children}</span>
    <label htmlFor={id} className="cursor-pointer text-[13px] leading-5">{label}{help && <span id={`${id}-hint`} className="block text-[12px] text-muted">{help}</span>}</label>
  </div>
}

function CheckboxField({ label, help }: { label: ReactNode; help?: ReactNode }) {
  const field = useFieldContext<boolean>()
  const id = useId()
  return <Choice id={id} label={label} help={help}>
    <Checkbox id={id} name={field.name} checked={!!field.state.value} onBlur={field.handleBlur}
      onCheckedChange={v => field.handleChange(v === true)} aria-describedby={help ? `${id}-hint` : undefined} />
  </Choice>
}

/** A switch for a text value that is "true" or "false" (a package's yes/no input). */
function ToggleField({ label, help }: { label: ReactNode; help?: ReactNode }) {
  const field = useFieldContext<string>()
  const id = useId()
  return <div className="flex items-start justify-between gap-4">
    <label htmlFor={id} className="cursor-pointer text-[13px] font-medium leading-5">{label}{help && <span id={`${id}-hint`} className="block text-[12px] font-normal text-muted">{help}</span>}</label>
    <Switch id={id} name={field.name} checked={field.state.value === 'true'} onBlur={field.handleBlur}
      onCheckedChange={v => field.handleChange(v ? 'true' : 'false')} aria-describedby={help ? `${id}-hint` : undefined} />
  </div>
}

/** The field components a form.AppField render prop gets, for helpers that render one. */
export type FieldControls = { TextField: typeof TextField; SelectField: typeof SelectField; ToggleField: typeof ToggleField }

export const { useAppForm } = createFormHook({ fieldContext, formContext, fieldComponents: { TextField, SelectField, TextareaField, CheckboxField, ToggleField }, formComponents: {} })
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
