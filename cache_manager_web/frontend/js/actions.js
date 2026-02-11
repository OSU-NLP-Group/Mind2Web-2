/**
 * Shared actions module — breaks circular dependency between main.js and components.
 *
 * Components import actions from here instead of from main.js.
 */
import { getState, setState } from './store.js';
import * as api from './api.js';

// ---- Task & URL selection ----

export async function selectTask(taskId) {
    setState({ selectedTaskId: taskId, selectedUrl: null, urls: [], currentText: null, currentIssues: null, answers: [] });
    try {
        const data = await api.getUrls(taskId);
        setState({
            urls: data.urls || [],
            urlTotal: data.total || 0,
            urlReviewedCount: data.reviewed_count || 0,
        });
    } catch (err) {
        console.error('Failed to load URLs:', err);
    }
    // Load answers
    try {
        const data = await api.getAnswers(taskId);
        setState({ answers: data.files || [] });
    } catch {}
}

export async function selectUrl(taskId, url) {
    setState({ selectedUrl: url, currentText: null, currentIssues: null });
    // Set capture target for the extension
    api.setCaptureTarget(taskId, url).catch(() => {});
    // Check if this is a PDF (no text content available)
    const s = getState();
    const urlData = s.urls.find(u => u.url === url);
    const isPdf = urlData?.content_type === 'pdf';

    if (isPdf) {
        setState({ currentText: '', currentIssues: { has_issues: false } });
        // Auto-mark PDF as reviewed when viewed
        if (urlData && !['ok', 'fixed', 'skip'].includes(urlData.reviewed)) {
            api.setReview(taskId, url, 'ok').catch(() => {});
            const urls = s.urls.map(u => u.url === url ? { ...u, reviewed: 'ok' } : u);
            setState({ urls, urlReviewedCount: (s.urlReviewedCount || 0) + 1 });
            incrementTaskReviewedCount(taskId);
            updateReviewProgress();
        }
        return;
    }

    // Load text content for web URLs
    try {
        const data = await api.getText(taskId, url);
        setState({ currentText: data.text, currentIssues: data.issues });

        // Auto-mark clean URLs as reviewed when viewed
        if (!data.issues?.has_issues) {
            const fresh = getState();
            const ud = fresh.urls.find(u => u.url === url);
            if (ud && !['ok', 'fixed', 'skip'].includes(ud.reviewed)) {
                api.setReview(taskId, url, 'ok').catch(() => {});
                const urls = fresh.urls.map(u => u.url === url ? { ...u, reviewed: 'ok' } : u);
                setState({ urls, urlReviewedCount: (fresh.urlReviewedCount || 0) + 1 });
                incrementTaskReviewedCount(taskId);
                updateReviewProgress();
            }
        }
    } catch {
        setState({ currentText: null, currentIssues: null });
    }
}

// ---- Reload current task ----

export async function reloadCurrentTask() {
    const s = getState();
    if (!s.selectedTaskId) return;
    try {
        const data = await api.getUrls(s.selectedTaskId);
        setState({
            urls: data.urls || [],
            urlTotal: data.total || 0,
            urlReviewedCount: data.reviewed_count || 0,
        });
        // Re-select current URL if still exists
        if (s.selectedUrl && data.urls?.some(u => u.url === s.selectedUrl)) {
            selectUrl(s.selectedTaskId, s.selectedUrl);
        }
    } catch {}
}

// ---- Review progress ----

export async function updateReviewProgress() {
    try {
        const data = await api.getReviewProgress();
        const el = document.querySelector('#review-progress');
        if (el) {
            el.textContent = data.total > 0
                ? `Reviewed: ${data.reviewed}/${data.total}`
                : '';
        }
    } catch {}
}

// ---- Task reviewed count ----

export function incrementTaskReviewedCount(taskId) {
    const s = getState();
    const tasks = s.tasks.map(t =>
        t.task_id === taskId ? { ...t, reviewed_count: (t.reviewed_count || 0) + 1 } : t
    );
    setState({ tasks });
}

// ---- Toast & Status ----

export function showStatus(msg, cls = '') {
    const el = document.querySelector('#preview-status');
    if (el) {
        el.textContent = msg;
        el.className = 'status-text' + (cls ? ' ' + cls : '');
    }
}

let _toastTimer = null;
export function toast(msg, type = '') {
    const el = document.querySelector('#toast');
    if (!el) return;
    el.textContent = msg;
    el.className = 'toast visible' + (type ? ' ' + type : '');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { el.className = 'toast'; }, 3000);
}

// ---- DOM helper ----

export function $(sel) { return document.querySelector(sel); }
