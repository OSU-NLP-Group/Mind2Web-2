console.log('task-explorer.js loaded');


let taskData = {};

// Load task data from JSON file
async function loadTaskData() {
    try {
        const response = await fetch('./static/data/task_examples.json');
        taskData = await response.json();
        console.log('Task data loaded successfully:', taskData);
        populateTaskDropdown();
    } catch (error) {
        console.error('Error loading task data:', error);
        document.getElementById('taskDescription').innerHTML = 
            '<p class="has-text-danger">Error loading task data.</p>';
    }
}

// Populate the dropdown with task IDs
function populateTaskDropdown() {
    const selector = document.getElementById('taskSelector');
    
    // Clear existing options except the first one
    selector.innerHTML = '<option value="">Select a task...</option>';
    
    // Add task options
    Object.keys(taskData).forEach(taskId => {
        const option = document.createElement('option');
        option.value = taskId;
        option.textContent = taskId;
        selector.appendChild(option);
        console.log('Added option:', taskId);
    });
    
    // Add event listener for selection changes
    selector.addEventListener('change', function() {
        console.log('Dropdown changed to:', this.value);
        console.log('displayTaskDescription function exists:', typeof displayTaskDescription);
        console.log('displayTaskDescription function:', displayTaskDescription);
    
        try {
            displayTaskDescription(this.value);
        } catch (error) {
            console.error('Error calling displayTaskDescription:', error);
        }
    });
}

// Display the selected task description
function displayTaskDescription(taskId) {
    console.log('=== START displayTaskDescription ===');
    console.log('Function called with taskId:', taskId);
    console.log('typeof taskId:', typeof taskId);
    console.log('taskData object:', taskData);
    console.log('Object.keys(taskData):', Object.keys(taskData));
    
    if (taskId) {
        console.log('taskData[taskId]:', taskData[taskId]);
        if (taskData[taskId]) {
            console.log('task_description exists:', !!taskData[taskId]['task_description']);
            console.log('task_description value:', taskData[taskId]['task_description']);
        }
    }
    
    const descriptionContainer = document.getElementById('taskDescription');
    console.log('descriptionContainer found:', !!descriptionContainer);
    
    if (!taskId || !taskData[taskId] || !taskData[taskId]['task_description']) {
        console.log('Condition failed - showing default message');
        console.log('  !taskId:', !taskId);
        console.log('  !taskData[taskId]:', !taskData[taskId]);
        console.log('  !taskData[taskId]["task_description"]:', !taskData[taskId]?.['task_description']);
        
        descriptionContainer.innerHTML = 
            '<p class="has-text-grey">Select a task from the dropdown to view its description.</p>';
        descriptionContainer.classList.remove('loaded');
        console.log('=== END displayTaskDescription (default) ===');
        return;
    }
    
    console.log('SUCCESS - showing task description');
    descriptionContainer.classList.add('loaded');
    
    const description = taskData[taskId]['task_description'];
    console.log('Description to display:', description);
    
    descriptionContainer.innerHTML = `
        <div class="task-description-text">
            ${formatTaskDescription(description)}
        </div>
    `;
    console.log('=== END displayTaskDescription (success) ===');
}

// Format task description (handle line breaks, etc.)
function formatTaskDescription(description) {
    // Convert line breaks to HTML
    return description
        .replace(/\n\n/g, '</p><p>')
        .replace(/\n/g, '<br>')
        .replace(/^/, '<p>')
        .replace(/$/, '</p>');
}

// Initialize when the page loads
document.addEventListener('DOMContentLoaded', function() {
    loadTaskData();
});