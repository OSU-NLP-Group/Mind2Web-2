/**
 * The Cache Manager backend URL, shared by the service worker (importScripts) and the popup (<script>).
 *
 * It is kept in chrome.storage.local under "backend", and it defaults to
 * the address `run.py` serves without --host or --port.
 */

const DEFAULT_BACKEND = 'http://127.0.0.1:8000';
const LOOPBACK_HOSTS = ['127.0.0.1', 'localhost', '[::1]'];

/** The backend URL, without a trailing slash. */
async function getBackend() {
    const { backend } = await chrome.storage.local.get('backend');
    return (backend || DEFAULT_BACKEND).replace(/\/+$/, '');
}

/**
 * Store the backend URL; an empty value restores the default.
 * Throws an Error with a message for the user unless the value is an http(s) URL.
 */
async function setBackend(value) {
    const trimmed = (value || '').trim();
    if (!trimmed) {
        await chrome.storage.local.remove('backend');
        return;
    }
    let url;
    try {
        url = new URL(trimmed);
    } catch {
        throw new Error(`Not a URL: ${trimmed}`);
    }
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error(`Not an http(s) URL: ${trimmed}`);
    await chrome.storage.local.set({ backend: url.origin });
}

/** Whether `url` is a page of the Cache Manager at `backend`; the loopback host names count as one host. */
function isCacheManagerUrl(url, backend) {
    try {
        const page = new URL(url);
        const server = new URL(backend);
        const host = (u) => (LOOPBACK_HOSTS.includes(u.hostname) ? 'loopback' : u.hostname);
        return page.protocol === server.protocol && page.port === server.port && host(page) === host(server);
    } catch {
        return false;
    }
}
