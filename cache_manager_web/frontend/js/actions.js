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
    const number = ++urlsRequested;
    try {
        showUrls(number, taskId, await api.getUrls(taskId));
    } catch (err) {
        console.error('Failed to load URLs:', err);
    }
    // Load answers, unless another task was selected meanwhile
    try {
        const data = await api.getAnswers(taskId);
        if (getState().selectedTaskId === taskId) setState({ answers: data.files || [] });
    } catch {}
}

export async function selectUrl(taskId, url) {
    // Auto-switch from answer view to screenshot when selecting a URL
    const mode = getState().previewMode;
    const updates = { selectedUrl: url, currentText: null, currentIssues: null };
    if (mode === 'answer') updates.previewMode = 'screenshot';
    setState(updates);
    // Set capture target for the extension
    api.setCaptureTarget(taskId, url).catch(() => {});
    // Check if this is a PDF (no text content available)
    const s = getState();
    const urlData = s.urls.find(u => u.url === url);
    const isPdf = urlData?.content_type === 'pdf';

    if (urlData?.content_type === 'failed' || urlData?.content_type === 'pending') {
        // Nothing is stored: say why instead of fetching text
        setState({
            currentText: urlData.content_type === 'failed' ? failureText(urlData.failure) : PENDING_TEXT,
            currentIssues: { has_issues: true, severity: 'definite', keywords: urlData.issues || [], patterns: [] },
        });
        return;
    }

    if (isPdf) {
        setState({ currentText: '', currentIssues: { has_issues: false } });
        // Auto-mark unflagged PDF as reviewed when viewed
        if (urlData && !['ok', 'fixed', 'skip'].includes(urlData.reviewed)) {
            // Only auto-review if no definite issues (i.e., not flagged)
            if (urlData.severity !== 'definite') {
                api.setReview(taskId, url, 'ok').catch(() => {});
                const urls = s.urls.map(u => u.url === url ? { ...u, reviewed: 'ok' } : u);
                setState({ urls });
                if (urlData.issues?.length > 0) {
                    incrementTaskIssueFixedCount(taskId);
                    updateReviewProgress();
                }
            }
        }
        return;
    }

    // Load text content for web URLs
    try {
        const data = await api.getText(taskId, url);
        setState({ currentText: data.text, currentIssues: data.issues });

        // Auto-mark as reviewed when viewed:
        // - Clean URLs (no issues) — no progress impact
        // - Possible-issue URLs (yellow) — viewing confirms they're OK
        // - Definite-issue URLs (red) — require manual recapture/mark
        if (!data.issues?.has_issues || data.issues?.severity !== 'definite') {
            const fresh = getState();
            const ud = fresh.urls.find(u => u.url === url);
            if (ud && !['ok', 'fixed', 'skip'].includes(ud.reviewed)) {
                api.setReview(taskId, url, 'ok').catch(() => {});
                const urls = fresh.urls.map(u => u.url === url ? { ...u, reviewed: 'ok' } : u);
                setState({ urls });
                // Update issue progress for possible-issue URLs
                if (data.issues?.has_issues) {
                    incrementTaskIssueFixedCount(taskId);
                    updateReviewProgress();
                }
            }
        }
    } catch {
        setState({ currentText: null, currentIssues: null });
    }
}

const PENDING_TEXT = [
    'This URL is not captured yet: it was added in the Cache Manager, or its stored page was reset.',
    'Until it is captured, evaluation treats it as not cached and captures it live.',
    '',
    'Open it in your browser and capture it with the extension, or upload a PDF or MHTML file.',
].join('\n');

function failureText(failure) {
    const f = failure || {};
    const lines = [`Capturing this URL failed: ${f.reason || 'unknown reason'}.`];
    if (f.blocked) lines.push('The site refused the automated browser; it may load in your own browser.');
    if (f.attempts) lines.push(`Attempts: ${f.attempts}${f.time ? ` (latest ${f.time})` : ''}.`);
    lines.push('', 'Open it in your browser and capture it with the extension, or upload a PDF or MHTML file.');
    return lines.join('\n');
}

// ---- Reload current task ----

// Captures can trigger reloads faster than they complete, and another task can be
// selected while one is under way.  Each request for a task's URL list is numbered,
// and its answer is shown only if no answer to a later request has been shown and
// its task is still selected; so an earlier answer never replaces a later one, and
// when the latest request fails, the latest answer received stays.
let urlsRequested = 0;
let urlsShown = 0;

function showUrls(number, taskId, data) {
    if (number < urlsShown || getState().selectedTaskId !== taskId) return false;
    urlsShown = number;
    setState({
        urls: data.urls || [],
        urlTotal: data.total || 0,
        urlReviewedCount: data.reviewed_count || 0,
    });
    return true;
}

export async function reloadCurrentTask() {
    const number = ++urlsRequested;
    const s = getState();
    refreshIssues();
    if (!s.selectedTaskId) return;
    try {
        const data = await api.getUrls(s.selectedTaskId);
        if (!showUrls(number, s.selectedTaskId, data)) return;
        // Re-select current URL if still exists
        if (s.selectedUrl && getState().selectedUrl === s.selectedUrl && data.urls?.some(u => u.url === s.selectedUrl)) {
            selectUrl(s.selectedTaskId, s.selectedUrl);
        }
    } catch {}
}

// ---- Issue index and task list, after an edit or capture changed them ----

// Numbered like the requests for a task's URL list: an earlier answer never replaces a later one.
let issuesRequested = 0;
let issuesShown = 0;

export async function refreshIssues() {
    const number = ++issuesRequested;
    try {
        const [issues, taskData] = await Promise.all([api.getIssues(), api.getTasks()]);
        if (number < issuesShown) return;
        issuesShown = number;
        const issueIndex = issues.issue_index || [];
        setState({
            issueIndex,
            taskIssues: issues.task_issues || {},
            tasks: taskData.tasks || [],
            issueCursor: Math.min(getState().issueCursor, issueIndex.length - 1),
        });
    } catch {}
}

// ---- Review progress ----

export async function updateReviewProgress() {
    try {
        const data = await api.getReviewProgress();
        const el = document.querySelector('#review-progress');
        if (el) {
            el.textContent = data.total > 0
                ? `Fixed: ${data.reviewed}/${data.total} issues`
                : '';
        }
    } catch {}
}

// ---- Task issue fixed count ----

export function incrementTaskIssueFixedCount(taskId) {
    const s = getState();
    const tasks = s.tasks.map(t =>
        t.task_id === taskId ? { ...t, issue_reviewed_count: (t.issue_reviewed_count || 0) + 1 } : t
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

// ---- URL filtering (shared between url-list.js and main.js) ----

export function filterUrls(s) {
    let urls = s.urls;
    if (s.urlSearch) {
        const q = s.urlSearch.toLowerCase();
        urls = urls.filter(u => u.url.toLowerCase().includes(q) || u.domain.toLowerCase().includes(q));
    }
    if (s.urlContentFilter !== 'all') {
        urls = urls.filter(u => u.content_type === s.urlContentFilter);
    }
    if (s.urlIssuesFilter) {
        urls = urls.filter(u => u.issues?.length > 0);
    }
    if (s.urlTodoFilter) {
        urls = urls.filter(u => !['ok', 'fixed', 'skip'].includes(u.reviewed));
    }
    return urls;
}

// ---- DOM helper ----

export function $(sel) { return document.querySelector(sel); }
