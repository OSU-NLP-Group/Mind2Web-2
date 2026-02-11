/**
 * Popup script for Cache Manager Capture extension.
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

// Capture button click
captureBtn.addEventListener('click', async () => {
    if (!currentTarget) return;

    captureBtn.disabled = true;
    captureBtn.textContent = 'Capturing...';

    try {
        // Get current active tab
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab) throw new Error('No active tab');

        // Extract text
        const textResults = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: () => document.body?.innerText || '',
        });
        const text = textResults?.[0]?.result || '';

        // Screenshot
        const screenshotDataUrl = await chrome.tabs.captureVisibleTab(null, {
            format: 'jpeg',
            quality: 85,
        });
        const base64 = screenshotDataUrl.replace(/^data:image\/jpeg;base64,/, '');

        // Send to backend
        const res = await fetch(`${BACKEND}/api/capture`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                task_id: currentTarget.task_id,
                url: currentTarget.url,
                text: text,
                screenshot_base64: base64,
            }),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }

        captureBtn.textContent = 'Captured!';
        captureBtn.className = 'capture-btn success';

        // Close captured tab and switch to cache manager
        try {
            const isCM = tab.url?.startsWith(BACKEND);
            if (!isCM) {
                await chrome.tabs.remove(tab.id);
            }
            const cmTabs = await chrome.tabs.query({
                url: ['http://127.0.0.1:8000/*', 'http://localhost:8000/*'],
            });
            if (cmTabs.length > 0) {
                await chrome.tabs.update(cmTabs[0].id, { active: true });
            }
        } catch (e) {
            console.warn('Tab switch failed:', e);
        }

        setTimeout(() => window.close(), 500);
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
