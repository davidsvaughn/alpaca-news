# Auto-Save Feature

## Overview
The single article view now includes automatic saving functionality that preserves your field values when navigating away from the page.

## Features

### Auto-Save Triggers
- **Page Navigation**: Automatically saves when clicking "Back to Articles", "Previous", or "Next"
- **Browser Navigation**: Saves when closing the tab, refreshing, or using browser back/forward
- **Periodic Saving**: Auto-saves every 30 seconds if there are unsaved changes
- **Form Submission**: Prevents conflicts with manual save buttons

### User Experience
- **Change Detection**: Tracks all form field changes (input, select, textarea)
- **Visual Feedback**: Shows browser warning if trying to leave with unsaved changes
- **Seamless Navigation**: Automatically saves before navigating to next/previous articles
- **Conflict Prevention**: Prevents form submission while auto-save is in progress

### Technical Implementation
- **Event Listeners**: Monitors form changes, navigation clicks, and page unload
- **AJAX Saving**: Uses Fetch API to save without page reload
- **State Management**: Tracks changes and prevents duplicate saves
- **Error Handling**: Logs save status and handles failures gracefully

## How It Works

### Change Detection
```javascript
// Tracks changes on all form inputs
formInputs.forEach(input => {
    input.addEventListener('change', function() {
        hasChanges = true;
    });
    
    input.addEventListener('input', function() {
        hasChanges = true;
    });
});
```

### Auto-Save Function
```javascript
function autoSave() {
    if (hasChanges && !isSubmitting) {
        // Submit form data via AJAX
        fetch(form.action, {
            method: 'POST',
            body: currentFormData
        })
    }
}
```

### Navigation Handling
```javascript
// Intercepts navigation clicks
navigationLinks.forEach(link => {
    link.addEventListener('click', function(e) {
        if (hasChanges) {
            e.preventDefault();
            autoSave();
            // Navigate after save completes
            setTimeout(() => {
                window.location.href = this.href;
            }, 100);
        }
    });
});
```

## Benefits
- **No Data Loss**: Never lose your work when navigating
- **Faster Workflow**: No need to manually save before navigation
- **Background Saving**: Saves happen automatically without interrupting your work
- **Reliable**: Multiple save triggers ensure data is preserved
- **User-Friendly**: Clear warnings when leaving with unsaved changes

## Browser Compatibility
- Modern browsers with ES6 support
- Requires JavaScript enabled
- Uses Fetch API for AJAX requests
- Compatible with all form field types (text, select, boolean, number, date)
