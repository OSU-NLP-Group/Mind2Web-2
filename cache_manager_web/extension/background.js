/**
 * Background service worker for Cache Manager Capture extension.
 *
 * Handles:
 * - Keyboard shortcut (Alt+Shift+C) to capture current page
 * - Communication with the local backend at localhost:8000
 */

const BACKEND = 'http://127.0.0.1:8000';

// Handle keyboard shortcut
chrome.commands.onCommand.addListener(async (command) => {
    if (command === 'capture-page') {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) {
            await capturePage(tab);
        }
    }
});

/**
 * Capture the current page and send to backend.
 */
async function capturePage(tab) {
    try {
        // 1. Get capture target from backend
        const targetRes = await fetch(`${BACKEND}/api/capture/target`);
        const target = await targetRes.json();

        if (!target.active) {
            // No active target — use current tab URL, try to find matching task
            // For now, notify user to select a URL in the web UI first
            chrome.action.setBadgeText({ text: '!' });
            chrome.action.setBadgeBackgroundColor({ color: '#dc2626' });
            setTimeout(() => chrome.action.setBadgeText({ text: '' }), 3000);
            return { success: false, error: 'No capture target set. Select a URL in Cache Manager first.' };
        }

        // 2. Extract text from the page via content script
        const textResults = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: () => document.body?.innerText || '',
        });
        const text = textResults?.[0]?.result || '';

        // 3. Capture visible tab as screenshot
        const screenshotDataUrl = await chrome.tabs.captureVisibleTab(null, {
            format: 'jpeg',
            quality: 85,
        });

        // Strip data URL prefix to get base64
        const base64 = screenshotDataUrl.replace(/^data:image\/jpeg;base64,/, '');

        // 4. Send to backend
        const captureRes = await fetch(`${BACKEND}/api/capture`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                task_id: target.task_id,
                url: target.url,
                text: text,
                screenshot_base64: base64,
            }),
        });

        if (!captureRes.ok) {
            throw new Error(`Backend returned ${captureRes.status}`);
        }

        // Success feedback
        chrome.action.setBadgeText({ text: '✓' });
        chrome.action.setBadgeBackgroundColor({ color: '#22c55e' });
        setTimeout(() => chrome.action.setBadgeText({ text: '' }), 2000);

        return { success: true };
    } catch (err) {
        console.error('Capture failed:', err);
        chrome.action.setBadgeText({ text: '✗' });
        chrome.action.setBadgeBackgroundColor({ color: '#dc2626' });
        setTimeout(() => chrome.action.setBadgeText({ text: '' }), 3000);
        return { success: false, error: err.message };
    }
}

// Expose capturePage for popup to call
self.capturePage = capturePage;
