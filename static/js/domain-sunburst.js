// Domain distribution data
const domainData = {
    'Domain': [
        'Lifestyle & Leisure','Lifestyle & Leisure','Lifestyle & Leisure','Lifestyle & Leisure',
        'Lifestyle & Leisure','Lifestyle & Leisure','Lifestyle & Leisure','Career & Education',
        'Career & Education','Science & Research','Science & Research','Entertainment',
        'Entertainment','Entertainment','Entertainment','Entertainment','Travel & Transportation',
        'Travel & Transportation','Travel & Transportation','Misc.','Misc.','Misc.','Misc.','Misc.'
    ],
    'Sub-domain': [
        'Sports & Fitness','Health & Medicine','Shopping','Food & Cooking',
        'Pets & Animal Welfare','Fashion & Beauty','Hobbies & DIY','Jobs & Career',
        'Education & Learning','Technology & Science','Research & Academia','Music',
        'Books & Reading','Films & TV Shows','Live Shows & Performances',
        'Gaming & Virtual Worlds','Outdoor & Recreation','Ticketed Activities',
        'Travel & Accommodation','News','General Info.','Real Estate',
        'Finance & Investment','Legal & Government Services'
    ],
    'Count':  [6,4,10,6,4,3,1,5,6,9,14,2,2,12,4,8,1,1,7,4,13,3,2,3]
};

// Process data for sunburst chart
function processDataForSunburst(data) {
    const total = data.Count.reduce((a, b) => a + b, 0);
    
    // Pastel color palette
    const pastelColors = [
        '#f89c74',  // Light pink
        '#a3b8ee',  // Light blue
        '#88c560',  // Light green
        '#d5b2ee',  // Light purple
        '#f6cf71',  // Light yellow
        '#66c5cc',  // Light magenta
    ];
    
    // Create domain totals and color mapping
    const domainTotals = {};
    const uniqueDomains = [...new Set(data.Domain)];
    const domainColorMap = {};
    
    uniqueDomains.forEach((domain, i) => {
        domainColorMap[domain] = pastelColors[i % pastelColors.length];
        domainTotals[domain] = 0;
    });
    
    data.Domain.forEach((domain, i) => {
        domainTotals[domain] += data.Count[i];
    });

    const labels = [];
    const parents = [];
    const values = [];
    const ids = [];
    const colors = [];

    // Add domain entries
    Object.keys(domainTotals).forEach(domain => {
        const percentage = Math.round(domainTotals[domain] / total * 100); // Calculate percentage for display, not used atm
        labels.push(`${domain}`);
        parents.push('');
        values.push(domainTotals[domain]);
        ids.push(domain);
        colors.push(domainColorMap[domain]);
    });

    // Add subdomain entries
    data['Sub-domain'].forEach((subdomain, i) => {
        const percentage = Math.round(data.Count[i] / total * 100);  // Calculate percentage for display, not used atm
        labels.push(`${subdomain}`);
        parents.push(data.Domain[i]);
        values.push(data.Count[i]);
        ids.push(`${data.Domain[i]}-${subdomain}`);
        colors.push(domainColorMap[data.Domain[i]]);
    });

    return { labels, parents, values, ids, colors };
}

// Create sunburst chart
function createSunburstChart(containerId = 'sunburstChart') {
    const { labels, parents, values, ids, colors } = processDataForSunburst(domainData);

    const data = [{
        type: "sunburst",
        labels: labels,
        parents: parents,
        values: values,
        ids: ids,
        branchvalues: "total",
        hovertemplate: '<b>%{label}</b><br>Count: %{value}<br><extra></extra>',
        maxdepth: 2,
        insidetextorientation: 'radial',
        textfont: { size: 12 },
        marker: {
            colors: colors,  // Use the colors array
            line: { width: 2, color: '#FFFFFF' }
        }
    }];

    const layout = {
        margin: { t: 0, l: 0, r: 0, b: 0 },
        font: { size: 12 },
        showlegend: false,
        paper_bgcolor: 'rgba(0,0,0,0)',  // Transparent paper background
        plot_bgcolor: 'rgba(0,0,0,0)',    // Transparent plot background
        autosize: true,
        // width: 500,   // Explicit size
        // height: 500
    };

    const config = {
        responsive: true,
        displayModeBar: false
    };

    Plotly.newPlot(containerId, data, layout, config);
}

// Initialize charts when DOM is loaded
function initializeCharts() {
    // Check if Plotly is loaded
    if (typeof Plotly === 'undefined') {
        console.error('Plotly.js is not loaded. Please include it in your HTML.');
        return;
    }

    // Check if chart container exists
    const chartContainer = document.getElementById('sunburstChart');
    if (chartContainer) {
        createSunburstChart();
    }
}

// Export functions for potential use elsewhere
window.ChartUtils = {
    createSunburstChart,
    domainData,
    processDataForSunburst
};

// Auto-initialize when DOM is ready
document.addEventListener('DOMContentLoaded', initializeCharts);