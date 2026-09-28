# 4-Tunnel SOCKS5 Setup Guide
**Date**: 2026-09-28  
**Purpose**: Configure 4 independent SOCKS tunnels for KGP Crew Monitoring System

---

## Architecture Overview

```
Oracle VM (103.211.65.148)
    ↓ SSH Dynamic Port Forwarding via Tailscale
    ├─ Port 1080 → Office PC Old (100.124.187.97) - kgplo user
    ├─ Port 1081 → Laptop (100.85.52.9) - hp user
    ├─ Port 1082 → Office PC New (100.110.163.89) - admin user
    └─ Port 1083 → TLC Computer (100.114.255.7) - hp user

Python scraper tests ports in order: 1080 → 1081 → 1082 → 1083
First working tunnel is used automatically.
```

---

## STEP 1: Update Python Code ✅ DONE

**Files Modified**:
- `scraper/login.py` - Updated `get_active_proxy()` to check all 4 ports
- `scraper/rtis_scraper.py` - Updated `get_active_proxy()` to check all 4 ports

**Changes**:
- Added port 1082 check (Office PC New)
- Added port 1083 check (TLC Computer)
- Updated tunnel names in logs
- Changed "All three tunnels" → "All four tunnels"

---

## STEP 2: Setup Each Tailscale Device

### Device 1: Office PC Old (100.124.187.97)
**User**: `kgplo`  
**Port**: Will bind to Oracle VM port 1080

1. Install Tailscale:
   ```bash
   # Download and install from https://tailscale.com/download
   ```

2. Enable as Exit Node:
   ```bash
   # Windows (run as Administrator in PowerShell):
   tailscale up --advertise-exit-node
   
   # Linux:
   sudo tailscale up --advertise-exit-node
   ```

3. Allow SSH server:
   - **Windows**: Enable OpenSSH Server in Windows Features
     ```powershell
     Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
     Start-Service sshd
     Set-Service -Name sshd -StartupType 'Automatic'
     ```
   
   - **Linux**: 
     ```bash
     sudo apt install openssh-server
     sudo systemctl enable ssh
     sudo systemctl start ssh
     ```

4. Test SSH access FROM Oracle VM:
   ```bash
   ssh kgplo@100.124.187.97
   ```

---

### Device 2: Laptop (100.85.52.9)
**User**: `hp`  
**Port**: Will bind to Oracle VM port 1081

Repeat same steps as Device 1, but:
- Login as `hp` user
- Test: `ssh hp@100.85.52.9` from Oracle VM

---

### Device 3: Office PC New (100.110.163.89)
**User**: `admin`  
**Port**: Will bind to Oracle VM port 1082

Repeat same steps as Device 1, but:
- Login as `admin` user
- Test: `ssh admin@100.110.163.89` from Oracle VM

---

### Device 4: TLC Computer (100.114.255.7)
**User**: `hp`  
**Port**: Will bind to Oracle VM port 1083

Repeat same steps as Device 1, but:
- Login as `hp` user
- Test: `ssh hp@100.114.255.7` from Oracle VM

---

## STEP 3: Oracle VM SSH Key Setup

**Critical**: All 4 devices must accept the SAME SSH key from Oracle VM.

1. **Check if key exists** (on Oracle VM as ubuntu user):
   ```bash
   ls -la /home/ubuntu/.ssh/cms_tunnel_key
   ```

2. **If key doesn't exist, create it**:
   ```bash
   ssh-keygen -t ed25519 -f /home/ubuntu/.ssh/cms_tunnel_key -N ""
   chmod 600 /home/ubuntu/.ssh/cms_tunnel_key
   ```

3. **Copy public key to ALL 4 devices**:
   
   **Get the public key**:
   ```bash
   cat /home/ubuntu/.ssh/cms_tunnel_key.pub
   ```
   
   **On EACH device** (run on the device itself, OR via ssh from Oracle VM):
   
   **For Windows devices**:
   ```powershell
   # Create .ssh directory if it doesn't exist
   mkdir -Force $env:USERPROFILE\.ssh
   
   # Add the public key (replace with actual key content)
   Add-Content -Path "$env:USERPROFILE\.ssh\authorized_keys" -Value "ssh-ed25519 AAAA... ubuntu@crew-monitor"
   
   # Set permissions (PowerShell as Admin)
   icacls "$env:USERPROFILE\.ssh\authorized_keys" /inheritance:r
   icacls "$env:USERPROFILE\.ssh\authorized_keys" /grant:r "$env:USERNAME:R"
   ```
   
   **For Linux devices**:
   ```bash
   mkdir -p ~/.ssh
   chmod 700 ~/.ssh
   
   # Add the public key
   echo "ssh-ed25519 AAAA... ubuntu@crew-monitor" >> ~/.ssh/authorized_keys
   
   chmod 600 ~/.ssh/authorized_keys
   ```

4. **Test passwordless SSH from Oracle VM to EACH device**:
   ```bash
   ssh -i /home/ubuntu/.ssh/cms_tunnel_key kgplo@100.124.187.97 echo "Test 1 OK"
   ssh -i /home/ubuntu/.ssh/cms_tunnel_key hp@100.85.52.9 echo "Test 2 OK"
   ssh -i /home/ubuntu/.ssh/cms_tunnel_key admin@100.110.163.89 echo "Test 3 OK"
   ssh -i /home/ubuntu/.ssh/cms_tunnel_key hp@100.114.255.7 echo "Test 4 OK"
   ```
   
   **All 4 should print "Test N OK" WITHOUT asking for password**.

---

## STEP 4: Update Coolify Deployment

**Location**: Oracle VM → Coolify web UI → KGP Crew Monitor project

1. **Update `entrypoint.sh`** with your new version (already provided in chat history)

2. **Verify Docker has access to SSH key**:
   
   In Coolify's **Environment Variables** or **docker-compose.yml**:
   ```yaml
   volumes:
     - /home/ubuntu/.ssh/cms_tunnel_key:/home/appuser/.ssh/cms_tunnel_key:ro
   ```
   
   **OR** if using Dockerfile volume mount:
   ```dockerfile
   VOLUME ["/home/appuser/.ssh"]
   ```

3. **Key Permissions in Docker**:
   
   Your `entrypoint.sh` already has:
   ```bash
   chmod 600 /home/appuser/.ssh/cms_tunnel_key 2>/dev/null || true
   chown appuser:appuser /home/appuser/.ssh/cms_tunnel_key 2>/dev/null || true
   ```

4. **Deploy** via Coolify (it will rebuild container with new entrypoint.sh)

---

## STEP 5: Verify Production Tunnels

**After deployment, check logs in Coolify**:

1. **Tunnel startup logs** (should show all 4 starting):
   ```
   [Tunnel-1080] Office PC SSH tunnel dropped, retrying in 5s...
   [Tunnel-1081] Laptop SSH tunnel dropped, retrying in 5s...
   [Tunnel-1082] office new computer SSH tunnel dropped, retrying in 5s...
   [Tunnel-1083] TLC computer SSH tunnel dropped, retrying in 5s...
   Waiting for SOCKS5 tunnel on port 1080...
   Tunnel ready after Xs.
   ```

2. **Inside the container** (via Coolify shell or `docker exec`):
   ```bash
   # Check all 4 ports are listening
   netstat -tuln | grep -E '1080|1081|1082|1083'
   
   # Should show:
   # tcp  0  0  127.0.0.1:1080  0.0.0.0:*  LISTEN
   # tcp  0  0  127.0.0.1:1081  0.0.0.0:*  LISTEN
   # tcp  0  0  127.0.0.1:1082  0.0.0.0:*  LISTEN
   # tcp  0  0  127.0.0.1:1083  0.0.0.0:*  LISTEN
   
   # Test tunnel connectivity
   curl --socks5 127.0.0.1:1080 https://cms.indianrail.gov.in -I --max-time 10
   curl --socks5 127.0.0.1:1081 https://cms.indianrail.gov.in -I --max-time 10
   curl --socks5 127.0.0.1:1082 https://cms.indianrail.gov.in -I --max-time 10
   curl --socks5 127.0.0.1:1083 https://cms.indianrail.gov.in -I --max-time 10
   ```

3. **Check scraper logs** (via Coolify logs or Flask app logs):
   ```
   [Proxy] Testing Primary Tunnel (Office PC Old) on port 1080...
   [Proxy] Using Office PC Old proxy on port 1080.
   ```

---

## STEP 6: Commit and Push Changes

```bash
cd "D:\KN\cms automate"

git add scraper/login.py scraper/rtis_scraper.py

git commit -m "feat: add 4-tunnel SOCKS5 failover system

- Updated get_active_proxy() in login.py and rtis_scraper.py
- Added port 1082 (Office PC New) and 1083 (TLC Computer)
- Fallback order: 1080 → 1081 → 1082 → 1083
- All tunnels via Tailscale exit nodes for independent routing
- Docker entrypoint.sh manages all 4 SSH tunnels in background

Co-Authored-By: Claude Code <noreply@anthropic.com>"

git push origin master
```

---

## Common Issues & Solutions

### Issue 1: "Address already in use" on port 108X

**Symptom**: `bind [0.0.0.0]:1082: Address already in use`

**Cause**: Previous SSH tunnel process still running

**Fix**:
```bash
# Inside Docker container
pkill -f "ssh -D.*1082"

# OR restart entire container via Coolify
```

---

### Issue 2: SSH Connection Timeout

**Symptom**: `ssh: connect to host 100.X.X.X port 22: Connection timed out`

**Root Causes**:
1. Device not connected to Tailscale
2. SSH server not running on device
3. Firewall blocking SSH port
4. Device is asleep/powered off

**Debug Steps**:
```bash
# From Oracle VM, check if device is online
tailscale status | grep 100.X.X.X

# Ping device
tailscale ping 100.X.X.X

# If ping works but SSH fails, SSH server is down on that device
```

---

### Issue 3: Port listening but scraper says tunnel down

**Symptom**: `ss -tuln | grep 1082` shows listening, but scraper logs "Fallback 2 failed"

**Cause**: SSH tunnel established but proxy can't reach internet (device has no internet)

**Debug**:
```bash
# Test if tunnel can actually reach CMS
curl --socks5 127.0.0.1:1082 https://cms.indianrail.gov.in -I --max-time 10

# If this fails, the device acting as exit node has no internet
```

**Fix**: 
- Check device's internet connection
- Verify Tailscale exit node is enabled: `tailscale status` (should show "offers exit node")
- On Oracle VM, ensure you're using that exit node: `tailscale up --exit-node=100.X.X.X`

---

### Issue 4: All tunnels work but scraper still blocked

**Symptom**: All 4 ports listening, curl works, but scraper gets blocked by CMS

**Cause**: Oracle VM's IP is banned by CMS even when using tunnel

**Debug**:
```bash
# Check ENV variable
echo $ENV

# Should be "production" in Docker
```

**Verify** production blocking is active in Python:
- If `ENV=production` and all tunnels down → scraper BLOCKS direct connection
- If `ENV=local` and all tunnels down → scraper attempts direct connection (will fail on Oracle VM)

---

## Maintenance Checklist

### Daily (Automated - Docker handles this)
- ✅ Tunnels auto-reconnect on drop (5s retry loop in entrypoint.sh)
- ✅ Scraper tests tunnels every cycle and auto-switches

### Weekly (Manual)
1. Check all 4 devices are online in Tailscale:
   ```bash
   tailscale status | grep -E '100.124.187.97|100.85.52.9|100.110.163.89|100.114.255.7'
   ```

2. Verify tunnel health in Coolify logs:
   ```
   # Should NOT see continuous "tunnel dropped" messages
   ```

### Monthly (Manual)
1. Test each tunnel individually:
   ```bash
   # SSH into Oracle VM
   docker exec -it <container_name> bash
   
   # Test each port
   for port in 1080 1081 1082 1083; do
     echo "Testing port $port..."
     curl --socks5 127.0.0.1:$port https://cms.indianrail.gov.in -I --max-time 10
   done
   ```

2. Review scraper logs for tunnel switching patterns:
   ```bash
   # Check which tunnel is used most
   grep "Using.*proxy on port" /var/log/app.log | sort | uniq -c
   ```

---

## Emergency Procedures

### All 4 Tunnels Down

**Immediate Actions**:
1. Check Tailscale status on all 4 devices
2. Restart Tailscale daemon on devices if needed
3. Reboot devices if necessary
4. Check Oracle VM Tailscale: `tailscale status`

### Scraper Completely Offline

**Rollback Plan**:
```bash
# Revert to direct connection (NOT RECOMMENDED - Oracle IP is banned)
# Only use in absolute emergency

# Edit docker-compose.yml or Coolify env:
ENV=local

# This will attempt direct connection and fail unless ban is lifted
```

---

## Testing Failover Manually

Simulate tunnel failures to verify auto-switching:

```bash
# SSH into Oracle VM
ssh ubuntu@103.211.65.148 -i ~/.ssh/id_ed25519_oracle_vm

# Inside Docker container
docker exec -it <container_name> bash

# Kill tunnel 1080 (primary)
pkill -f "ssh -D.*1080"

# Watch logs - should switch to 1081
tail -f /var/log/app.log

# Expected log:
# [Proxy] Testing Primary Tunnel (Office PC Old) on port 1080...
# [Proxy] Primary tunnel failed. Testing Fallback Tunnel 1 (Laptop) on port 1081...
# [Proxy] Using Laptop proxy on port 1081.

# Tunnel 1080 will auto-restart in 5s due to while loop in entrypoint.sh
```

---

## Architecture Diagram

```
                    ┌─────────────────────────────────────────┐
                    │      Oracle Cloud VM (103.211.65.148)   │
                    │                                          │
                    │  Docker Container (appuser)              │
                    │  ┌────────────────────────────────────┐  │
                    │  │  Flask App + Scrapers              │  │
                    │  │  (scraper/login.py)                │  │
                    │  │  (scraper/rtis_scraper.py)         │  │
                    │  │                                    │  │
                    │  │  get_active_proxy()                │  │
                    │  │    ↓                               │  │
                    │  │  Tests 1080→1081→1082→1083         │  │
                    │  └─────────┬──────────────────────────┘  │
                    │            │                             │
                    │  ┌─────────┴──────────────────────────┐  │
                    │  │  4 SSH Tunnels (background loops)  │  │
                    │  │  - ssh -D 127.0.0.1:1080 kgplo@... │  │
                    │  │  - ssh -D 127.0.0.1:1081 hp@...    │  │
                    │  │  - ssh -D 127.0.0.1:1082 admin@... │  │
                    │  │  - ssh -D 127.0.0.1:1083 hp@...    │  │
                    │  └────────────────────────────────────┘  │
                    └──────────┬──────────────────────────────┘
                               │ Tailscale VPN (encrypted)
        ┌──────────────────────┼──────────────────────────────────┐
        │                      │                                   │
        │                      │                                   │
   ┌────▼─────┐         ┌─────▼──────┐       ┌──────▼─────┐  ┌───▼────────┐
   │ Office   │         │  Laptop    │       │ Office     │  │ TLC        │
   │ PC Old   │         │ (Home)     │       │ PC New     │  │ Computer   │
   │ Exit Node│         │ Exit Node  │       │ Exit Node  │  │ Exit Node  │
   │ (1080)   │         │ (1081)     │       │ (1082)     │  │ (1083)     │
   └────┬─────┘         └─────┬──────┘       └──────┬─────┘  └───┬────────┘
        │                     │                     │            │
        └─────────────────────┴─────────────────────┴────────────┘
                                      │
                                      ▼
                          ┌───────────────────────┐
                          │  CMS Portal           │
                          │  cms.indianrail.gov.in│
                          └───────────────────────┘
```

---

## Key Learnings from Previous Mistakes

### ❌ Mistake 1: Port Already in Use
**What happened**: Tried to start tunnel while old process was stuck  
**Solution**: Always `pkill` or `systemctl stop` before restarting

### ❌ Mistake 2: Python Code Missing Port
**What happened**: Added port 1083 in Docker but forgot to update Python  
**Solution**: ✅ Updated both `login.py` AND `rtis_scraper.py`

### ❌ Mistake 3: SSH Key Permission Mismatch
**What happened**: Key at `/root/.ssh/` but service ran as `ubuntu`  
**Solution**: Keep key at `/home/ubuntu/.ssh/` and use `appuser` in Docker

### ❌ Mistake 4: Device Offline = Silent Failure
**What happened**: Mobile locked, Tailscale killed, tunnel silently timed out  
**Solution**: Monitor all 4 devices, ensure they're always-on or have Wake-on-LAN

### ❌ Mistake 5: Wrong Tunnel Labels in Logs
**What happened**: Logs said "Mobile" but it was "Office PC New"  
**Solution**: ✅ Corrected all tunnel names in `get_active_proxy()` functions

---

## Success Criteria

✅ All 4 devices show "active" in `tailscale status`  
✅ All 4 devices accept passwordless SSH from Oracle VM  
✅ All 4 ports (1080-1083) show LISTEN in `netstat -tuln`  
✅ Scraper logs show successful proxy usage  
✅ CMS scraper completes full cycle without blocks  
✅ Failover works: Kill primary tunnel → scraper switches to next working tunnel within 1 cycle

---

**Setup Complete! 🎉**

Monitor Coolify logs for first 24 hours to ensure stability.
