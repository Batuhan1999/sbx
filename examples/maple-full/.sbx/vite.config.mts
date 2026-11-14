import sourceConfig from '/home/user/maple/apps/web/vite.config.ts';
import { preferredPort, PortRange } from '/home/user/maple/lib/alchemy-portless/src/ports.ts';

export default async (environment) => {
  const base = await sourceConfig(environment);
  const proxy = (port, prefix) => ({
    target: `http://127.0.0.1:${port}`, changeOrigin: true,
    rewrite: (path) => path.replace(new RegExp(`^${prefix}`), '') || '/',
  });
  return {
    ...base,
    define: {
      ...base.define,
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/_api'),
      'import.meta.env.VITE_API_PUBLIC_URL': JSON.stringify('/_api'),
      'import.meta.env.VITE_ELECTRIC_SYNC_URL': JSON.stringify('/_sync'),
      'import.meta.env.VITE_INGEST_URL': JSON.stringify('/_ingest'),
      'import.meta.env.VITE_MAPLE_SELF_INGEST_URL': JSON.stringify('/_ingest'),
      'import.meta.env.VITE_MAPLE_REPLAY': JSON.stringify('off'),
      'import.meta.env.VITE_MAPLE_CAPTURE': JSON.stringify('off'),
    },
    server: {
      ...base.server, host: '0.0.0.0', port: 3471, strictPort: true,
      allowedHosts: ['.e2b.sandboxes.internal', 'localhost', '127.0.0.1'],
      proxy: {
        '/_api': proxy(preferredPort('api', PortRange.worker), '/_api'),
        '/_sync': proxy(preferredPort('electric-sync', PortRange.worker), '/_sync'),
        '/_ingest': proxy(3474, '/_ingest'),
      },
    },
  };
};
