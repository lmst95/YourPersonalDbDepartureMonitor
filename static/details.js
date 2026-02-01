// DB Live Tracker - Details Page

class DetailsPage {
    constructor() {
        this.currentPage = 0;
        this.pageSize = 100;
        this.filters = {
            route_id: null,
            query: '',
            delay_range: '',
            status: '',
            time_mode: 'relative',
            hours: 24,
            date_from: null,
            date_to: null
        };
        this.routes = [];
        this.departures = [];
        this.notificationContainer = document.getElementById('notificationContainer');

        this.init();
    }

    showNotification(title, message, type = 'info', duration = 5000) {
        const icons = {
            error: '❌',
            warning: '⚠️',
            success: '✅',
            info: 'ℹ️'
        };

        const notification = document.createElement('div');
        notification.className = `notification ${type}`;
        notification.innerHTML = `
            <div class="notification-icon">${icons[type]}</div>
            <div class="notification-content">
                <div class="notification-title">${title}</div>
                <div class="notification-message">${message}</div>
            </div>
            <div class="notification-close" onclick="this.parentElement.remove()">×</div>
        `;

        this.notificationContainer.appendChild(notification);

        if (duration > 0) {
            setTimeout(() => {
                notification.style.opacity = '0';
                notification.style.transform = 'translateX(400px)';
                setTimeout(() => notification.remove(), 300);
            }, duration);
        }
    }

    async fetchWithErrorHandling(url, options = {}) {
        try {
            const response = await fetch(url, options);

            if (!response.ok) {
                if (response.status === 401) {
                    this.showNotification(
                        'Authentication Error',
                        'Invalid API credentials. Please check DB_CLIENT_ID and DB_API_KEY in .env file.',
                        'error',
                        10000
                    );
                } else if (response.status === 403) {
                    this.showNotification(
                        'Access Forbidden',
                        'API access denied. Your API key may not have the required permissions.',
                        'error',
                        10000
                    );
                } else if (response.status === 429) {
                    this.showNotification(
                        'Rate Limit Exceeded',
                        'Too many API requests. Please wait a moment before trying again.',
                        'warning',
                        10000
                    );
                } else if (response.status >= 500) {
                    this.showNotification(
                        'Server Error',
                        'Deutsche Bahn API is currently unavailable. Please try again later.',
                        'error',
                        10000
                    );
                } else {
                    this.showNotification(
                        'Request Failed',
                        `HTTP ${response.status}: ${response.statusText}`,
                        'error',
                        8000
                    );
                }
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            return response;
        } catch (error) {
            if (error.name === 'TypeError' && error.message.includes('fetch')) {
                this.showNotification(
                    'Network Error',
                    'Unable to connect to the server. Please check your internet connection.',
                    'error',
                    8000
                );
            }
            throw error;
        }
    }

    async init() {
        // Load routes for filter dropdown
        await this.loadRoutes();

        // Get route IDs from sessionStorage if available
        const routeIds = sessionStorage.getItem('detailsRouteIds');
        if (routeIds) {
            const ids = routeIds.split(',').map(id => parseInt(id.trim()));
            if (ids.length === 1) {
                this.filters.route_id = ids[0];
                document.getElementById('routeFilter').value = ids[0];
            }
            // If multiple routes (bidirectional), show all
        }

        // Load departures
        await this.loadDepartures();

        // Setup filter listeners
        this.setupFilterListeners();
    }

    async loadRoutes() {
        try {
            const response = await this.fetchWithErrorHandling('/api/routes');
            const data = await response.json();
            this.routes = data.routes;

            // Populate route dropdown
            const select = document.getElementById('routeFilter');
            this.routes.forEach(route => {
                const option = document.createElement('option');
                option.value = route.id;
                option.textContent = `${route.origin_name} → ${route.dest_name}`;
                select.appendChild(option);
            });
        } catch (error) {
            console.error('Error loading routes:', error);
        }
    }

    async loadDepartures() {
        try {
            // Build query parameters
            const params = new URLSearchParams();
            params.append('limit', this.pageSize);
            params.append('offset', this.currentPage * this.pageSize);

            // Time filter based on mode
            if (this.filters.time_mode === 'relative') {
                if (this.filters.hours === 'all') {
                    params.append('all_time', 'true');
                } else {
                    params.append('since', this.filters.hours);
                }
            } else if (this.filters.time_mode === 'range') {
                if (this.filters.date_from) {
                    params.append('date_from', this.filters.date_from);
                }
                if (this.filters.date_to) {
                    params.append('date_to', this.filters.date_to);
                }
            }

            if (this.filters.route_id) {
                params.append('route_id', this.filters.route_id);
            }

            if (this.filters.query) {
                params.append('q', this.filters.query);
            }

            const response = await this.fetchWithErrorHandling(`/api/departures?${params}`);
            const data = await response.json();

            this.departures = data.departures;

            // Apply client-side filters
            let filteredDepartures = this.departures;
            if (this.filters.delay_range) {
                filteredDepartures = this.filterByDelay(filteredDepartures);
            }
            if (this.filters.status) {
                filteredDepartures = this.filterByStatus(filteredDepartures);
            }

            // Render table
            this.renderTable(filteredDepartures);

            // Update statistics from server (all data, not just current page)
            await this.updateStatisticsFromServer();

            // Update pagination
            this.updatePagination(data.meta);
        } catch (error) {
            console.error('Error loading departures:', error);
            this.showError('Error loading data');
        }
    }

    filterByDelay(departures) {
        const range = this.filters.delay_range;

        return departures.filter(dep => {
            const delay = dep.delay_min !== null ? dep.delay_min : 0;

            switch (range) {
                case 'ontime':
                    return delay <= 0;
                case 'minor':
                    return delay > 0 && delay <= 5;
                case 'moderate':
                    return delay > 5 && delay <= 15;
                case 'major':
                    return delay > 15;
                default:
                    return true;
            }
        });
    }

    filterByStatus(departures) {
        const status = this.filters.status;

        return departures.filter(dep => {
            switch (status) {
                case 'active':
                    return !dep.status || dep.status === 'a';
                case 'cancelled':
                    return dep.status === 'c';
                case 'partial':
                    return dep.status === 'p';
                case 'additional':
                    return dep.status === 'a';
                default:
                    return true;
            }
        });
    }

    renderTable(departures) {
        const tbody = document.getElementById('tableBody');

        if (departures.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="no-data">No data found</td></tr>';
            return;
        }

        tbody.innerHTML = departures.map(dep => {
            const plannedTime = new Date(dep.planned_dt);
            const realtimeTime = dep.realtime_dt ? new Date(dep.realtime_dt) : null;
            const delay = dep.delay_min !== null ? dep.delay_min : 0;

            // Format date and time
            const dateStr = plannedTime.toLocaleDateString('de-DE', {
                day: '2-digit',
                month: '2-digit',
                year: 'numeric'
            });
            const timeStr = plannedTime.toLocaleTimeString('de-DE', {
                hour: '2-digit',
                minute: '2-digit'
            });

            const actualTimeStr = realtimeTime ? realtimeTime.toLocaleTimeString('de-DE', {
                hour: '2-digit',
                minute: '2-digit'
            }) : '-';

            // Status badge (cancellation, partial, additional)
            let statusBadge = '';
            if (dep.status === 'c') {
                statusBadge = '<span class="status-badge status-cancelled">❌ Cancelled</span>';
            } else if (dep.status === 'p') {
                statusBadge = '<span class="status-badge status-partial">⚠️ Partial</span>';
            } else if (dep.status === 'a') {
                statusBadge = '<span class="status-badge status-additional">➕ Additional</span>';
            }

            // Delay badge
            let delayClass = 'delay-neutral';
            let delayText = 'On-time';
            if (delay > 0) {
                delayClass = 'delay-positive';
                delayText = `+${delay} min`;
            } else if (delay < 0) {
                delayClass = 'delay-negative';
                delayText = `${delay} min`;
            }

            // Platform
            const plannedPlatform = dep.planned_platform || '-';
            const realtimePlatform = dep.realtime_platform || plannedPlatform;
            const platformChanged = dep.realtime_platform && dep.realtime_platform !== dep.planned_platform;
            const platformStr = platformChanged
                ? `<span style="text-decoration: line-through;">${plannedPlatform}</span> → ${realtimePlatform}`
                : plannedPlatform;

            return `
                <tr${dep.status === 'c' ? ' class="cancelled-row"' : ''}>
                    <td>${dateStr}</td>
                    <td>
                        <strong>${dep.category || ''} ${dep.number || ''}</strong>
                        ${statusBadge}
                    </td>
                    <td>
                        <span class="route-badge">${dep.origin_name} → ${dep.dest_name}</span>
                    </td>
                    <td>${platformStr}</td>
                    <td>
                        <span class="delay-badge ${delayClass}">${delayText}</span>
                    </td>
                    <td>
                        <div style="font-size: 0.85rem; color: #6b7280;">
                            <div>Planned: ${timeStr}</div>
                            ${realtimeTime ? `<div>Actual: ${actualTimeStr}</div>` : ''}
                        </div>
                    </td>
                </tr>
            `;
        }).join('');
    }

    async updateStatisticsFromServer() {
        try {
            // Fetch stats from server for ALL matching data (not just current page)
            const stats = await this.fetchStats();
            const summary = stats.summary;

            // Update stats bar with server-side statistics
            document.getElementById('totalCount').textContent = summary.total_count || 0;
            document.getElementById('avgDelay').textContent = summary.avg_delay !== null
                ? `${summary.avg_delay.toFixed(1)} min`
                : '-';
            document.getElementById('maxDelay').textContent = summary.max_delay !== null
                ? `${summary.max_delay} min`
                : '-';
            document.getElementById('onTimePercent').textContent = summary.ontime_rate !== null
                ? `${summary.ontime_rate.toFixed(1)}%`
                : '-';
        } catch (error) {
            console.error('Error updating statistics:', error);
            // Show placeholder values on error
            document.getElementById('totalCount').textContent = '-';
            document.getElementById('avgDelay').textContent = '-';
            document.getElementById('maxDelay').textContent = '-';
            document.getElementById('onTimePercent').textContent = '-';
        }
    }

    updateStatistics(departures) {
        const delays = departures
            .map(d => d.delay_min !== null ? d.delay_min : 0)
            .filter(d => d !== null);

        const totalCount = delays.length;
        const avgDelay = totalCount > 0
            ? (delays.reduce((a, b) => a + b, 0) / totalCount).toFixed(1)
            : '0';
        const maxDelay = totalCount > 0 ? Math.max(...delays) : 0;
        const onTimeCount = delays.filter(d => d <= 0).length;
        const onTimePercent = totalCount > 0
            ? ((onTimeCount / totalCount) * 100).toFixed(1)
            : '0';

        document.getElementById('totalCount').textContent = totalCount;
        document.getElementById('avgDelay').textContent = `${avgDelay} min`;
        document.getElementById('maxDelay').textContent = `${maxDelay} min`;
        document.getElementById('onTimePercent').textContent = `${onTimePercent}%`;
    }

    updatePagination(meta) {
        const pagination = document.getElementById('pagination');
        const prevBtn = document.getElementById('prevBtn');
        const nextBtn = document.getElementById('nextBtn');
        const pageInfo = document.getElementById('pageInfo');

        const totalPages = Math.ceil(meta.total / this.pageSize);
        const currentPageNum = this.currentPage + 1;

        if (totalPages > 1) {
            pagination.style.display = 'flex';
            pageInfo.textContent = `Page ${currentPageNum} of ${totalPages}`;
            prevBtn.disabled = this.currentPage === 0;
            nextBtn.disabled = currentPageNum >= totalPages;
        } else {
            pagination.style.display = 'none';
        }
    }

    setupFilterListeners() {
        // Enter key on text input triggers filter
        document.getElementById('trainFilter').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                this.applyFilters();
            }
        });

        // Set default date values
        const today = new Date();
        const lastWeek = new Date(today);
        lastWeek.setDate(lastWeek.getDate() - 7);

        document.getElementById('dateTo').valueAsDate = today;
        document.getElementById('dateFrom').valueAsDate = lastWeek;
    }

    toggleTimeMode() {
        const mode = document.getElementById('timeMode').value;
        this.filters.time_mode = mode;

        if (mode === 'relative') {
            document.getElementById('relativeTimeFilter').style.display = 'grid';
            document.getElementById('dateRangeFilter').style.display = 'none';
        } else {
            document.getElementById('relativeTimeFilter').style.display = 'none';
            document.getElementById('dateRangeFilter').style.display = 'grid';
        }
    }

    applyFilters() {
        // Read filter values
        const routeId = document.getElementById('routeFilter').value;
        const trainQuery = document.getElementById('trainFilter').value.trim();
        const delayRange = document.getElementById('delayFilter').value;
        const statusFilter = document.getElementById('statusFilter').value;
        const timeMode = document.getElementById('timeMode').value;

        // Update filters
        this.filters.route_id = routeId ? parseInt(routeId) : null;
        this.filters.query = trainQuery;
        this.filters.delay_range = delayRange;
        this.filters.status = statusFilter;
        this.filters.time_mode = timeMode;

        if (timeMode === 'relative') {
            const hours = document.getElementById('hoursFilter').value;
            this.filters.hours = hours; // Can be number or 'all'
        } else {
            this.filters.date_from = document.getElementById('dateFrom').value || null;
            this.filters.date_to = document.getElementById('dateTo').value || null;
        }

        // Reset to first page
        this.currentPage = 0;

        // Reload data
        this.loadDepartures();
    }

    resetFilters() {
        // Reset form
        document.getElementById('routeFilter').value = '';
        document.getElementById('trainFilter').value = '';
        document.getElementById('delayFilter').value = '';
        document.getElementById('statusFilter').value = '';
        document.getElementById('timeMode').value = 'relative';
        document.getElementById('hoursFilter').value = '24';

        // Reset date inputs
        const today = new Date();
        const lastWeek = new Date(today);
        lastWeek.setDate(lastWeek.getDate() - 7);
        document.getElementById('dateTo').valueAsDate = today;
        document.getElementById('dateFrom').valueAsDate = lastWeek;

        // Show relative time filter
        document.getElementById('relativeTimeFilter').style.display = 'grid';
        document.getElementById('dateRangeFilter').style.display = 'none';

        // Reset filters object
        this.filters = {
            route_id: null,
            query: '',
            delay_range: '',
            status: '',
            time_mode: 'relative',
            hours: 24,
            date_from: null,
            date_to: null
        };

        // Reset page
        this.currentPage = 0;

        // Reload data
        this.loadDepartures();
    }

    previousPage() {
        if (this.currentPage > 0) {
            this.currentPage--;
            this.loadDepartures();
        }
    }

    nextPage() {
        this.currentPage++;
        this.loadDepartures();
    }

    showError(message) {
        const tbody = document.getElementById('tableBody');
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="no-data">
                    ❌ ${message}
                </td>
            </tr>
        `;
    }

    async showStatsModal() {
        const modal = document.getElementById('statsModal');
        const modalBody = document.getElementById('statsModalBody');

        // Show modal with loading state
        modal.classList.add('active');
        modalBody.innerHTML = `
            <div class="loading">
                <div class="spinner"></div>
                Loading statistics...
            </div>
        `;

        try {
            // Fetch stats using current filter settings
            const stats = await this.fetchStats();
            this.renderStats(stats);
        } catch (error) {
            modalBody.innerHTML = `
                <div class="error-message">
                    ❌ Failed to load statistics: ${error.message}
                </div>
            `;
        }
    }

    closeStatsModal() {
        const modal = document.getElementById('statsModal');
        modal.classList.remove('active');
    }

    async fetchStats() {
        // Build query params based on current filters
        const params = new URLSearchParams();

        if (this.filters.route_id) {
            params.append('route_id', this.filters.route_id);
        }

        if (this.filters.query) {
            params.append('q', this.filters.query);
        }

        if (this.filters.time_mode === 'relative') {
            if (this.filters.hours === 'all') {
                params.append('all_time', 'true');
            } else {
                params.append('since', this.filters.hours);
            }
        } else {
            if (this.filters.date_from) {
                params.append('date_from', this.filters.date_from);
            }
            if (this.filters.date_to) {
                params.append('date_to', this.filters.date_to);
            }
        }

        const response = await this.fetchWithErrorHandling(`/api/departures/stats?${params}`);
        return await response.json();
    }

    renderStats(data) {
        const modalBody = document.getElementById('statsModalBody');
        const { hourly_stats, daily_stats, summary } = data;

        // Create stats summary cards
        const summaryHTML = `
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="label">Total Departures</div>
                    <div class="value">${summary.total_count}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Average Delay</div>
                    <div class="value">${summary.avg_delay !== null ? summary.avg_delay.toFixed(1) + ' min' : 'N/A'}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Median Delay</div>
                    <div class="value">${summary.median_delay !== null ? summary.median_delay + ' min' : 'N/A'}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Max Delay</div>
                    <div class="value">${summary.max_delay !== null ? summary.max_delay + ' min' : 'N/A'}</div>
                </div>
                <div class="stat-card">
                    <div class="label">On-time Rate</div>
                    <div class="value">${summary.ontime_rate !== null ? summary.ontime_rate.toFixed(1) + '%' : 'N/A'}</div>
                </div>
            </div>
        `;

        modalBody.innerHTML = summaryHTML;

        // Create hourly boxplot
        if (hourly_stats && hourly_stats.length > 0) {
            const hourlyDiv = document.createElement('div');
            hourlyDiv.className = 'plot-container';
            hourlyDiv.innerHTML = '<h3 class="plot-title">Delay Distribution by Time of Day</h3><div id="hourlyPlot"></div>';
            modalBody.appendChild(hourlyDiv);

            this.createHourlyBoxplot(hourly_stats, 'hourlyPlot');
        }

        // Create daily boxplot
        if (daily_stats && daily_stats.length > 0) {
            const dailyDiv = document.createElement('div');
            dailyDiv.className = 'plot-container';
            dailyDiv.innerHTML = '<h3 class="plot-title">Delay Distribution by Day of Week</h3><div id="dailyPlot"></div>';
            modalBody.appendChild(dailyDiv);

            this.createDailyBoxplot(daily_stats, 'dailyPlot');
        }
    }

    createHourlyBoxplot(hourly_stats, containerId) {
        const hours = [];
        const delays = [];

        hourly_stats.forEach(stat => {
            if (stat.count > 0 && stat.delays && stat.delays.length > 0) {
                hours.push(`${stat.hour}:00`);
                delays.push(stat.delays);
            }
        });

        if (hours.length === 0) {
            document.getElementById(containerId).innerHTML = '<div class="error-message">No data available for hourly distribution</div>';
            return;
        }

        const traces = hours.map((hour, idx) => ({
            y: delays[idx],
            type: 'box',
            name: hour,
            boxmean: 'sd',
            marker: { color: '#667eea' },
            line: { width: 2 }
        }));

        const layout = {
            title: '',
            xaxis: { title: 'Time of Day' },
            yaxis: { title: 'Delay (minutes)' },
            showlegend: false,
            height: 400,
            margin: { l: 60, r: 30, t: 30, b: 60 }
        };

        Plotly.newPlot(containerId, traces, layout, { responsive: true });
    }

    createDailyBoxplot(daily_stats, containerId) {
        const days = [];
        const delays = [];

        daily_stats.forEach(stat => {
            if (stat.count > 0 && stat.delays && stat.delays.length > 0) {
                days.push(stat.day_name);
                delays.push(stat.delays);
            }
        });

        if (days.length === 0) {
            document.getElementById(containerId).innerHTML = '<div class="error-message">No data available for daily distribution</div>';
            return;
        }

        const traces = days.map((day, idx) => ({
            y: delays[idx],
            type: 'box',
            name: day,
            boxmean: 'sd',
            marker: { color: '#764ba2' },
            line: { width: 2 }
        }));

        const layout = {
            title: '',
            xaxis: { title: 'Day of Week' },
            yaxis: { title: 'Delay (minutes)' },
            showlegend: false,
            height: 400,
            margin: { l: 60, r: 30, t: 30, b: 60 }
        };

        Plotly.newPlot(containerId, traces, layout, { responsive: true });
    }
}

// Initialize when DOM is ready
let details;
document.addEventListener('DOMContentLoaded', () => {
    details = new DetailsPage();
});
