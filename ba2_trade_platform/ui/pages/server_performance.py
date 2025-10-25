"""
Server Performance Monitoring Page
Displays real-time server metrics including CPU, memory, and network usage
"""

import psutil
import threading
import time
from datetime import datetime
from collections import deque
from nicegui import ui
from ...logger import logger

try:
    import plotly.graph_objects as go
except ImportError:
    logger.error("plotly not installed")
    go = None

# Track active HTTP clients
active_clients = {}  # {ip: timestamp}
clients_lock = threading.Lock()

# Data storage for historical metrics
class MetricsCollector:
    def __init__(self, max_history=3600):  # 3600 seconds = 1 hour
        self.max_history = max_history
        self.timestamps = deque(maxlen=max_history)
        self.cpu_percent = deque(maxlen=max_history)
        self.memory_percent = deque(maxlen=max_history)
        self.memory_mb = deque(maxlen=max_history)
        self.network_in_mb = deque(maxlen=max_history)
        self.network_out_mb = deque(maxlen=max_history)
        self.connections = deque(maxlen=max_history)
        self.active_clients = deque(maxlen=max_history)  # Track active HTTP clients
        
        self.lock = threading.Lock()
        self.collecting = True
        self.collection_thread = None
        
        # Start collection
        self._start_collection()
    
    def _start_collection(self):
        """Start background thread to collect metrics"""
        self.collection_thread = threading.Thread(target=self._collect_loop, daemon=True)
        self.collection_thread.start()
    
    def _collect_loop(self):
        """Collect metrics every second"""
        last_net = psutil.net_io_counters()
        
        while self.collecting:
            try:
                now = datetime.now()
                
                # CPU and Memory
                cpu = psutil.cpu_percent(interval=0.1)
                mem = psutil.virtual_memory()
                
                # Network
                net = psutil.net_io_counters()
                net_in = (net.bytes_recv - last_net.bytes_recv) / 1024 / 1024  # Convert to MB
                net_out = (net.bytes_sent - last_net.bytes_sent) / 1024 / 1024  # Convert to MB
                last_net = net
                
                # Connections
                try:
                    connections = len(psutil.net_connections())
                except:
                    connections = 0
                
                # Active HTTP clients
                active_clients_count = get_active_users_count()
                
                with self.lock:
                    self.timestamps.append(now)
                    self.cpu_percent.append(cpu)
                    self.memory_percent.append(mem.percent)
                    self.memory_mb.append(mem.used / 1024 / 1024)
                    self.network_in_mb.append(net_in)
                    self.network_out_mb.append(net_out)
                    self.connections.append(connections)
                    self.active_clients.append(active_clients_count)
                
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error collecting metrics: {e}")
                time.sleep(1)
    
    def get_current_metrics(self):
        """Get current system metrics"""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            cpu_count = psutil.cpu_count()
            uptime = datetime.now() - datetime.fromtimestamp(psutil.boot_time())
            
            return {
                'cpu_percent': cpu_percent,
                'cpu_count': cpu_count,
                'memory_percent': mem.percent,
                'memory_used_mb': mem.used / 1024 / 1024,
                'memory_total_mb': mem.total / 1024 / 1024,
                'memory_available_mb': mem.available / 1024 / 1024,
                'disk_percent': disk.percent,
                'disk_used_gb': disk.used / 1024 / 1024 / 1024,
                'disk_total_gb': disk.total / 1024 / 1024 / 1024,
                'uptime_days': uptime.days,
                'uptime_hours': uptime.seconds // 3600,
            }
        except Exception as e:
            logger.error(f"Error getting current metrics: {e}")
            return {}
    
    def get_history(self):
        """Get historical metrics"""
        with self.lock:
            return {
                'timestamps': list(self.timestamps),
                'cpu': list(self.cpu_percent),
                'memory_percent': list(self.memory_percent),
                'memory_mb': list(self.memory_mb),
                'network_in': list(self.network_in_mb),
                'network_out': list(self.network_out_mb),
                'connections': list(self.connections),
                'active_clients': list(self.active_clients),
            }
    
    def stop(self):
        """Stop collecting metrics"""
        self.collecting = False
        if self.collection_thread:
            self.collection_thread.join(timeout=2)

# Global metrics collector
_metrics_collector = MetricsCollector()

def get_active_connections_details():
    """Get detailed information about active connections from request headers"""
    with clients_lock:
        # Clean up old entries (older than 2 minutes)
        current_time = time.time()
        valid_clients = {ip: ts for ip, ts in active_clients.items() 
                        if current_time - ts < 120}
        active_clients.clear()
        active_clients.update(valid_clients)
        
        return [{
            'remote_ip': ip,
            'last_seen': datetime.fromtimestamp(ts).strftime('%H:%M:%S'),
            'status': 'ACTIVE'
        } for ip, ts in sorted(active_clients.items())]

def get_active_users_count():
    """Get count of active websocket connections to the website"""
    return len(get_active_connections_details())

def content() -> None:
    """Render server performance monitoring page"""
    
    logger.info("Loading Server Performance page")
    
    # Apply dark theme
    ui.dark_mode(True)
    
    with ui.column().classes('w-full'):
        # Header section
        with ui.row().classes('w-full items-center justify-between mb-8 px-6 pt-6'):
            with ui.column():
                ui.label('Server Performance').classes('text-4xl font-bold text-white')
                ui.label('Real-time system metrics (auto-refresh every 30 seconds)').classes('text-gray-400 text-sm mt-1')
        
        # Current metrics cards with circular progress indicators - Modern style
        with ui.row().classes('w-full gap-6 mb-8 px-6 justify-center items-start'):
            
            # Website Connections - Circular
            with ui.column().classes('items-center gap-4 flex-1 max-w-xs h-full'):
                with ui.element().classes('relative w-36 h-36'):
                    # SVG Circle
                    with ui.html('''
                        <div style="position: absolute; width: 140px; height: 140px;">
                            <svg width="140" height="140" style="transform: rotate(-90deg);">
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#374151" stroke-width="8"/>
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#818cf8" stroke-width="8" 
                                        stroke-dasharray="376.99" stroke-dashoffset="376.99" 
                                        style="transition: stroke-dashoffset 0.3s ease; filter: drop-shadow(0 0 10px rgba(129, 140, 248, 0.5));"
                                        id="conn-circle"/>
                            </svg>
                        </div>
                    ''', sanitize=False):
                        pass
                    # Value overlay (centered on circle)
                    conn_value_label = ui.label('0').classes('absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 text-3xl font-bold text-indigo-300')
                
                ui.label('Website Connections').classes('text-center font-semibold text-gray-300 text-sm uppercase tracking-wide')
                ui.label('active clients').classes('text-center text-xs text-gray-500')
                
                def show_connections_info():
                    """Show dialog with connection details"""
                    connections = get_active_connections_details()
                    
                    with ui.dialog() as dialog:
                        with ui.card().classes('bg-gray-900 border border-gray-700'):
                            ui.label('Active Client Connections').classes('text-2xl font-bold text-white mb-2')
                            ui.label('(Real IPs from Duo DNG reverse proxy)').classes('text-sm text-gray-400 mb-4')
                            
                            if not connections:
                                ui.label('No active connections').classes('text-gray-500')
                            else:
                                with ui.column().classes('w-full gap-3'):
                                    for i, conn in enumerate(connections, 1):
                                        with ui.card().classes('bg-gray-800 border border-gray-700 w-full'):
                                            ui.label(f"Client {i}").classes('font-bold text-indigo-300')
                                            ui.label(f"🌐 {conn['remote_ip']}").classes('text-sm text-gray-300 font-mono')
                                            ui.label(f"⏱️ Last Seen: {conn['last_seen']}").classes('text-xs text-gray-400')
                                            ui.label(f"✓ {conn['status']}").classes('text-xs text-green-400')
                            
                            with ui.row().classes('w-full justify-end gap-2 mt-6'):
                                ui.button('Close', on_click=dialog.close).props('flat').classes('text-gray-400 hover:text-white')
                    
                    dialog.open()
                
                users_label = ui.button('View Details', on_click=show_connections_info).props('flat').classes('w-full text-indigo-400 hover:text-indigo-300 text-sm font-semibold')
            
            # CPU - Circular
            with ui.column().classes('items-center gap-4 flex-1 max-w-xs h-full'):
                with ui.element().classes('relative w-36 h-36'):
                    # SVG Circle
                    with ui.html('''
                        <div style="position: absolute; width: 140px; height: 140px;">
                            <svg width="140" height="140" style="transform: rotate(-90deg);">
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#374151" stroke-width="8"/>
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#60a5fa" stroke-width="8" 
                                        stroke-dasharray="376.99" stroke-dashoffset="376.99" 
                                        style="transition: stroke-dashoffset 0.3s ease; filter: drop-shadow(0 0 10px rgba(96, 165, 250, 0.5));"
                                        id="cpu-circle"/>
                            </svg>
                        </div>
                    ''', sanitize=False):
                        pass
                    # Value overlay (centered on circle)
                    cpu_value_label = ui.label('0%').classes('absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 text-3xl font-bold text-blue-300')
                
                ui.label('CPU Usage').classes('text-center font-semibold text-gray-300 text-sm uppercase tracking-wide')
                cpu_count = ui.label('0 cores').classes('text-center text-xs text-gray-500')
            
            # Memory - Circular
            with ui.column().classes('items-center gap-4 flex-1 max-w-xs h-full'):
                with ui.element().classes('relative w-36 h-36'):
                    # SVG Circle
                    with ui.html('''
                        <div style="position: absolute; width: 140px; height: 140px;">
                            <svg width="140" height="140" style="transform: rotate(-90deg);">
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#374151" stroke-width="8"/>
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#34d399" stroke-width="8" 
                                        stroke-dasharray="376.99" stroke-dashoffset="376.99" 
                                        style="transition: stroke-dashoffset 0.3s ease; filter: drop-shadow(0 0 10px rgba(52, 211, 153, 0.5));"
                                        id="mem-circle"/>
                            </svg>
                        </div>
                    ''', sanitize=False):
                        pass
                    # Value overlay (centered on circle)
                    mem_value_label = ui.label('0%').classes('absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 text-3xl font-bold text-emerald-300')
                
                ui.label('Memory Usage').classes('text-center font-semibold text-gray-300 text-sm uppercase tracking-wide')
                mem_mb = ui.label('0 / 0 MB').classes('text-center text-xs text-gray-500')
            
            # Disk - Circular
            with ui.column().classes('items-center gap-4 flex-1 max-w-xs h-full'):
                with ui.element().classes('relative w-36 h-36'):
                    # SVG Circle
                    with ui.html('''
                        <div style="position: absolute; width: 140px; height: 140px;">
                            <svg width="140" height="140" style="transform: rotate(-90deg);">
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#374151" stroke-width="8"/>
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#fbbf24" stroke-width="8" 
                                        stroke-dasharray="376.99" stroke-dashoffset="376.99" 
                                        style="transition: stroke-dashoffset 0.3s ease; filter: drop-shadow(0 0 10px rgba(251, 191, 36, 0.5));"
                                        id="disk-circle"/>
                            </svg>
                        </div>
                    ''', sanitize=False):
                        pass
                    # Value overlay (centered on circle)
                    disk_value_label = ui.label('0%').classes('absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 text-3xl font-bold text-amber-300')
                
                ui.label('Disk Usage').classes('text-center font-semibold text-gray-300 text-sm uppercase tracking-wide')
                disk_space = ui.label('0 / 0 GB').classes('text-center text-xs text-gray-500')
            
            # Uptime - Circular (RIGHT)
            with ui.column().classes('items-center gap-4 flex-1 max-w-xs h-full'):
                with ui.element().classes('relative w-36 h-36'):
                    # SVG Circle
                    with ui.html('''
                        <div style="position: absolute; width: 140px; height: 140px;">
                            <svg width="140" height="140" style="transform: rotate(-90deg);">
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#374151" stroke-width="8"/>
                                <circle cx="70" cy="70" r="60" fill="none" stroke="#c084fc" stroke-width="8" 
                                        stroke-dasharray="376.99" stroke-dashoffset="0" 
                                        style="transition: stroke-dashoffset 0.3s ease; filter: drop-shadow(0 0 10px rgba(192, 132, 252, 0.5));"
                                        id="uptime-circle"/>
                            </svg>
                        </div>
                    ''', sanitize=False):
                        pass
                    # Value overlay (centered on circle)
                    uptime_value_label = ui.label('0d 0h').classes('absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 text-2xl font-bold text-fuchsia-300')
                
                ui.label('System Uptime').classes('text-center font-semibold text-gray-300 text-sm uppercase tracking-wide')
                connections_label = ui.label('running').classes('text-center text-xs text-gray-500')
        
        # Charts section
        ui.separator().classes('w-full my-8 bg-gray-700')
        
        with ui.tabs().classes('w-full text-gray-400') as tabs:
            # CPU Chart Tab
            with ui.tab('📊 CPU Usage (Last 1 Hour)'):
                cpu_plot_container = ui.row().classes('w-full')
                cpu_plot = None
            
            # Memory Chart Tab
            with ui.tab('💾 Memory Usage (Last 1 Hour)'):
                mem_plot_container = ui.row().classes('w-full')
                mem_plot = None
            
            # Network Chart Tab
            with ui.tab('🌐 Network Traffic (Last 1 Hour)'):
                net_plot_container = ui.row().classes('w-full')
                net_plot = None
            
            # Connections Chart Tab
            with ui.tab('🔗 Active Clients (Last 1 Hour)'):
                conn_plot_container = ui.row().classes('w-full')
                conn_plot = None
        
        # System Information
        ui.separator().classes('w-full my-8 bg-gray-700')
        
        with ui.card().classes('w-full bg-gray-900 border border-gray-700 mx-6 mb-6'):
            ui.label('System Information').classes('font-bold text-lg text-white mb-4')
            system_info = ui.label('Loading system information...').classes('text-sm text-gray-400 font-mono')
        
        # Auto-refresh metrics every 2 seconds
        def update_metrics():
            nonlocal cpu_plot, mem_plot, net_plot, conn_plot
            
            try:
                metrics = _metrics_collector.get_current_metrics()
                history = _metrics_collector.get_history()
                
                if metrics:
                    # Update circular indicator values using NiceGUI labels
                    cpu_percent = metrics['cpu_percent']
                    mem_percent = metrics['memory_percent']
                    disk_percent = metrics['disk_percent']
                    active_users = get_active_users_count()
                    uptime_days = metrics['uptime_days']
                    uptime_hours = metrics['uptime_hours']
                    
                    # Update value labels (these are actual NiceGUI labels, not HTML)
                    cpu_value_label.text = f'{cpu_percent:.1f}%'
                    mem_value_label.text = f'{mem_percent:.1f}%'
                    disk_value_label.text = f'{disk_percent:.1f}%'
                    conn_value_label.text = str(active_users)
                    uptime_value_label.text = f'{uptime_days}d {uptime_hours}h'
                    
                    # Also update the SVG circles with JavaScript for animation
                    cpu_offset = 376.99 * (1 - cpu_percent / 100)
                    mem_offset = 376.99 * (1 - mem_percent / 100)
                    disk_offset = 376.99 * (1 - disk_percent / 100)
                    
                    js_code = f"""
                    document.getElementById('cpu-circle')?.setAttribute('stroke-dashoffset', {cpu_offset});
                    document.getElementById('mem-circle')?.setAttribute('stroke-dashoffset', {mem_offset});
                    document.getElementById('disk-circle')?.setAttribute('stroke-dashoffset', {disk_offset});
                    """
                    
                    # Execute JavaScript for SVG animations
                    try:
                        ui.run_javascript(js_code)
                    except:
                        pass
                    
                    # Update text labels
                    cpu_count.text = f"{metrics['cpu_count']} cores"
                    mem_mb.text = f"{metrics['memory_used_mb']:.0f} / {metrics['memory_total_mb']:.0f} MB"
                    disk_space.text = f"{metrics['disk_used_gb']:.1f} / {metrics['disk_total_gb']:.1f} GB"
                    connections_label.text = 'running'
                    
                    # Update system info
                    try:
                        import socket
                        hostname = socket.gethostname()
                        load_avg = psutil.getloadavg()
                        system_info.text = f"Hostname: {hostname} | OS: {psutil.os.name} | Load: {load_avg[0]:.2f}, {load_avg[1]:.2f}, {load_avg[2]:.2f}"
                    except:
                        pass
                    
                    # Update charts with Plotly
                    if history['timestamps'] and len(history['cpu']) > 1 and go:
                        x_range = list(range(len(history['cpu'])))
                        
                        # CPU Chart
                        fig_cpu = go.Figure()
                        fig_cpu.add_trace(go.Scatter(
                            x=x_range,
                            y=history['cpu'],
                            mode='lines',
                            name='CPU %',
                            line=dict(color='#60a5fa', width=3),
                            fill='tozeroy',
                            fillcolor='rgba(96, 165, 250, 0.2)'
                        ))
                        fig_cpu.update_layout(
                            title='CPU Usage',
                            xaxis_title='Time (seconds)',
                            yaxis_title='CPU %',
                            yaxis=dict(range=[0, 100]),
                            height=350,
                            margin=dict(l=50, r=50, t=50, b=50),
                            hovermode='x unified',
                            template='plotly_dark',
                            paper_bgcolor='rgba(17, 24, 39, 1)',
                            plot_bgcolor='rgba(31, 41, 55, 1)',
                            font=dict(color='#d1d5db')
                        )
                        if cpu_plot:
                            cpu_plot_container.remove(cpu_plot)
                        cpu_plot = ui.plotly(fig_cpu).classes('w-full')
                        
                        # Memory Chart
                        fig_mem = go.Figure()
                        fig_mem.add_trace(go.Scatter(
                            x=x_range,
                            y=history['memory_mb'],
                            mode='lines',
                            name='Memory (MB)',
                            line=dict(color='#34d399', width=3),
                            fill='tozeroy',
                            fillcolor='rgba(52, 211, 153, 0.2)'
                        ))
                        fig_mem.update_layout(
                            title='Memory Usage',
                            xaxis_title='Time (seconds)',
                            yaxis_title='Memory (MB)',
                            height=350,
                            margin=dict(l=50, r=50, t=50, b=50),
                            hovermode='x unified',
                            template='plotly_dark',
                            paper_bgcolor='rgba(17, 24, 39, 1)',
                            plot_bgcolor='rgba(31, 41, 55, 1)',
                            font=dict(color='#d1d5db')
                        )
                        if mem_plot:
                            mem_plot_container.remove(mem_plot)
                        mem_plot = ui.plotly(fig_mem).classes('w-full')
                        
                        # Network Chart
                        fig_net = go.Figure()
                        fig_net.add_trace(go.Scatter(
                            x=x_range,
                            y=history['network_out'],
                            mode='lines',
                            name='Upload (MB/s)',
                            line=dict(color='#fbbf24', width=3)
                        ))
                        fig_net.add_trace(go.Scatter(
                            x=x_range,
                            y=history['network_in'],
                            mode='lines',
                            name='Download (MB/s)',
                            line=dict(color='#f87171', width=3)
                        ))
                        fig_net.update_layout(
                            title='Network Traffic',
                            xaxis_title='Time (seconds)',
                            yaxis_title='MB/s',
                            height=350,
                            margin=dict(l=50, r=50, t=50, b=50),
                            hovermode='x unified',
                            template='plotly_dark',
                            paper_bgcolor='rgba(17, 24, 39, 1)',
                            plot_bgcolor='rgba(31, 41, 55, 1)',
                            font=dict(color='#d1d5db')
                        )
                        if net_plot:
                            net_plot_container.remove(net_plot)
                        net_plot = ui.plotly(fig_net).classes('w-full')
                        
                        # Connections Chart
                        fig_conn = go.Figure()
                        fig_conn.add_trace(go.Scatter(
                            x=x_range,
                            y=history['active_clients'],
                            mode='lines',
                            name='Active Clients',
                            line=dict(color='#c084fc', width=3),
                            fill='tozeroy',
                            fillcolor='rgba(192, 132, 252, 0.2)'
                        ))
                        fig_conn.update_layout(
                            title='Active Clients',
                            xaxis_title='Time (seconds)',
                            yaxis_title='Count',
                            height=350,
                            margin=dict(l=50, r=50, t=50, b=50),
                            hovermode='x unified',
                            template='plotly_dark',
                            paper_bgcolor='rgba(17, 24, 39, 1)',
                            plot_bgcolor='rgba(31, 41, 55, 1)',
                            font=dict(color='#d1d5db')
                        )
                        if conn_plot:
                            conn_plot_container.remove(conn_plot)
                        conn_plot = ui.plotly(fig_conn).classes('w-full')
            
            except Exception as e:
                logger.error(f"Error updating metrics display: {e}")
        
        # Auto-refresh every 30 seconds
        ui.timer(30.0, update_metrics)
