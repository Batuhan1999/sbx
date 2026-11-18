// The one-time "Your server is ready" dialog: the setup marks it, the control plane shows it once.
const READY_FLAG = 'sbx-ready'

export function markReady() { try { sessionStorage.setItem(READY_FLAG, '1') } catch { /* shown or not, nothing breaks */ } }
export function isMarkedReady() { try { return sessionStorage.getItem(READY_FLAG) === '1' } catch { return false } }
export function clearReady() { try { sessionStorage.removeItem(READY_FLAG) } catch { /* nothing to clear */ } }
