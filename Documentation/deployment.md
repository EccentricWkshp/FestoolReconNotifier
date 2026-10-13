# Deployment Guide

Complete guide for deploying Festool Recon Monitor in various environments.

## Table of Contents
1. [Local Development Setup](#local-development-setup)
2. [Production Deployment](#production-deployment)
3. [Cloud Deployment](#cloud-deployment)
4. [Docker Deployment](#docker-deployment)
5. [Security Best Practices](#security-best-practices)
6. [Performance Tuning](#performance-tuning)

---

## Local Development Setup

### Prerequisites
- Python 3.8 or higher
- 4GB RAM minimum
- 500MB disk space

### Step-by-Step Installation

1. **Clone/Download the Repository**
```bash
cd festool-recon-monitor
```

2. **Run Automated Setup**
```bash
python setup.py
```

3. **Activate Virtual Environment**

**Windows:**
```bash
venv\Scripts\activate
```

**macOS/Linux:**
```bash
source venv/bin/activate
```

4. **Verify Installation**
```bash
python test_installation.py
```

5. **Launch Application**
```bash
python main.py
```

---

## Production Deployment

### For Always-On Monitoring

#### Windows Service

Create `run_service.bat`:
```batch
@echo off
cd /d "C:\path\to\festool-recon-monitor"
call venv\Scripts\activate
python main.py
```

**Using Task Scheduler:**
1. Open Task Scheduler
2. Create Basic Task
3. Trigger: At startup
4. Action: Start a program
5. Program: `C:\path\to\festool-recon-monitor\run_service.bat`
6. Configure for "Run whether user is logged on or not"

#### Linux Systemd Service

Create `/etc/systemd/system/festool-monitor.service`:
```ini
[Unit]
Description=Festool Recon Monitor
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/festool-recon-monitor
ExecStart=/home/youruser/festool-recon-monitor/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable festool-monitor
sudo systemctl start festool-monitor
sudo systemctl status festool-monitor
```

#### macOS LaunchAgent

Create `~/Library/LaunchAgents/com.festool.monitor.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.festool.monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/festool-recon-monitor/venv/bin/python</string>
        <string>/path/to/festool-recon-monitor/main.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>/path/to/festool-recon-monitor</string>
</dict>
</plist>
```

Load the service:
```bash
launchctl load ~/Library/LaunchAgents/com.festool.monitor.plist
```

---

## Cloud Deployment

### AWS EC2

#### 1. Launch Instance
- AMI: Ubuntu Server 22.04 LTS
- Instance Type: t2.micro (Free Tier eligible)
- Storage: 10GB

#### 2. Install Dependencies
```bash
sudo apt update
sudo apt install python3.10 python3-pip python3-venv -y
```

#### 3. Setup Application
```bash
git clone <your-repo> festool-recon-monitor
cd festool-recon-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 4. Configure as Service
Follow the Linux systemd service instructions above.

#### 5. Security Group Rules
- Outbound: Allow HTTPS (443) for web scraping
- Outbound: Allow SMTP (587) if using email notifications
- No inbound rules needed (unless adding web interface)

### Google Cloud Platform

Similar to AWS but using:
- Compute Engine VM
- e2-micro instance (Free Tier)
- Ubuntu 22.04 LTS

### Heroku (Free Tier)

**Note:** GUI applications don't work on Heroku. Need to modify for headless operation.

Create `Procfile`:
```
worker: python main.py --headless
```

Add to `main.py`:
```python
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--headless', action='store_true')
args = parser.parse_args()

if args.headless:
    # Run without GUI
    pass
```

---

## Docker Deployment

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libxcb-xinerama0 \
    libxkbcommon-x11-0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    && rm -rf /var/lib/apt/lists/*

# Copy application
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create directories
RUN mkdir -p backups logs exports

# Run application
CMD ["python", "main.py"]
```

### docker-compose.yml

```yaml
version: '3.8'

services:
  festool-monitor:
    build: .
    container_name: festool-recon-monitor
    volumes:
      - ./backups:/app/backups
      - ./logs:/app/logs
      - ./exports:/app/exports
      - ./festool_recon.db:/app/festool_recon.db
    environment:
      - DISPLAY=${DISPLAY}
    restart: unless-stopped
```

### Build and Run

```bash
docker-compose build
docker-compose up -d
```

---

## Security Best Practices

### 1. Secure Credentials

**Use Environment Variables:**

Create `.env` file:
```bash
EMAIL_USER=your.email@gmail.com
EMAIL_PASS=your-app-password
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
```

Load in Python:
```python
import os
from dotenv import load_dotenv

load_dotenv()

email_user = os.getenv('EMAIL_USER')
email_pass = os.getenv('EMAIL_PASS')
```

Install: `pip install python-dotenv`

### 2. Database Security

- Set proper file permissions:
```bash
chmod 600 festool_recon.db
```

- Encrypt database at rest (using SQLCipher):
```python
from sqlalchemy import create_engine

engine = create_engine(
    f'sqlite+pysqlcipher:///:memory:?cipher=aes-256-cfb&kdf_iter=64000'
)
```

### 3. API Key Storage

Never commit:
- `.env` files
- `config.ini` with credentials
- Database files

Add to `.gitignore`:
```
.env
config.ini
*.db
```

### 4. Update Dependencies Regularly

```bash
pip list --outdated
pip install --upgrade -r requirements.txt
```

---

## Performance Tuning

### 1. Database Optimization

**Enable WAL Mode** (already done in code):
```python
cursor.execute("PRAGMA journal_mode=WAL")
```

**Regular Maintenance:**
```python
# Add to DatabaseManager
def vacuum(self):
    """Optimize database."""
    with self.engine.connect() as conn:
        conn.execute("VACUUM")
```

Run monthly via cron or scheduled task.

### 2. Memory Management

**Limit Product History:**
```python
# In settings
max_products_in_history = 1000

# In query
products = session.query(Product).order_by(
    Product.posted_at.desc()
).limit(max_products_in_history).all()
```

**Clear Old Logs:**
```python
# Delete logs older than 30 days
cutoff = datetime.now() - timedelta(days=30)
session.query(NotificationLog).filter(
    NotificationLog.sent_at < cutoff
).delete()
```

### 3. Network Optimization

**Reuse HTTP Sessions:**
```python
# Already implemented in FestoolScraper
self.session = requests.Session()
```

**Add Connection Pooling:**
```python
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

adapter = HTTPAdapter(
    max_retries=Retry(total=3, backoff_factor=0.5),
    pool_connections=10,
    pool_maxsize=20
)
self.session.mount('https://', adapter)
```

### 4. Check Interval Guidelines

| Interval | Use Case | Server Load |
|----------|----------|-------------|
| 1 min | Critical monitoring | High |
| 5 min | Normal use (Recommended) | Medium |
| 15 min | Casual monitoring | Low |
| 30 min | Very light monitoring | Very Low |

---

## Monitoring and Logging

### Log Rotation

**Linux (logrotate):**

Create `/etc/logrotate.d/festool-monitor`:
```
/path/to/festool-recon-monitor/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

**Windows (PowerShell script):**
```powershell
$logPath = "C:\path\to\festool_monitor.log"
$maxSize = 10MB

if ((Get-Item $logPath).Length -gt $maxSize) {
    Move-Item $logPath "$logPath.old" -Force
}
```

### Health Checks

Add to application:
```python
def health_check(self):
    """Check if monitoring is working."""
    return {
        'monitoring_active': self.monitor.is_running,
        'last_check': self.monitor.last_check_time,
        'database_ok': self.test_database_connection(),
        'scraper_ok': self.scraper.test_connection()
    }
```

---

## Backup Strategies

### Automated Backups

**Already Implemented:**
- Daily automatic backups
- Keeps last 10 backups
- Stored in `backups/` directory

**Additional Off-Site Backup:**

```python
import shutil
import boto3  # AWS S3

def backup_to_s3(backup_file):
    """Upload backup to S3."""
    s3 = boto3.client('s3')
    s3.upload_file(
        backup_file,
        'your-bucket-name',
        f'festool-backups/{os.path.basename(backup_file)}'
    )
```

### Backup Schedule Recommendations

- **Local:** Daily (already implemented)
- **Off-site:** Weekly
- **Before updates:** Manual backup

---

## Troubleshooting Production Issues

### Application Won't Start

**Check logs:**
```bash
tail -f festool_monitor.log
```

**Check service status:**
```bash
# Linux
sudo systemctl status festool-monitor

# View logs
sudo journalctl -u festool-monitor -f
```

### High CPU Usage

1. Check interval is not too aggressive
2. Verify no infinite loops in logs
3. Monitor with:
```bash
top -p $(pgrep -f main.py)
```

### Memory Leaks

Monitor memory:
```bash
ps aux | grep main.py
```

Add memory profiling:
```python
import tracemalloc
tracemalloc.start()
# ... your code ...
snapshot = tracemalloc.take_snapshot()
```

### Database Locks

If "database is locked" errors:
1. Check no multiple instances running
2. Increase timeout in DatabaseManager
3. Consider switching to PostgreSQL for high-concurrency

---

## Migration to Multi-User

Future enhancement steps:

1. **Add User Authentication UI**
2. **Implement Session Management**
3. **Separate User Settings**
4. **Add Role-Based Access Control**
5. **Consider Web Framework** (Flask/Django)

---

## Scaling Considerations

### For Multiple Users (Future)

**Architecture Changes:**
- Move to client-server model
- PostgreSQL or MySQL database
- REST API for communication
- Load balancer for multiple instances

**Estimated Capacity:**
- SQLite: 1-10 users
- PostgreSQL: 100+ users
- Distributed: 1000+ users

---

## Support and Maintenance

### Regular Tasks

**Daily:**
- Check logs for errors
- Verify monitoring is active

**Weekly:**
- Review notification delivery rate
- Check disk space for backups

**Monthly:**
- Update dependencies
- Review and optimize database
- Test restore procedure

**Quarterly:**
- Review access logs
- Update documentation
- Security audit

---

**For questions or issues, check the main README.md and logs first.**