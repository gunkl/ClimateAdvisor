<!-- Nav: ← [Development](DEVELOPMENT.md) -->

# SSH Setup for Climate Advisor Deployment

How to give your development machine SSH access to a Home Assistant OS (HAOS) instance, so that `tools/deploy.py`
can deploy the integration and the diagnostic tools (`ha_logs.py`, `learning_db.py` and others) can read logs and
state. Every one of these tools reads the same `.deploy.env` file in the repo root.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| Which `.deploy.env` values does SSH deployment use? | `HA_HOST` (default `homeassistant.local`), `HA_SSH_PORT` (default `22`), `HA_SSH_USER` (default `hassio`) and `HA_CONFIG_PATH` (default `/config`). `HA_SSH_KEY` is optional and names a dedicated private key. `HA_API_TOKEN` is needed only by `ha_logs.py --history`. | [§Step 3: Create the Deploy Configuration](SSH-SETUP.md#step-3-create-the-deploy-configuration) |
| Does `deploy.py --dry-run` test the SSH connection? | No. It runs `tools/validate.py` and lists the files that would deploy, without connecting. To test SSH, use `ssh` directly or a read-only tool such as `python tools/ha_logs.py --lines 20`. | [§Step 4: Test the Tools](SSH-SETUP.md#step-4-test-the-tools) |
| What is the daily deployment workflow once SSH is set up? | `python tools/deploy.py` (validate, back up, transfer, restart, check logs). Use `--skip-restart` for file changes only, and `--rollback` to restore the newest local backup. | [§Daily Usage](SSH-SETUP.md#daily-usage) |
| How do I fix "Permission denied (publickey)" or host-key errors? | Check the public key is in the add-on's authorized keys and that `deploy.py`'s `Using SSH key:` line names the key you expect. After an HA reinstall, remove the stale host key with `ssh-keygen -R <host>`. | [§Troubleshooting](SSH-SETUP.md#troubleshooting) |

## Prerequisites

- Home Assistant OS on your server (Pi, NUC, VM and so on).
- An OpenSSH client (`ssh`, `scp`, `ssh-keygen`). Windows 10 and 11 include one; Linux, macOS and WSL do too.
- Network access from your dev machine to the HA server.

## Step 1: Install the SSH Add-on on HAOS

1. In the Home Assistant UI, go to **Settings** → **Add-ons** → **Add-on Store**.
2. Install an SSH add-on that provides the `ha` CLI, such as **Advanced SSH & Web Terminal**. `deploy.py` runs
   `ha core restart` and `ha core logs`, and `ha_logs.py` runs `ha core logs`.
3. Add your **public** key to the add-on's authorized keys, then click **Start** and enable **Start on boot**. Use key
   authentication: the tools open several SSH connections per run, and a password would be asked for each time.

## Step 2: Test the Connection

```bash
ssh hassio@homeassistant.local        # or the server's IP address if .local does not resolve
ls /config/custom_components/          # on the server; create it with mkdir -p if it is missing
exit
```

### Using a Dedicated SSH Key (Optional)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ha_key -C "climate-advisor-deploy"
```

Add `~/.ssh/ha_key.pub` to the add-on's authorized keys, then set `HA_SSH_KEY=~/.ssh/ha_key` in `.deploy.env`. On
Linux and macOS, `deploy.py` logs a warning if the key file is readable by group or others (`chmod 600` it).

## Step 3: Create the Deploy Configuration

```bash
cp .deploy.env.sample .deploy.env
```

Fill in your values. `.deploy.env` is git-ignored; only `.deploy.env.sample` is committed. Each line is `KEY=value`,
and blank lines and `#` comments are ignored. Never commit `.deploy.env` or paste its contents into an issue or PR.

| Key | Default | Used for |
|---|---|---|
| `HA_HOST` | `homeassistant.local` | SSH host. `deploy.py` accepts only letters, digits, `.`, `_` and `-`. |
| `HA_SSH_PORT` | `22` | SSH port (`-p`; `-P` for `scp`), 1-65535. |
| `HA_SSH_USER` | `hassio` | SSH user (`<user>@<host>`). |
| `HA_SSH_KEY` | empty | Optional private key, passed as `-i`. `deploy.py` refuses to run if the file does not exist. Empty means `ssh` picks its default identity or uses `ssh-agent`. |
| `HA_CONFIG_PATH` | `/config` | Must be absolute. `deploy.py` installs into `<HA_CONFIG_PATH>/custom_components/climate_advisor`. `learning_db.py` always reads from `/config`. |
| `HA_API_TOKEN` | empty | A long-lived access token for `ha_logs.py --history`, which calls the REST API on port 8123 (http for `.local` hosts and IP addresses, https otherwise). |

How the tools connect:

- Every SSH call is `ssh -p <port> -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 [-i <key>] <user>@<host>`
  (`ssh_args` in `tools/deploy.py` and `tools/ha_logs.py`). `learning_db.py` uses `ha_logs.py`'s `load_config`,
  `ssh_args` and `ssh_target`. `engine_status.py` and `swing_audit.py` use its `load_config`, and
  `deploy_dev_sim.py` uses `deploy.py`'s helpers.
- `deploy.py` falls back to the defaults, with a warning, when `.deploy.env` is missing. `ha_logs.py`, and every tool
  that uses its `load_config`, exits instead.
- `learning_db.py` and `thermal_health.py` read the REST credentials `HA_URL` and `HA_TOKEN` from the environment or
  from a separate git-ignored `.env` in the repo root. `learning_db.py` needs them only for its optional live section.

## Step 4: Test the Tools

```bash
python tools/deploy.py --dry-run       # validation + file list only; does NOT connect
python tools/ha_logs.py --lines 20     # one read-only SSH connection
python tools/learning_db.py --model    # reads the learning DB over SSH
```

## Daily Usage

```bash
python tools/deploy.py                 # validate → back up → transfer → restart HA → check logs
python tools/deploy.py --skip-restart  # deploy files only; restart HA yourself
python tools/deploy.py --rollback      # restore the newest backup in backups/ (asks first; skips validation)
```

- Every run writes a debug log to `logs/deploy-<timestamp>.log`.
- Before transferring, a deploy downloads the existing install as `backups/climate_advisor-<timestamp>.tar.gz` and
  keeps the newest 5.
- The upload is extracted to a temp directory and then swapped into place, so the live directory always matches the
  source tree exactly.

Do not run `deploy.py` separately from several worktrees: each run replaces the whole remote directory, so only the
last one survives. Combine the changes into one tree first.

## Troubleshooting

### "Connection refused" or "Connection timed out"
- Check that the SSH add-on is running, and that `HA_SSH_PORT` matches its port.
- Try the IP address instead of `homeassistant.local`, and check that no firewall blocks the port.

### "Permission denied (publickey)"
- Check that your public key is in the add-on's authorized keys and has not been altered (re-copy it).
- `deploy.py` prints the identity it resolved (`Using SSH key: ...`) before connecting. To see what `ssh` would pick
  without connecting, run `ssh -G <user>@<host>` and read the `identityfile` lines.
- On Windows, Git for Windows' `ssh.exe` and the native OpenSSH client can both be on `PATH`, and they may resolve
  `~/.ssh` default keys differently (#547). Set `HA_SSH_KEY` explicitly so the choice is not left to either one.

### "Host key verification failed" / "REMOTE HOST IDENTIFICATION HAS CHANGED"
The tools use `StrictHostKeyChecking=accept-new`. A new host is trusted on first connection, but a changed key is
refused, for example after reinstalling HA or the add-on. Remove the old entry with `ssh-keygen -R <host>` (use
`ssh-keygen -R "[<host>]:<port>"` for a non-22 port) and connect once by hand.

### The first steps succeed, then "Connection reset by peer" or a timeout for the rest of the run
The SSH add-on's connection rate limiting has blocked your IP after several connections in a short window. A deploy
keeps its connection count low (#553):

| Command | SSH connections |
|---|---|
| `deploy.py` | 3: back up and prepare (`ssh`), download the backup (`scp`, skipped on a fresh install), then transfer, extract, verify, restart and fetch logs (one `ssh` with a tar stream on stdin) |
| `deploy.py --skip-restart` | 3, minus the restart and the log fetch |
| `deploy.py --rollback` | 1 |

If you still hit the limit, raise or disable the add-on's rate-limit setting while you deploy, and wait for its
cooldown before you retry, because retrying at once usually extends the block. `ControlMaster` multiplexing (#549)
was tried and reverted (#551) because this add-on reset the multiplexed connections on Windows.
