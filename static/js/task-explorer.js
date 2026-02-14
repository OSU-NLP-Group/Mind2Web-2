let taskData = {};

// Load task data from JSON file
async function loadTaskData() {
    try {
        const response = await fetch('./static/data/task_examples.json');
        taskData = await response.json();
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
    selector.innerHTML = '<option value="">Select a task...</option>';

    Object.keys(taskData).forEach(taskId => {
        const option = document.createElement('option');
        option.value = taskId;
        option.textContent = taskId;
        selector.appendChild(option);
    });

    selector.addEventListener('change', function() {
        displayTaskDescription(this.value);
    });
}

// Display the selected task description
function displayTaskDescription(taskId) {
    const descriptionContainer = document.getElementById('taskDescription');

    if (!taskId || !taskData[taskId] || !taskData[taskId]['task_description']) {
        descriptionContainer.innerHTML =
            '<p class="has-text-grey">Select a task from the dropdown to view its description.</p>';
        descriptionContainer.classList.remove('loaded');
        return;
    }

    descriptionContainer.classList.add('loaded');
    const description = taskData[taskId]['task_description'];

    descriptionContainer.innerHTML = `
        <div class="task-description-text">
            ${formatTaskDescription(description)}
        </div>
    `;
}

// Format task description (handle line breaks, etc.)
function formatTaskDescription(description) {
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
