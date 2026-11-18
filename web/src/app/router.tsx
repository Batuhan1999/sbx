import { createBrowserRouter, Navigate } from 'react-router'
import { Loading } from '@/components/states'
import { Shell } from './Shell'

const page = (load: () => Promise<{ default: React.ComponentType }>) => async () => ({ Component: (await load()).default })

export const routes = [
  { path: '/app/setup', HydrateFallback: Loading, lazy: page(() => import('@/pages/setup/SetupPage')) },
  { path: '/login', HydrateFallback: Loading, lazy: page(() => import('@/pages/SignInPage')) },
  { path: '/app/keys/:id', HydrateFallback: Loading, lazy: page(() => import('@/pages/KeysPage')) },
  { path: '/app/set-password', HydrateFallback: Loading, lazy: page(() => import('@/pages/SetPasswordPage')) },
  {
    path: '/app',
    element: <Shell />,
    HydrateFallback: Loading,
    children: [
      { index: true, element: <Navigate to="/app/repos" replace /> },
      { path: 'repos', lazy: page(() => import('@/pages/repos/ReposPage')) },
      { path: 'repos/new', lazy: page(() => import('@/pages/repos/ConnectRepoPage')) },
      {
        path: 'repos/:owner/:name',
        lazy: page(() => import('@/pages/repos/RepoLayout')),
        children: [
          { index: true, lazy: page(() => import('@/pages/repos/RepoSetup')) },
          { path: 'sandboxes', lazy: page(() => import('@/pages/repos/RepoSandboxes')) },
          { path: 'env', lazy: page(() => import('@/pages/repos/RepoEnv')) },
          { path: 'packages', lazy: page(() => import('@/pages/repos/RepoPackages')) },
          { path: 'recipe', lazy: page(() => import('@/pages/repos/RepoRecipe')) },
          { path: '*', element: <Navigate to=".." replace /> },
        ],
      },
      { path: 'sandboxes', lazy: page(() => import('@/pages/sandboxes/SandboxesPage')) },
      { path: 'sandboxes/new', lazy: page(() => import('@/pages/sandboxes/NewSandboxPage')) },
      { path: 'sandboxes/:name', lazy: page(() => import('@/pages/sandboxes/SandboxPage')) },
      { path: 'extensions', lazy: page(() => import('@/pages/extensions/ExtensionsPage')) },
      { path: 'settings', element: <Navigate to="/app/settings/general" replace /> },
      { path: 'settings/:section', lazy: page(() => import('@/pages/settings/SettingsPage')) },
      { path: 'cli', lazy: page(() => import('@/pages/CliPage')) },
      { path: '*', element: <Navigate to="/app/repos" replace /> },
    ],
  },
  { path: '*', element: <Navigate to="/app/repos" replace /> },
]

export const router = createBrowserRouter(routes)
