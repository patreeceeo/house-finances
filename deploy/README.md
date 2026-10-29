# Deploying House Finances

A small Linux server (1-2 GB RAM, e.g. Ubuntu 24.04) runs the app on
`127.0.0.1:8501`. Cloudflare Tunnel carries traffic to it, so no web port is
open, and Cloudflare Access signs people in before they reach it. Roles
(editor or view only) are explained under Hosting in the main README.

You need a domain on Cloudflare. Below, `money.example.com` is the app's
address and `myhouse` is your Zero Trust team name; use your own.

Files here:

| File | Goes to |
|---|---|
| `house-finances.service` | `/etc/systemd/system/` |
| `house-finances.env.example` | `/etc/house-finances.env` (filled in) |
| `cloudflared-config.yml` | `/etc/cloudflared/config.yml` (filled in) |
| `backup.sh`, `house-finances-backup.service`, `house-finances-backup.timer` | backups (step 6) |
| `house-finances-update` | `/usr/local/bin/` (Deploying from GitHub) |

## 1. Server

```bash
# As your admin user, with your SSH key already in ~/.ssh/authorized_keys:
echo "PasswordAuthentication no" | sudo tee /etc/ssh/sshd_config.d/no-passwords.conf
sudo systemctl reload ssh
sudo apt update && sudo apt -y upgrade && sudo apt -y install git rclone
sudo ufw allow OpenSSH && sudo ufw --force enable   # SSH is the only open port

curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo useradd --system --create-home --shell /usr/sbin/nologin housefin
```

## 2. The app

```bash
sudo mkdir -p /srv/house-finances && sudo chown housefin: /srv/house-finances
sudo -u housefin git clone <repo-url> /srv/house-finances   # or rsync the folder
cd /srv/house-finances && sudo -u housefin uv sync --frozen
```

Copy your existing database over from your own machine, then give it to
`housefin`:

```bash
scp finances.db house:/tmp/ && ssh house 'sudo install -o housefin -m 600 /tmp/finances.db /srv/house-finances/'
```

## 3. Cloudflare Access

In the Cloudflare dashboard, Zero Trust:

1. Settings > Authentication: add **One-time PIN** as a login method.
2. Access > Applications > Add > **Self-hosted**: domain `money.example.com`.
   Policy: Allow, Include > Emails: every housemate who should get in.
3. Open the application and copy its **Application Audience (AUD) tag**.

## 4. Settings

```bash
sudo install -m 640 -o root -g housefin deploy/house-finances.env.example /etc/house-finances.env
sudo nano /etc/house-finances.env
```

Set `HOUSEFIN_ACCESS_TEAM_DOMAIN=myhouse.cloudflareaccess.com`, the AUD tag,
and `HOUSEFIN_EDITORS` (emails, comma-separated). Anyone else Access lets in
can only look. Then start the app:

```bash
sudo cp deploy/house-finances.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now house-finances
curl -sI http://127.0.0.1:8501 | head -1   # HTTP/1.1 200 OK
```

## 5. Cloudflare Tunnel

```bash
# Install cloudflared from Cloudflare's package repository (pkg.cloudflare.com), then:
cloudflared tunnel login                       # pick your domain in the browser
cloudflared tunnel create house-finances       # writes ~/.cloudflared/<id>.json
cloudflared tunnel route dns house-finances money.example.com
sudo mkdir -p /etc/cloudflared
sudo mv ~/.cloudflared/<id>.json /etc/cloudflared/house-finances.json
sudo cp deploy/cloudflared-config.yml /etc/cloudflared/config.yml   # set your hostname
sudo cloudflared service install && sudo systemctl enable --now cloudflared
```

Open `https://money.example.com`: Access asks for your email, then the app
shows "Signed in as ..." in the sidebar. A housemate not in
`HOUSEFIN_EDITORS` sees "(view only)".

The tunnel can instead be created in the dashboard (Networks > Tunnels),
which gives a one-line `cloudflared service install <token>`; add the public
hostname `money.example.com` -> `http://127.0.0.1:8501` there.

### SSH through the tunnel (optional)

Keep port 22 closed to the internet. `cloudflared-config.yml` already routes
`ssh.example.com` to the server's SSH. Add a second Access application for
`ssh.example.com` (only your email), run
`cloudflared tunnel route dns house-finances ssh.example.com`, install
`cloudflared` on your own machine and add to its `~/.ssh/config`:

```
Host house
    HostName ssh.example.com
    User your-admin-user
    ProxyCommand cloudflared access ssh --hostname %h
```

Once `ssh house` works that way, close the port: `sudo ufw delete allow OpenSSH`.

## 6. Backups

`backup.sh` makes a consistent copy with SQLite's backup API, checks it,
compresses it into `/var/backups/house-finances`, copies it to an rclone
remote (Backblaze B2 or Cloudflare R2) and deletes local copies older than
`KEEP_DAYS`. Set a lifecycle rule on the bucket to expire old remote copies.

```bash
sudo install -d -o housefin /var/backups/house-finances
sudo -u housefin rclone config          # create the remote named in RCLONE_REMOTE
sudo cp deploy/house-finances-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now house-finances-backup.timer
sudo systemctl start house-finances-backup && journalctl -u house-finances-backup -n 5
```

Restore:

```bash
sudo systemctl stop house-finances
gunzip -c finances-YYYY-MM-DD.db.gz | sudo install -o housefin -m 600 /dev/stdin /srv/house-finances/finances.db
sudo systemctl start house-finances
```

## Importing

Editors upload exports on the Import page. Over SSH works too:

```bash
scp ~/Downloads/transactions.csv house:/tmp/
ssh house 'cd /srv/house-finances && sudo -u housefin uv run --frozen python ingest.py /tmp/transactions.csv'
```

The importer uses the same database as the app (`HOUSEFIN_DB`, which is the
default path here anyway).

## Updating

Pushes to `main` deploy themselves once the tests pass (next section). By
hand, on the server:

```bash
sudo /usr/local/bin/house-finances-update "$(git -C /srv/house-finances ls-remote origin main | cut -f1)"
```

`house-finances-update` backs up the database, checks out the commit, runs
`uv sync`, restarts the app and waits for it to answer.

## Deploying from GitHub

`.github/workflows/deploy.yml` runs the tests on every push and pull
request. For a push to `main` it then SSHes into the server as a `deploy`
user whose key can do one thing: run `house-finances-update` with the pushed
commit. To roll back, revert the commit and push.

### On the server (once)

```bash
# The update script, root-owned and outside the checkout, so a push can't
# change what runs as root. Reinstall it by hand if deploy/house-finances-update changes.
sudo install -m 755 -o root -g root deploy/house-finances-update /usr/local/bin/
echo 'deploy ALL=(root) NOPASSWD: /usr/local/bin/house-finances-update' |
    sudo tee /etc/sudoers.d/house-finances-deploy && sudo chmod 440 /etc/sudoers.d/house-finances-deploy
sudo useradd --system --create-home --shell /bin/sh deploy

# A key just for GitHub (keep the private half for the DEPLOY_SSH_KEY secret):
ssh-keygen -t ed25519 -N '' -C github-deploy -f /tmp/github-deploy
sudo install -d -m 700 -o deploy ~deploy/.ssh
printf '%s %s\n' 'command="sudo /usr/local/bin/house-finances-update \"$SSH_ORIGINAL_COMMAND\"",restrict' \
    "$(cat /tmp/github-deploy.pub)" | sudo install -m 600 -o deploy /dev/stdin ~deploy/.ssh/authorized_keys
cat /tmp/github-deploy        # paste into DEPLOY_SSH_KEY, then:
rm /tmp/github-deploy /tmp/github-deploy.pub
```

The repo is public, so the server fetches over HTTPS with no key.

### Reaching SSH

- **Port 22 open** (key-only): GitHub's runners come from changing
  addresses, so the port has to be open to everyone. `DEPLOY_HOST` is the
  server's IP or name.
- **Through the tunnel** (port 22 closed): in Zero Trust, Access > Service
  credentials > Service Tokens, create a token for GitHub. In the
  `ssh.example.com` Access application, add a policy with action **Service
  Auth** that includes that token. `DEPLOY_HOST` is `ssh.example.com`.

### In GitHub

Settings > Environments > New environment `production`. Under Deployment
branches choose Selected branches and add `main`; optionally add yourself as
a required reviewer to approve each deploy. Then add, in that environment:

| Name | Kind | Value |
|---|---|---|
| `DEPLOY_SSH_KEY` | secret | The private key printed above (all of it, including the BEGIN/END lines) |
| `DEPLOY_KNOWN_HOSTS` | secret | `<DEPLOY_HOST> <the server's host key>`: on the server, `echo "ssh.example.com $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"` |
| `CF_ACCESS_CLIENT_ID` | secret | Tunnel only: the service token's Client ID |
| `CF_ACCESS_CLIENT_SECRET` | secret | Tunnel only: the service token's Client Secret |
| `DEPLOY_HOST` | variable | `ssh.example.com`, or the server's IP for port 22 |
| `DEPLOY_USER` | variable | Optional; defaults to `deploy` |

`DEPLOY_KNOWN_HOSTS` pins the server's identity, so the workflow refuses to
talk to anything else. Leave the two `CF_ACCESS_*` secrets out when SSH is on
port 22.
