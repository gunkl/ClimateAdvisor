# SSH Setup for Climate Advisor Deployment

This guide walks through setting up SSH access from your Windows development machine to your Home Assistant OS (HAOS) instance for automated deployments.

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

### "Host key verification failed"
- The deploy script uses `StrictHostKeyChecking=no` to avoid this
- If you see this with manual SSH, run: `ssh-keygen -R homeassistant.local`

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
