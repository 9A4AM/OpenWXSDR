/**
 * OpenWXSDR - Common Receiver Statistics Functions
 * Shared between index.html and dashboard.html
 */

/**
 * Helper function to create integer histogram (for satellite counts)
 * @param {number[]} values - Array of numeric values
 * @returns {{labels: string[], counts: number[]}} - Histogram data
 */
function createIntegerHistogram(values) {
    if (!values || values.length === 0) {
        return { labels: [], counts: [] };
    }
    
    const min = Math.floor(Math.min(...values));
    const max = Math.ceil(Math.max(...values));
    
    const counts = {};
    const labels = [];
    
    // Create bins for each integer value
    for (let i = min; i <= max; i++) {
        labels.push(i.toString());
        counts[i] = 0;
    }
    
    // Count occurrences
    for (const value of values) {
        const rounded = Math.round(value);
        if (rounded >= min && rounded <= max) {
            counts[rounded]++;
        }
    }
    
    return { labels, counts: labels.map(l => counts[parseInt(l)]) };
}

/**
 * Get color for sonde type
 * @param {string} sondeType - Sonde type string
 * @returns {string} - CSS color code
 */
function getSondeTypeColor(sondeType) {
    const colors = {
        'RS41': '#007bff',
        'RS92': '#0056b3',
        'DFM': '#28a745',
        'DFM-09': '#1e7e34',
        'DFM-17': '#155724',
        'M10': '#ffc107',
        'M20': '#ff9800',
        'iMet': '#6c757d'
    };
    return colors[sondeType] || '#6c757d';
}

/**
 * Common Chart.js options for RX statistics charts
 */
const rxStatisticsChartDefaults = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: {
            display: true,
            position: 'top'
        }
    }
};

/**
 * Create frequency chart data with today's frequencies marked
 * @param {Object} frequencies - Frequency data with today/total counts
 * @returns {Object} - Chart.js data configuration
 */
function createFrequencyChartData(frequencies) {
    const freqLabels = Object.keys(frequencies).sort((a, b) => parseFloat(a) - parseFloat(b));
    const freqCountsToday = freqLabels.map(k => frequencies[k].today);
    const freqCountsOther = freqLabels.map(k => frequencies[k].total - frequencies[k].today);
    
    return {
        labels: freqLabels,
        datasets: [
            {
                label: 'Other days',
                data: freqCountsOther,
                backgroundColor: '#ffc107',
            },
            {
                label: 'Today',
                data: freqCountsToday,
                backgroundColor: '#cc8800',  // Darker orange
            }
        ]
    };
}

/**
 * Create altitude chart data with colored points by sonde type
 * @param {Array} sondeAltitudes - Array of sonde altitude objects
 * @param {string} label - Chart label
 * @param {string} borderColor - Line color
 * @returns {Object} - Chart.js data configuration
 */
function createAltitudeChartData(sondeAltitudes, label, borderColor) {
    const labels = sondeAltitudes.map(s => s.serial);
    const values = sondeAltitudes.map(s => s.altitude);
    const pointColors = sondeAltitudes.map(s => getSondeTypeColor(s.sonde_type));
    
    return {
        labels: labels,
        datasets: [{
            label: label,
            data: values,
            borderColor: borderColor,
            backgroundColor: borderColor.replace(')', ', 0.1)').replace('rgb', 'rgba'),
            fill: false,
            tension: 0.1,
            pointRadius: 4,
            pointBackgroundColor: pointColors
        }]
    };
}

/**
 * Main function to load and display receiver statistics
 * @param {Object} options - Configuration options
 * @param {string} options.currentActivityLogfile - Current activity log filename (optional, will auto-select if not provided)
 * @param {Object} options.rxStatisticsCharts - Chart instances object to update
 * @param {string} options.loadingModalId - ID of loading modal (optional, for index.html)
 * @param {Function} options.onBeforeShow - Callback before showing modal (optional)
 * @param {Function} options.onAfterShow - Callback after showing modal (optional)
 * @returns {Promise<void>}
 */
async function showRxStatisticsCommon(options) {
    const {
        currentActivityLogfile: providedLogfile,
        rxStatisticsCharts,
        loadingModalId,
        onBeforeShow,
        onAfterShow
    } = options;
    
    let currentActivityLogfile = providedLogfile;
    
    // Auto-select latest activity log if not provided
    if (!currentActivityLogfile) {
        try {
            const logfilesResponse = await fetch('/api/logfiles');
            const logfilesData = await logfilesResponse.json();
            
            const latestActivityLog = logfilesData.files.find(f => f.startsWith('openwxsdr_'));
            
            if (!latestActivityLog) {
                alert('No activity log files found in data/logs');
                return;
            }
            
            currentActivityLogfile = latestActivityLog;
            console.log('Auto-selected latest activity log:', currentActivityLogfile);
        } catch (error) {
            console.error('Error fetching logfiles:', error);
            alert('Failed to fetch log files');
            return;
        }
    }
    
    // Show loading modal if provided (index.html)
    let loadingModal = null;
    let loadingModalEl = null;
    if (loadingModalId) {
        loadingModalEl = document.getElementById(loadingModalId);
        if (loadingModalEl) {
            // Reset modal text before showing (prevent stale text from previous operations)
            const loadingModalTextEl = document.getElementById('loadingModalText');
            if (loadingModalTextEl) {
                loadingModalTextEl.textContent = 'Loading statistics...';
            }
            
            loadingModal = new bootstrap.Modal(loadingModalEl);
            
            await new Promise((resolve) => {
                loadingModalEl.addEventListener('shown.bs.modal', resolve, { once: true });
                loadingModal.show();
            });
        }
    }
    
    try {
        // Fetch statistics data
        console.log('Fetching RX statistics for', currentActivityLogfile);
        const response = await fetch(`/api/logfile/${currentActivityLogfile}/rx_statistics`);
        const data = await response.json();
        
        if (!data.success) {
            throw new Error(data.error || 'Failed to load statistics');
        }
        
        console.log('RX Statistics data:', data);
        
        // Hide loading modal if it was shown
        if (loadingModal && loadingModalEl) {
            await new Promise((resolve) => {
                loadingModalEl.addEventListener('hidden.bs.modal', resolve, { once: true });
                loadingModal.hide();
            });
        }
        
        // Destroy existing charts
        Object.values(rxStatisticsCharts).forEach(chart => {
            if (chart) chart.destroy();
        });
        
        // Update summary statistics
        document.getElementById('rxStatsTotalSondes').textContent = data.statistics.total_sondes;
        document.getElementById('rxStatsTotalFrames').textContent = data.statistics.total_frames;
        document.getElementById('rxStatsRecordedDays').textContent = data.statistics.recorded_days || '0';
        document.getElementById('rxStatsTotalFramesStopped').textContent = (data.statistics.total_frames_from_stopped || 0).toLocaleString();
        
        const maxSondesEl = document.getElementById('rxStatsMaxSondes');
        const maxSondesDateEl = document.getElementById('rxStatsMaxSondesDate');
        maxSondesEl.textContent = data.statistics.max_sondes_per_day || '0';
        if (data.statistics.max_sondes_date) {
            maxSondesDateEl.textContent = data.statistics.max_sondes_date;
            maxSondesDateEl.style.display = 'block';
        } else {
            maxSondesDateEl.style.display = 'none';
        }
        
        // Create sondes per day chart
        const sondePerDayLabels = Object.keys(data.sonde_timeline).sort();
        const sondePerDayCounts = sondePerDayLabels.map(k => data.sonde_timeline[k]);
        
        rxStatisticsCharts.sondesPerDay = new Chart(document.getElementById('sondesPerDayChart'), {
            type: 'bar',
            data: {
                labels: sondePerDayLabels,
                datasets: [{
                    label: 'Unique Sondes',
                    data: sondePerDayCounts,
                    backgroundColor: '#007bff',
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { display: true, title: { display: true, text: 'Date' } },
                    y: { display: true, title: { display: true, text: 'Number of Sondes' }, beginAtZero: true, ticks: { stepSize: 1 } }
                }
            }
        });
        
        // Create frames per day chart
        const framesPerDayLabels = Object.keys(data.frames_timeline).sort();
        const framesPerDayCounts = framesPerDayLabels.map(k => data.frames_timeline[k]);
        
        rxStatisticsCharts.framesPerDay = new Chart(document.getElementById('framesPerDayChart'), {
            type: 'bar',
            data: {
                labels: framesPerDayLabels,
                datasets: [{
                    label: 'Total Frames',
                    data: framesPerDayCounts,
                    backgroundColor: '#17a2b8',
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { display: true, title: { display: true, text: 'Date' } },
                    y: { display: true, title: { display: true, text: 'Number of Frames' }, beginAtZero: true, ticks: { stepSize: 100 } }
                }
            }
        });
        
        // Create sonde types chart
        const sondeTypeLabels = Object.keys(data.sonde_types).sort();
        const sondeTypeCounts = sondeTypeLabels.map(k => data.sonde_types[k]);
        const sondeTypeLabelsWithCounts = sondeTypeLabels.map((type, i) => `${type} (${sondeTypeCounts[i]})`);
        
        rxStatisticsCharts.sondeTypes = new Chart(document.getElementById('sondeTypesChart'), {
            type: 'bar',
            data: {
                labels: sondeTypeLabelsWithCounts,
                datasets: [{
                    label: 'Sondes/Type',
                    data: sondeTypeCounts,
                    backgroundColor: '#28a745',
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { display: true, title: { display: true, text: 'Sonde Type' } },
                    y: { display: true, title: { display: true, text: 'Count' }, beginAtZero: true, ticks: { stepSize: 1 } }
                }
            }
        });
        
        // Create frequencies chart with today marked
        const freqChartData = createFrequencyChartData(data.frequencies);
        rxStatisticsCharts.frequencies = new Chart(document.getElementById('frequenciesChart'), {
            type: 'bar',
            data: freqChartData,
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { display: true, title: { display: true, text: 'Frequency (MHz)' }, stacked: true },
                    y: { display: true, title: { display: true, text: 'Count' }, beginAtZero: true, ticks: { stepSize: 1 }, stacked: true }
                },
                plugins: {
                    legend: { display: true, position: 'top' }
                }
            }
        });
        
        // Create RSSI timeline chart
        if (data.rssi_timeline && data.rssi_timeline.length > 0) {
            const rssiTimelineData = data.rssi_timeline.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
            const rssiLabels = rssiTimelineData.map(d => d.timestamp);
            const rssiValues = rssiTimelineData.map(d => d.rssi);
            
            rxStatisticsCharts.rssiTimeline = new Chart(document.getElementById('rssiTimelineChart'), {
                type: 'line',
                data: {
                    labels: rssiLabels,
                    datasets: [{
                        label: 'RSSI (dBm)',
                        data: rssiValues,
                        borderColor: '#dc3545',
                        backgroundColor: 'rgba(220, 53, 69, 0.1)',
                        fill: false,
                        tension: 0.1,
                        pointRadius: 2
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { display: true, title: { display: true, text: 'Time' } },
                        y: { display: true, title: { display: true, text: 'RSSI (dBm)' } }
                    }
                }
            });
        }
        
        // Create satellites distribution chart
        if (data.sats_values && data.sats_values.length > 0) {
            const satsHistogram = createIntegerHistogram(data.sats_values);
            rxStatisticsCharts.satsDist = new Chart(document.getElementById('satsDistChart'), {
                type: 'bar',
                data: {
                    labels: satsHistogram.labels,
                    datasets: [{
                        label: 'Frequency',
                        data: satsHistogram.counts,
                        backgroundColor: '#6c757d',
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { display: true, title: { display: true, text: 'Satellite Count' } },
                        y: { display: true, title: { display: true, text: 'Count' }, beginAtZero: true, ticks: { stepSize: 1 } }
                    }
                }
            });
        }
        
        // Create first altitude chart
        if (data.sonde_altitudes && data.sonde_altitudes.length > 0) {
            const altChartData = createAltitudeChartData(data.sonde_altitudes, 'First Frame Altitude (m)', '#007bff');
            rxStatisticsCharts.altitudeBySonde = new Chart(document.getElementById('altitudeBySondeChart'), {
                type: 'line',
                data: altChartData,
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { display: true, title: { display: true, text: 'Sonde Serial' } },
                        y: { display: true, title: { display: true, text: 'Altitude (m)' }, beginAtZero: true }
                    }
                }
            });
        }
        
        // Create last altitude chart
        if (data.last_sonde_altitudes && data.last_sonde_altitudes.length > 0) {
            const lastAltChartData = createAltitudeChartData(data.last_sonde_altitudes, 'Last Frame Altitude (m)', '#dc3545');
            rxStatisticsCharts.lastAltitudeBySonde = new Chart(document.getElementById('lastAltitudeBySondeChart'), {
                type: 'line',
                data: lastAltChartData,
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { display: true, title: { display: true, text: 'Sonde Serial' } },
                        y: { display: true, title: { display: true, text: 'Altitude (m)' }, beginAtZero: true }
                    }
                }
            });
        }
        
        // Call before show callback
        if (onBeforeShow) {
            onBeforeShow(data);
        }
        
        // Show the RX statistics modal
        const rxStatsModal = new bootstrap.Modal(document.getElementById('rxStatisticsModal'));
        rxStatsModal.show();
        
        // Call after show callback
        if (onAfterShow) {
            onAfterShow(data);
        }
        
    } catch (error) {
        // Hide loading modal if shown
        if (loadingModal && loadingModalEl) {
            await new Promise((resolve) => {
                loadingModalEl.addEventListener('hidden.bs.modal', resolve, { once: true });
                loadingModal.hide();
            });
        }
        
        console.error('Error loading RX statistics:', error);
        alert('Error loading RX statistics: ' + error.message);
    }
}
