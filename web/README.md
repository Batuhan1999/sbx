# Web app

The browser interface of sbx-control: a Vite and React single-page app.

```sh
npm ci
npm test
npm run build    # writes control/sbx_control/web_dist (not committed; releases carry it)
npm run dev      # proxies the API to a control service on port 8080
```

## Forms

Every form that takes user input uses TanStack Form through
`src/components/form.tsx`:

- Create the form with `useAppForm({ defaultValues, onSubmit })` and render it
  in `AppForm`.
- Add inputs with `form.AppField` and the control that fits the value:
  `TextField` (with `type` email, url or password), `TextareaField` for
  several lines, `NumberField` (min, max, step, unit), `SelectField` or
  `RadioField` for a known set, `SwitchField` or `CheckboxField` for a boolean,
  `ToggleField` for a "true"/"false" text. A field the server describes
  (connection fields, package inputs, runtime settings) takes its control from
  the server's metadata (`kind`/`type`, `options`, `min`/`max`), not a guess.
- Show only what a person sets. Rarely changed fields go under `MoreOptions`;
  pinned, internal or operator-only ones stay out of the form (the server
  marks them `hidden`) and are set through the API and CLI. A saved secret's
  empty field says `SECRET_KEPT`. Validation, required fields included, lives
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
