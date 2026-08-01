<!-- Nav: ← [Development](DEVELOPMENT.md) -->

# SSH Setup for Climate Advisor Deployment

This guide walks through setting up SSH access from your Windows development machine to your Home Assistant OS (HAOS) instance for automated deployments.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What are the four required `.deploy.env` values for SSH deployment? | `HA_HOST` (hostname or IP), `HA_SSH_PORT` (default 22), `HA_SSH_USER` (default `hassio`), `HA_CONFIG_PATH` (default `/config`). Add `HA_SSH_KEY` for a dedicated key. | [§Step 3: Create the Deploy Configuration](SSH-SETUP.md#step-3-create-the-deploy-configuration) |
| How do you do a dry-run deploy to verify SSH connectivity without making changes? | `python tools/deploy.py --dry-run` — runs validation only and shows what would be deployed. | [§Step 4: Test the Deploy Script](SSH-SETUP.md#step-4-test-the-deploy-script) |
| What is the daily deployment workflow once SSH is set up? | `python tools/deploy.py` (full: validate → backup → copy → restart → verify); `--skip-restart` for file changes only; `--rollback` to revert to the previous version. | [§Daily Usage](SSH-SETUP.md#daily-usage) |
| How do you fix "Permission denied (publickey)" during SSH connection? | Verify the public key is in the add-on's Authorized Keys config, confirm you are pointing to the correct private key file, and check the key wasn't accidentally modified. | [§Troubleshooting](SSH-SETUP.md#troubleshooting) |

## Prerequisites

- Home Assistant OS running on your server (Pi, NUC, VM, etc.)
- Windows 11 (OpenSSH client is built in)
- Network access from your dev machine to the HA server

## Step 1: Install the SSH Add-on on HAOS

1. Open your Home Assistant web UI
2. Go to **Settings** → **Add-ons** → **Add-on Store**
3. Search for **"Advanced SSH & Web Terminal"** (by the Community)
4. Click **Install**
5. Configure a password or authorized SSH key, then click **Start**
6. Make sure **"Start on boot"** is enabled

## Step 2: Test the Connection

From a terminal on your Windows machine:

```bash
ssh hassio@homeassistant.local
```

If `homeassistant.local` doesn't resolve, use the IP address of your HA server instead.

You should see a command prompt on the HA server. Verify you can access the config directory:

```bash
ls /config/custom_components/
```

Type `exit` to disconnect.

### Using a Dedicated SSH Key (Optional)

If you prefer a dedicated key instead of your default SSH key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ha_key -C "climate-advisor-deploy"
```

Add the public key to the SSH add-on's **Authorized Keys** config, then set `HA_SSH_KEY=~/.ssh/ha_key` in your `.deploy.env`.

## Step 3: Create the Deploy Configuration

Copy the sample environment file and edit it:

```bash
cp .deploy.env.sample .deploy.env
```

Edit `.deploy.env` with your values. The defaults work for most HAOS setups:

```
HA_HOST=homeassistant.local
HA_SSH_PORT=22
HA_SSH_USER=hassio
HA_CONFIG_PATH=/config
```

Replace `homeassistant.local` with your HA server's IP address if mDNS doesn't work on your network. Add `HA_SSH_KEY=~/.ssh/ha_key` if using a dedicated key.

**Important:** `.deploy.env` is git-ignored and will never be committed. The `.deploy.env.sample` file is committed as a reference template.

## Step 4: Test the Deploy Script

Run a dry run to verify everything connects:

```bash
python tools/deploy.py --dry-run
```

This runs validation only and shows what would be deployed without making changes.

## Troubleshooting

### "Connection refused" or "Connection timed out"
- Verify the SSH add-on is running in the HA UI
- Check the port number matches your `.deploy.env`
- Try the IP address instead of `homeassistant.local`
- Check your firewall isn't blocking the SSH port

### "Permission denied (publickey)"
- Verify your public key is in the add-on's Authorized Keys config
- Make sure you're pointing to the correct private key file
- Check the key wasn't accidentally modified (re-copy it)
- `python tools/deploy.py` prints the resolved identity file (`Using SSH key: ...`) before
  connecting — check that it's the key you expect. You can also run this yourself, purely
  locally (no connection attempted): `ssh -G <user>@<host>` and look for the `identityfile`
  line(s).

### Windows: default-key resolution behaves inconsistently
Windows commonly has two `ssh.exe` binaries on `PATH` — Git for Windows' bundled MSYS build
and Windows' native OpenSSH client — which can resolve default identity files differently
depending on how a program invokes `ssh` (this affects `HOME` resolution and, in turn, which
`~/.ssh/` default key gets picked). If you have a specific key you expect to be used, set
`HA_SSH_KEY=~/.ssh/your_key` explicitly in `.deploy.env` rather than relying on default
resolution — it costs nothing (still per-user, git-ignored config, not hardcoded anywhere)
and removes the ambiguity entirely.

### "Host key verification failed"
- The deploy script uses `StrictHostKeyChecking=no` to avoid this
- If you see this with manual SSH, run: `ssh-keygen -R homeassistant.local`

### First few steps succeed, then "Connection reset by peer" / "Connection timed out" for the rest of the run
This is the signature of the SSH add-on's rate-limit/brute-force protection ("Protection
mode," a fail2ban-style feature many HAOS SSH add-ons enable by default) blocking your
machine's IP after several connections in a short window — a full `deploy.py` run opens
several separate SSH/SCP connections (connection test, backup, remote cleanup, file copy,
restart, log check), which can trip it even though every connection was legitimate.

Since #551, `deploy.py` batches what used to be several separate remote commands into fewer,
combined round trips (e.g. the backup existence-check + tar creation happen in one SSH call
instead of two; temp-file cleanup + legacy-backup removal + directory setup happen in one call
instead of three-plus) to reduce the total connection count per run.

(An earlier attempt, #549, tried SSH connection multiplexing — `ControlMaster` — for the same
goal. It was reverted in #551: it failed outright on this project's Windows/Git-for-Windows SSH
client against a real HAOS SSH add-on, with `ControlMaster` connections immediately getting
`Connection reset by peer` even when a plain, non-multiplexed connection to the same host
succeeded instantly right before and after. If you're on a different platform where
`ControlMaster` is reliable, adding it back locally is safe to try — just be aware it wasn't
portable enough to ship by default here.)

Batching reduces connection count but isn't a guarantee against a strict rate limit. If you
still hit this:
- Check your SSH add-on's configuration for a "Protection mode" or rate-limit setting and
  raise its threshold or disable it, at least while deploying — this is the authoritative fix
- If it just tripped, wait for its cooldown window to clear before retrying — retrying
  immediately usually just extends the block
- `python tools/deploy.py --skip-restart` skips the restart + log-check steps (2 fewer
  connections), useful when iterating on file changes without needing a full deploy each time

### Can't find `/config/custom_components/`
- The directory may not exist yet. Create it: `mkdir -p /config/custom_components/`
- This is normal on a fresh HA install with no custom integrations

## Daily Usage

Once set up, deploying is a single command:

```bash
# Full deploy (validate → backup → copy → restart → verify)
python tools/deploy.py

# Quick deploy without restart (for testing file changes)
python tools/deploy.py --skip-restart

# Roll back to previous version if something breaks
python tools/deploy.py --rollback
```
