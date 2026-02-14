class LeaderboardTable {
    constructor() {
        this.data = null;
        this.currentSet = 'eval_set';
        this.sortColumn = 'partial_completion';  // Default sort by partial_completion
        this.sortDirection = 'desc';
        this.popover = null;
        this.popoverTimeout = null;
        this.isPopoverVisible = false;
        this.init();
    }

    async init() {
        await this.loadData();
        this.createPopover();
        this.createTable();
        this.setupTabs();
    }

    async loadData() {
        try {
            const response = await fetch('./leaderboard_data.json');
            const jsonData = await response.json();
            this.data = jsonData.leaderboardData;
        } catch (error) {
            console.error('Error loading leaderboard data:', error);
        }
    }

    createPopover() {
        const existing = document.querySelector('.agent-popover');
        if (existing) existing.remove();

        const popover = document.createElement('div');
        popover.className = 'agent-popover';
        popover.innerHTML = `
            <div class="agent-popover-header">
                <span class="agent-popover-name"></span>
                <span class="agent-popover-org"></span>
            </div>
            <div class="agent-popover-body">
                <div class="agent-popover-row">
                    <span class="agent-popover-label">Base Model</span>
                    <span class="agent-popover-value" data-field="base_model"></span>
                </div>
                <div class="agent-popover-row">
                    <span class="agent-popover-label">Architecture</span>
                    <span class="agent-popover-value" data-field="architecture"></span>
                </div>
                <div class="agent-popover-row agent-popover-contact-row">
                    <span class="agent-popover-label">Contact</span>
                    <span class="agent-popover-value" data-field="contact"></span>
                </div>
                <div class="agent-popover-desc">
                    <span class="agent-popover-description"></span>
                </div>
            </div>
        `;
        document.body.appendChild(popover);
        this.popover = popover;

        // Keep popover visible when hovering over it
        this.popover.addEventListener('mouseenter', () => {
            clearTimeout(this.popoverTimeout);
        });
        this.popover.addEventListener('mouseleave', () => {
            this.popoverTimeout = setTimeout(() => {
                this.hidePopover();
            }, 200);
        });
    }

    showPopover(agentData, triggerElement) {
        const info = agentData.info;
        const details = info.details || {};

        // Populate content
        this.popover.querySelector('.agent-popover-name').textContent = info.name;
        this.popover.querySelector('.agent-popover-org').textContent = details.organization || '';

        // Only show rows that have values
        const fields = ['base_model', 'architecture', 'contact'];
        fields.forEach(field => {
            const row = this.popover.querySelector(`[data-field="${field}"]`).closest('.agent-popover-row');
            if (details[field]) {
                this.popover.querySelector(`[data-field="${field}"]`).textContent = details[field];
                row.style.display = '';
            } else {
                row.style.display = 'none';
            }
        });

        // Description (only show if present)
        const descEl = this.popover.querySelector('.agent-popover-description');
        if (details.description) {
            descEl.textContent = details.description;
            descEl.parentElement.style.display = '';
        } else {
            descEl.parentElement.style.display = 'none';
        }

        // Show off-screen for measurement
        this.popover.style.visibility = 'hidden';
        this.popover.style.display = 'block';
        this.popover.classList.remove('is-visible');

        // Calculate position
        const triggerRect = triggerElement.getBoundingClientRect();
        const popoverRect = this.popover.getBoundingClientRect();
        const scrollY = window.scrollY;
        const scrollX = window.scrollX;
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;

        // Default: below and left-aligned to trigger
        let top = triggerRect.bottom + scrollY + 8;
        let left = triggerRect.left + scrollX;

        // Flip above if not enough room below
        if (triggerRect.bottom + popoverRect.height + 8 > viewportHeight) {
            top = triggerRect.top + scrollY - popoverRect.height - 8;
        }

        // Adjust horizontally if going off-screen
        if (left + popoverRect.width > viewportWidth + scrollX) {
            left = triggerRect.right + scrollX - popoverRect.width;
        }
        if (left < scrollX) {
            left = scrollX + 8;
        }

        this.popover.style.top = `${top}px`;
        this.popover.style.left = `${left}px`;
        this.popover.style.visibility = 'visible';
        this.popover.classList.add('is-visible');
        this.isPopoverVisible = true;
    }

    hidePopover() {
        this.popover.classList.remove('is-visible');
        this.isPopoverVisible = false;
        setTimeout(() => {
            if (!this.isPopoverVisible) {
                this.popover.style.display = 'none';
            }
        }, 200);
    }

    setupPopovers() {
        const wrappers = document.querySelectorAll('.agent-name-wrapper');

        wrappers.forEach(wrapper => {
            wrapper.addEventListener('mouseenter', () => {
                const agentName = wrapper.dataset.agentName;
                const agentData = this.data.find(d => d.info.name === agentName);
                if (!agentData || !agentData.info.details) return;

                clearTimeout(this.popoverTimeout);
                this.showPopover(agentData, wrapper);
            });

            wrapper.addEventListener('mouseleave', () => {
                this.popoverTimeout = setTimeout(() => {
                    this.hidePopover();
                }, 200);
            });
        });
    }

    createTable() {
        const tableContainer = document.querySelector('#m2w2-table');
        if (!tableContainer) {
            console.error('Table container not found!');
            return;
        }

        const tabsHtml = '';

        // Insert tabs before the table wrapper
        const tableWrapper = tableContainer.parentElement;
        tableWrapper.insertAdjacentHTML('beforebegin', tabsHtml);

        this.renderTable();
    }

    renderTable() {
        const tableContainer = document.querySelector('#m2w2-table');

        if (!tableContainer) {
            console.error('Table container not found!');
            return;
        }

        // Hide popover if visible during re-render
        if (this.isPopoverVisible) {
            this.hidePopover();
        }

        // Create single table with sticky header
        let tableHtml = `
            <thead>
                <tr>
                    <th class="has-text-left">Agent</th>
                    <th class="has-text-centered sortable-header" data-column="date" data-sort-order="desc">
                        Date ${this.getSortIcon('date')}
                    </th>
                    <th class="has-text-centered sortable-header" data-column="partial_completion" data-sort-order="desc">
                        Partial Completion ${this.getSortIcon('partial_completion')}
                    </th>
                    <th class="has-text-centered sortable-header" data-column="success_rate" data-sort-order="desc">
                        Success Rate ${this.getSortIcon('success_rate')}
                    </th>
                    <th class="has-text-centered sortable-header" data-column="pass3" data-sort-order="desc">
                        Pass@3 ${this.getSortIcon('pass3')}
                    </th>
                    <th class="has-text-centered sortable-header" data-column="time" data-sort-order="asc">
                        Time (min) ${this.getSortIcon('time')}
                    </th>
                    <th class="has-text-centered sortable-header" data-column="answer_length" data-sort-order="asc">
                        Answer Length ${this.getSortIcon('answer_length')}
                    </th>
                </tr>
            </thead>
            <tbody>
        `;

        // Sort the data
        const sortedData = this.getSortedData();

        if (sortedData && sortedData.length > 0) {

            const maxValues = this.findMaxValues(sortedData);

            sortedData.forEach((model, index) => {
                const info = model.info;
                const results = model[this.currentSet];
                const isHuman = info.name === 'Human';

                let agentNameHtml;
                if (info.url && info.url.trim() !== "") {
                    agentNameHtml = `<span class="agent-name-wrapper" data-agent-name="${info.name}">
                        <a href="${info.url}" target="_blank" class="agent-link" rel="noopener noreferrer">
                            ${info.name}
                        </a>
                    </span>`;
                } else if (!isHuman) {
                    agentNameHtml = `<span class="agent-name-wrapper" data-agent-name="${info.name}">
                        <a href="javascript:void(0)" class="agent-link agent-link-pending" title="Model details coming soon">
                            ${info.name}
                        </a>
                    </span>`;
                } else {
                    agentNameHtml = `<span class="agent-name-wrapper" data-agent-name="${info.name}">
                        ${info.name}
                    </span>`;
                }

                const rowClass = isHuman ? 'model-row human-row' : 'model-row';
                const rankDisplay = isHuman ? '<span class="human-reference-badge">Reference</span>' : `${index + 1}`;

                tableHtml += `
                    <tr class="${rowClass}" data-rank="${index + 1}">
                        <td class="model-name-cell">
                            <div class="model-info">
                                ${agentNameHtml}
                                ${isHuman ? ' <span class="human-indicator"><i class="fas fa-user"></i></span>' : ''}
                            </div>
                        </td>
                        <td class="has-text-centered">${info.date}</td>
                        <td class="has-text-centered metric-cell">${this.formatMetric(results.partial_completion, isHuman ? null : maxValues.partial_completion, 2)}</td>
                        <td class="has-text-centered metric-cell">${this.formatMetric(results.success_rate, isHuman ? null : maxValues.success_rate, 2)}</td>
                        <td class="has-text-centered metric-cell">${this.formatMetric(results.pass3, isHuman ? null : maxValues.pass3, 2)}</td>
                        <td class="has-text-centered metric-cell">${this.formatMetric(results.time)}</td>
                        <td class="has-text-centered metric-cell">${this.formatMetric(results.answer_length)}</td>
                    </tr>
                `;
            });
        } else {
            tableHtml += `
                <tr>
                    <td colspan="5" class="has-text-centered has-text-grey">
                        <em>No data available</em>
                    </td>
                </tr>
            `;
        }

        tableHtml += '</tbody>';

        // Replace table content
        tableContainer.innerHTML = tableHtml;

        // Setup sorting and popovers
        this.setupSorting();
        this.setupPopovers();
    }

    getSortIcon(column) {
        if (this.sortColumn === column) {
            const defaultOrder = document.querySelector(`[data-column="${column}"]`)?.dataset.sortOrder || 'desc';

            // For columns where ascending is "better" (time, answer_length)
            if (defaultOrder === 'asc') {
                return this.sortDirection === 'asc' ?
                    '<i class="fas fa-arrow-down"></i>' :
                    '<i class="fas fa-arrow-up"></i>';
            } else {
                // For columns where descending is "better" (scores, rates)
                return this.sortDirection === 'asc' ?
                    '<i class="fas fa-arrow-up"</i>' :
                    '<i class="fas fa-arrow-down"></i>';
            }
        }
        return '<i class="fas fa-sort" style="opacity: 0.3;"></i>';
    }

    findMaxValues(data) {
        const maxValues = {
            partial_completion: -Infinity,
            success_rate: -Infinity,
            pass3: -Infinity
        };

        data.forEach(model => {
            if (model.info?.name === 'Human') {
                return;
            }

            const results = model[this.currentSet];

            ['partial_completion', 'success_rate', 'pass3'].forEach(metric => {
                const value = results[metric];
                if (value !== "-" && value !== null && value !== undefined) {
                    let numValue;
                    if (typeof value === 'string') {
                        numValue = parseFloat(value.replace(/[,±]/g, '').split('±')[0]);
                    } else {
                        numValue = parseFloat(value);
                    }

                    if (!isNaN(numValue) && numValue > maxValues[metric]) {
                        maxValues[metric] = numValue;
                    }
                }
            });
        });

        return maxValues;
    }

    parseValueForSorting(value, column) {
        if (value === "-" || value === null || value === undefined) {
            return null;
        }

        if (typeof value !== 'string') {
            return parseFloat(value);
        }

        if (column === 'time') {
            if (value.startsWith('<')) {
                return parseFloat(value.substring(1)) * 0.5;
            }
            if (value.startsWith('>')) {
                return parseFloat(value.substring(1)) * 1.5;
            }
            if (value.startsWith('~')) {
                return parseFloat(value.substring(1));
            }
        }

        return parseFloat(value.replace(/[,±]/g, '').split('±')[0]);
    }


    getSortedData() {
        if (!this.data || this.data.length === 0) {
            return [];
        }

        const humanData = this.data.filter(item => item.info?.name === 'Human');
        const otherData = this.data.filter(item => item.info?.name !== 'Human');

        const sortedOtherData = otherData.sort((a, b) => {
            let aValue, bValue;

            if (this.sortColumn === 'date') {
                aValue = new Date(a.info?.date || 0);
                bValue = new Date(b.info?.date || 0);
            } else {
                const rawAValue = a[this.currentSet]?.[this.sortColumn];
                const rawBValue = b[this.currentSet]?.[this.sortColumn];

                if (rawAValue === "-" && rawBValue === "-") return 0;
                if (rawAValue === "-") return this.sortDirection === 'asc' ? 1 : -1;
                if (rawBValue === "-") return this.sortDirection === 'asc' ? -1 : 1;

                aValue = this.parseValueForSorting(rawAValue, this.sortColumn);
                bValue = this.parseValueForSorting(rawBValue, this.sortColumn);
            }

            if (this.sortDirection === 'asc') {
                return aValue < bValue ? -1 : aValue > bValue ? 1 : 0;
            } else {
                return aValue > bValue ? -1 : aValue < bValue ? 1 : 0;
            }
        });

        return [...humanData, ...sortedOtherData];
    }

    setupSorting() {
        const sortableHeaders = document.querySelectorAll('.sortable-header');

        sortableHeaders.forEach(header => {
            header.addEventListener('click', () => {
                const column = header.dataset.column;
                const defaultSortOrder = header.dataset.sortOrder || 'desc';

                if (this.sortColumn === column) {
                    this.sortDirection = this.sortDirection === 'asc' ? 'desc' : 'asc';
                } else {
                    this.sortColumn = column;
                    this.sortDirection = defaultSortOrder;
                }

                this.renderTable();
            });
        });
    }

    formatMetric(value, maxValue = null, decimals = null) {
        if (value === "-" || value === null || value === undefined) {
            return '<span class="has-text-grey">-</span>';
        }

        // Parse the numeric value
        let numValue;
        if (typeof value === 'string') {
            numValue = parseFloat(value.replace(/[,±]/g, '').split('±')[0]);
        } else {
            numValue = parseFloat(value);
        }

        // Format display value with specified decimal places
        let displayValue = value;
        if (decimals !== null && !isNaN(numValue)) {
            displayValue = numValue.toFixed(decimals);
        }

        // Check if this is the max value (bold it)
        if (maxValue !== null) {
            const isMaxValue = !isNaN(numValue) && Math.abs(numValue - maxValue) < 0.001;
            if (isMaxValue) {
                return `<span class="metric-max"><strong>${displayValue}</strong></span>`;
            }
        }

        return displayValue;
    }

    setupTabs() {
        const tabItems = document.querySelectorAll('.tab-item');

        tabItems.forEach(tab => {
            tab.addEventListener('click', (e) => {
                e.preventDefault();

                tabItems.forEach(t => t.classList.remove('is-active'));
                tab.classList.add('is-active');

                this.currentSet = tab.dataset.set;
                this.renderTable();
            });
        });
    }
}

// Initialize the leaderboard when the page loads
document.addEventListener('DOMContentLoaded', function() {
    new LeaderboardTable();
});
