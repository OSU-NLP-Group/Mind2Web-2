/**
 * Main entry point for Cache Manager Web UI.
 */
import { getState, setState, subscribe } from './store.js';
import * as api from './api.js';
import { selectTask, selectUrl, reloadCurrentTask, updateReviewProgress, showStatus, toast, $ } from './actions.js';
import { initTaskPanel } from './components/task-panel.js';
import { initUrlList } from './components/url-list.js';
import { initPreview } from './components/preview.js';

// ============================================================
// Initialization
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    initTaskPanel();
    initUrlList();
    initPreview();
    initToolbar();
    initKeyboardShortcuts();
    initSSE();
    initDragDrop();

    // Check if cache was auto-loaded
    api.getStatus().then(async status => {
        if (status.loaded) {
            const result = await api.loadCache(status.agent_path);
            refreshAfterLoad(result);
        }
    }).catch(() => {});
});

// ============================================================
// Toolbar
// ============================================================

function initToolbar() {
    $('#btn-open').addEventListener('click', onOpenFolder);
    $('#btn-refresh').addEventListener('click', onRefresh);
    $('#btn-prev-issue').addEventListener('click', () => navigateIssue(-1));
    $('#btn-next-issue').addEventListener('click', () => navigateIssue(1));
    $('#btn-mark-reviewed').addEventListener('click', onMarkReviewed);
    $('#btn-open-browser').addEventListener('click', onOpenInBrowser);
    $('#btn-delete-url').addEventListener('click', onDeleteUrl);
    $('#btn-upload-mhtml').addEventListener('click', onUploadMhtml);
    $('#btn-recapture').addEventListener('click', onRecapture);

    // MHTML file picker callback
    $('#mhtml-picker').addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const s = getState();
        if (!s.selectedTaskId || !s.selectedUrl) return;
        try {
            showStatus('Uploading MHTML...', 'warning');
            await api.uploadMhtml(s.selectedTaskId, s.selectedUrl, file);
            toast('MHTML uploaded successfully', 'success');
            await reloadCurrentTask();
        } catch (err) {
            toast('MHTML upload failed: ' + err.message, 'error');
        }
        e.target.value = '';
    });

    subscribe((s) => {
        $('#btn-refresh').disabled = !s.loaded;
        const hasIssues = s.issueIndex.length > 0;
        $('#btn-prev-issue').disabled = !hasIssues;
        $('#btn-next-issue').disabled = !hasIssues;
        if (hasIssues && s.issueCursor >= 0) {
            $('#issue-counter').textContent = `Issue ${s.issueCursor + 1}/${s.issueIndex.length}`;
        } else {
            $('#issue-counter').textContent = hasIssues ? `${s.issueIndex.length} issues` : '';
        }
        $('#agent-info').textContent = s.loaded
            ? `${s.agentName} | ${s.stats.total_tasks || 0} tasks | ${s.stats.total_urls || 0} URLs`
            : 'No cache loaded';
        const hasUrl = !!(s.selectedTaskId && s.selectedUrl);
        $('#btn-mark-reviewed').disabled = !hasUrl;
        $('#btn-open-browser').disabled = !hasUrl;
        $('#btn-delete-url').disabled = !hasUrl;
        $('#btn-upload-mhtml').disabled = !hasUrl;
        $('#btn-recapture').disabled = !hasUrl;
    }, ['loaded', 'issueIndex', 'issueCursor', 'agentName', 'stats',
        'selectedTaskId', 'selectedUrl']);
}

// ============================================================
// Actions
// ============================================================

async function onOpenFolder() {
    const path = prompt('Enter the path to an agent cache folder:\n\nExample: /Users/you/data/JudyAgent');
    if (!path) return;
    try {
        showStatus('Loading cache...', 'warning');
        const result = await api.loadCache(path.trim());
        refreshAfterLoad(result);
        toast(`Loaded ${result.loaded_tasks}/${result.total_tasks} tasks`, 'success');
    } catch (err) {
        toast('Failed to load: ' + err.message, 'error');
        showStatus('Load failed', 'error');
    }
}

async function onRefresh() {
    const s = getState();
    if (!s.agentPath) return;
    try {
        showStatus('Refreshing & scanning...', 'warning');
        setState({ contentVersion: s.contentVersion + 1 });
        const result = await api.loadCache(s.agentPath);
        refreshAfterLoad(result);
        toast('Refreshed', 'success');
    } catch (err) {
        toast('Refresh failed: ' + err.message, 'error');
        showStatus('Refresh failed', 'error');
    }
}

async function refreshAfterLoad(result) {
    setState({
        loaded: true,
        agentName: result.agent_name || '',
        agentPath: result.agent_path || '',
        stats: result.stats || {},
        taskIssues: result.task_issues || {},
        issueIndex: result.issue_index || [],
        issueCursor: -1,
    });
    // Fetch task list
    try {
        const data = await api.getTasks();
        setState({ tasks: data.tasks || [] });
    } catch (err) {
        console.error('Failed to load tasks:', err);
    }
    await updateReviewProgress();
    showStatus('Ready');
}

async function onMarkReviewed() {
    const s = getState();
    if (!s.selectedTaskId || !s.selectedUrl) return;
    try {
        await api.setReview(s.selectedTaskId, s.selectedUrl, 'ok');
        // Update local state
        const urls = s.urls.map(u => u.url === s.selectedUrl ? { ...u, reviewed: 'ok' } : u);
        setState({ urls });
        await updateReviewProgress();
        toast('Marked as reviewed');
    } catch (err) {
        toast('Failed: ' + err.message, 'error');
    }
}

async function onOpenInBrowser() {
    const s = getState();
    if (!s.selectedUrl) return;
    await api.setCaptureTarget(s.selectedTaskId, s.selectedUrl).catch(() => {});
    window.open(s.selectedUrl, '_blank');
    toast('Opened in browser. Use the extension to capture.');
}

async function onDeleteUrl() {
    const s = getState();
    if (!s.selectedTaskId || !s.selectedUrl) return;
    if (!confirm(`Delete ${s.selectedUrl}?`)) return;
    try {
        await api.deleteUrl(s.selectedTaskId, s.selectedUrl);
        setState({ selectedUrl: null, currentText: null, currentIssues: null });
        await reloadCurrentTask();
        toast('URL deleted');
    } catch (err) {
        toast('Delete failed: ' + err.message, 'error');
    }
}

function onUploadMhtml() {
    $('#mhtml-picker').click();
}

async function onRecapture() {
    const s = getState();
    if (!s.selectedUrl) return;
    await api.setCaptureTarget(s.selectedTaskId, s.selectedUrl).catch(() => {});
    window.open(s.selectedUrl, '_blank');
    toast('Page opened. Pass any verification, then use the extension to capture.', 'success');
}

// ============================================================
// Issue Navigation
// ============================================================

function navigateIssue(direction) {
    const s = getState();
    if (!s.issueIndex.length) return;
    let cursor = s.issueCursor + direction;
    if (cursor < 0) cursor = s.issueIndex.length - 1;
    if (cursor >= s.issueIndex.length) cursor = 0;
    setState({ issueCursor: cursor });

    const entry = s.issueIndex[cursor];
    if (entry.task_id !== s.selectedTaskId) {
        selectTask(entry.task_id).then(() => {
            selectUrl(entry.task_id, entry.url);
        });
    } else {
        selectUrl(entry.task_id, entry.url);
    }
}

// ============================================================
// Keyboard Shortcuts
// ============================================================

function initKeyboardShortcuts() {
    document.addEventListener('keydown', (e) => {
        const tag = e.target.tagName;
        const inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

        // Ctrl/Cmd shortcuts work even in inputs
        if (e.ctrlKey || e.metaKey) {
            if (e.key === 'Enter') { e.preventDefault(); onMarkReviewed(); return; }
            if (e.key === 'u' || e.key === 'U') { e.preventDefault(); onRecapture(); return; }
            if (e.key === 'o' || e.key === 'O') { e.preventDefault(); onOpenFolder(); return; }
        }

        // Don't handle single-key shortcuts when typing in inputs
        if (inInput) return;

        if (e.key === 'n') { e.preventDefault(); navigateIssue(1); return; }
        if (e.key === 'N') { e.preventDefault(); navigateIssue(-1); return; }
        if (e.key === '1') { setState({ previewMode: 'screenshot' }); return; }
        if (e.key === '2') { setState({ previewMode: 'text' }); return; }
        if (e.key === '3') { setState({ previewMode: 'answer' }); return; }
        if (e.key === ' ') {
            e.preventDefault();
            const s = getState();
            setState({ previewMode: s.previewMode === 'text' ? 'screenshot' : 'text' });
            return;
        }
        // Arrow key navigation in URL list
        if (e.key === 'ArrowDown' || e.key === 'j') {
            e.preventDefault();
            navigateUrlList(1);
            return;
        }
        if (e.key === 'ArrowUp' || e.key === 'k') {
            e.preventDefault();
            navigateUrlList(-1);
            return;
        }
    });
}

function navigateUrlList(direction) {
    const s = getState();
    if (!s.selectedTaskId) return;

    // If no URLs in current task or navigating past boundaries, jump to adjacent task
    if (!s.urls.length) {
        navigateToAdjacentTask(direction);
        return;
    }

    const currentIdx = s.urls.findIndex(u => u.url === s.selectedUrl);
    let nextIdx = currentIdx + direction;

    if (nextIdx < 0 || nextIdx >= s.urls.length) {
        // Past the boundary — jump to next/previous task
        navigateToAdjacentTask(direction);
        return;
    }

    selectUrl(s.selectedTaskId, s.urls[nextIdx].url);
}

function navigateToAdjacentTask(direction) {
    const s = getState();
    if (!s.tasks.length) return;
    const taskIdx = s.tasks.findIndex(t => t.task_id === s.selectedTaskId);
    if (taskIdx < 0) return;

    let nextTaskIdx = taskIdx + direction;
    if (nextTaskIdx < 0) nextTaskIdx = s.tasks.length - 1;
    if (nextTaskIdx >= s.tasks.length) nextTaskIdx = 0;

    const nextTask = s.tasks[nextTaskIdx];
    selectTask(nextTask.task_id).then(() => {
        const urls = getState().urls;
        if (urls.length > 0) {
            // Down → select first URL; Up → select last URL
            const url = direction > 0 ? urls[0].url : urls[urls.length - 1].url;
            selectUrl(nextTask.task_id, url);
        }
    });
}

// ============================================================
// SSE (real-time updates from extension captures)
// ============================================================

function initSSE() {
    api.subscribeEvents((data) => {
        if (data.type === 'capture_complete') {
            toast(`Captured: ${data.url?.substring(0, 60)}...`, 'success');
            // Bump contentVersion to bust browser cache for screenshots
            setState({ contentVersion: getState().contentVersion + 1 });
            reloadCurrentTask();
            updateReviewProgress();
        }
    });
}

// ============================================================
// Drag & Drop MHTML
// ============================================================

function initDragDrop() {
    const preview = $('#preview-panel');
    preview.addEventListener('dragover', (e) => {
        e.preventDefault();
        preview.classList.add('drop-target');
    });
    preview.addEventListener('dragleave', (e) => {
        // Only remove if actually leaving the panel (not entering a child)
        if (!preview.contains(e.relatedTarget)) {
            preview.classList.remove('drop-target');
        }
    });
    preview.addEventListener('drop', async (e) => {
        e.preventDefault();
        preview.classList.remove('drop-target');
        const s = getState();
        if (!s.selectedTaskId || !s.selectedUrl) {
            toast('Select a URL first, then drop MHTML', 'error');
            return;
        }
        const file = [...(e.dataTransfer?.files || [])].find(
            f => f.name.toLowerCase().endsWith('.mhtml') || f.name.toLowerCase().endsWith('.mht')
        );
        if (!file) {
            toast('Please drop an .mhtml or .mht file', 'error');
            return;
        }
        try {
            showStatus('Uploading MHTML...', 'warning');
            await api.uploadMhtml(s.selectedTaskId, s.selectedUrl, file);
            toast('MHTML uploaded successfully', 'success');
            await reloadCurrentTask();
        } catch (err) {
            toast('MHTML upload failed: ' + err.message, 'error');
        }
    });
}

// Re-export for backward compatibility if needed
export { selectTask, selectUrl, showStatus, toast, $ };
