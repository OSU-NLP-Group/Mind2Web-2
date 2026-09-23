/**
 * Background service worker for Cache Manager Capture extension.
 *
 * Handles:
 * - Keyboard shortcut (Alt+Shift+C) to capture current page
 * - Batch auto-capture mode with CAPTCHA detection
 * - Communication with the Cache Manager backend, whose URL is set in the popup
 *
 * A capture sends the page's HTML, which the backend converts to text as the
 * crawler does, and a screenshot, both taken as the crawler takes them (see captureFullPage).
 */

importScripts('settings.js');  // getBackend(), isCacheManagerUrl()

// The crawler's capture steps, repeated by captureFullPage (sizes in CSS pixels)
const CRAWLER_WINDOW = { width: 1100, height: 750 };  // the middle of the crawler's 1050-1150 by 700-800
const MAX_VIEWPORT_HEIGHT = 6000;  // limits the viewport, not the screenshot
const SCROLL_PAUSE_MS = 550;  // the crawler waits 0.3-0.8 s after each End and Home key press
const SETTLE_AFTER_RESIZE_MS = 750;  // the crawler waits 0.5-1 s after resizing the viewport
const COMMAND_TIMEOUT_MS = 10000;  // for a DevTools command or a script run in the page
const SCREENSHOT_TIMEOUT_MS = 30000;  // for Page.captureScreenshot, which is slow on long pages

/** fetch() a backend path such as '/api/status'. */
async function api(path, options) {
    return fetch(`${await getBackend()}${path}`, options);
}

// ---------------------------------------------------------------------------
// Batch mode state
// ---------------------------------------------------------------------------

let batchMode = false;
let batchTabId = null;
let captchaCheckTimer = null;
let batchProcessing = false;  // guard against re-entrant onUpdated calls
let pageTimeoutTimer = null;  // 15s page load timeout
let currentRetryCount = 0;    // retry count for current batch URL
let pauseOnCaptcha = false;   // false = capture CAPTCHA pages and move on; true = wait for user
const PAGE_TIMEOUT_MS = 15000;
const MAX_RETRIES = 2;
const MIN_BODY_LENGTH = 200;  // pages shorter than this get retried

// Rich batch status (for popup display)
let batchState = {
    total: 0,
    done: 0,          // the server's count of queued pages that are done: captured, skipped, or left out
    before: 0,        // pages done before this run, when it resumes a batch
    completed: 0,     // pages this run captured
    skipped: 0,       // pages this run skipped
    currentTaskId: '',  // the batch's current URL when the batch tab was sent to it, and its task
    currentUrl: '',
    status: '',       // 'loading', 'retrying', 'captcha', 'capturing', 'advancing', 'done'
    log: [],          // [{time, msg, type}] — last 20 entries
};

function batchLog(msg, type = 'info') {
    const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    batchState.log.push({ time, msg, type });
    if (batchState.log.length > 30) batchState.log.shift();
}

function setBatchStatus(status, current = null) {
    batchState.status = status;
    if (current !== null) {
        batchState.currentTaskId = current.task_id;
        batchState.currentUrl = current.url;
    }
}

// ---------------------------------------------------------------------------
// CAPTCHA detection (injected into target pages)
// ---------------------------------------------------------------------------

/**
 * Injected into the target page via chrome.scripting.executeScript.
 * Returns a string describing the CAPTCHA type, or null if none detected.
 */
function detectCaptcha() {
    const title = document.title.toLowerCase();
    const text = (document.body?.innerText || '').substring(0, 3000).toLowerCase();

    // Cloudflare challenge page
    if (title.includes('just a moment') || title.includes('attention required'))
        return 'cloudflare';
    if (document.querySelector('#challenge-form, .cf-challenge-running, #cf-challenge-running'))
        return 'cloudflare';
    if (text.includes('checking your browser') || text.includes('verify you are human'))
        return 'cloudflare';

    // Cloudflare Turnstile widget
    if (document.querySelector('iframe[src*="challenges.cloudflare.com"]'))
        return 'turnstile';

    // reCAPTCHA
    if (document.querySelector('iframe[src*="recaptcha"], .g-recaptcha'))
        return 'recaptcha';

    // hCaptcha
    if (document.querySelector('iframe[src*="hcaptcha"], .h-captcha'))
        return 'hcaptcha';

    // Generic access denied / blocked (only if page is very short)
    if (text.includes('access denied') && text.length < 2000) return 'blocked';
    if (text.includes('403 forbidden') && text.length < 2000) return 'blocked';

    return null;
}

/**
 * Run CAPTCHA detection in a tab; null also when the check fails or times out.
 * @returns {Promise<string|null>}
 */
async function detectCaptchaInTab(tabId) {
    try {
        return (await runInPage(tabId, detectCaptcha)) || null;
    } catch (e) {
        console.warn('detectCaptchaInTab failed:', e);
        return null;
    }
}

// ---------------------------------------------------------------------------
// Keyboard shortcut handler
// ---------------------------------------------------------------------------

chrome.commands.onCommand.addListener(async (command) => {
    if (command === 'capture-page') {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) {
            await capturePage(tab);
        }
    }
});

// ---------------------------------------------------------------------------
// Core capture function
// ---------------------------------------------------------------------------

/**
 * Capture the current page and send to backend.
 *
 * Returns {success: true} when the page was stored; otherwise {success: false,
 * error}, with moved: true when the backend refused a batch capture because
 * the batch no longer waits for opts.url (nothing was stored).
 * @param {chrome.tabs.Tab} tab
 * @param {object} [opts] - Optional overrides for batch mode
 * @param {boolean} [opts.skipTargetFetch] - Skip fetching capture target (use opts.task_id/url)
 * @param {string} [opts.task_id]
 * @param {string} [opts.url]
 * @param {boolean} [opts.batch] - a batch capture of opts.url, which the backend stores only while the batch waits for opts.url
 */
async function capturePage(tab, opts = {}) {
    try {
        let task_id, url;

        if (opts.skipTargetFetch) {
            task_id = opts.task_id;
            url = opts.url;
        } else {
            // Fetch capture target from backend
            const targetRes = await api('/api/capture/target');
            const target = await targetRes.json();

            if (!target.active) {
                setBadge('!', '#dc2626', 3000);
                return { success: false, error: 'No capture target set. Select a URL in Cache Manager first.' };
            }
            task_id = target.task_id;
            url = target.url;
        }

        // Check if page is a PDF — handle differently
        const isPdf = await detectPdfInTab(tab.id) || (tab.url && tab.url.toLowerCase().endsWith('.pdf'));
        if (isPdf) {
            const actual_url = tab.url && tab.url !== url ? tab.url : undefined;
            const pdf = await capturePdfAndUpload(task_id, url, actual_url, !!opts.batch);
            if (pdf !== 'stored') {
                setBadge('✗', '#dc2626', 3000);
                return pdf === 'moved' ? { success: false, moved: true, error: 'The batch moved on' }
                                       : { success: false, error: 'Failed to download PDF' };
            }
            if (batchMode) {
                setBadge('✓', '#22c55e', 1000);
                return { success: true, batch: true };
            }
            setBadge('✓', '#22c55e', 2000);
            await switchToCacheManager(tab.id);
            return { success: true };
        }

        // Ensure the target tab is active/visible before screenshot
        await chrome.tabs.update(tab.id, { active: true });
        await sleep(150);

        // The page's HTML, which the backend converts to text as the crawler does, and a screenshot
        let captured = await captureFullPage(tab.id);
        const visiblePartOnly = !captured;
        if (visiblePartOnly) {
            await scrollAsTheCrawler(tab.id);
            captured = { html: await readPage(tab.id), screenshot: await captureVisiblePart(tab.id) };
            if (batchMode) batchLog('Full-page screenshot unavailable; captured the visible part only', 'warn');
        }

        // Detect redirect: tab.url may differ from the original URL
        const actual_url = tab.url && tab.url !== url ? tab.url : undefined;

        // Send to backend, which converts the HTML to text as the crawler does
        const captureRes = await api('/api/capture', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                task_id,
                url,
                html: captured.html,
                screenshot_base64: captured.screenshot,
                ...(visiblePartOnly ? { visible_part_only: true } : {}),
                ...(actual_url ? { actual_url } : {}),
                ...(opts.batch ? { batch: true } : {}),
            }),
        });

        if (captureRes.status === 409 && opts.batch) {
            setBadge('✗', '#dc2626', 3000);
            return { success: false, moved: true, error: 'The batch moved on' };
        }
        if (!captureRes.ok) {
            throw new Error(`Backend returned ${captureRes.status}`);
        }

        // In batch mode, don't close tab or switch — batch logic handles advancement
        if (batchMode) {
            setBadge('✓', '#22c55e', 1000);
            return { success: true, batch: true };
        }

        // Normal mode — close tab and switch back
        setBadge('✓', '#22c55e', 2000);
        await switchToCacheManager(tab.id);
        return { success: true };
    } catch (err) {
        console.error('Capture failed:', err);
        setBadge('✗', '#dc2626', 3000);
        return { success: false, error: err.message };
    }
}

/**
 * The HTML of the page in a tab and a screenshot of the whole page, as base64 PNG, taken as the crawler takes them.
 *
 * Through the DevTools protocol, the page is laid out in a viewport of the
 * crawler's window size (CRAWLER_WINDOW) at a device scale factor of 1, as in
 * the crawler, and scrolled as the crawler scrolls (see scrollAsTheCrawler).  The viewport is then resized to the page's
 * content height, at most MAX_VIEWPORT_HEIGHT, and after
 * SETTLE_AFTER_RESIZE_MS the HTML is read (see readPage), the tab
 * is brought to the front, since Chrome may not render a background tab, and
 * the page is captured with
 * captureBeyondViewport, which covers the whole page, including any part
 * below that viewport.  The DevTools commands are Page and Emulation commands
 * only, and scrolling and reading the page run as content scripts, so the
 * Runtime domain, which some bot checks detect, is never enabled.  Chrome
 * shows a "started debugging this browser" bar while the debugger is
 * attached, and the tab gets its own viewport back afterwards.
 *
 * Returns {html, screenshot}, or null when the debugger cannot attach (for
 * example under a policy that blocks it), when a command fails (for example
 * after Cancel on that bar), or when a command or content script does not
 * finish within its timeout.
 */
async function captureFullPage(tabId) {
    const target = { tabId };
    const attaching = chrome.debugger.attach(target, '1.3');
    try {
        await withTimeout(attaching, COMMAND_TIMEOUT_MS, 'debugger.attach');
    } catch (e) {
        console.warn('debugger.attach failed:', e);
        attaching.then(() => chrome.debugger.detach(target)).catch(() => {});  // if it attaches after the timeout
        return null;
    }
    const send = (method, params, ms = COMMAND_TIMEOUT_MS) =>
        withTimeout(chrome.debugger.sendCommand(target, method, params), ms, method);
    const setViewport = (height) => send('Emulation.setDeviceMetricsOverride', {
        mobile: false, width: CRAWLER_WINDOW.width, height, deviceScaleFactor: 1,
    });
    try {
        await setViewport(CRAWLER_WINDOW.height);
        await scrollAsTheCrawler(tabId);
        const metrics = await send('Page.getLayoutMetrics');
        await setViewport(Math.round(Math.min(metrics.cssContentSize.height, MAX_VIEWPORT_HEIGHT)));
        await sleep(SETTLE_AFTER_RESIZE_MS);
        const html = await readPage(tabId);
        await send('Page.bringToFront').catch(() => {});  // best effort: the screenshot decides success
        const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true },
                                SCREENSHOT_TIMEOUT_MS);
        return { html, screenshot: shot.data };
    } catch (e) {
        console.warn('Full-page screenshot failed:', e);
        return null;
    } finally {
        await send('Emulation.clearDeviceMetricsOverride').catch(() => {});
        await withTimeout(chrome.debugger.detach(target), COMMAND_TIMEOUT_MS, 'debugger.detach').catch(() => {});
    }
}

/**
 * Scroll the page in a tab to the end three times and back to the top, pausing
 * SCROLL_PAUSE_MS after each, as the crawler does with the End and Home keys
 * so that content loaded on scrolling is loaded.
 */
async function scrollAsTheCrawler(tabId) {
    for (const toEnd of [true, true, true, false]) {
        await runInPage(tabId, (end) => {
            const root = document.scrollingElement || document.documentElement;
            window.scrollTo(0, end ? root.scrollHeight : 0);
        }, [toEnd]);
        await sleep(SCROLL_PAUSE_MS);
    }
}

/** The outerHTML of the document in a tab, which the backend converts to the text it stores. */
async function readPage(tabId) {
    return (await runInPage(tabId, () => document.documentElement.outerHTML)) || '';
}

/**
 * A screenshot of the visible part of the page in a tab, as base64 JPEG.
 *
 * captureVisibleTab captures whichever tab is active in a window, so the tab is
 * made active again, and the screenshot is kept only if the tab is the active tab
 * of its window both right before and right after it is taken; otherwise this
 * throws, and the capture fails instead of storing another tab's screenshot.
 */
async function captureVisiblePart(tabId) {
    await chrome.tabs.update(tabId, { active: true });
    await sleep(150);
    const before = await chrome.tabs.get(tabId);
    if (!before.active) throw new Error('The page is not the active tab of its window');
    const dataUrl = await withTimeout(chrome.tabs.captureVisibleTab(before.windowId, { format: 'jpeg', quality: 85 }),
                                      COMMAND_TIMEOUT_MS, 'captureVisibleTab');
    const after = await chrome.tabs.get(tabId);
    if (!after.active || after.windowId !== before.windowId) {
        throw new Error('The page stopped being the active tab of its window during the screenshot');
    }
    return dataUrl.replace(/^data:image\/jpeg;base64,/, '');
}

/** The result of `func(...args)` run as a content script in a tab; rejects after COMMAND_TIMEOUT_MS. */
async function runInPage(tabId, func, args = []) {
    const results = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func, args }), COMMAND_TIMEOUT_MS, 'a script in the page');
    return results?.[0]?.result;
}

// ---------------------------------------------------------------------------
// Batch mode orchestration
// ---------------------------------------------------------------------------

async function startBatch(opts = {}) {
    try {
        const res = await api('/api/capture/batch/status');
        const status = await res.json();

        if (!status.active || !status.current) {
            setBadge('!', '#dc2626', 3000);
            return { success: false, error: 'No active batch. Queue URLs from Cache Manager first.' };
        }

        batchMode = true;
        batchProcessing = false;
        currentRetryCount = 0;
        pauseOnCaptcha = !!opts.pauseOnCaptcha;
        // A batch that an earlier run left unfinished (the batch tab was closed, or the extension
        // was reloaded) resumes at its current URL; the server's completed count is what that run did.
        batchState = {
            total: status.total, done: status.completed, before: status.completed, completed: 0, skipped: 0,
            currentTaskId: status.current.task_id, currentUrl: status.current.url, status: 'loading', log: [],
        };
        const mode = pauseOnCaptcha ? 'pause on CAPTCHA' : 'auto';
        batchLog(batchState.before
            ? `Batch resumed: ${status.remaining} of ${status.total} URLs left (${mode})`
            : `Batch started: ${status.total} URLs (${mode})`);
        batchLog(`Loading: ${truncUrl(status.current.url)}`);
        setBadge(`${batchState.done}/${status.total}`, '#2563eb');

        // Open the first URL in a new tab
        const tab = await chrome.tabs.create({ url: status.current.url });
        batchTabId = tab.id;
        startPageTimeout(tab.id);

        return { success: true, total: status.total };
    } catch (err) {
        console.error('startBatch failed:', err);
        batchMode = false;
        return { success: false, error: err.message };
    }
}

async function advanceBatch() {
    try {
        setBatchStatus('advancing');
        const res = await api('/api/capture/batch/status');
        const status = await res.json();

        if (!status.active || !status.current) {
            // Batch complete
            batchState.done = batchState.total;
            endBatch();
            return;
        }

        batchState.total = status.total;
        batchState.done = status.completed;
        setBadge(`${batchState.done}/${status.total}`, '#2563eb');

        // Navigate existing tab to the next URL
        if (batchTabId) {
            batchProcessing = false;
            currentRetryCount = 0;  // reset retry count for new URL
            setBatchStatus('loading', status.current);
            batchLog(`Loading: ${truncUrl(status.current.url)}`);
            startPageTimeout(batchTabId);
            await chrome.tabs.update(batchTabId, { url: status.current.url });
        }
    } catch (err) {
        console.error('advanceBatch failed:', err);
        batchLog(`Error advancing: ${err.message}`, 'error');
        endBatch();
    }
}

async function endBatch() {
    batchMode = false;
    stopCaptchaPolling();
    clearPageTimeout();

    setBatchStatus('done');
    // URLs that stopped needing a capture while queued (captured by hand, reviewed, deleted) were left out
    const leftOut = batchState.done - batchState.before - batchState.completed - batchState.skipped;
    batchLog(`Batch finished: ${batchState.completed} captured, ${batchState.skipped} skipped`
             + (leftOut > 0 ? `, ${leftOut} left out as no longer needing a capture` : '')
             + (batchState.before ? `, ${batchState.before} done before it was resumed` : ''));
    setBadge('Done', '#22c55e', 3000);

    if (batchTabId) {
        await switchToCacheManager(batchTabId);
        batchTabId = null;
    }
}

async function stopBatch() {
    try {
        await api('/api/capture/batch/stop', { method: 'POST' });
    } catch {}
    await endBatch();
}

// ---------------------------------------------------------------------------
// Tab monitoring — auto-capture on page load during batch mode
// ---------------------------------------------------------------------------

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
    // Only monitor our batch tab
    if (!batchMode || tabId !== batchTabId) return;
    if (changeInfo.status !== 'complete') return;

    // Guard against re-entrant calls
    if (batchProcessing) return;
    batchProcessing = true;

    // Page loaded — cancel timeout timer
    clearPageTimeout();

    // Wait for page to settle (JS rendering, redirects)
    await sleep(2500);

    // Verify we're still in batch mode and this is still our tab
    if (!batchMode || tabId !== batchTabId) {
        batchProcessing = false;
        return;
    }

    // Check for CAPTCHA
    const captchaType = await detectCaptchaInTab(tabId);

    if (captchaType && pauseOnCaptcha) {
        // CAPTCHA detected + pause mode — wait for user to solve
        console.log(`CAPTCHA detected: ${captchaType} (pausing)`);
        currentRetryCount = 0;
        setBatchStatus('captcha');
        batchLog(`CAPTCHA (${captchaType}) — solve in browser`, 'warn');
        setBadge('⏳', '#f59e0b');

        try {
            await api('/api/capture/batch/captcha', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type: captchaType }),
            });
        } catch {}

        startCaptchaPolling(tabId);
    } else {
        // No CAPTCHA, or CAPTCHA but auto mode — capture and move on
        if (captchaType) {
            batchLog(`CAPTCHA (${captchaType}) — capturing anyway`, 'warn');
        }
        await autoCaptureAndAdvance(tabId, currentRetryCount);
    }
});

// Handle tab closure during batch
chrome.tabs.onRemoved.addListener((tabId) => {
    if (batchMode && tabId === batchTabId) {
        console.log('Batch tab was closed');
        batchTabId = null;
        endBatch();
    }
});

// ---------------------------------------------------------------------------
// CAPTCHA polling
// ---------------------------------------------------------------------------

function startCaptchaPolling(tabId) {
    stopCaptchaPolling();

    const poll = async () => {
        if (!batchMode || tabId !== batchTabId) return;

        try {
            const result = await detectCaptchaInTab(tabId);
            if (!result) {
                // CAPTCHA resolved!
                stopCaptchaPolling();
                batchLog('CAPTCHA resolved, capturing...', 'success');
                setBatchStatus('capturing');
                await sleep(1500); // Let page settle after CAPTCHA
                await autoCaptureAndAdvance(tabId);
                return;
            }
        } catch (e) {
            // Tab might be gone
            stopCaptchaPolling();
            return;
        }

        // Continue polling
        captchaCheckTimer = setTimeout(poll, 3000);
    };

    captchaCheckTimer = setTimeout(poll, 3000);
}

function stopCaptchaPolling() {
    if (captchaCheckTimer) {
        clearTimeout(captchaCheckTimer);
        captchaCheckTimer = null;
    }
}

// ---------------------------------------------------------------------------
// PDF detection and download
// ---------------------------------------------------------------------------

/**
 * Detect if the tab is showing Chrome's built-in PDF viewer.
 * Returns true if the page is a PDF; false otherwise, and when the check fails or times out.
 */
async function detectPdfInTab(tabId) {
    try {
        return (await runInPage(tabId, () => {
            // Chrome's PDF viewer uses an <embed type="application/pdf">
            const embed = document.querySelector('embed[type="application/pdf"]');
            if (embed) return true;
            // Also check if the content type meta tag says PDF
            const ct = document.contentType || '';
            if (ct === 'application/pdf') return true;
            return false;
        })) || false;
    } catch {
        return false;
    }
}

/**
 * Download PDF bytes from a URL and upload to the backend.
 *
 * With batch, the upload is a batch capture of url, which the backend stores
 * only while the batch waits for url.  Returns 'stored'; 'moved' when the
 * backend refused the upload because the batch no longer waits for url
 * (nothing was stored); or 'failed'.
 */
async function capturePdfAndUpload(taskId, url, actualUrl, batch) {
    const downloadUrl = actualUrl || url;
    try {
        const res = await fetch(downloadUrl);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();

        const form = new FormData();
        form.append('file', blob, 'page.pdf');

        const batchQuery = batch ? '&batch=true' : '';
        const uploadRes = await api(
            `/api/upload-pdf/${encodeURIComponent(taskId)}?url=${encodeURIComponent(url)}${batchQuery}`,
            { method: 'POST', body: form }
        );
        if (uploadRes.status === 409 && batch) return 'moved';
        if (!uploadRes.ok) throw new Error(`Upload failed: ${uploadRes.status}`);
        return 'stored';
    } catch (err) {
        console.warn('capturePdfAndUpload failed:', err);
        return 'failed';
    }
}

// ---------------------------------------------------------------------------
// Auto-capture helper
// ---------------------------------------------------------------------------

/**
 * Capture the page in the batch tab for the batch's current URL, then load the next one.
 *
 * The tab shows the URL the batch waited for when the tab was sent to it
 * (batchState.currentTaskId and currentUrl).  If the batch has moved on since,
 * because that URL was captured or uploaded by hand, skipped, or replaced by a new
 * batch, the page is not captured, which would store it under another URL, and the
 * tab is sent to the batch's current URL instead.  The capture is sent as a batch
 * capture of that URL, so the backend also refuses it, with status 409, when the
 * batch moves on during the capture; that too only moves the tab on.  Neither
 * counts as captured or skipped.
 */
async function autoCaptureAndAdvance(tabId, retryCount = 0) {
    try {
        // Get current batch target
        const statusRes = await api('/api/capture/batch/status');
        const status = await statusRes.json();

        if (!status.active || !status.current) {
            endBatch();
            return;
        }

        if (status.current.task_id !== batchState.currentTaskId || status.current.url !== batchState.currentUrl) {
            batchLog(`Batch moved on from ${truncUrl(batchState.currentUrl)}; not captured`, 'warn');
            await advanceBatch();
            return;
        }

        const tab = await chrome.tabs.get(tabId);

        // Check if page is actually a PDF (e.g., URL was misrecorded as "web")
        const isPdf = await detectPdfInTab(tabId);
        if (isPdf || (tab.url && tab.url.toLowerCase().endsWith('.pdf'))) {
            setBatchStatus('capturing');
            batchLog(`PDF detected: ${truncUrl(status.current.url)}`, 'info');
            const actual_url = tab.url && tab.url !== status.current.url ? tab.url : undefined;
            const pdf = await capturePdfAndUpload(status.current.task_id, status.current.url, actual_url, true);
            if (pdf === 'stored') {
                batchState.completed++;
                batchLog(`PDF saved OK`, 'success');
                setBadge('✓', '#22c55e', 1000);
            } else if (pdf === 'moved') {
                batchLog(`Batch moved on from ${truncUrl(status.current.url)}; PDF not stored`, 'warn');
            } else {
                batchLog(`PDF download failed, skipping`, 'error');
                await skipAndAdvance();
                return;
            }
            await sleep(500);
            await advanceBatch();
            return;
        }

        // Check if page body is too short (may need retry)
        if (retryCount < MAX_RETRIES) {
            try {
                const bodyLength = (await runInPage(tabId, () => (document.body?.innerText || '').length)) || 0;
                if (bodyLength < MIN_BODY_LENGTH) {
                    console.log(`Page body too short (${bodyLength} chars), retry ${retryCount + 1}/${MAX_RETRIES}`);
                    setBatchStatus('retrying');
                    batchLog(`Short page (${bodyLength} chars), retry ${retryCount + 1}/${MAX_RETRIES}`, 'warn');
                    setBadge(`R${retryCount + 1}`, '#f59e0b');
                    // Reload the page and let onUpdated handle it again
                    batchProcessing = false;
                    currentRetryCount = retryCount + 1;
                    startPageTimeout(tabId);
                    await chrome.tabs.reload(tabId);
                    return;
                }
            } catch (e) {
                // If we can't check body length, proceed with capture
            }
        }

        setBatchStatus('capturing');
        batchLog(`Capturing: ${truncUrl(status.current.url)}`);
        const result = await capturePage(tab, {
            skipTargetFetch: true,
            task_id: status.current.task_id,
            url: status.current.url,
            batch: true,
        });

        if (result.moved) {
            batchLog(`Batch moved on from ${truncUrl(status.current.url)}; not stored`, 'warn');
            await sleep(500);
            await advanceBatch();
            return;
        }
        if (!result.success) {
            // Capture failed — skip this URL and advance
            console.warn(`Capture failed for ${status.current.url}: ${result.error}, skipping`);
            batchLog(`Failed: ${result.error || 'unknown'}, skipping`, 'error');
            await skipAndAdvance();
            return;
        }

        batchState.completed++;
        batchLog(`Captured OK`, 'success');
        // Wait briefly then advance
        await sleep(500);
        await advanceBatch();
    } catch (err) {
        // Unexpected error — skip current URL to avoid getting stuck
        console.error('autoCaptureAndAdvance failed:', err);
        batchLog(`Error: ${err.message}, skipping`, 'error');
        await skipAndAdvance();
    }
}

/**
 * Skip the URL the batch tab was sent to (on failure) and load the batch's next URL.
 *
 * The backend skips the URL only while the batch still waits for it (status 409
 * otherwise); a URL the batch has moved on from does not count as skipped.
 */
async function skipAndAdvance() {
    try {
        const res = await api('/api/capture/batch/skip', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_id: batchState.currentTaskId, url: batchState.currentUrl }),
        });
        if (res.ok) batchState.skipped++;
        else if (res.status === 409) batchLog(`Batch moved on from ${truncUrl(batchState.currentUrl)}`, 'warn');
        else throw new Error(`Backend returned ${res.status}`);
        await sleep(300);
        await advanceBatch();
    } catch (err) {
        console.error('skipAndAdvance failed:', err);
        batchLog(`Fatal error: ${err.message}`, 'error');
        endBatch();
    }
}

// ---------------------------------------------------------------------------
// Page load timeout (15s)
// ---------------------------------------------------------------------------

function startPageTimeout(tabId) {
    clearPageTimeout();
    pageTimeoutTimer = setTimeout(async () => {
        if (!batchMode || tabId !== batchTabId) return;
        if (batchProcessing) return;  // already being handled
        batchProcessing = true;

        console.log(`Page timeout (${PAGE_TIMEOUT_MS}ms) — force capturing`);
        setBatchStatus('timeout');
        batchLog(`Timeout (${PAGE_TIMEOUT_MS/1000}s) — force capturing`, 'warn');
        setBadge('T/O', '#f59e0b', 1500);
        // Force capture whatever is visible
        await autoCaptureAndAdvance(tabId, MAX_RETRIES);  // skip retries on timeout
    }, PAGE_TIMEOUT_MS);
}

function clearPageTimeout() {
    if (pageTimeoutTimer) {
        clearTimeout(pageTimeoutTimer);
        pageTimeoutTimer = null;
    }
}

// ---------------------------------------------------------------------------
// Tab management
// ---------------------------------------------------------------------------

async function switchToCacheManager(capturedTabId) {
    try {
        const backend = await getBackend();
        const capturedTab = await chrome.tabs.get(capturedTabId);
        if (!isCacheManagerUrl(capturedTab.url, backend)) {
            await chrome.tabs.remove(capturedTabId);
        }
        const cmTabs = (await chrome.tabs.query({})).filter(t => isCacheManagerUrl(t.url, backend));
        if (cmTabs.length > 0) {
            await chrome.tabs.update(cmTabs[0].id, { active: true });
            await chrome.windows.update(cmTabs[0].windowId, { focused: true });
        }
    } catch (e) {
        console.warn('switchToCacheManager:', e);
    }
}

// ---------------------------------------------------------------------------
// Badge helper
// ---------------------------------------------------------------------------

function setBadge(text, color, clearAfterMs = 0) {
    chrome.action.setBadgeText({ text });
    if (color) chrome.action.setBadgeBackgroundColor({ color });
    if (clearAfterMs > 0) {
        setTimeout(() => chrome.action.setBadgeText({ text: '' }), clearAfterMs);
    }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
}

/** `promise`, or a rejection that names `what` when it has not settled after `ms` milliseconds. */
function withTimeout(promise, ms, what) {
    let timer;
    const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${what} did not finish within ${ms / 1000} s`)), ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function truncUrl(url, maxLen = 60) {
    if (!url) return '';
    try {
        const u = new URL(url);
        const short = u.hostname + u.pathname;
        return short.length > maxLen ? short.substring(0, maxLen) + '...' : short;
    } catch {
        return url.length > maxLen ? url.substring(0, maxLen) + '...' : url;
    }
}

// ---------------------------------------------------------------------------
// Message handler (from popup)
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === 'capture') {
        chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
            if (tab) capturePage(tab).then(sendResponse);
        });
        return true;
    }
    if (msg.action === 'start-batch') {
        startBatch({ pauseOnCaptcha: !!msg.pauseOnCaptcha }).then(sendResponse);
        return true;
    }
    if (msg.action === 'stop-batch') {
        stopBatch().then(sendResponse);
        return true;
    }
    if (msg.action === 'get-batch-status') {
        sendResponse({ batchMode, batchTabId, pauseOnCaptcha, ...batchState });
        return false;
    }
});
