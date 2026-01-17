// API base URL - set by index.html script tag or defaults to localhost
const API_BASE = (typeof window !== 'undefined' && window.API_BASE_URL) 
    ? window.API_BASE_URL 
    : 'http://localhost:5000/api';

// Valorant-themed loading messages
const VALORANT_MESSAGES = [
    "Spike planted... analyzing stats",
    "Clutch or kick... fetching data",
    "Ace incoming... processing players",
    "One tap... loading slate",
    "Frag out... scraping VLR.gg",
    "GG EZ... almost done",
    "Sit... gathering intel",
    "Diff... comparing stats",
    "Jett diff... loading matches",
    "Raze ult... processing data",
    "Sova dart... finding players",
    "Omen TP... fetching stats",
    "Reyna dismiss... analyzing",
    "Sage res... loading results",
    "Brimstone ult... finalizing"
];

let messageInterval = null;
let progressPollInterval = null;
let currentJobId = null;

function showLoading() {
    document.getElementById('loading').classList.remove('hidden');
    document.getElementById('error').classList.add('hidden');
    document.getElementById('results').innerHTML = '';
    document.getElementById('loadingDetails').classList.add('hidden');
    document.getElementById('toggleDetails').textContent = 'Show Details';
    // Disable buttons during loading
    document.getElementById('loadSlate').disabled = true;
    document.getElementById('searchPlayer').disabled = true;
    
    // Start rotating Valorant messages
    startMessageRotation();
    updateProgress(0, 0, []);
}

function hideLoading() {
    document.getElementById('loading').classList.add('hidden');
    // Stop message rotation and progress polling
    stopMessageRotation();
    stopProgressPolling();
    // Re-enable buttons
    document.getElementById('loadSlate').disabled = false;
    document.getElementById('searchPlayer').disabled = false;
}

function startMessageRotation() {
    let messageIndex = 0;
    const messageEl = document.getElementById('loadingMessage');
    
    // Update immediately
    messageEl.textContent = VALORANT_MESSAGES[messageIndex];
    
    messageInterval = setInterval(() => {
        messageIndex = (messageIndex + 1) % VALORANT_MESSAGES.length;
        messageEl.textContent = VALORANT_MESSAGES[messageIndex];
    }, 2000); // Change message every 2 seconds
}

function stopMessageRotation() {
    if (messageInterval) {
        clearInterval(messageInterval);
        messageInterval = null;
    }
}

function updateProgress(current, total, details) {
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    const progressDetails = document.getElementById('progressDetails');
    
    const percent = total > 0 ? Math.round((current / total) * 100) : 0;
    progressFill.style.width = `${percent}%`;
    progressText.textContent = `${percent}%`;
    
    // Update details - show only the most recent/last detail
    if (details && details.length > 0) {
        const latestDetail = details[details.length - 1]; // Get the last/most recent detail
        progressDetails.innerHTML = `<div class="detail-item">${latestDetail}</div>`;
    }
}

function startProgressPolling(jobId) {
    currentJobId = jobId;
    
    // Use Server-Sent Events for real-time updates
    const eventSource = new EventSource(`${API_BASE}/progress/${jobId}`);
    
    eventSource.onmessage = (event) => {
        try {
            const progress = JSON.parse(event.data);
            updateProgress(progress.current, progress.total, progress.details);
            
            if (progress.status === 'complete') {
                eventSource.close();
                stopProgressPolling();
                // Display the result
                if (progress.result) {
                    displaySlate(progress.result);
                } else {
                    showError('Processing complete but no data received');
                }
            } else if (progress.status === 'error') {
                eventSource.close();
                stopProgressPolling();
                showError(progress.details && progress.details.length > 0 ? progress.details[0] : 'An error occurred during processing');
            }
        } catch (error) {
            // Silently fail - progress is optional
        }
    };
    
    eventSource.onerror = (error) => {
        // Don't close on first error - might be temporary
        // Only close if connection is actually closed
        if (eventSource.readyState === EventSource.CLOSED) {
            eventSource.close();
            stopProgressPolling();
        }
    };
    
    // Store event source for cleanup
    progressPollInterval = eventSource;
}

function stopProgressPolling() {
    if (progressPollInterval) {
        if (progressPollInterval.close) {
            progressPollInterval.close(); // EventSource
        } else {
            clearInterval(progressPollInterval); // Fallback for interval
        }
        progressPollInterval = null;
    }
    currentJobId = null;
}

function showError(message) {
    const errorDiv = document.getElementById('error');
    errorDiv.textContent = message;
    errorDiv.classList.remove('hidden');
    hideLoading();
}

function hasDisplayedData() {
    const resultsDiv = document.getElementById('results');
    const content = resultsDiv.innerHTML.trim();
    // Check if there's actual data displayed (tables, player stats, etc.)
    // Exclude empty states and error messages
    if (content.length === 0) {
        return false;
    }
    // Check if it's an empty state message
    if (content.includes('empty-state') || 
        content.includes('No players found') ||
        content.includes('No pick\'em slate') ||
        content.includes('No players found on Underdog')) {
        return false;
    }
    // If there's content and it's not an empty state, assume there's data
    return true;
}

function confirmClearData(action) {
    if (!hasDisplayedData()) {
        return true; // No data to clear, proceed without confirmation
    }
    return confirm('This will remove currently displayed data. Continue?');
}

function cleanTeamName(teamName) {
    if (!teamName || teamName === 'N/A') {
        return teamName;
    }
    // Remove patterns like " (1)", " (2)", etc. from the end of team names
    return teamName.replace(/\s*\(\d+\)\s*$/, '').trim();
}

function formatStat(value, line) {
    if (value === null || value === undefined) {
        return '<span class="stat-cell stat-na">N/A</span>';
    }
    
    const numValue = parseFloat(value);
    const numLine = parseFloat(line);
    
    if (isNaN(numValue) || isNaN(numLine)) {
        return `<span class="stat-cell">${value}</span>`;
    }
    
    if (numValue > numLine) {
        return `<span class="stat-cell stat-above">${numValue}</span>`;
    } else if (numValue < numLine) {
        return `<span class="stat-cell stat-below">${numValue}</span>`;
    } else {
        return `<span class="stat-cell stat-equal">${numValue}</span>`;
    }
}

function displaySlate(data) {
    const resultsDiv = document.getElementById('results');
    
    // Check for no slate message
    if (data.message) {
        resultsDiv.innerHTML = `<div class="empty-state">${data.message}</div>`;
        hideLoading();
        return;
    }
    
    if (!data.players || data.players.length === 0) {
        resultsDiv.innerHTML = '<div class="empty-state">No players found in the slate.</div>';
        hideLoading();
        return;
    }
    
    // Organize by match if we have players_by_match data, otherwise fall back to team grouping
    const playersByMatch = data.players_by_match || {};
    let html = '<div class="table-container">';
    html += '<table>';
    html += '<thead>';
    html += '<tr>';
    html += '<th>Player</th>';
    html += '<th>Team</th>';
    html += '<th>Line</th>';
    html += '<th>Last 5 Avg</th>';
    html += '<th>Last 10 Avg</th>';
    html += '<th>Last 25 Avg</th>';
    html += '<th>Status</th>';
    html += '</tr>';
    html += '</thead>';
    html += '<tbody>';
    
    if (Object.keys(playersByMatch).length > 0) {
        // Organize by match: for each match, show team1 players, then team2 players
        Object.keys(playersByMatch).forEach(matchKey => {
            const matchData = playersByMatch[matchKey];
            const teams = matchData.teams || [];
            const players = matchData.players || [];
            
            if (teams.length >= 2) {
                // Backend has already organized players by team correctly
                // Just display them in the order they come (team1 players first, then team2 players)
                players.forEach(player => {
                    const hasError = player.error;
                    const rowClass = hasError ? 'error-row' : '';
                    html += `<tr class="${rowClass}">`;
                    // Player name as link
                    if (player.vlr_url) {
                        html += `<td><strong><a href="${player.vlr_url}" target="_blank" style="color: inherit; text-decoration: none;">${player.player}</a></strong></td>`;
                    } else {
                        html += `<td><strong>${player.player}</strong></td>`;
                    }
                    // Team name as link - always use player.team from API (player's actual team)
                    if (player.team_url) {
                        html += `<td><a href="${player.team_url}" target="_blank" style="color: inherit; text-decoration: underline;">${cleanTeamName(player.team) || 'N/A'}</a></td>`;
                    } else {
                        html += `<td>${cleanTeamName(player.team) || 'N/A'}</td>`;
                    }
                    html += `<td class="stat-cell">${player.line || 'N/A'}</td>`;
                    html += `<td>${formatStat(player.avg_last_5, player.line)}</td>`;
                    html += `<td>${formatStat(player.avg_last_10, player.line)}</td>`;
                    html += `<td>${formatStat(player.avg_last_25, player.line)}</td>`;
                    if (hasError) {
                        html += `<td class="error-cell" title="${player.error}">⚠️</td>`;
                    } else {
                        html += `<td class="success-cell">✓</td>`;
                    }
                    html += '</tr>';
                });
            } else {
                // Single team or other - just display all players grouped by team
                // Group by team first
                const playersByTeam = {};
                players.forEach(player => {
                    const team = player.team || 'Other';
                    if (!playersByTeam[team]) {
                        playersByTeam[team] = [];
                    }
                    playersByTeam[team].push(player);
                });
                
                // Display each team's players together
                Object.keys(playersByTeam).forEach(team => {
                    playersByTeam[team].forEach(player => {
                        const hasError = player.error;
                        const rowClass = hasError ? 'error-row' : '';
                        html += `<tr class="${rowClass}">`;
                        // Player name as link
                        if (player.vlr_url) {
                            html += `<td><strong><a href="${player.vlr_url}" target="_blank" style="color: inherit; text-decoration: none;">${player.player}</a></strong></td>`;
                        } else {
                            html += `<td><strong>${player.player}</strong></td>`;
                        }
                        // Team name as link - always use player.team from API
                        if (player.team_url) {
                            html += `<td><a href="${player.team_url}" target="_blank" style="color: inherit; text-decoration: underline;">${cleanTeamName(player.team) || 'N/A'}</a></td>`;
                        } else {
                            html += `<td>${cleanTeamName(player.team) || 'N/A'}</td>`;
                        }
                        html += `<td class="stat-cell">${player.line || 'N/A'}</td>`;
                        html += `<td>${formatStat(player.avg_last_5, player.line)}</td>`;
                        html += `<td>${formatStat(player.avg_last_10, player.line)}</td>`;
                        html += `<td>${formatStat(player.avg_last_25, player.line)}</td>`;
                        if (hasError) {
                            html += `<td class="error-cell" title="${player.error}">⚠️</td>`;
                        } else {
                            html += `<td class="success-cell">✓</td>`;
                        }
                        html += '</tr>';
                    });
                });
            }
        });
    } else {
        // Fall back to team-based organization if no match teams
        const playersByTeam = {};
        const playersWithoutTeam = [];
        
        data.players.forEach(player => {
            const team = player.team || 'Unknown';
            if (team === 'Unknown' || team === 'N/A' || !team) {
                playersWithoutTeam.push(player);
            } else {
                if (!playersByTeam[team]) {
                    playersByTeam[team] = [];
                }
                playersByTeam[team].push(player);
            }
        });
        
        const sortedTeams = Object.keys(playersByTeam).sort((a, b) => {
            return playersByTeam[b].length - playersByTeam[a].length;
        });
        
        sortedTeams.forEach(team => {
            playersByTeam[team].forEach(player => {
                const hasError = player.error;
                const rowClass = hasError ? 'error-row' : '';
                html += `<tr class="${rowClass}">`;
                // Player name as link
                if (player.vlr_url) {
                    html += `<td><strong><a href="${player.vlr_url}" target="_blank" style="color: inherit; text-decoration: none;">${player.player}</a></strong></td>`;
                } else {
                    html += `<td><strong>${player.player}</strong></td>`;
                }
                // Team name as link - always use player.team from API
                if (player.team_url) {
                    html += `<td><a href="${player.team_url}" target="_blank" style="color: inherit; text-decoration: underline;">${cleanTeamName(player.team) || 'N/A'}</a></td>`;
                } else {
                    html += `<td>${cleanTeamName(player.team) || 'N/A'}</td>`;
                }
                html += `<td class="stat-cell">${player.line || 'N/A'}</td>`;
                html += `<td>${formatStat(player.avg_last_5, player.line)}</td>`;
                html += `<td>${formatStat(player.avg_last_10, player.line)}</td>`;
                html += `<td>${formatStat(player.avg_last_25, player.line)}</td>`;
                if (hasError) {
                    html += `<td class="error-cell" title="${player.error}">⚠️</td>`;
                } else {
                    html += `<td class="success-cell">✓</td>`;
                }
                html += '</tr>';
            });
        });
        
        playersWithoutTeam.forEach(player => {
            const hasError = player.error;
            const rowClass = hasError ? 'error-row' : '';
            html += `<tr class="${rowClass}">`;
            // Player name as link
            if (player.vlr_url) {
                html += `<td><strong><a href="${player.vlr_url}" target="_blank" style="color: inherit; text-decoration: none;">${player.player}</a></strong></td>`;
            } else {
                html += `<td><strong>${player.player}</strong></td>`;
            }
            // Team name as link
            if (player.team_url) {
                html += `<td><a href="${player.team_url}" target="_blank" style="color: inherit; text-decoration: underline;">${cleanTeamName(player.team) || 'N/A'}</a></td>`;
            } else {
                html += `<td>${cleanTeamName(player.team) || 'N/A'}</td>`;
            }
            html += `<td class="stat-cell">${player.line || 'N/A'}</td>`;
            html += `<td>${formatStat(player.avg_last_5, player.line)}</td>`;
            html += `<td>${formatStat(player.avg_last_10, player.line)}</td>`;
            html += `<td>${formatStat(player.avg_last_25, player.line)}</td>`;
            if (hasError) {
                html += `<td class="error-cell" title="${player.error}">⚠️</td>`;
            } else {
                html += `<td class="success-cell">✓</td>`;
            }
            html += '</tr>';
        });
    }
    
    html += '</tbody>';
    html += '</table>';
    html += '</div>';
    
    resultsDiv.innerHTML = html;
    hideLoading();
}

async function loadSlate() {
    if (!confirmClearData('load slate')) {
        return; // User cancelled
    }
    
    showLoading();
    updateProgress(0, 1, ['Connecting to Underdog API...']);
    
    try {
        const response = await fetch(`${API_BASE}/slate`);
        const data = await response.json();
        
        if (!response.ok) {
            const errorMsg = data.error || data.details || `HTTP error! status: ${response.status}`;
            throw new Error(errorMsg);
        }
        
        if (data.error && !data.message) {
            throw new Error(data.error);
        }
        
        // If we got a job_id, poll for progress and result
        if (data.job_id) {
            startProgressPolling(data.job_id);
        } else if (data.players) {
            // Fallback: if we got data directly (no job_id), display it
            displaySlate(data);
        } else {
            throw new Error('No data received');
        }
    } catch (error) {
        showError(`Failed to load slate: ${error.message}`);
    }
}

async function searchPlayer() {
    const playerName = document.getElementById('playerSearch').value.trim();
    if (!playerName) {
        showError('Please enter a player name');
        return;
    }
    
    if (!confirmClearData('search player')) {
        return; // User cancelled
    }
    
    showLoading();
    
    try {
        const response = await fetch(`${API_BASE}/player/${encodeURIComponent(playerName)}`);
        if (!response.ok) {
            if (response.status === 404) {
                throw new Error('Player not found on VLR.gg');
            }
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        displayPlayerStats(data);
    } catch (error) {
        showError(`Failed to load player stats: ${error.message}`);
    }
}

function displayPlayerStats(data) {
    const resultsDiv = document.getElementById('results');
    
    let html = '<div class="table-container">';
    html += '<div class="player-header">';
    // Make player name a link to their VLR profile
    if (data.vlr_url) {
        html += `<h2><a href="${data.vlr_url}" target="_blank" style="color: inherit; text-decoration: none; font-weight: bold;">${data.player}</a></h2>`;
    } else {
        html += `<h2 style="font-weight: bold;">${data.player}</h2>`;
    }
    html += `<div>`;
    if (data.team) {
        // Make team name a link to team's VLR page
        if (data.team_url) {
            html += `<span style="margin-right: 20px;">Team: <strong><a href="${data.team_url}" target="_blank" style="color: inherit; text-decoration: underline;">${cleanTeamName(data.team)}</a></strong></span>`;
        } else {
            html += `<span style="margin-right: 20px;">Team: <strong>${cleanTeamName(data.team)}</strong></span>`;
        }
    }
    html += '</div>';
    html += '</div>';
    
    html += '<table>';
    html += '<thead>';
    html += '<tr>';
    html += '<th>Stat</th>';
    html += '<th>Last 5</th>';
    html += '<th>Last 10</th>';
    html += '<th>Last 25</th>';
    html += '</tr>';
    html += '</thead>';
    html += '<tbody>';
    html += '<tr>';
    html += '<td><strong>Average Kills on Map 1+2</strong></td>';
    html += `<td>${data.averages.last_5 || 'N/A'}</td>`;
    html += `<td>${data.averages.last_10 || 'N/A'}</td>`;
    html += `<td>${data.averages.last_25 || 'N/A'}</td>`;
    html += '</tr>';
    html += '</tbody>';
    html += '</table>';
    html += '</div>';
    
    if (data.matches && data.matches.length > 0) {
        html += '<div class="table-container" style="margin-top: 30px;">';
        html += '<h3 style="padding: 20px; margin: 0; border-bottom: 1px solid #e0e0e0;">Recent Matches</h3>';
        html += '<table id="matchesTable">';
        html += '<thead>';
        html += '<tr>';
        html += '<th>Match</th>';
        html += '<th>Date</th>';
        html += '<th>Total Kills</th>';
        html += '<th>Maps</th>';
        html += '</tr>';
        html += '</thead>';
        html += '<tbody id="matchesTableBody">';
        
        // Show first 10 matches
        const matchesToShow = Math.min(10, data.matches.length);
        data.matches.slice(0, matchesToShow).forEach(match => {
            html += '<tr>';
            // Make match name a clickable link if match_url is available
            if (match.match_url) {
                html += `<td><a href="${match.match_url}" target="_blank" style="color: inherit; text-decoration: underline;">${match.match}</a></td>`;
            } else {
                html += `<td>${match.match}</td>`;
            }
            html += `<td>${match.date}</td>`;
            html += `<td><strong>${match.total_kills}</strong></td>`;
            html += `<td>${match.map_kills.map(m => `${m.map} (${m.kills})`).join(', ')}</td>`;
            html += '</tr>';
        });
        
        html += '</tbody>';
        html += '</table>';
        
        // Add "Show More" button if there are more matches
        if (data.matches.length > 10) {
            html += '<div style="padding: 20px; text-align: center;">';
            html += `<button id="showMoreMatches" class="btn-secondary" data-total="${data.matches.length}" data-shown="10">Show 10 More</button>`;
            html += '</div>';
        }
        
        html += '</div>';
    }
    
    resultsDiv.innerHTML = html;
    
    // Store matches data for "Show More" functionality
    if (data.matches && data.matches.length > 0) {
        window.currentMatchesData = data.matches;
    }
    
    hideLoading();
}

// Show more matches functionality
function showMoreMatches() {
    const button = document.getElementById('showMoreMatches');
    if (!button) return;
    
    const totalMatches = parseInt(button.getAttribute('data-total'));
    const currentlyShown = parseInt(button.getAttribute('data-shown'));
    const matchesTableBody = document.getElementById('matchesTableBody');
    
    if (!matchesTableBody) return;
    
    // Get the matches data from the button (we stored it)
    const allMatches = window.currentMatchesData || [];
    
    // Show next 10 matches
    const nextBatch = allMatches.slice(currentlyShown, currentlyShown + 10);
    nextBatch.forEach(match => {
        const row = document.createElement('tr');
        // Make match name a clickable link if match_url is available
        const matchCell = match.match_url 
            ? `<td><a href="${match.match_url}" target="_blank" style="color: inherit; text-decoration: underline;">${match.match}</a></td>`
            : `<td>${match.match}</td>`;
        row.innerHTML = `
            ${matchCell}
            <td>${match.date}</td>
            <td><strong>${match.total_kills}</strong></td>
            <td>${match.map_kills.map(m => `${m.map} (${m.kills})`).join(', ')}</td>
        `;
        matchesTableBody.appendChild(row);
    });
    
    const newShown = currentlyShown + nextBatch.length;
    button.setAttribute('data-shown', newShown);
    
    // Hide button if all matches are shown
    if (newShown >= totalMatches) {
        button.style.display = 'none';
    } else {
        button.textContent = `Show 10 More (${newShown}/${totalMatches})`;
    }
}

// Event listeners
document.getElementById('loadSlate').addEventListener('click', loadSlate);
document.getElementById('searchPlayer').addEventListener('click', searchPlayer);
document.getElementById('toggleDetails').addEventListener('click', () => {
    const detailsEl = document.getElementById('loadingDetails');
    const toggleBtn = document.getElementById('toggleDetails');
    if (detailsEl.classList.contains('hidden')) {
        detailsEl.classList.remove('hidden');
        toggleBtn.textContent = 'Hide Details';
    } else {
        detailsEl.classList.add('hidden');
        toggleBtn.textContent = 'Show Details';
    }
});
document.getElementById('playerSearch').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        searchPlayer();
    }
});

// Delegate event listener for "Show More" button (since it's dynamically created)
document.addEventListener('click', (e) => {
    if (e.target && e.target.id === 'showMoreMatches') {
        showMoreMatches();
    }
});

