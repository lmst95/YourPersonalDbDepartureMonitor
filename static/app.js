// DB Live Tracker - Main Application JavaScript

class DBLiveTracker {
    constructor() {
        this.map = null;
        this.routes = [];
        this.routeLayers = [];
        this.modal = document.getElementById('statsModal');
        this.loadingOverlay = document.getElementById('loadingOverlay');
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

        // Auto-remove after duration
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
                // Handle different HTTP errors
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
        // Initialize map
        this.initMap();

        // Load routes
        await this.loadRoutes();

        // Setup event listeners
        this.setupEventListeners();

        // Hide loading overlay
        this.hideLoading();
    }

    initMap() {
        // Initialize Leaflet map centered on Germany
        this.map = L.map('map').setView([51.1657, 10.4515], 6);

        // Add OpenStreetMap tile layer
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxZoom: 18,
            minZoom: 5
        }).addTo(this.map);
    }

    async loadRoutes() {
        try {
            const response = await this.fetchWithErrorHandling('/api/routes');
            const data = await response.json();
            this.routes = data.routes;

            // Group bidirectional routes
            this.groupedRoutes = this.groupBidirectionalRoutes(this.routes);

            // Draw routes on map
            this.drawRoutes();
        } catch (error) {
            console.error('Error loading routes:', error);
            this.showError('Error loading routes: ' + error.message);
        }
    }

    groupBidirectionalRoutes(routes) {
        const grouped = [];
        const processed = new Set();

        routes.forEach(route => {
            if (processed.has(route.id)) return;

            // Look for reverse route
            const reverseRoute = routes.find(r =>
                r.origin_eva === route.dest_eva &&
                r.dest_eva === route.origin_eva &&
                !processed.has(r.id)
            );

            if (reverseRoute) {
                // Bidirectional route
                grouped.push({
                    type: 'bidirectional',
                    routes: [route, reverseRoute],
                    origin_lat: route.origin_lat,
                    origin_lon: route.origin_lon,
                    dest_lat: route.dest_lat,
                    dest_lon: route.dest_lon,
                    origin_name: route.origin_name,
                    dest_name: route.dest_name,
                    arrow: '↔'
                });
                processed.add(route.id);
                processed.add(reverseRoute.id);
            } else {
                // Unidirectional route
                grouped.push({
                    type: 'unidirectional',
                    routes: [route],
                    origin_lat: route.origin_lat,
                    origin_lon: route.origin_lon,
                    dest_lat: route.dest_lat,
                    dest_lon: route.dest_lon,
                    origin_name: route.origin_name,
                    dest_name: route.dest_name,
                    arrow: '→'
                });
                processed.add(route.id);
            }
        });

        return grouped;
    }

    drawRoutes() {
        // Clear existing layers
        this.routeLayers.forEach(layer => this.map.removeLayer(layer));
        this.routeLayers = [];

        this.groupedRoutes.forEach(group => {
            // Only draw route if both origin and destination have coordinates
            if (!group.origin_lat || !group.origin_lon || !group.dest_lat || !group.dest_lon) {
                console.warn(`Route group missing coordinates, skipping`);
                return;
            }

            const originCoords = [group.origin_lat, group.origin_lon];
            const destCoords = [group.dest_lat, group.dest_lon];

            // Color based on direction
            const lineColor = group.type === 'bidirectional' ? '#667eea' : '#9333ea';

            // Create a line between stations
            const line = L.polyline([originCoords, destCoords], {
                color: lineColor,
                weight: 3,
                opacity: 0.7,
                smoothFactor: 1
            }).addTo(this.map);

            // Add hover effect
            line.on('mouseover', (e) => {
                e.target.setStyle({
                    color: '#764ba2',
                    weight: 5,
                    opacity: 1
                });
            });

            line.on('mouseout', (e) => {
                e.target.setStyle({
                    color: lineColor,
                    weight: 3,
                    opacity: 0.7
                });
            });

            // Bind popup with route information
            const popupContent = this.createRoutePopup(group);
            line.bindPopup(popupContent, {
                maxWidth: 300,
                className: 'route-popup'
            });

            this.routeLayers.push(line);

            // Add markers for origin and destination
            const originMarker = L.circleMarker(originCoords, {
                radius: 6,
                fillColor: '#667eea',
                color: 'white',
                weight: 2,
                opacity: 1,
                fillOpacity: 0.8
            }).addTo(this.map);

            const destMarker = L.circleMarker(destCoords, {
                radius: 6,
                fillColor: '#764ba2',
                color: 'white',
                weight: 2,
                opacity: 1,
                fillOpacity: 0.8
            }).addTo(this.map);

            // Tooltips for stations
            originMarker.bindTooltip(group.origin_name, { permanent: false, direction: 'top' });
            destMarker.bindTooltip(group.dest_name, { permanent: false, direction: 'top' });

            this.routeLayers.push(originMarker, destMarker);
        });

        console.log(`Drew ${this.groupedRoutes.length} route groups on map (${this.routes.length} total routes)`);
    }

    createRoutePopup(group) {
        const popup = document.createElement('div');
        popup.className = 'route-popup';

        // Create buttons for each direction
        let buttonsHtml = '';
        if (group.type === 'bidirectional') {
            // Show buttons for both directions
            buttonsHtml = `
                <div class="popup-buttons">
                    <button class="direction-btn" onclick="app.showRouteStats('${group.routes[0].id}')">
                        ${group.routes[0].origin_name} → ${group.routes[0].dest_name}
                    </button>
                    <button class="direction-btn" onclick="app.showRouteStats('${group.routes[1].id}')">
                        ${group.routes[1].origin_name} → ${group.routes[1].dest_name}
                    </button>
                    <button class="both-directions-btn" onclick="app.showRouteStats('${group.routes.map(r => r.id).join(',')}')">
                        Both Directions
                    </button>
                </div>
            `;
        } else {
            // Single direction
            buttonsHtml = `
                <button class="single-direction-btn" onclick="app.showRouteStats('${group.routes[0].id}')">
                    Show Statistics
                </button>
            `;
        }

        popup.innerHTML = `
            <h3>${group.origin_name} ${group.arrow} ${group.dest_name}</h3>
            <div class="info">
                <div class="route-type">${group.type === 'bidirectional' ? 'Bidirectional' : 'Unidirectional'}</div>
            </div>
            ${buttonsHtml}
        `;
        return popup;
    }

    async showRouteStats(routeIds) {
        // Parse route IDs (can be single or comma-separated)
        const ids = routeIds.toString().split(',').map(id => parseInt(id.trim()));
        const routes = ids.map(id => this.routes.find(r => r.id === id)).filter(r => r);

        if (routes.length === 0) return;

        // Store routes for later use in renderStatistics
        this.currentStatsRoutes = routes;

        // Determine arrow direction and title
        let arrow = '→';
        let titleStations = `${routes[0].origin_name} → ${routes[0].dest_name}`;

        if (routes.length === 2) {
            // Bidirectional
            arrow = '↔';
            titleStations = `${routes[0].origin_name} ↔ ${routes[0].dest_name}`;
        }

        // Update modal title
        document.getElementById('modalTitle').textContent = titleStations;

        // Show modal
        this.modal.style.display = 'block';

        // Show loading state
        document.getElementById('modalContent').innerHTML =
            '<div class="loading">Loading statistics...</div>';

        try {
            // Fetch statistics for all routes
            const statsPromises = ids.map(id =>
                this.fetchWithErrorHandling(`/api/routes/${id}/stats`).then(r => r.json())
            );
            const allStats = await Promise.all(statsPromises);

            // Render statistics
            this.renderStatistics(allStats);
        } catch (error) {
            console.error('Error loading statistics:', error);
            document.getElementById('modalContent').innerHTML =
                `<div class="error-message">Error loading statistics: ${error.message}</div>`;
        }
    }

    renderStatistics(allStatsData) {
        const content = document.createElement('div');
        content.className = 'modal-statistics';

        // Handle single or multiple routes
        const isBidirectional = allStatsData.length === 2;

        if (isBidirectional) {
            const routes = this.currentStatsRoutes;

            // Show tabs for combined view and both individual directions
            content.innerHTML = `
                <div class="direction-tabs">
                    <button class="tab-button active" onclick="app.switchDirection(0)">
                        Combined ↔
                    </button>
                    <button class="tab-button" onclick="app.switchDirection(1)">
                        ${routes[0].origin_name} → ${routes[0].dest_name}
                    </button>
                    <button class="tab-button" onclick="app.switchDirection(2)">
                        ${routes[1].origin_name} → ${routes[1].dest_name}
                    </button>
                </div>
                <div id="direction0" class="direction-content active"></div>
                <div id="direction1" class="direction-content" style="display: none;"></div>
                <div id="direction2" class="direction-content" style="display: none;"></div>
            `;

            document.getElementById('modalContent').innerHTML = '';
            document.getElementById('modalContent').appendChild(content);

            // Render combined view first (active tab)
            const combinedData = this.combineRouteStatistics(allStatsData);
            this.renderSingleRouteStats(combinedData, 'direction0');

            // Render each individual direction
            allStatsData.forEach((data, idx) => {
                this.renderSingleRouteStats(data, `direction${idx + 1}`);
            });
        } else {
            // Single direction
            document.getElementById('modalContent').innerHTML = '<div id="singleDirection"></div>';
            this.renderSingleRouteStats(allStatsData[0], 'singleDirection');
        }
    }

    combineRouteStatistics(allStatsData) {
        // Merge hourly statistics from both directions
        const hourlyStatsMap = new Map();

        allStatsData.forEach(data => {
            data.hourly_stats.forEach(stat => {
                const hour = stat.hour;
                if (!hourlyStatsMap.has(hour)) {
                    hourlyStatsMap.set(hour, {
                        hour: hour,
                        count: 0,
                        delays: []
                    });
                }

                const combined = hourlyStatsMap.get(hour);
                combined.count += stat.count;
                combined.delays = combined.delays.concat(stat.delays);
            });
        });

        // Convert map to sorted array
        const combinedHourlyStats = Array.from(hourlyStatsMap.values())
            .sort((a, b) => a.hour - b.hour);

        return {
            hourly_stats: combinedHourlyStats
        };
    }

    switchDirection(index) {
        // Update tab buttons
        document.querySelectorAll('.tab-button').forEach((btn, idx) => {
            btn.classList.toggle('active', idx === index);
        });

        // Update content visibility
        document.querySelectorAll('.direction-content').forEach((div, idx) => {
            div.style.display = idx === index ? 'block' : 'none';
        });
    }

    renderSingleRouteStats(data, containerId) {
        const { hourly_stats } = data;

        // Calculate overall statistics
        const allDelays = hourly_stats.flatMap(h => h.delays).filter(d => d !== null);
        const totalCount = allDelays.length;
        const avgDelay = totalCount > 0
            ? (allDelays.reduce((a, b) => a + b, 0) / totalCount).toFixed(1)
            : 'N/A';
        const maxDelay = totalCount > 0 ? Math.max(...allDelays) : 'N/A';
        const onTimeCount = allDelays.filter(d => d <= 0).length;
        const onTimePercent = totalCount > 0
            ? ((onTimeCount / totalCount) * 100).toFixed(1)
            : 'N/A';

        // Prepare data for boxplot
        const hours = [];
        const delays = [];

        hourly_stats.forEach(stat => {
            if (stat.count > 0) {
                hours.push(stat.hour);
                delays.push(stat.delays);
            }
        });

        // Create HTML content
        const container = document.getElementById(containerId);

        // Summary statistics
        container.innerHTML = `
            <div class="stats-summary">
                <div class="stat-card">
                    <div class="label">Total Count</div>
                    <div class="value">${totalCount}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Avg Delay</div>
                    <div class="value">${avgDelay} min</div>
                </div>
                <div class="stat-card">
                    <div class="label">Max Delay</div>
                    <div class="value">${maxDelay} min</div>
                </div>
                <div class="stat-card">
                    <div class="label">On-time Rate</div>
                    <div class="value">${onTimePercent}%</div>
                </div>
            </div>
            <div id="plotContainer${containerId}"></div>
        `;

        // Create boxplot with Plotly
        if (hours.length > 0) {
            this.createBoxplot(hours, delays, `plotContainer${containerId}`);
        } else {
            document.getElementById(`plotContainer${containerId}`).innerHTML =
                '<div class="error-message">No data available for boxplot</div>';
        }
    }

    createBoxplot(hours, delays, containerId = 'plotContainer') {
        // Create traces for each hour
        const traces = hours.map((hour, idx) => ({
            y: delays[idx],
            type: 'box',
            name: `${hour}:00`,
            boxmean: 'sd',
            marker: {
                color: '#667eea'
            },
            line: {
                color: '#764ba2'
            }
        }));

        const layout = {
            title: {
                text: 'Delay Distribution by Time of Day',
                font: { size: 16 }
            },
            yaxis: {
                title: 'Delay (minutes)',
                zeroline: true,
                zerolinecolor: '#999',
                zerolinewidth: 2
            },
            xaxis: {
                title: 'Time of Day',
                tickmode: 'linear'
            },
            showlegend: false,
            height: 400,  // Reduced from 500 to fit modal better
            margin: { t: 40, b: 60, l: 50, r: 20 },  // Reduced margins
            plot_bgcolor: '#fafafa',
            paper_bgcolor: 'white',
            autosize: true
        };

        const config = {
            responsive: true,
            displayModeBar: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['pan2d', 'lasso2d', 'select2d']
        };

        Plotly.newPlot(containerId, traces, layout, config);
    }

    openDetailsPage() {
        // Store current route IDs in sessionStorage for the details page
        if (this.currentStatsRoutes && this.currentStatsRoutes.length > 0) {
            const routeIds = this.currentStatsRoutes.map(r => r.id).join(',');
            sessionStorage.setItem('detailsRouteIds', routeIds);
        }

        // Open details page in new window/tab
        window.open('/details.html', '_blank');
    }

    setupEventListeners() {
        // Close modal when clicking X
        const closeBtn = document.querySelector('.close');
        closeBtn.onclick = () => {
            this.modal.style.display = 'none';
        };

        // Close modal when clicking outside
        window.onclick = (event) => {
            if (event.target === this.modal) {
                this.modal.style.display = 'none';
            }
        };

        // Close modal on Escape key
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && this.modal.style.display === 'block') {
                this.modal.style.display = 'none';
            }
        });
    }

    hideLoading() {
        this.loadingOverlay.classList.add('hidden');
    }

    showError(message) {
        this.loadingOverlay.innerHTML = `
            <div class="error-message">
                <h3>Error</h3>
                <p>${message}</p>
            </div>
        `;
    }
}

// Initialize application when DOM is ready
let app;
document.addEventListener('DOMContentLoaded', () => {
    app = new DBLiveTracker();
});
