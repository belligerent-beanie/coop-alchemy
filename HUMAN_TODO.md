# To do (the parts only a human can do)

Everything here needs your accounts, your hardware, your judgement or your eyes. The code side is done and committed; for each launch step
the exact commands and a check are in [LAUNCH.md](LAUNCH.md). Tick as you go.

## 1. Before anything else (5 minutes)

- [ ] **Push the commits.** Nothing from this work is on GitHub yet, and the server clones from there: `git push` from the project folder.
      If the repository is private, decide how the server will clone it (a deploy key is the tidy way: GitHub > Settings > Deploy keys).
- [ ] **Read the terms of use** (`static/terms.html`, or `/terms` when running) and change anything that is not how you want to run this.
      Two lines are real decisions, not boilerplate:
  - [ ] the **"13 or over"** age rule (it is also the answer to the "check COPPA" item);
  - [ ] the line that the administrators may **disable or delete accounts without notice**.
- [ ] Optional but sensible: have someone who knows the rules for your country look at the terms and privacy pages once. I am not a lawyer.
- [ ] **Decide how players reach you**, an email or web address for `CONTACT_ADDRESS` in `.env`. It appears on `/terms` and `/privacy`.

## 2. Accounts and hardware (LAUNCH.md steps 0 to 2)

- [ ] A **domain** with its DNS on Cloudflare (free plan is enough). Changing nameservers can take minutes to a day, so start early.
- [ ] A **small server** with Docker: Ubuntu 24.04, 1 CPU, 2 GB. Any provider. SSH keys only; firewall allows only SSH.
- [ ] A **Cloudflare tunnel**: Zero Trust > Networks > Tunnels. Copy the token. Public hostname service must be `alchemy:8000`.

## 3. Bring it up (LAUNCH.md steps 3 to 7)

- [ ] `git clone`, `cp .env.example .env`, put the token and contact address in, `docker compose up -d --build`.
- [ ] `https://<your host>/healthz` answers on **mobile data**, with a padlock.
- [ ] **Create the first administrator**, read the one-time password file, change the password, delete the file.
- [ ] Check the log shows **your real address** (`client=...`), not a `172.x` Docker address. If it shows Docker's, stop and fix before inviting anyone.
- [ ] **Lock `/ops*` and `/admin*`** with Cloudflare Access (one-time PIN to your email).
- [ ] Make a **throwaway invite code** and test from a phone: signup refused without it, works with it, refused the second time.
- [ ] Delete the throwaway accounts.

## 4. Data and safety net (LAUNCH.md steps 8 and 9)

- [ ] Move the existing accounts and saves from this PC, **if you want them** (stop the Windows server first). Otherwise start fresh.
- [ ] Turn off this PC's scheduled tasks once the new host is working: `powershell -File ops\install_tasks.ps1 -Remove`.
- [ ] Set up the **nightly backup** cron job on the server.
- [ ] **Try a restore once**, and copy a backup **off the server** (your PC or a cloud drive).

## 5. Test it like a player (needs real devices)

- [ ] Ten minutes on a **real iPhone** and a **real Android phone** (the checklist is in DEPLOYMENT.md, "Phones and tablets"). The thing most
      likely to misbehave is hold-to-lift on an element in the list. Also: sound, the discovery banner, emotes, Solo, the Collection view.
- [ ] Play one full game with a friend: invite link, a report (the ⚑ button), the end-of-game awards.
- [ ] Finish a **Solo** game to 830 (or ask me to help you unlock it for testing) and check **Create recipes** opens for you.

## 6. Decisions only you can make

- [ ] **Existing Create drafts are locked** until their owner finishes Solo. Fine, or grandfather the people who already have drafts?
- [ ] **Emotes:** design the real ones, then replace the six placeholder glyphs in `emotes.py` (or ask me to support images).
- [ ] **Sound defaults to on.** Keep that, or start muted?
- [ ] **Signup:** invite-only is the default. If you ever want open signup, the choices are Turnstile (a few clicks in Cloudflare) or email
      confirmation (against your minimal-data rule).
- [ ] **Companions are built but OFF.** Add `"--bots"`, `"on"` to the `alchemy` command in `docker-compose.yml` when you want lobbies to have
      company, and watch the first few on the Operations page (*Add a companion* and *Remove* are there for trying one). Look at the cast in
      `cast.py` and veto any names. Mr Alchemy is built and ON by default (`--mr-alchemy off` to stop him).
- [ ] The terms now say some players may be computer-controlled. Keep or reword that line (`static/terms.html`).

## 7. Later

- [ ] An **OWASP ZAP scan** against a staging copy, then a manual pass through every page.
- [ ] The hardening backlog (DEPLOYMENT.md, "Remaining hardening"): move the session token to an HttpOnly cookie, a breached-password check, an
      absolute session lifetime, drop `unsafe-inline` from the CSP.
- [ ] Pin the `cloudflare/cloudflared` image to a version once it works, so an update never surprises you.
- [ ] Decide when to switch off the Windows live server for good.

## What I can do when you say go

Anything marked "code side done" above can be redone or changed on request. Ones that are waiting on you: checking the tunnel and server once they
exist (send me the errors from `docker compose logs`), making invite batches, rewording the terms, and everything on the bots plan.
