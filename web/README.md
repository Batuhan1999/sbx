# Web app

The browser interface of sbx-control: a Vite and React single-page app.

```sh
npm ci
npm test
npm run build    # writes control/sbx_control/web_dist, which is committed
npm run dev      # proxies the API to a control service on port 8080
```

## Forms

Every form that takes user input uses TanStack Form through
`src/components/form.tsx`:

- Create the form with `useAppForm({ defaultValues, onSubmit })` and render it
  in `AppForm`.
- Add inputs with `form.AppField` and its `TextField`, `TextareaField`,
  `SelectField` or `CheckboxField`. Validation, required fields included, lives
  in the field's validators; use `onChangeListenTo` when one field's validity
  depends on another.
- Errors show after blur or a submit attempt and clear while the value is being
  corrected. `AppForm` focuses the first invalid field and shows request
  failures.
- Submit with `SubmitButton`, or pass a mutation's pending state to `Button`.
  Other buttons default to `type="button"`.
- Passwords and secrets stay in form memory only: clear them after saving and
  never put them in saved drafts.

View filters, navigation controls and the GitHub App manifest hand-off (a
browser POST, not user input) are not forms.
