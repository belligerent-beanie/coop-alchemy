# Launch runbook: from this PC to a public game behind Cloudflare

Follow the steps in order. Each one ends with a check, so you know it worked before moving on. Everything runs from one folder on the
server, with two containers: the game (no open ports) and Cloudflare's tunnel (the only way in). Background and the reasons are in
[DEPLOYMENT.md](DEPLOYMENT.md); the Windows live server this replaces is described in [OPS.md](OPS.md).

**Already verified on this PC (2026-11-07):** the image builds, runs as a non-root user, passes its health check, keeps accounts and solo
progress across a restart, publishes no port, sees real visitor addresses from the `CF-Connecting-IP` header, and refuses signup without
an invite code (and accepts a code exactly once). **Not yet verified:** the tunnel container (needs your token) and anything on the real
server. Steps 3 to 6 are where that happens, so do them with a throwaway invite code before telling anyone.

## 0. What you need

| Need | Notes |
|---|---|
| A domain whose DNS is on Cloudflare | A free Cloudflare account is enough. Moving an existing domain's nameservers takes a few minutes to a day. |
| A small server with Docker | Ubuntu 24.04, 1 CPU and 2 GB is comfortable (1 GB holds about 50 players in rooms). Any provider. |
| The code on GitHub | The repository is `belligerent-beanie/coop-alchemy`. **Nothing from this session has been pushed yet:** run `git push` from this PC first. If the repository is private, the server needs a deploy key or token to clone it. |
| An email or web address to show players | Goes on the terms and privacy pages (`CONTACT_ADDRESS`). Optional, but the terms say to contact you. |

## 1. Cloudflare: the tunnel

1. Cloudflare dashboard > **Zero Trust** > **Networks** > **Tunnels** > **Create a tunnel** > type **Cloudflared** > name it `alchemy`.
2. Copy the **token** (the long string after `--token` in the `docker run` command it shows). Treat it like a password.
3. Skip the "install connector" step: the compose file runs it for you. On the **Public hostname** tab add a hostname, for example
   `alchemy.example.com`, with **Service** type `HTTP` and URL `alchemy:8000`.

Check: the tunnel's status page shows it as *Inactive* for now. That is right: nothing is connected yet.

## 2. The server

```
# as root, or with sudo, on a fresh Ubuntu 24.04
apt-get update && apt-get -y upgrade
curl -fsSL https://get.docker.com | sh                 # installs Docker Engine and the compose plugin
adduser --disabled-password --gecos "" alchemy && usermod -aG docker alchemy
ufw default deny incoming && ufw allow OpenSSH && ufw --force enable
```

Add your SSH public key to `/home/alchemy/.ssh/authorized_keys` and turn off password logins (`PasswordAuthentication no` in
`/etc/ssh/sshd_config`, then `systemctl restart ssh`). The game needs no inbound port at all: the tunnel only connects outwards.

Check: `docker run --rm hello-world` works as the `alchemy` user, and `ufw status` shows only OpenSSH.

## 3. Get the code and configure it

```
su - alchemy
git clone https://github.com/belligerent-beanie/coop-alchemy.git && cd coop-alchemy
cp .env.example .env
nano .env                       # TUNNEL_TOKEN=<the token>   CONTACT_ADDRESS=<your email or web address>
chmod 600 .env
```

`.env` is ignored by git. Signup is **invite-only** from the first start (the compose file passes `--require-invite`).

## 4. Start it

```
docker compose up -d --build
docker compose ps                 # alchemy: healthy;  tunnel: running
docker compose logs tunnel | tail # look for "Registered tunnel connection"
```

Check: open `https://alchemy.example.com/healthz` in a browser on your phone, on mobile data rather than Wi-Fi. You should see
`{"ok": true}` and a padlock. If you get a Cloudflare 502, the public hostname's service is wrong (it must be `alchemy:8000`).

## 5. The first administrator

```
docker compose exec alchemy python accounts_admin.py create-admin YourName
docker compose exec alchemy cat /data/YourName-first-password.txt      # shown once
```

Sign in at `https://alchemy.example.com/login` with that password, choose your own when asked, then delete the file:

```
docker compose exec alchemy rm /data/YourName-first-password.txt
```

Check: the home page loads, **Operations** appears for you, and `docker compose exec alchemy tail -3 /data/live.log` shows your login with
`client=<your real address>`, not a `172.x` Docker address. If it shows a Docker address, the `CF-Connecting-IP` setup is not working:
stop and fix it before inviting anyone, because every rate limit would treat all players as one person.

## 6. Lock the admin pages (a second lock)

Zero Trust > **Access** > **Applications** > **Add an application** > **Self-hosted**. Hostname `alchemy.example.com`, paths `/ops*` and
`/admin*`. Policy: **Allow**, include **Emails** = yours, with the one-time PIN login method.

Check: in a private window, `https://alchemy.example.com/ops` asks for an email code *before* anything of the game loads. Signed in as
yourself it works, and the game's own administrator check still applies after that.

Optional, if your Cloudflare plan allows: **Security** > **WAF** > **Rate limiting rules** for `POST /login` and `POST /register`.

## 7. Invite your first players

On the **Operations** page, make invite codes (or: `docker compose exec alchemy python accounts_admin.py invite create --count 5 --note "first friends"`).
Each code is shown once and works once. Give one code to each person, with the address.

Check with a throwaway code, from a phone on mobile data: signup without a code is refused, with the code it works, the same code a
second time is refused, you can start a game, a second account can join it by link, **Solo** starts, and the ⚑ report button reaches the
Operations page. Then delete the test accounts (`accounts_admin.py delete <name>`).

## 8. Move the data from this PC (only if you want the existing accounts and saves)

On this PC, stop the Windows server first so nothing changes, then:

```
python ops\backup.py --label migration            # makes <live folder>\backups\<stamp>-migration
```

Copy that folder to the server (`scp -r`), then on the server:

```
docker compose stop alchemy
docker compose cp ./<stamp>-migration/. alchemy:/data
docker compose run --rm -u root --entrypoint chown alchemy -R alchemy /data
docker compose start alchemy
```

Players log in again afterwards. When it is working, remove this PC's scheduled tasks: `powershell -File ops\install_tasks.ps1 -Remove`.

## 9. Backups, and a restore you have actually tried

A nightly backup from the host's cron (`crontab -e` as `alchemy`), keeping 14 days:

```
mkdir -p ~/backups
0 3 * * * cd ~/coop-alchemy && docker run --rm -v coop-alchemy_alchemy-data:/data -v "$HOME/backups":/backups alpine tar czf /backups/data-$(date +\%F).tgz -C /data . && find "$HOME/backups" -name 'data-*.tgz' -mtime +14 -delete
```

The volume's name is `<folder name>_alchemy-data`; check it with `docker volume ls`. Copy the newest file off the server now and then (to your
own PC or a cloud drive): a backup on the same disk does not survive the disk.

**Try a restore once, before you need one.** This throws away the current data and puts the backup in its place, so on a live game only do it
for real when you mean to:

```
docker compose down -v                                  # stops everything and deletes the data volume
docker compose up --no-start alchemy                    # makes a new, empty one
docker run --rm -v coop-alchemy_alchemy-data:/data -v "$HOME/backups":/backups alpine tar xzf /backups/data-YYYY-MM-DD.tgz -C /data
docker compose run --rm -u root --entrypoint chown alchemy -R alchemy /data
docker compose up -d
```

Check: you can still log in as yourself and your games are listed. (These exact commands were tried on this PC with a scratch account:
backup, restore into an empty volume and the data move all worked, and the game came back healthy with the account in place.)

## 10. Day to day

| Task | Command |
|---|---|
| Update to the newest code | `git pull && docker compose up -d --build` (a few seconds of downtime; players log in again) |
| Roll back | `git checkout <earlier commit> && docker compose up -d --build` |
| Is it healthy? | `docker compose ps`, and the Operations page |
| Activity log (kept 14 days) | `docker compose exec alchemy tail -50 /data/live.log` |
| A player forgot their password | Operations page > **New password**, or `accounts_admin.py reset <name>` |
| Add more invites | Operations page, or `accounts_admin.py invite create --count N` |
| Stop everything | `docker compose down` (the data volume is kept; `down -v` would delete it) |

## 11. If something is wrong

| You see | Likely cause and fix |
|---|---|
| Cloudflare 502 or 1033 | The tunnel is not connected or the hostname's service is not `alchemy:8000`. `docker compose logs tunnel`. |
| Everyone is locked out after a few wrong passwords | Rate limits see one address: the client-address header is not reaching the game. Re-check step 5. |
| "forbidden" when logging in or posting | The page's address and the `Host` the game sees differ (a second hostname, or a proxy that rewrites it). Use one hostname. |
| `docker compose up` stops at "put TUNNEL_TOKEN in .env" | `.env` is missing or the token line is empty. |
| Signup says "an invite code is needed" for someone who has one | The code was already used, mistyped, or revoked: `accounts_admin.py invite list`. |
| The game works but pictures are missing | Normal for a minute after a restart (addresses change each start); reload the page. |

## Before you say it is open

- [ ] Steps 4 to 7 done, including the throwaway-invite check from a phone on mobile data
- [ ] `/ops` and `/admin` ask for the Cloudflare Access code
- [ ] A restore tested (step 9) and one backup copied off the server
- [ ] The terms page shows your contact address (`https://<host>/terms`), and you are happy with its **13 or over** rule
- [ ] Ten minutes on a real iPhone and a real Android phone (see "Phones and tablets" in DEPLOYMENT.md): the hold-to-lift gesture is the part
      most likely to misbehave there
- [ ] (Optional, later) an OWASP ZAP scan against a staging copy
