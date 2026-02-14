let taskRubricData = {};
let currentRubricData = null;

// Load task and rubric data
async function loadTaskRubricData() {
    try {
        const response = await fetch('./static/data/task_examples.json');
        taskRubricData = await response.json();
        populateRubricTaskDropdown();
    } catch (error) {
        console.error('Error loading task rubric data:', error);
        document.getElementById('rubricTaskDescription').innerHTML = 
            '<p class="has-text-danger">Error loading task data.</p>';
    }
}

// Populate the dropdown with task IDs that have rubric paths
function populateRubricTaskDropdown() {
    const selector = document.getElementById('rubricTaskSelector');
    
    // Clear existing options
    selector.innerHTML = '<option value="">Select a task...</option>';
    
    // **FILTER TASKS: Only include tasks with non-null rubric_path**
    const tasksWithRubrics = Object.entries(taskRubricData).filter(([taskId, taskData]) => {
        return taskData.rubric_path !== null && taskData.rubric_path !== undefined;
    });
    
    if (tasksWithRubrics.length === 0) {
        clearRubricTree();
        return;
    }
    
    // Set default to first available task with rubric or preferred task
    const preferred_default = 'nyc_sport_event';
    const availableTaskIds = tasksWithRubrics.map(([id]) => id);
    const default_task = availableTaskIds.includes(preferred_default) ? preferred_default : availableTaskIds[0];
    
    // Add task options (only those with rubrics)
    tasksWithRubrics.forEach(([taskId, taskData]) => {
        const option = document.createElement('option');
        option.value = taskId;
        option.textContent = taskId;
        
        // Mark default as selected
        if (taskId === default_task) {
            option.selected = true;
        }
        
        selector.appendChild(option);
    });

    // Event listener
    selector.addEventListener('change', function() {
        handleTaskSelection(this.value);
    });

    // Trigger default selection
    selector.value = default_task;
    handleTaskSelection(default_task);
}

// Handle task selection
function handleTaskSelection(taskId) {
    if (!taskId) {
        clearRubricTree();
        document.getElementById('rubricTaskDescription').innerHTML = 
            '<p class="has-text-grey">Select a task to view its description.</p>';
        return;
    }

    const taskData = taskRubricData[taskId];
    if (!taskData) {
        console.error('Task not found:', taskId);
        return;
    }

    // **CHECK IF RUBRIC PATH EXISTS**
    if (!taskData.rubric_path) {
        clearRubricTree();
        document.getElementById('rubricTaskDescription').innerHTML = 
            '<p class="has-text-warning">This task has no evaluation rubric available.</p>';
        return;
    }

    // Update task description
    const description = taskData.task_description || 'No description available.';
    document.getElementById('rubricTaskDescription').innerHTML = formatTaskDescription(description);

    // Load and display the rubric
    loadAndDisplayRubric(taskData.rubric_path);
}

// Display the selected task description
function displayRubricTaskDescription(taskId) {
    const descriptionContainer = document.getElementById('rubricTaskDescription');
    
    if (!taskId || !taskRubricData[taskId]) {
        descriptionContainer.innerHTML = 
            '<p class="has-text-grey">Select a task from the dropdown to view its description and rubric evaluation.</p>';
        descriptionContainer.classList.remove('loaded');
        descriptionContainer.classList.add('empty');
        return;
    }
    
    descriptionContainer.classList.remove('empty');
    descriptionContainer.classList.add('loaded');
    
    const description = taskRubricData[taskId].task_description;
    descriptionContainer.innerHTML = `
        <div class="task-description-text">
            ${formatTaskDescription(description)}
        </div>
    `;
}

// Load and display rubric from JSON file
async function loadAndDisplayRubric(rubricPath) {
    try {
        const response = await fetch(rubricPath);
        
        if (!response.ok) {
            throw new Error(`Failed to load rubric: ${response.status}`);
        }
        
        const rubricJson = await response.json();
        
        // Extract the verification tree from the JSON structure
        if (rubricJson.eval_breakdown && rubricJson.eval_breakdown[0]?.verification_tree) {
            currentRubricData = rubricJson.eval_breakdown[0].verification_tree;
            createDynamicRubricTree(currentRubricData);
        } else {
            throw new Error('Invalid rubric JSON structure');
        }
        
    } catch (error) {
        console.error('Error loading rubric:', error);
        document.getElementById('rubricTree').innerHTML = 
            '<p class="has-text-danger">Error loading rubric data.</p>';
    }
}

// Clear the rubric tree
function clearRubricTree() {
    document.getElementById('rubricTree').innerHTML = 
        '<p class="has-text-grey">Select a task to view its evaluation rubric.</p>';
}

// Format task description (handle line breaks, etc.)
function formatTaskDescription(description) {
    return description
        .replace(/\n\n/g, '</p><p>')
        .replace(/\n/g, '<br>')
        .replace(/^/, '<p>')
        .replace(/$/, '</p>');
}

// Create dynamic rubric tree from JSON data
function createDynamicRubricTree(rubricData, containerId = 'rubricTree') {
    // Check if container exists
    const container = document.getElementById(containerId);
    if (!container) {
        console.error('Container not found:', containerId);
        return;
    }
    
    // Check if D3 is available
    if (typeof d3 === 'undefined') {
        console.error('D3 is not loaded!');
        return;
    }
    
    // Clear any existing content
    d3.select(`#${containerId}`).selectAll("*").remove();
    
    const maxNodesAtLevel = calculateMaxNodesAtLevel(rubricData);
    
    const margin = {top: 20, right: 500, bottom: 30, left: 500};
    const width = 800;
    
    // **DYNAMIC HEIGHT: 50-60px per node minimum**
    const minHeight = 600;
    const nodeSpacing = 60; 
    const height = Math.max(minHeight, maxNodesAtLevel * nodeSpacing);

    // **STORE THE ACTUAL SVG DIMENSIONS**
    const svgWidth = width + margin.right + margin.left;
    const svgHeight = height + margin.top + margin.bottom;

    // Create SVG with zoom behavior
    const svg = d3.select(`#${containerId}`)
        .append("svg")
        .attr("width", "100%")
        .attr("height", "1000px") // Fixed container height
        .attr("viewBox", `0 0 ${svgWidth} ${svgHeight}`)
        .attr("preserveAspectRatio", "xMidYMid meet")
        .style("cursor", "grab")
        .style("border", "1px solid #ddd");

    // Create a group for the tree content that will be transformed
    const g = svg.append("g")
        .attr("transform", `translate(${margin.left},${margin.top})`);

    // Define zoom behavior
    const zoom = d3.zoom()
        .scaleExtent([0.1, 30]) // Allow zoom from 30% to 300%
        .on("zoom", function(event) {
            g.attr("transform", event.transform);
        });

    // Apply zoom behavior to SVG
    svg.call(zoom);

    // Add zoom controls (optional - buttons for better UX)
    const controls = d3.select(`#${containerId}`)
        .append("div")
        .attr("class", "zoom-controls")
        .style("position", "absolute")
        .style("top", "10px")
        .style("left", "10px")
        .style("z-index", "1000");

    // Zoom in button
    controls.append("button")
        .attr("class", "zoom-btn zoom-in")
        .text("+")
        .style("margin", "2px")
        .style("padding", "5px 10px")
        .style("background", "#007bff")
        .style("color", "white")
        .style("border", "none")
        .style("border-radius", "3px")
        .style("cursor", "pointer")
        .on("click", function() {
            svg.transition().duration(300).call(
                zoom.scaleBy, 1.5
            );
        });

    // Zoom out button
    controls.append("button")
        .attr("class", "zoom-btn zoom-out")
        .text("-")
        .style("margin", "2px")
        .style("padding", "5px 12px")
        .style("background", "#007bff")
        .style("color", "white")
        .style("border", "none")
        .style("border-radius", "3px")
        .style("cursor", "pointer")
        .on("click", function() {
            svg.transition().duration(300).call(
                zoom.scaleBy, 1/1.5
            );
        });

    // Reset zoom button
    controls.append("button")
        .attr("class", "zoom-btn zoom-reset")
        .text("Reset")
        .style("margin", "2px")
        .style("padding", "5px 10px")
        .style("background", "#6c757d")
        .style("color", "white")
        .style("border", "none")
        .style("border-radius", "3px")
        .style("cursor", "pointer")
        .on("click", function() {
            centerTree(); // Use the same centering logic
        });


    controls.append("span")
        .attr("class", "help-text")
        .text("Critical nodes are colored in blue")
        .style("margin-left", "10px")
        .style("font-size", "12px")
        .style("color", "#666")
        .style("vertical-align", "middle")
        .style("font-family", "inherit")
        .style("text-align", "center")
        .style("display", "inline-block");

    // Tooltip for node details
    const tooltip = d3.select("body").append("div")
        .attr("class", "custom-tooltip")
        .style("position", "absolute")
        .style("background", "rgba(0, 0, 0, 0.8)")
        .style("color", "white")
        .style("padding", "8px 12px")
        .style("border-radius", "4px")
        .style("font-size", "14px")
        .style("font-family", "inherit")
        .style("pointer-events", "none")
        .style("opacity", 0)
        .style("z-index", 10000);

    // Convert JSON structure to D3 hierarchy format
    const hierarchyData = convertToHierarchy(rubricData);
    
    // Create tree layout
    const tree = d3.tree().size([height, width]);
    const root = d3.hierarchy(hierarchyData, d => d.children);
    
    root.x0 = height / 2;
    root.y0 = 0;

    function expandAll(d) {
        if (d._children) {
            d.children = d._children;
            d._children = null;
        }
        if (d.children) {
            d.children.forEach(expandAll);
        }
    }

    expandAll(root);

    // Initialize the tree with proper centering
    update(root);
    
    // Center the tree initially
    setTimeout(() => {
        centerTree();
    }, 500);

    function centerTree() {
        setTimeout(() => {
            try {
                const bounds = g.node().getBBox();
                
                if (bounds.width === 0 || bounds.height === 0) {
                    setTimeout(() => centerTree(), 200);
                    return;
                }
                
                // **USE THE STORED ACTUAL DIMENSIONS**
                const containerWidth = svgWidth;
                const containerHeight = svgHeight;
                
                // Calculate scale to fit the entire tree with padding
                const padding = 40; // Increased padding
                const availableWidth = containerWidth - padding * 2;
                const availableHeight = containerHeight - padding * 2;
                
                const scaleX = availableWidth / bounds.width;
                const scaleY = availableHeight / bounds.height;
                const scale = Math.min(scaleX, scaleY, 0.9); // Max 90% to ensure buffer


                // Calculate translation to center the scaled tree
                const scaledWidth = bounds.width * scale;
                const scaledHeight = bounds.height * scale;
                
                const translateX = (containerWidth - scaledWidth) / 2 - bounds.x * scale;
                const translateY = (containerHeight - scaledHeight) / 2 - bounds.y * scale;
                
                svg.transition()
                    .duration(750)
                    .call(zoom.transform, d3.zoomIdentity.translate(translateX, translateY).scale(scale));
                    
            } catch (error) {
                console.error('Error centering tree:', error);
            }
        }, 300);
    }

    function update(source) {
        // Compute the new tree layout
        const treeData = tree(root);
        const nodes = treeData.descendants();
        const links = treeData.descendants().slice(1);

        // Normalize for fixed-depth
        const levelSpacing = 400; // Increased from 180
        nodes.forEach(d => d.y = d.depth * levelSpacing);

        // Update nodes
        const node = g.selectAll('g.node')
            .data(nodes, d => d.id || (d.id = ++nodeIdCounter));

        // Enter new nodes
        const nodeEnter = node.enter().append('g')
            .attr('class', 'node')
            .attr('transform', d => `translate(${source.y0 || 0},${source.x0 || 0})`)
            .on('click', function(event, d) { 
                // Prevent zoom when clicking on nodes
                event.stopPropagation();
                click(event, d); 
            });

        // Add circles for nodes with status-based coloring
        nodeEnter.append('circle')
            .attr('class', 'node-circle')
            .attr('r', 1e-6)
            .style('fill', d => getNodeColor(d.data))
            .style('stroke', d => getNodeStrokeColor(d.data))
            .style('stroke-width', '2px')
            .style('cursor', 'pointer');

        // Add labels for nodes
        nodeEnter.append('text')
            .attr('class', 'node-text')
            .attr('dy', '.35em')
            .attr('x', d => d.children || d._children ? -13 : 13)
            .attr('text-anchor', d => d.children || d._children ? 'end' : 'start')
            .text(d => truncateText(d.data.desc, 30))
            .style('fill-opacity', 1e-6)
            .style('font-size', '25px')
            .style('font-family', 'inherit')
            .style('cursor', 'pointer')
            .style('pointer-events', 'none'); // Prevent text from blocking click events

        nodeEnter.on("mouseover", function(event, d) {
                const data = d.data;
                
                // Show tooltip instantly (no transition)
                tooltip
                    .style("opacity", 1)
                    .html(`
                        <p>${data.desc}</p> <br> Critical: ${data.critical ? 'Yes' : 'No'}
                    `)
                    .style("left", (event.pageX + 10) + "px")
                    .style("top", (event.pageY - 10) + "px");
            })
            .on("mouseout", function(d) {
                // Hide tooltip instantly
                tooltip.style("opacity", 0);
            });

        // Transition nodes to their new position
        const nodeUpdate = nodeEnter.merge(node);

        nodeUpdate.transition()
            .duration(500)
            .attr('transform', d => `translate(${d.y},${d.x})`);

        nodeUpdate.select('circle.node-circle')
            .attr('r', 8)
            .style('fill', d => getNodeColor(d.data))
            .style('stroke', d => getNodeStrokeColor(d.data));

        nodeUpdate.select('text.node-text')
            .style('fill-opacity', 1);

        // Remove exiting nodes
        const nodeExit = node.exit().transition()
            .duration(500)
            .attr('transform', d => `translate(${source.y},${source.x})`)
            .remove();

        nodeExit.select('circle')
            .attr('r', 1e-6);

        nodeExit.select('text')
            .style('fill-opacity', 1e-6);

        // Update links
        const link = g.selectAll('path.link')
            .data(links, d => d.id);

        // Enter new links
        const linkEnter = link.enter().insert('path', 'g')
            .attr('class', 'link')
            .attr('d', d => {
                const o = {x: source.x0 || 0, y: source.y0 || 0};
                return diagonal(o, o);
            })
            .style('fill', 'none')
            .style('stroke', '#ccc')
            .style('stroke-width', '2px');

        // Transition links to their new position
        const linkUpdate = linkEnter.merge(link);

        linkUpdate.transition()
            .duration(500)
            .attr('d', d => diagonal(d, d.parent));

        // Remove exiting links
        link.exit().transition()
            .duration(500)
            .attr('d', d => {
                const o = {x: source.x, y: source.y};
                return diagonal(o, o);
            })
            .remove();

        // Store the old positions for transition
        nodes.forEach(d => {
            d.x0 = d.x;
            d.y0 = d.y;
        });
    }

    // Creates a curved path from parent to child
    function diagonal(s, d) {
        return `M ${s.y} ${s.x}
                C ${(s.y + d.y) / 2} ${s.x},
                  ${(s.y + d.y) / 2} ${d.x},
                  ${d.y} ${d.x}`;
    }

    // Toggle children on click
    function click(event, d) {
        if (d.children) {
            d._children = d.children;
            d.children = null;
        } else {
            d.children = d._children;
            d._children = null;
        }
        update(d);
    }
}


function calculateMaxNodesAtLevel(node, level = 0, levelCounts = {}) {
    levelCounts[level] = (levelCounts[level] || 0) + 1;
    
    if (node.children && node.children.length > 0) {
        node.children.forEach(child => {
            calculateMaxNodesAtLevel(child, level + 1, levelCounts);
        });
    }
    
    return Math.max(...Object.values(levelCounts));
}

// Convert JSON structure to D3 hierarchy format
function convertToHierarchy(node) {
    const converted = {
        name: node.id || 'Unknown',
        desc: node.desc || '',
        id: node.id,
        status: node.status,
        score: node.score,
        strategy: node.strategy,
        critical: node.critical
    };
    
    if (node.children && node.children.length > 0) {
        converted.children = node.children.map(child => convertToHierarchy(child));
    }
    
    return converted;
}

// Get node color based on status
function getNodeColor(data) {    
    switch (data.critical) {
        case true: return '#d6d6d6';
        case false: return '#d6d6d6';
    }
}

// Get node stroke color based on status
function getNodeStrokeColor(data) {    
    switch (data.critical) {
        case true: return '#59b5ff';
        case false: return '#666666';
    }
}

// Truncate text to fit in nodes
function truncateText(text, maxLength) {
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength - 3) + '...';
}

let nodeIdCounter = 0; // Counter for node IDs

// Initialize when the page loads
document.addEventListener('DOMContentLoaded', function() {
    setTimeout(() => {
        loadTaskRubricData();
    }, 500);
});