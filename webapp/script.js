document.addEventListener("DOMContentLoaded", () => {
    const tg = window.Telegram.WebApp;
    tg.ready();

    const form = document.getElementById("settings-form");
    const statusMessage = document.getElementById("status-message");
    let currentConfig = {};

    // Create headers with the authentication data
    const headers = new Headers();
    headers.append("X-Telegram-Auth", tg.initData);
    headers.append("Content-Type", "application/json");

    // Fetch initial config
    fetch('/api/config', { headers: headers })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(config => {
            currentConfig = config;
            // Populate form fields
            document.getElementById('chat_ids').value = config.telegram.chat_ids.join(', ');
            document.getElementById('post_time_utc').value = config.scheduler.post_time_utc;
            document.getElementById('holiday_url').value = config.scraper.holiday_url;
            document.getElementById('holiday_limit').value = config.scraper.holiday_limit;
        })
        .catch(error => {
            statusMessage.textContent = `Error loading config: ${error.message}`;
            statusMessage.style.color = 'red';
            tg.showAlert(`Failed to load settings: ${error.message}`);
        });

    // Handle form submission
    form.addEventListener("submit", (event) => {
        event.preventDefault();

        // Build the new config object from form values
        const updatedConfig = JSON.parse(JSON.stringify(currentConfig)); // Deep copy

        updatedConfig.telegram.chat_ids = document.getElementById('chat_ids').value
            .split(',')
            .map(id => id.trim())
            .filter(id => id); // Filter out empty strings

        updatedConfig.scheduler.post_time_utc = document.getElementById('post_time_utc').value;
        updatedConfig.scraper.holiday_url = document.getElementById('holiday_url').value;
        updatedConfig.scraper.holiday_limit = parseInt(document.getElementById('holiday_limit').value, 10);

        tg.MainButton.showProgress();

        fetch('/api/config', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify(updatedConfig)
        })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    tg.showAlert('Settings saved successfully! The bot will apply them shortly.');
                    tg.close();
                } else {
                    throw new Error(data.message || 'Unknown error occurred.');
                }
            })
            .catch(error => {
                tg.showAlert(`Error saving settings: ${error.message}`);
            })
            .finally(() => {
                tg.MainButton.hideProgress();
            });
    });
});
