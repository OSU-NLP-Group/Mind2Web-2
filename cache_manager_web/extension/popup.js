/**
 * Popup script for Cache Manager Capture extension.
 *
 * Delegates actual capture to the background service worker via message passing.
 */

const BACKEND = 'http://127.0.0.1:8000';

const statusEl = document.getElementById('status');
const targetSection = document.getElementById('target-section');
const captureBtn = document.getElementById('capture-btn');

let currentTarget = null;

// Check backend connection and load target
async function init() {
    try {
        const res = await fetch(`${BACKEND}/api/status`);
        const data = await res.json();

        if (data.loaded) {
            statusEl.className = 'status connected';
            statusEl.textContent = `Connected — ${data.agent_name}`;
        } else {
            statusEl.className = 'status connected';
            statusEl.textContent = 'Connected (no cache loaded)';
        }

        // Get capture target
        const targetRes = await fetch(`${BACKEND}/api/capture/target`);
        const target = await targetRes.json();

        if (target.active) {
            currentTarget = target;
            targetSection.innerHTML = `
                <div class="target-info">
                    <div class="label">Capture target:</div>
                    <div class="value task-id">${escHtml(target.task_id)}</div>
                    <div class="value">${escHtml(target.url)}</div>
                </div>
            `;
            captureBtn.disabled = false;
        } else {
            targetSection.innerHTML = `
                <div class="no-target">
                    No capture target set.<br>
                    Select a URL in Cache Manager and click "Open in Browser" first.
                </div>
            `;
            captureBtn.disabled = true;
        }
    } catch (err) {
        statusEl.className = 'status disconnected';
        statusEl.textContent = 'Cannot connect to backend (is it running?)';
        captureBtn.disabled = true;
    }
}

// Capture button — delegates to background service worker
captureBtn.addEventListener('click', async () => {
    if (!currentTarget) return;

    captureBtn.disabled = true;
    captureBtn.textContent = 'Capturing...';

    try {
        const result = await chrome.runtime.sendMessage({ action: 'capture' });

        if (result?.success) {
            captureBtn.textContent = 'Captured!';
            captureBtn.className = 'capture-btn success';
            setTimeout(() => window.close(), 500);
        } else {
            throw new Error(result?.error || 'Capture failed');
        }
    } catch (err) {
        captureBtn.textContent = 'Failed: ' + err.message;
        captureBtn.className = 'capture-btn error';
        setTimeout(() => {
            captureBtn.textContent = 'Capture This Page';
            captureBtn.className = 'capture-btn';
            captureBtn.disabled = false;
        }, 3000);
    }
});

function escHtml(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
}

init();
