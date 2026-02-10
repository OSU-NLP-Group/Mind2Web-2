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
    // Load text content
    try {
        const data = await api.getText(taskId, url);
        setState({ currentText: data.text, currentIssues: data.issues });
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
