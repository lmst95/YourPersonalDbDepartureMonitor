// DB Live Tracker - Main Application JavaScript

class DBLiveTracker {
    constructor() {
        this.map = null;
        this.routes = [];
        this.routeLayers = [];
        this.stationMarkers = new Map(); // Map of station key -> marker
        this.stationRoutes = new Map(); // Map of station key -> {name, routes[]}
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

            // Fit map to show all routes
            this.fitMapToRoutes();
        } catch (error) {
            console.error('Error loading routes:', error);
            this.showError('Error loading routes: ' + error.message);
        }
    }

    fitMapToRoutes() {
        // Collect all coordinates from routes
        const coords = [];

        this.groupedRoutes.forEach(group => {
            if (group.origin_lat && group.origin_lon) {
                coords.push([group.origin_lat, group.origin_lon]);
            }
            if (group.dest_lat && group.dest_lon) {
                coords.push([group.dest_lat, group.dest_lon]);
            }
        });

        // If we have coordinates, fit the map to show all of them
        if (coords.length > 0) {
            const bounds = L.latLngBounds(coords);
            // Fit bounds with some padding
            this.map.fitBounds(bounds, {
                padding: [50, 50], // 50px padding on all sides
                maxZoom: 10 // Don't zoom in too much even for single route
            });
        }
        // Otherwise keep the default view (Germany centered)
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
        this.stationMarkers.clear();
        this.stationRoutes.clear();

        // Build station routes map
        this.routes.forEach(route => {
            const originKey = `${route.origin_eva}`;
            const destKey = `${route.dest_eva}`;

            // Add to origin station's outgoing routes
            if (!this.stationRoutes.has(originKey)) {
                this.stationRoutes.set(originKey, {
                    name: route.origin_name,
                    lat: route.origin_lat,
                    lon: route.origin_lon,
                    eva: route.origin_eva,
                    outgoing: [],
                    incoming: []
                });
            }
            this.stationRoutes.get(originKey).outgoing.push(route);

            // Add to destination station's incoming routes
            if (!this.stationRoutes.has(destKey)) {
                this.stationRoutes.set(destKey, {
                    name: route.dest_name,
                    lat: route.dest_lat,
                    lon: route.dest_lon,
                    eva: route.dest_eva,
                    outgoing: [],
                    incoming: []
                });
            }
            this.stationRoutes.get(destKey).incoming.push(route);
        });

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
        });

        // Create station markers (one per unique station)
        this.stationRoutes.forEach((stationInfo, stationKey) => {
            if (!stationInfo.lat || !stationInfo.lon) return;

            const coords = [stationInfo.lat, stationInfo.lon];
            const routeCount = stationInfo.outgoing.length + stationInfo.incoming.length;

            // Create marker
            const marker = L.circleMarker(coords, {
                radius: 8,
                fillColor: '#667eea',
                color: 'white',
                weight: 2,
                opacity: 1,
                fillOpacity: 0.9
            }).addTo(this.map);

            // Tooltip
            marker.bindTooltip(stationInfo.name, { permanent: false, direction: 'top' });

            // Click handler - show station details
            marker.on('click', () => {
                this.showStationDetails(stationInfo);
            });

            // Hover effect
            marker.on('mouseover', (e) => {
                e.target.setStyle({
                    radius: 10,
                    fillColor: '#764ba2'
                });
            });

            marker.on('mouseout', (e) => {
                e.target.setStyle({
                    radius: 8,
                    fillColor: '#667eea'
                });
            });

            this.stationMarkers.set(stationKey, marker);
            this.routeLayers.push(marker);
        });

        console.log(`Drew ${this.groupedRoutes.length} route groups and ${this.stationMarkers.size} stations on map`);
    }

    findRouteGroup(routeId) {
        // Find the grouped route that contains this route ID
        return this.groupedRoutes.find(group => {
            if (group.type === 'bidirectional') {
                return group.routes[0].id === routeId || group.routes[1].id === routeId;
            } else {
                return group.routes[0].id === routeId;
            }
        });
    }

    showStationDetails(stationInfo) {
        const popup = document.createElement('div');
        popup.className = 'station-details-popup';
        popup.style.cssText = 'max-width: 400px; font-family: system-ui;';

        const outgoingCount = stationInfo.outgoing.length;
        const incomingCount = stationInfo.incoming.length;

        let html = `
            <div style="padding: 10px;">
                <h3 style="margin: 0 0 10px 0; color: #667eea; font-size: 18px;">
                    ${stationInfo.name}
                </h3>
                <p style="margin: 0 0 10px 0; font-size: 12px; color: #666;">
                    EVA: ${stationInfo.eva}
                </p>
        `;

        // Outgoing routes
        if (outgoingCount > 0) {
            html += `
                <div style="margin-bottom: 15px;">
                    <h4 style="margin: 0 0 8px 0; color: #764ba2; font-size: 14px;">
                        ➜ Outgoing Routes (${outgoingCount})
                    </h4>
                    <div style="max-height: 150px; overflow-y: auto;">
            `;

            stationInfo.outgoing.forEach(route => {
                html += `
                    <div style="padding: 6px; margin: 4px 0; background: #f3f4f6; border-radius: 4px; cursor: pointer; transition: background 0.2s;"
                         onmouseover="this.style.background='#e5e7eb'"
                         onmouseout="this.style.background='#f3f4f6'"
                         onclick="app.showRouteStatsFromId(${route.id})">
                        <div style="font-size: 13px; color: #333; font-weight: 500;">
                            → ${route.dest_name}
                        </div>
                    </div>
                `;
            });

            html += `
                    </div>
                </div>
            `;
        }

        // Incoming routes
        if (incomingCount > 0) {
            html += `
                <div>
                    <h4 style="margin: 0 0 8px 0; color: #764ba2; font-size: 14px;">
                        ➜ Incoming Routes (${incomingCount})
                    </h4>
                    <div style="max-height: 150px; overflow-y: auto;">
            `;

            stationInfo.incoming.forEach(route => {
                html += `
                    <div style="padding: 6px; margin: 4px 0; background: #f3f4f6; border-radius: 4px; cursor: pointer; transition: background 0.2s;"
                         onmouseover="this.style.background='#e5e7eb'"
                         onmouseout="this.style.background='#f3f4f6'"
                         onclick="app.showRouteStatsFromId(${route.id})">
                        <div style="font-size: 13px; color: #333; font-weight: 500;">
                            ← ${route.origin_name}
                        </div>
                    </div>
                `;
            });

            html += `
                    </div>
                </div>
            `;
        }

        if (outgoingCount === 0 && incomingCount === 0) {
            html += `
                <p style="color: #666; font-size: 13px; font-style: italic;">
                    No routes configured for this station.
                </p>
            `;
        }

        html += `</div>`;
        popup.innerHTML = html;

        // Open modal with station details
        this.modal.style.display = 'flex';
        document.getElementById('modalTitle').textContent = `Station: ${stationInfo.name}`;
        document.getElementById('modalContent').innerHTML = '';
        document.getElementById('modalContent').appendChild(popup);
    }

    createRoutePopup(group) {
        const popup = document.createElement('div');
        popup.className = 'route-popup';

        // Create buttons for each direction
        let buttonsHtml = '';
        if (group.type === 'bidirectional') {
            // Show buttons for both directions - always pass all routes and focus on clicked tab
            const allRouteIds = group.routes.map(r => r.id).join(',');
            buttonsHtml = `
                <div class="popup-buttons">
                    <button class="direction-btn" onclick="app.showRouteStats('${allRouteIds}', 1)">
                        ${group.routes[0].origin_name} → ${group.routes[0].dest_name}
                    </button>
                    <button class="direction-btn" onclick="app.showRouteStats('${allRouteIds}', 2)">
                        ${group.routes[1].origin_name} → ${group.routes[1].dest_name}
                    </button>
                    <button class="both-directions-btn" onclick="app.showRouteStats('${allRouteIds}', 0)">
                        Both Directions
                    </button>
                </div>
            `;
        } else {
            // Unidirectional - find reverse route and show all tabs, focusing on the clicked direction
            buttonsHtml = `
                <button class="single-direction-btn" onclick="app.showRouteStatsFromId(${group.routes[0].id})">
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

    showRouteStatsFromId(routeId) {
        // Find the route that was clicked
        const route = this.routes.find(r => r.id === parseInt(routeId));

        if (!route) {
            console.error('Route not found for route ID:', routeId);
            return;
        }

        // Always look for the reverse route in the raw routes data
        const reverseRoute = this.routes.find(r =>
            r.origin_eva === route.dest_eva &&
            r.dest_eva === route.origin_eva
        );

        if (reverseRoute) {
            // Both directions exist - show all tabs with focus on clicked direction
            // Tab 0: Combined, Tab 1: First route, Tab 2: Second route
            const routeIds = [route.id, reverseRoute.id].join(',');
            // Focus on tab 1 (the clicked route is first)
            this.showRouteStats(routeIds, 1);
        } else {
            // Only one direction exists - show single tab
            this.showRouteStats(route.id.toString(), 0);
        }
    }

    async showRouteStats(routeIds, focusedTabIndex = 0) {
        // Parse route IDs (can be single or comma-separated)
        const ids = routeIds.toString().split(',').map(id => parseInt(id.trim()));
        const routes = ids.map(id => this.routes.find(r => r.id === id)).filter(r => r);

        if (routes.length === 0) return;

        // Store routes and focused tab for later use in renderStatistics
        this.currentStatsRoutes = routes;
        this.focusedTabIndex = focusedTabIndex;

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

            // Render statistics with the focused tab
            this.renderStatistics(allStats, this.focusedTabIndex);
        } catch (error) {
            console.error('Error loading statistics:', error);
            document.getElementById('modalContent').innerHTML =
                `<div class="error-message">Error loading statistics: ${error.message}</div>`;
        }
    }

    renderStatistics(allStatsData, focusedTabIndex = 0) {
        const content = document.createElement('div');
        content.className = 'modal-statistics';

        // Handle single or multiple routes
        const isBidirectional = allStatsData.length === 2;

        if (isBidirectional) {
            const routes = this.currentStatsRoutes;

            // Show tabs for combined view and both individual directions
            content.innerHTML = `
                <div class="direction-tabs">
                    <button class="tab-button ${focusedTabIndex === 0 ? 'active' : ''}" onclick="app.switchDirection(0)">
                        Combined ↔
                    </button>
                    <button class="tab-button ${focusedTabIndex === 1 ? 'active' : ''}" onclick="app.switchDirection(1)">
                        ${routes[0].origin_name} → ${routes[0].dest_name}
                    </button>
                    <button class="tab-button ${focusedTabIndex === 2 ? 'active' : ''}" onclick="app.switchDirection(2)">
                        ${routes[1].origin_name} → ${routes[1].dest_name}
                    </button>
                </div>
                <div id="direction0" class="direction-content ${focusedTabIndex === 0 ? 'active' : ''}" style="display: ${focusedTabIndex === 0 ? 'block' : 'none'}"></div>
                <div id="direction1" class="direction-content ${focusedTabIndex === 1 ? 'active' : ''}" style="display: ${focusedTabIndex === 1 ? 'block' : 'none'}"></div>
                <div id="direction2" class="direction-content ${focusedTabIndex === 2 ? 'active' : ''}" style="display: ${focusedTabIndex === 2 ? 'block' : 'none'}"></div>
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
            // Single direction - also show with tab interface for consistency
            const route = this.currentStatsRoutes[0];

            content.innerHTML = `
                <div class="direction-tabs">
                    <button class="tab-button active" onclick="app.switchDirection(0)">
                        ${route.origin_name} → ${route.dest_name}
                    </button>
                </div>
                <div id="direction0" class="direction-content active"></div>
            `;

            document.getElementById('modalContent').innerHTML = '';
            document.getElementById('modalContent').appendChild(content);

            this.renderSingleRouteStats(allStatsData[0], 'direction0');
        }
    }

    combineRouteStatistics(allStatsData) {
        // Merge hourly statistics from both directions
        const hourlyStatsMap = new Map();
        const dailyStatsMap = new Map();

        allStatsData.forEach(data => {
            // Combine hourly stats
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

            // Combine daily stats
            data.daily_stats.forEach(stat => {
                const day = stat.day;
                if (!dailyStatsMap.has(day)) {
                    dailyStatsMap.set(day, {
                        day: day,
                        day_name: stat.day_name,
                        count: 0,
                        delays: []
                    });
                }

                const combined = dailyStatsMap.get(day);
                combined.count += stat.count;
                combined.delays = combined.delays.concat(stat.delays);
            });
        });

        // Convert maps to sorted arrays
        const combinedHourlyStats = Array.from(hourlyStatsMap.values())
            .sort((a, b) => a.hour - b.hour);
        const combinedDailyStats = Array.from(dailyStatsMap.values())
            .sort((a, b) => a.day - b.day);

        // Combine day_hour_stats (7x24 matrix)
        const dayNames = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
        const combinedDayHourStats = [];
        for (let day = 0; day < 7; day++) {
            const dayRow = [];
            for (let hour = 0; hour < 24; hour++) {
                // Collect delays from all data sources for this day/hour
                const allDelays = [];
                allStatsData.forEach(data => {
                    if (data.day_hour_stats && data.day_hour_stats[day] && data.day_hour_stats[day][hour]) {
                        const cell = data.day_hour_stats[day][hour];
                        if (cell.count > 0) {
                            // We don't have the raw delays, so we need to estimate from the available stats
                            // For accurate combination, we'll use the median values weighted by count
                            for (let i = 0; i < cell.count; i++) {
                                allDelays.push(cell.median);
                            }
                        }
                    }
                });

                if (allDelays.length > 0) {
                    allDelays.sort((a, b) => a - b);
                    const n = allDelays.length;
                    // Calculate combined stats from all sources
                    let minVal = Infinity, maxVal = -Infinity, sum = 0;
                    allStatsData.forEach(data => {
                        if (data.day_hour_stats && data.day_hour_stats[day] && data.day_hour_stats[day][hour]) {
                            const cell = data.day_hour_stats[day][hour];
                            if (cell.count > 0) {
                                if (cell.min !== null && cell.min < minVal) minVal = cell.min;
                                if (cell.max !== null && cell.max > maxVal) maxVal = cell.max;
                                sum += (cell.mean || 0) * cell.count;
                            }
                        }
                    });
                    dayRow.push({
                        day: day,
                        day_name: dayNames[day],
                        hour: hour,
                        count: n,
                        min: minVal === Infinity ? null : minVal,
                        max: maxVal === -Infinity ? null : maxVal,
                        median: allDelays[Math.floor(n / 2)],
                        mean: Math.round((sum / n) * 10) / 10
                    });
                } else {
                    dayRow.push({
                        day: day,
                        day_name: dayNames[day],
                        hour: hour,
                        count: 0,
                        min: null,
                        max: null,
                        median: null,
                        mean: null
                    });
                }
            }
            combinedDayHourStats.push(dayRow);
        }

        return {
            hourly_stats: combinedHourlyStats,
            daily_stats: combinedDailyStats,
            day_hour_stats: combinedDayHourStats
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

        // Resize Plotly charts in the newly visible tab
        // This is necessary because charts rendered while hidden don't have correct dimensions
        setTimeout(() => {
            const visibleDiv = document.querySelectorAll('.direction-content')[index];
            if (visibleDiv) {
                // Find all Plotly divs in the visible content (including heatmap)
                const plotlyDivs = visibleDiv.querySelectorAll('[id^="hourlyPlot"], [id^="dailyPlot"], [id^="heatmap"]');
                plotlyDivs.forEach(div => {
                    if (div && typeof Plotly !== 'undefined') {
                        Plotly.Plots.resize(div);
                    }
                });
            }
        }, 50); // Small delay to ensure display:block has taken effect
    }

    renderSingleRouteStats(data, containerId) {
        const { hourly_stats, daily_stats, day_hour_stats } = data;

        // Calculate overall statistics
        const allDelays = hourly_stats.flatMap(h => h.delays).filter(d => d !== null);
        const totalCount = allDelays.length;
        const avgDelay = totalCount > 0
            ? (allDelays.reduce((a, b) => a + b, 0) / totalCount).toFixed(1)
            : 'N/A';
        const maxDelay = totalCount > 0 ? Math.max(...allDelays) : 'N/A';
        const onTimeCount = allDelays.filter(d => d <= 3).length;
        const onTimePercent = totalCount > 0
            ? ((onTimeCount / totalCount) * 100).toFixed(1)
            : 'N/A';

        // Prepare data for hourly boxplot
        const hours = [];
        const hourlyDelays = [];

        hourly_stats.forEach(stat => {
            if (stat.count > 0) {
                hours.push(stat.hour);
                hourlyDelays.push(stat.delays);
            }
        });

        // Prepare data for daily boxplot
        const days = [];
        const dailyDelays = [];

        daily_stats.forEach(stat => {
            if (stat.count > 0) {
                days.push(stat.day_name);
                dailyDelays.push(stat.delays);
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
            <div id="heatmap${containerId}" style="margin-top: 20px;"></div>
            <div id="hourlyPlot${containerId}" style="margin-top: 30px;"></div>
            <div id="dailyPlot${containerId}" style="margin-top: 30px;"></div>
        `;

        // Create heatmap
        if (day_hour_stats && day_hour_stats.length > 0) {
            this.createHeatmap(day_hour_stats, `heatmap${containerId}`);
        } else {
            document.getElementById(`heatmap${containerId}`).innerHTML =
                '<div class="error-message">No data available for heatmap</div>';
        }

        // Create hourly boxplot with Plotly
        if (hours.length > 0) {
            this.createBoxplot(hours, hourlyDelays, `hourlyPlot${containerId}`, 'Delay Distribution by Time of Day', 'Time of Day');
        } else {
            document.getElementById(`hourlyPlot${containerId}`).innerHTML =
                '<div class="error-message">No data available for hourly boxplot</div>';
        }

        // Create daily boxplot with Plotly
        if (days.length > 0) {
            this.createDailyBoxplot(days, dailyDelays, `dailyPlot${containerId}`);
        } else {
            document.getElementById(`dailyPlot${containerId}`).innerHTML =
                '<div class="error-message">No data available for daily boxplot</div>';
        }
    }

    createBoxplot(hours, delays, containerId = 'plotContainer', title = 'Delay Distribution by Time of Day', xAxisTitle = 'Time of Day') {
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
                text: title,
                font: { size: 16 }
            },
            yaxis: {
                title: 'Delay (minutes)',
                zeroline: true,
                zerolinecolor: '#999',
                zerolinewidth: 2
            },
            xaxis: {
                title: xAxisTitle,
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

    createDailyBoxplot(days, delays, containerId = 'dailyPlotContainer') {
        // Create traces for each day
        const traces = days.map((day, idx) => ({
            y: delays[idx],
            type: 'box',
            name: day,
            boxmean: 'sd',
            marker: {
                color: '#f59e0b'
            },
            line: {
                color: '#d97706'
            }
        }));

        const layout = {
            title: {
                text: 'Delay Distribution by Day of Week',
                font: { size: 16 }
            },
            yaxis: {
                title: 'Delay (minutes)',
                zeroline: true,
                zerolinecolor: '#999',
                zerolinewidth: 2
            },
            xaxis: {
                title: 'Day of Week',
                tickmode: 'linear'
            },
            showlegend: false,
            height: 400,
            margin: { t: 40, b: 60, l: 50, r: 20 },
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

    createHeatmap(dayHourStats, containerId) {
        // dayHourStats is a 7x24 matrix (days x hours)
        // Each cell has: day, day_name, hour, count, min, max, median, mean

        const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
        const hours = Array.from({ length: 24 }, (_, i) => `${i}:00`);

        // Build z-values (median delays) and custom data for hover
        const zValues = [];
        const customData = [];

        for (let day = 0; day < 7; day++) {
            const zRow = [];
            const customRow = [];
            for (let hour = 0; hour < 24; hour++) {
                const cell = dayHourStats[day][hour];
                zRow.push(cell.median);
                customRow.push({
                    count: cell.count,
                    min: cell.min,
                    max: cell.max,
                    median: cell.median,
                    mean: cell.mean
                });
            }
            zValues.push(zRow);
            customData.push(customRow);
        }

        const trace = {
            z: zValues,
            x: hours,
            y: dayNames,
            type: 'heatmap',
            colorscale: [
                [0, '#059669'],      // 0 min - Green (on-time)
                [0.033, '#10b981'],  // 1 min - Green
                [0.067, '#34d399'],  // 2 min - Light green
                [0.167, '#a3e635'],  // 5 min - Lime green
                [0.33, '#fbbf24'],   // 10 min - Yellow
                [0.5, '#f97316'],    // 15 min - Orange
                [0.67, '#ef4444'],   // 20 min - Red
                [1, '#991b1b']       // 30 min - Dark red
            ],
            zmin: 0,
            zmax: 30,
            colorbar: {
                title: 'Median Delay (min)',
                titleside: 'right',
                tickvals: [0, 5, 10, 15, 20, 25, 30],
                ticktext: ['0', '5', '10', '15', '20', '25', '30']
            },
            customdata: customData,
            hovertemplate:
                '<b>%{y} %{x}</b><br>' +
                'Median: %{customdata.median} min<br>' +
                'Mean: %{customdata.mean} min<br>' +
                'Min: %{customdata.min} min<br>' +
                'Max: %{customdata.max} min<br>' +
                'Count: %{customdata.count}<extra></extra>',
            showscale: true
        };

        const layout = {
            title: {
                text: 'Delay Heatmap (Day × Hour)',
                font: { size: 16 }
            },
            xaxis: {
                title: 'Hour of Day',
                tickmode: 'linear',
                dtick: 2
            },
            yaxis: {
                title: 'Day of Week',
                autorange: 'reversed'
            },
            height: 300,
            margin: { t: 40, b: 60, l: 60, r: 100 },
            paper_bgcolor: 'white',
            autosize: true
        };

        const config = {
            responsive: true,
            displayModeBar: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['pan2d', 'lasso2d', 'select2d']
        };

        Plotly.newPlot(containerId, [trace], layout, config);
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

    showPrivacyPolicy() {
        const privacyContent = `
            <div style="max-width: 800px; margin: 0 auto; padding: 20px;">
                <h2 style="margin-bottom: 20px; color: #333;">Privacy Policy</h2>

                <h3 style="margin-top: 25px; margin-bottom: 10px; color: #555;">Data Collection</h3>
                <p style="line-height: 1.6; color: #666;">
                    This application collects and stores train departure data from the Deutsche Bahn IRIS API.
                    No personal user data is collected, stored, or transmitted. All data displayed is publicly
                    available information from Deutsche Bahn's public API.
                </p>

                <h3 style="margin-top: 25px; margin-bottom: 10px; color: #555;">Third-Party Services</h3>
                <p style="line-height: 1.6; color: #666;">
                    This application uses the following third-party services:
                </p>
                <ul style="line-height: 1.8; color: #666; margin-left: 20px;">
                    <li><strong>Deutsche Bahn IRIS API:</strong> Train data is retrieved from Deutsche Bahn's public API</li>
                    <li><strong>OpenStreetMap Nominatim:</strong> Used for geocoding station coordinates</li>
                    <li><strong>Leaflet.js:</strong> Map visualization library</li>
                    <li><strong>Plotly.js:</strong> Statistical visualization library</li>
                </ul>

                <h3 style="margin-top: 25px; margin-bottom: 10px; color: #555;">Cookies and Local Storage</h3>
                <p style="line-height: 1.6; color: #666;">
                    This application does not use cookies. It may use browser session storage temporarily
                    to maintain state when navigating between pages, which is cleared when you close the browser.
                </p>

                <h3 style="margin-top: 25px; margin-bottom: 10px; color: #555;">Data Usage</h3>
                <p style="line-height: 1.6; color: #666;">
                    The train departure data collected is used solely for:
                </p>
                <ul style="line-height: 1.8; color: #666; margin-left: 20px;">
                    <li>Displaying real-time and historical delay statistics</li>
                    <li>Generating visualizations of delay patterns</li>
                    <li>Providing route reliability information</li>
                </ul>

                <h3 style="margin-top: 25px; margin-bottom: 10px; color: #555;">Data Retention</h3>
                <p style="line-height: 1.6; color: #666;">
                    Train departure data is stored in a local SQLite database on the server hosting this application.
                    Data is retained indefinitely for historical analysis purposes. No user-identifiable information
                    is stored.
                </p>

                <h3 style="margin-top: 25px; margin-bottom: 10px; color: #555;">Open Source</h3>
                <p style="line-height: 1.6; color: #666;">
                    This application is open source and licensed under the MIT License. You can review the complete
                    source code and deployment configuration to verify privacy practices at:
                    <br>
                    <a href="https://github.com/lmst95/YourPersonalDbDepartureMonitor" target="_blank" style="color: #667eea; text-decoration: none;">
                        https://github.com/lmst95/YourPersonalDbDepartureMonitor
                    </a>
                </p>

                <h3 style="margin-top: 25px; margin-bottom: 10px; color: #555;">Contact</h3>
                <p style="line-height: 1.6; color: #666;">
                    For questions about data handling or privacy concerns, please open an issue on the
                    <a href="https://github.com/lmst95/YourPersonalDbDepartureMonitor/issues" target="_blank" style="color: #667eea; text-decoration: none;">
                        GitHub repository
                    </a>.
                </p>

                <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; font-size: 0.9em; color: #888;">
                    Last updated: January 2026
                </p>
            </div>
        `;

        this.modal.style.display = 'flex';
        document.getElementById('modalTitle').textContent = 'Privacy Policy';
        document.getElementById('modalContent').innerHTML = privacyContent;
    }
}

// Initialize application when DOM is ready
let app;
document.addEventListener('DOMContentLoaded', () => {
    app = new DBLiveTracker();
});
