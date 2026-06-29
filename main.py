import sys, time, threading, psutil, os, json, logging
from collections import defaultdict
from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout, QSystemTrayIcon, QMenu, QStyle, QTabWidget, QTableWidget, QTableWidgetItem, QFormLayout, QSpinBox, QDoubleSpinBox, QCheckBox, QMessageBox, QAbstractItemView
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtCore import QTimer
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sklearn.ensemble import IsolationForest
import numpy as np
from scapy.all import sniff, IP, TCP, conf
import pyqtgraph as pg
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import QTimer
import joblib
import shutil
from datetime import datetime, timezone
import csv
import statistics
import smtplib
from email.message import EmailMessage
import requests
from logging.handlers import RotatingFileHandler

# ================== GLOBALS ==================
CONFIG_FILE = 'config.json'
INCIDENTS_FILE = 'incidents.json'
MODEL_FILE = 'ai_model.joblib'
MODEL_META = MODEL_FILE + '.meta.json'

LAST_INCIDENT = 0
COOLDOWN = 60

attack_ips = defaultdict(list)

# defaults (will be merged with config file)
DEFAULT_CONFIG = {
    "auto_block": False,
    "alert_email": None,
    "siem_endpoint": None,
    "monitor_interval": 2,
    "ai": {"contamination": 0.03},
    # evaluation/rollback params
    "ai_eval_min_samples": 200,
    "ai_eval_rollback_drop": 0.02,
    "dark_mode": True
}

ai_model = None
ai_trained = False
CONFIG = {}

logger = None
FEATURE_BUFFER = []
FEATURE_FILE = 'ai_data.json'
SCORE_HISTORY = []
MAX_HISTORY = 600

# AI runtime defaults
DEFAULT_CONFIG.update({
    "ai_retrain_interval": 300,
    "ai_buffer_size": 1000,
    "ai_score_threshold": -0.2,
    "ai_feature_save_interval": 60
    ,"ai_eval_min_samples": 200,
    "ai_eval_rollback_drop": 0.02
})
INCIDENTS = []
ASSET_ICON = os.path.join('assets','icon.svg')
MONITORING = True

DARK_THEME = '''
QWidget { background-color: #121212; color: #e0e0e0; }
QLabel { color: #e0e0e0; }
QPushButton { background-color: #1f1f1f; color: #e0e0e0; border: 1px solid #333; padding: 6px; }
QTextEdit { background-color: #0d0d0d; color: #e0e0e0; }
QListWidget { background-color: #0d0d0d; color: #e0e0e0; }
QMenu { background-color: #1b1b1b; color: #e0e0e0; }
'''

# ================== INCIDENT ==================
def log_incident(data):
    global LAST_INCIDENT
    now = time.time()
    if now - LAST_INCIDENT < COOLDOWN:
        return
    LAST_INCIDENT = now
    data["time"] = time.ctime()
    INCIDENTS.append(data)
    ui.log(f"⚠ {data['type']} detected")
    try:
        ui.refresh_incident_list()
    except Exception:
        pass
    save_incidents()
    send_alert(data)
    if CONFIG.get("auto_block") and data.get("ip"):
        try_block_ip(data.get("ip"))

def save_incidents():
    try:
        with open(INCIDENTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(INCIDENTS, f, indent=2)
        logger.info("Incidents persisted")
    except Exception as e:
        logger.exception("Failed to save incidents: %s", e)

def load_incidents():
    global INCIDENTS
    if os.path.exists(INCIDENTS_FILE):
        try:
            with open(INCIDENTS_FILE, 'r', encoding='utf-8') as f:
                INCIDENTS = json.load(f)
        except Exception:
            INCIDENTS = []

# ================== PDF ==================
def generate_pdf():
    if not INCIDENTS:
        return
    fname = f"incident_{int(time.time())}.pdf"
    c = canvas.Canvas(fname, pagesize=A4)
    y = 800
    c.setFont("Helvetica", 10)

    c.drawString(40, y, "SYSTEM GUARD X - INCIDENT REPORT")
    y -= 30

    for inc in INCIDENTS:
        for k, v in inc.items():
            c.drawString(40, y, f"{k}: {v}")
            y -= 15
        y -= 10
        if y < 50:
            c.showPage()
            y = 800

    c.save()
    ui.log("📄 Incident PDF Generated")
    logger.info("PDF generated: %s", fname)

# ================== NETWORK ==================
def packet_monitor():
    def analyze(pkt):
        try:
            if not MONITORING:
                return
            if pkt.haslayer(IP) and pkt.haslayer(TCP):
                ip = pkt[IP].src
                now = time.time()
                attack_ips[ip].append(now)
                attack_ips[ip] = [t for t in attack_ips[ip] if now - t < 10]
                if len(attack_ips[ip]) > 120:
                    log_incident({"type": "Network Flood", "ip": ip, "packets": len(attack_ips[ip])})
        except Exception:
            # protect analyzer from unexpected packet parsing errors
            logger.exception('Packet analysis failed')

    try:
        sniff(prn=analyze, store=False)
    except Exception as e:
        logger.exception('L2 sniff failed: %s', e)
        try:
            # Try an L3 socket fallback (works when WinPcap/Npcap is not available)
            try:
                ui.log('⚠ Packet capture L2 unavailable; falling back to L3 (may miss link-layer frames)')
            except Exception:
                pass
            sniff(prn=analyze, store=False, L3socket=conf.L3socket)
        except Exception as e2:
            logger.exception('L3 sniff fallback failed: %s', e2)
            try:
                ui.log('❌ Packet monitoring disabled (sniff failed)')
            except Exception:
                pass
            return

# monitoring controls
def pause_monitoring():
    global MONITORING
    MONITORING = False
    try:
        if ui:
            ui.log('⏸ Monitoring paused')
            ui.status.setText('Status: PAUSED ⚪')
            ui.update_tray_menu()
    except Exception:
        pass
    logger.info('Monitoring paused')

def resume_monitoring():
    global MONITORING
    MONITORING = True
    try:
        if ui:
            ui.log('▶ Monitoring resumed')
            ui.status.setText('Status: NORMAL 🟢')
            ui.update_tray_menu()
    except Exception:
        pass
    logger.info('Monitoring resumed')

def toggle_monitoring():
    if MONITORING:
        pause_monitoring()
    else:
        resume_monitoring()


def open_logs():
    try:
        logfile = 'system_guard.log'
        if os.path.exists(logfile):
            if os.name == 'nt':
                os.startfile(logfile)
            else:
                import subprocess
                opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
                try:
                    subprocess.Popen([opener, logfile])
                except Exception:
                    logger.exception('Failed to open log file with subprocess')
        else:
            try:
                ui.log('No log file found')
            except Exception:
                pass
    except Exception:
        logger.exception('Failed to open logs')

def show_error_message(msg, title='System Guard X Error'):
    """Show an error message in GUI if available, otherwise print to console."""
    try:
        if app is not None and ui is not None:
            QMessageBox.critical(ui, title, str(msg))
        else:
            print(f"{title}: {msg}")
    except Exception:
        try:
            print(f"{title}: {msg}")
        except Exception:
            pass

# ================== PROCESS ==================
SUSPICIOUS = ["miner", "keylog", "stealer", "hack"]

def scan_processes():
    for p in psutil.process_iter(['pid','name']):
        try:
            name = p.info['name'].lower()
            if any(x in name for x in SUSPICIOUS):
                log_incident({"type":"Suspicious Process","process":name,"pid":p.info['pid']})
        except:
            pass

# ================== AI ==================
def ai_train():
    # legacy: train on a short live sample (kept for backward-compat)
    data = []
    for _ in range(30):
        data.append([
            psutil.cpu_percent(),
            psutil.virtual_memory().percent,
            len(psutil.pids()),
            psutil.net_io_counters().bytes_sent,
            psutil.net_io_counters().bytes_recv
        ])
        time.sleep(1)
    train_model(np.array(data))

def train_model(npdata):
    """Train the AI model on provided numpy array of features."""
    global ai_trained, ai_model
    try:
        # train a candidate model first
        candidate = IsolationForest(contamination=CONFIG.get('ai', {}).get('contamination', 0.03))
        candidate.fit(npdata)

        # evaluate candidate on a small holdout if possible
        eval_min = CONFIG.get('ai_eval_min_samples', 200)
        try:
            if len(npdata) >= eval_min:
                split = max(2, int(len(npdata) * 0.8))
                X_test = npdata[split:]
            elif len(FEATURE_BUFFER) >= eval_min:
                X_test = np.array(FEATURE_BUFFER[-eval_min:])
            else:
                X_test = None

            cand_score = None
            prev_score = None
            if X_test is not None:
                cand_eval = evaluate_model(candidate, X_test)
                cand_score = cand_eval['mean_score']
            # if previous model exists, compare
            if os.path.exists(MODEL_FILE):
                try:
                    prev = joblib.load(MODEL_FILE)
                    if X_test is not None:
                        prev_eval = evaluate_model(prev, X_test)
                        prev_score = prev_eval['mean_score']
                except Exception:
                    prev_score = None

            # decide replace/rollback
            replace = True
            if cand_score is not None and prev_score is not None:
                drop = CONFIG.get('ai_eval_rollback_drop', 0.02)
                # if candidate mean score drops by more than drop, reject
                if cand_score < (prev_score - drop):
                    replace = False
                    logger.warning('Candidate model rejected: cand_score=%s prev_score=%s drop=%s', cand_score, prev_score, drop)

            if replace:
                # backup current model
                try:
                    if os.path.exists(MODEL_FILE):
                        shutil.copy(MODEL_FILE, MODEL_FILE + '.bak')
                except Exception:
                    logger.exception('Failed to backup previous model')
                # accept candidate
                ai_model = candidate
                ai_trained = True
                save_model()
                meta = {'trained_at': datetime.now(timezone.utc).isoformat(), 'samples': len(npdata), 'score': cand_score}
                try:
                    with open(MODEL_META, 'w', encoding='utf-8') as f:
                        json.dump(meta, f)
                except Exception:
                    logger.exception('Failed to write model meta')
                logger.info("AI model trained and persisted (samples=%s, score=%s)", len(npdata), cand_score)
                try:
                    ui.log(f"🤖 AI trained on {len(npdata)} samples (score={cand_score})")
                    ui.update_ai_status()
                except Exception:
                    pass
            else:
                logger.warning('Training rolled back - candidate not accepted')
                try:
                    ui.log('⚠️ AI retrain skipped (worse than previous model)')
                except Exception:
                    pass
        except Exception:
            # fallback: accept candidate
            ai_model = candidate
            ai_trained = True
            save_model()
            logger.info("AI model trained and persisted (samples=%s)", len(npdata))
            try:
                ui.log(f"🤖 AI trained on {len(npdata)} samples")
                ui.update_ai_status()
            except Exception:
                pass
    except Exception:
        logger.exception('Failed to train model')

def evaluate_model(model, X_test):
    """Evaluate given model on X_test and return mean decision_function score and anomaly count."""
    res = {'mean_score': None, 'anomalies': None}
    try:
        scores = model.decision_function(X_test)
        preds = model.predict(X_test)
        res['mean_score'] = float(np.mean(scores))
        res['anomalies'] = int((preds == -1).sum())
    except Exception:
        logger.exception('Model evaluation failed')
    return res

def rollback_model():
    """Restore previous model backup if available."""
    global ai_model, ai_trained
    try:
        bak = MODEL_FILE + '.bak'
        if os.path.exists(bak):
            shutil.copy(bak, MODEL_FILE)
            load_model()
            ui.log('🔁 Model rolled back to previous version')
            logger.info('Model rollback performed')
            try:
                ui.update_ai_status()
            except Exception:
                pass
        else:
            ui.log('No backup model available to rollback')
    except Exception:
        logger.exception('Rollback failed')

def ai_detect(cpu, ram, proc):
    if not ai_trained or ai_model is None:
        return
    X = np.array([[cpu, ram, proc, psutil.net_io_counters().bytes_sent, psutil.net_io_counters().bytes_recv]])
    try:
        # decision_function gives anomaly score; predict returns -1 for anomalies
        pred = ai_model.predict(X)[0]
        score = float(ai_model.decision_function(X)[0])
        logger.debug('AI score %s pred %s', score, pred)
        # record score history
        try:
            SCORE_HISTORY.append(score)
            if len(SCORE_HISTORY) > MAX_HISTORY:
                del SCORE_HISTORY[0:len(SCORE_HISTORY)-MAX_HISTORY]
        except Exception:
            logger.exception('Failed to record score history')

        if pred == -1 or score < CONFIG.get('ai_score_threshold', -0.2):
            log_incident({"type":"AI Anomaly","cpu":cpu,"ram":ram,"proc":proc,"score":score})
    except Exception:
        logger.exception("AI detection failed")
    try:
        if ui:
            ui.update_tray_menu()
    except Exception:
        pass

def record_metrics(cpu, ram, proc):
    """Append metrics to the rolling feature buffer (persist periodically)."""
    try:
        entry = [cpu, ram, proc, psutil.net_io_counters().bytes_sent, psutil.net_io_counters().bytes_recv]
        FEATURE_BUFFER.append(entry)
        maxsz = CONFIG.get('ai_buffer_size', 1000)
        if len(FEATURE_BUFFER) > maxsz:
            del FEATURE_BUFFER[0:len(FEATURE_BUFFER)-maxsz]
    except Exception:
        logger.exception('Failed to record metrics')

def save_feature_buffer():
    try:
        with open(FEATURE_FILE, 'w', encoding='utf-8') as f:
            json.dump(FEATURE_BUFFER, f)
        logger.info('Feature buffer saved (%d samples)', len(FEATURE_BUFFER))
    except Exception:
        logger.exception('Failed to save feature buffer')

def load_feature_buffer():
    global FEATURE_BUFFER
    if os.path.exists(FEATURE_FILE):
        try:
            with open(FEATURE_FILE, 'r', encoding='utf-8') as f:
                FEATURE_BUFFER = json.load(f)
        except Exception:
            FEATURE_BUFFER = []

def retrain_worker():
    """Background thread that retrains the model periodically using the buffer."""
    while True:
        try:
            interval = CONFIG.get('ai_retrain_interval', 300)
            time.sleep(interval)
            retrain_from_buffer()
        except Exception:
            logger.exception('Retrain worker failed')

def retrain_from_buffer(min_samples=200):
    if len(FEATURE_BUFFER) < min_samples:
        logger.info('Not enough samples to retrain (have %d, need %d)', len(FEATURE_BUFFER), min_samples)
        return
    X = np.array(FEATURE_BUFFER)
    train_model(X)
    save_feature_buffer()

def save_model():
    try:
        joblib.dump(ai_model, MODEL_FILE)
    except Exception:
        logger.exception("Failed to save model")

def load_model():
    global ai_model, ai_trained
    if os.path.exists(MODEL_FILE):
        try:
            ai_model = joblib.load(MODEL_FILE)
            ai_trained = True
            logger.info("AI model loaded from disk")
        except Exception:
            logger.exception("Failed to load AI model")

# ================== MONITOR ==================
def monitor():
    load_model()
    if not ai_trained:
        ai_train()
    last_feature_save = time.time()
    while True:
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        proc = len(psutil.pids())

        ui.update_stats(cpu, ram, proc)
        ui.log(f"CPU:{cpu}% RAM:{ram}% PROC:{proc}")

        if cpu > 55 and ram > 92:
            log_incident({"type":"System Overload","cpu":cpu,"ram":ram,"proc":proc})

        # record metrics for AI buffer
        record_metrics(cpu, ram, proc)

        ai_detect(cpu, ram, proc)
        scan_processes()

        # periodic save of feature buffer
        if time.time() - last_feature_save > CONFIG.get('ai_feature_save_interval', 60):
            save_feature_buffer()
            last_feature_save = time.time()

        time.sleep(CONFIG.get('monitor_interval', 2))

# ================== UI ==================
class UI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("World's Most Advanced - System Guard X – REAL Security Monitor")
        self.resize(1000,700)
        try:
            if os.path.exists(ASSET_ICON):
                self.setWindowIcon(QIcon(ASSET_ICON))
        except Exception:
            pass

        self.status = QLabel("Status: NORMAL 🟢")
        self.stats = QLabel("CPU:0 RAM:0 PROC:0")
        self.ai_status = QLabel("AI: initializing…")
        self.logbox = QTextEdit()
        self.logbox.setReadOnly(True)
        # Realtime plots
        self.cpu_plot = pg.PlotWidget(title="CPU %")
        self.cpu_plot.showGrid(x=True, y=True)
        self.cpu_plot.setYRange(0, 100)
        self.cpu_curve = self.cpu_plot.plot(pen=pg.mkPen('r', width=2))

        self.ram_plot = pg.PlotWidget(title="RAM %")
        self.ram_plot.showGrid(x=True, y=True)
        self.ram_plot.setYRange(0, 100)
        self.ram_curve = self.ram_plot.plot(pen=pg.mkPen('b', width=2))

        self.score_plot = pg.PlotWidget(title="AI Score (decision_function)")
        self.score_plot.showGrid(x=True, y=True)
        self.score_curve = self.score_plot.plot(pen=pg.mkPen('y', width=2))
        
        # Dashboard widgets (summary)
        self.summary_cpu = QLabel("Avg CPU: -- %")
        self.summary_ram = QLabel("Avg RAM: -- %")
        self.summary_anomalies = QLabel("Anomalies (24h): --")
        self.summary_incidents = QLabel("Incidents: 0")
        btn = QPushButton("Export Incident PDF")
        btn.clicked.connect(generate_pdf)
        btn_json = QPushButton("Export Incidents JSON")
        btn_json.clicked.connect(lambda: self.export_json())
        btn_siem = QPushButton("Send to SIEM")
        btn_siem.clicked.connect(lambda: send_to_siem())
        btn_retrain = QPushButton("Retrain AI Now")
        btn_retrain.clicked.connect(lambda: threading.Thread(target=retrain_from_buffer, daemon=True).start())
        btn_rollback = QPushButton("Rollback Model")
        btn_rollback.clicked.connect(lambda: threading.Thread(target=rollback_model, daemon=True).start())

        # incidents table (for dashboard/incidents tab)
        self.incident_table = QTableWidget()
        self.incident_table.setColumnCount(4)
        self.incident_table.setHorizontalHeaderLabels(['Time','Type','Source','Details'])
        self.incident_table.verticalHeader().setVisible(False)
        self.incident_table.setAlternatingRowColors(True)
        # set selection behavior and mode in a PyQt-version compatible way
        try:
            # try modern enum API
            self.incident_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        except Exception:
            try:
                self.incident_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            except Exception:
                try:
                    self.incident_table.setSelectionBehavior(self.incident_table.SelectRows)
                except Exception:
                    logger.warning('Could not set SelectRows on incident table; leaving default')

        # selection mode (single selection preferred)
        try:
            self.incident_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        except Exception:
            try:
                self.incident_table.setSelectionMode(QAbstractItemView.SingleSelection)
            except Exception:
                try:
                    self.incident_table.setSelectionMode(self.incident_table.SingleSelection)
                except Exception:
                    logger.warning('Could not set SingleSelection on incident table; leaving default')

        # UX tweaks (wrap in tries for compatibility)
        try:
            self.incident_table.setSortingEnabled(True)
        except Exception:
            pass
        try:
            self.incident_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        except Exception:
            try:
                self.incident_table.setEditTriggers(self.incident_table.NoEditTriggers)
            except Exception:
                pass
        try:
            self.incident_table.horizontalHeader().setStretchLastSection(True)
        except Exception:
            pass
        try:
            self.incident_table.resizeColumnsToContents()
        except Exception:
            pass

        # build dashboard tab
        dashboard_widget = QWidget()
        dash_layout = QVBoxLayout()
        top_row = QHBoxLayout()
        top_row.addWidget(self.summary_cpu)
        top_row.addWidget(self.summary_ram)
        top_row.addWidget(self.summary_anomalies)
        top_row.addWidget(self.summary_incidents)
        dash_layout.addLayout(top_row)
        dash_layout.addWidget(self.cpu_plot)
        dash_layout.addWidget(self.ram_plot)
        dash_layout.addWidget(self.score_plot)
        dash_layout.addWidget(self.logbox)
        dashboard_widget.setLayout(dash_layout)

        # incidents tab
        incidents_widget = QWidget()
        inc_layout = QVBoxLayout()
        inc_layout.addWidget(self.incident_table)
        incidents_widget.setLayout(inc_layout)

        # settings tab
        settings_widget = QWidget()
        form = QFormLayout()
        self.input_threshold = QDoubleSpinBox()
        self.input_threshold.setRange(-10.0, 10.0)
        self.input_threshold.setSingleStep(0.01)
        self.input_threshold.setValue(CONFIG.get('ai_score_threshold', -0.2))

        self.input_retrain = QSpinBox()
        self.input_retrain.setRange(10, 3600)
        self.input_retrain.setValue(CONFIG.get('ai_retrain_interval', 300))

        self.input_buffer = QSpinBox()
        self.input_buffer.setRange(100, 100000)
        self.input_buffer.setValue(CONFIG.get('ai_buffer_size', 1000))

        self.input_dark = QCheckBox('Dark Mode')
        self.input_dark.setChecked(CONFIG.get('dark_mode', True))

        form.addRow('AI score threshold', self.input_threshold)
        form.addRow('Retrain interval (s)', self.input_retrain)
        form.addRow('Buffer size', self.input_buffer)
        form.addRow(self.input_dark)
        btn_save = QPushButton('Save Settings')
        btn_save.clicked.connect(self.save_settings)
        form.addRow(btn_save)

        btn_exp_inc = QPushButton('Export Incidents CSV')
        btn_exp_inc.clicked.connect(lambda: self.export_incidents_csv())
        form.addRow(btn_exp_inc)

        btn_exp_logs = QPushButton('Export Logs CSV')
        btn_exp_logs.clicked.connect(lambda: self.export_logs_csv())
        form.addRow(btn_exp_logs)

        btn_analyze = QPushButton('Analyze Logs')
        btn_analyze.clicked.connect(lambda: self.analyze_logs())
        form.addRow(btn_analyze)

        btn_autotune = QPushButton('Auto-tune AI Threshold')
        btn_autotune.clicked.connect(lambda: self.auto_tune_threshold())
        form.addRow(btn_autotune)

        settings_widget.setLayout(form)

        # tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(dashboard_widget, 'Dashboard')
        self.tabs.addTab(incidents_widget, 'Incidents')
        self.tabs.addTab(settings_widget, 'Settings')

        left_layout = QVBoxLayout()
        left_layout.addWidget(self.status)
        left_layout.addWidget(self.ai_status)
        left_layout.addWidget(self.stats)
        left_layout.addWidget(self.tabs)

        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("Recent Incidents:"))
        # keep a small list and full table (table in tab)
        right_layout.addWidget(self.incident_table)
        right_layout.addWidget(btn)
        right_layout.addWidget(btn_json)
        right_layout.addWidget(btn_siem)
        right_layout.addWidget(btn_retrain)
        right_layout.addWidget(btn_rollback)

        layout = QHBoxLayout()
        layout.addLayout(left_layout, 2)
        layout.addLayout(right_layout, 1)
        self.setLayout(layout)
        self.refresh_incident_list()
        # plot update timer
        self.plot_timer = QTimer()
        self.plot_timer.setInterval(1000)
        self.plot_timer.timeout.connect(self.update_plots)
        self.plot_timer.start()
        # dashboard update timer
        self.dash_timer = QTimer()
        self.dash_timer.setInterval(1000)
        self.dash_timer.timeout.connect(self.update_dashboard)
        self.dash_timer.start()

        # system tray
        try:
            icon = QIcon(ASSET_ICON) if os.path.exists(ASSET_ICON) else self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            tray = QSystemTrayIcon(icon, parent=self)
            menu = QMenu()
            show_action = QAction('Show', self)
            show_action.triggered.connect(lambda: self.show())
            menu.addAction(show_action)

            self.pause_action = QAction('Pause Monitoring' if MONITORING else 'Resume Monitoring', self)
            def _pause_triggered():
                toggle_monitoring()
                self.update_tray_menu()
            self.pause_action.triggered.connect(_pause_triggered)
            menu.addAction(self.pause_action)

            menu.addAction(QAction('Export Graphs', self, triggered=lambda: self.export_graphs()))
            menu.addAction(QAction('Open Logs', self, triggered=lambda: open_logs()))
            menu.addAction(QAction('Exit', self, triggered=lambda: os._exit(0)))
            tray.setContextMenu(menu)
            tray.show()
            self.tray = tray
        except Exception:
            pass

    def refresh_incident_list(self):
        try:
            # populate table
            self.incident_table.setRowCount(0)
            for inc in INCIDENTS[-200:][::-1]:
                t = inc.get('time', '')
                ty = inc.get('type', '')
                src = inc.get('ip') or inc.get('process') or ''
                details = json.dumps({k:v for k,v in inc.items() if k not in ('time','type','ip','process')})
                row = self.incident_table.rowCount()
                self.incident_table.insertRow(row)
                self.incident_table.setItem(row, 0, QTableWidgetItem(t))
                self.incident_table.setItem(row, 1, QTableWidgetItem(ty))
                self.incident_table.setItem(row, 2, QTableWidgetItem(src))
                self.incident_table.setItem(row, 3, QTableWidgetItem(details))
        except Exception:
            logger.exception('Failed to refresh incident list')

    def update_dashboard(self):
        try:
            # averages
            if FEATURE_BUFFER:
                cpu_vals = [f[0] for f in FEATURE_BUFFER[-MAX_HISTORY:]]
                ram_vals = [f[1] for f in FEATURE_BUFFER[-MAX_HISTORY:]]
                avg_cpu = sum(cpu_vals)/len(cpu_vals)
                avg_ram = sum(ram_vals)/len(ram_vals)
                self.summary_cpu.setText(f"Avg CPU: {avg_cpu:.1f} %")
                self.summary_ram.setText(f"Avg RAM: {avg_ram:.1f} %")
            # anomalies and incidents
            self.summary_anomalies.setText(f"Anomalies (window): {len([s for s in SCORE_HISTORY if s < CONFIG.get('ai_score_threshold', -0.2)])}")
            self.summary_incidents.setText(f"Incidents: {len(INCIDENTS)}")
        except Exception:
            logger.exception('Failed to update dashboard')

    def save_settings(self):
        try:
            CONFIG['ai_score_threshold'] = float(self.input_threshold.value())
            CONFIG['ai_retrain_interval'] = int(self.input_retrain.value())
            CONFIG['ai_buffer_size'] = int(self.input_buffer.value())
            CONFIG['dark_mode'] = bool(self.input_dark.isChecked())
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(CONFIG, f, indent=2)
            ui.log('⚙ Settings saved')
            # apply dark mode immediately
            if CONFIG.get('dark_mode'):
                app.setStyleSheet(DARK_THEME)
            else:
                app.setStyleSheet('')
        except Exception:
            logger.exception('Failed to save settings')

    def export_incidents_csv(self):
        try:
            if not INCIDENTS:
                ui.log('No incidents to export')
                return
            keys = set()
            for inc in INCIDENTS:
                keys.update(inc.keys())
            keys = list(sorted(keys))
            fname = f'incidents_{int(time.time())}.csv'
            with open(fname, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                for inc in INCIDENTS:
                    writer.writerow({k: inc.get(k, '') for k in keys})
            ui.log(f'📤 Incidents exported to {fname}')
        except Exception:
            logger.exception('Failed to export incidents CSV')

    def export_logs_csv(self):
        try:
            logf = 'system_guard.log'
            if not os.path.exists(logf):
                ui.log('No log file to export')
                return
            out = f'logs_{int(time.time())}.csv'
            with open(logf, 'r', encoding='utf-8', errors='replace') as src, open(out, 'w', newline='', encoding='utf-8') as dst:
                writer = csv.writer(dst)
                writer.writerow(['timestamp','level','message'])
                for line in src:
                    parts = line.strip().split(' ', 2)
                    if len(parts) >= 3:
                        ts = parts[0] + ' ' + parts[1].rstrip(':')
                        rest = parts[2]
                        if ':' in rest:
                            lvl, msg = rest.split(':',1)
                            writer.writerow([ts, lvl.strip(), msg.strip()])
                        else:
                            writer.writerow([ts, '', rest.strip()])
                    else:
                        writer.writerow(['', '', line.strip()])
            ui.log(f'📤 Logs exported to {out}')
        except Exception:
            logger.exception('Failed to export logs CSV')

    def analyze_logs(self):
        try:
            report = {}
            if FEATURE_BUFFER:
                cpu = [f[0] for f in FEATURE_BUFFER]
                ram = [f[1] for f in FEATURE_BUFFER]
                report['cpu_mean'] = statistics.mean(cpu)
                report['cpu_max'] = max(cpu)
                report['cpu_p95'] = float(np.percentile(cpu,95))
                report['ram_mean'] = statistics.mean(ram)
                report['ram_max'] = max(ram)
                report['ram_p95'] = float(np.percentile(ram,95))
            else:
                report['cpu_mean'] = None
            if SCORE_HISTORY:
                s = SCORE_HISTORY
                report['score_mean'] = float(np.mean(s))
                report['score_std'] = float(np.std(s))
                report['score_p5'] = float(np.percentile(s,5))
                report['score_p95'] = float(np.percentile(s,95))
                report['anomaly_count'] = len([x for x in s if x < CONFIG.get('ai_score_threshold', -0.2)])
            else:
                report['score_mean'] = None
            report['incidents'] = len(INCIDENTS)
            fname = f'report_{int(time.time())}.json'
            with open(fname, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2)
            msg = '\n'.join([f"{k}: {v}" for k,v in report.items()])
            ui.log('📈 Log analysis complete')
            QMessageBox.information(self, 'Log Analysis Report', msg)
        except Exception:
            logger.exception('Failed to analyze logs')

    def auto_tune_threshold(self):
        try:
            scores = []
            if len(SCORE_HISTORY) >= 100:
                scores = SCORE_HISTORY
            elif ai_model is not None and FEATURE_BUFFER:
                try:
                    X = np.array(FEATURE_BUFFER)
                    scores = list(ai_model.decision_function(X))
                except Exception:
                    logger.exception('Failed to compute scores from buffer')
                    scores = SCORE_HISTORY
            if not scores:
                ui.log('Not enough score data to auto-tune')
                return
            new_thr = float(np.percentile(scores,5))
            CONFIG['ai_score_threshold'] = new_thr
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(CONFIG, f, indent=2)
            self.input_threshold.setValue(new_thr)
            ui.log(f'🤖 AI threshold auto-tuned to {new_thr:.4f}')
            QMessageBox.information(self, 'Auto-tune Complete', f'New threshold: {new_thr:.4f}')
        except Exception:
            logger.exception('Auto-tune failed')

    def update_ai_status(self):
        try:
            if ai_trained:
                self.ai_status.setText("AI: trained ✅")
            else:
                self.ai_status.setText("AI: not trained ⚠️")
        except Exception:
            logger.exception('Failed to update AI status')

    def update_plots(self):
        try:
            # CPU/RAM from FEATURE_BUFFER (last N samples)
            if FEATURE_BUFFER:
                cpu_vals = [f[0] for f in FEATURE_BUFFER[-MAX_HISTORY:]]
                ram_vals = [f[1] for f in FEATURE_BUFFER[-MAX_HISTORY:]]
                xs = list(range(-len(cpu_vals)+1, 1))
                self.cpu_curve.setData(xs, cpu_vals)
                self.ram_curve.setData(xs, ram_vals)

            if SCORE_HISTORY:
                s = SCORE_HISTORY[-MAX_HISTORY:]
                xs = list(range(-len(s)+1, 1))
                self.score_curve.setData(xs, s)
        except Exception:
            logger.exception('Failed to update plots')

    def export_graphs(self):
        try:
            # grab the widgets and save
            self.cpu_plot.grab().save('cpu_plot.png')
            self.ram_plot.grab().save('ram_plot.png')
            self.score_plot.grab().save('score_plot.png')
            ui.log('📊 Graphs exported as PNG')
        except Exception:
            logger.exception('Failed to export graphs')

    def update_tray_menu(self):
        try:
            if hasattr(self, 'pause_action'):
                self.pause_action.setText('Pause Monitoring' if MONITORING else 'Resume Monitoring')
        except Exception:
            logger.exception('Failed to update tray menu')

    def update_stats(self, cpu, ram, proc):
        self.stats.setText(f"CPU:{cpu}%   RAM:{ram}%   PROC:{proc}")
        if ram > 92 or cpu > 55:
            self.status.setText("Status: ALERT 🔴")
        else:
            self.status.setText("Status: NORMAL 🟢")

    def log(self, msg):
        self.logbox.append(msg)
        logger.info(msg)

    def export_json(self):
        try:
            with open(INCIDENTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(INCIDENTS, f, indent=2)
            ui.log("📁 Incidents exported to JSON")
        except Exception:
            logger.exception("Failed to export incidents to JSON")

# ================== START ==================
# Application startup is executed from main() to allow importing for tests
app = None
ui = None

def init_logging():
    global logger
    logger = logging.getLogger('system_guard')
    logger.setLevel(logging.INFO)
    # File handler with UTF-8 to support emojis and non-ASCII
    fh = RotatingFileHandler('system_guard.log', maxBytes=1024*1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
    logger.addHandler(fh)

    # Safe stream handler to avoid UnicodeEncodeError on consoles that don't support some characters
    class SafeStreamHandler(logging.StreamHandler):
        def emit(self, record):
            try:
                super().emit(record)
            except UnicodeEncodeError:
                try:
                    msg = self.format(record)
                    # replace characters that can't be encoded
                    safe_msg = msg.encode('utf-8', errors='replace').decode('utf-8')
                    self.stream.write(safe_msg + self.terminator)
                    self.flush()
                except Exception:
                    pass

    sh = SafeStreamHandler()
    sh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
    logger.addHandler(sh)

    logger.info('Logging initialized')

def load_config():
    global CONFIG
    CONFIG = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                CONFIG.update(json.load(f))
        except Exception:
            logger.exception('Failed to load config')
    else:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, indent=2)
    logger.info('Config loaded')

def send_email_alert(data):
    if not CONFIG.get('alert_email'):
        return
    try:
        msg = EmailMessage()
        msg['Subject'] = f"[System Guard] {data.get('type')}"
        msg['From'] = CONFIG.get('alert_email')
        msg['To'] = CONFIG.get('alert_email')
        msg.set_content(json.dumps(data, indent=2))
        with smtplib.SMTP('localhost') as s:
            s.send_message(msg)
        logger.info('Email alert sent')
    except Exception:
        logger.exception('Failed to send email')

def send_to_siem():
    endpoint = CONFIG.get('siem_endpoint')
    if not endpoint:
        ui.log('No SIEM endpoint configured')
        return
    try:
        r = requests.post(endpoint, json=INCIDENTS, timeout=5)
        ui.log(f"SIEM POST {r.status_code}")
    except Exception:
        logger.exception('Failed to send to SIEM')

def send_alert(data):
    try:
        send_email_alert(data)
    except Exception:
        logger.exception('Alerting failed')

def try_block_ip(ip):
    # Safe default: only log. Implement platform firewall rule if explicitly enabled.
    logger.info('Auto-block requested for %s (not implemented)', ip)



def main():
    global app, ui
    try:
        init_logging()
        load_config()
        load_incidents()
        load_feature_buffer()

        # headless mode support
        if '--headless' in sys.argv:
            logger.info('Starting in headless mode')
            load_model()
            # start background workers without GUI
            threading.Thread(target=retrain_worker, daemon=True).start()
            threading.Thread(target=monitor, daemon=True).start()
            threading.Thread(target=packet_monitor, daemon=True).start()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info('Headless mode interrupted by user')
            return

        app = QApplication(sys.argv)
        # apply dark theme if enabled
        if CONFIG.get('dark_mode'):
            try:
                app.setStyleSheet(DARK_THEME)
            except Exception:
                pass
        try:
            if os.path.exists(ASSET_ICON):
                app.setWindowIcon(QIcon(ASSET_ICON))
        except Exception:
            pass

        ui = UI()
        ui.refresh_incident_list()
        try:
            ui.update_ai_status()
        except Exception:
            pass

        # start background workers
        threading.Thread(target=retrain_worker, daemon=True).start()
        threading.Thread(target=monitor, daemon=True).start()
        threading.Thread(target=packet_monitor, daemon=True).start()

        sys.exit(app.exec())
    except Exception as e:
        # log and show friendly message
        try:
            logger.exception('Fatal error in main: %s', e)
        except Exception:
            print('Fatal error:', e)
        try:
            show_error_message(str(e))
        except Exception:
            pass
if __name__ == '__main__':
    main()
