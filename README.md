# Sentinel

A cross-platform [discord.py](https://discordpy.readthedocs.io/) server
management bot: timed punishment roles, warnings, a published rules post with
reaction-role acceptance, reaction-role menus, and an authenticated
server-side web dashboard for managing connected servers, moderation,
warnings, rules, configuration and history.

Everything is one slash-command tree, `/manage` — so a moderator can learn the
whole bot by typing `/manage` and reading the list:

```
/manage punish        /manage warnings     /manage rules publish
/manage pardon        /manage status       /manage rules disable
/manage warn          /manage setup        /manage rules list
                                           /manage fixcommands
```

**Moderation: the role flow**

```
(any roles) ──/manage punish──▶  Punish role  ──timer──▶  Post-punish role
(any roles) ──/manage warn───▶  unchanged roles + a recorded warning
```

* When a moderator runs `/manage punish`, the bot **adds the punish
  role** on top of whatever the member already has. The member keeps all
  their other roles.
* The bot posts a **staff embed** in the configured staff channel and
  **DMs the punished member an embed** with the same info.
* When the timer expires, the bot **removes the punish role** and
  **adds the post-punish role**, and posts a final staff embed.
* `/manage pardon` ends the punishment early by removing the punish /
  post-punish role and posts a final "pardon" embed to staff and a
  DM to the member.

**Protected members**

The bot refuses to punish:
* bots
* the bot itself
* members with the **Administrator** permission
* members with any **moderation permission** (`Moderate Members`,
  `Manage Guild`, `Kick Members`, `Ban Members`)
* members whose top role is equal to or higher than the bot's top role
* members holding the configured **staff role**

These checks run in `/manage punish` before any role change is made, so
even if a mod mis-clicks, nothing happens.

All active punishments are stored in a local SQLite database, so timers
survive a bot restart. Warnings and completed punishments live in the same
database, which is what `/manage status`, `/manage warnings`, and the
dashboard's history read from.

**Who can run what**

Discord permissions are *not* the gate. Moderation — `/manage punish`,
`pardon`, `warn`, `warnings` and `status` — requires the server's configured
**staff role** (set a staff role with `/manage setup staff_role:@Staff`), and
administrators. Holding *Moderate Members* is not enough: a server can grant
that permission to anyone, so Sentinel re-checks the caller's roles on every
run and refuses with an ephemeral message naming the staff role.

While no staff role is configured yet, moderation falls back to the old rule
(*Moderate Members* or *Manage Server*) so a fresh install keeps working; the
refusal message points at `/manage setup staff_role:`.

The administrative commands — `/manage setup`, `/manage fixcommands` and
everything under `/manage rules` — check for **Administrator** when they run.
Ticket and application management re-check their own staff rules. See
[Commands](#5-commands).

> **Quick start** — pick one:
>
> * **Easiest:** install a [pre-built release](#download) below, run it once
>   and answer the setup prompts.
> * **From source:** paste the single block for your OS in
>   [§2 Setup](#2-setup-macos-linux-windows) — it downloads the code, installs
>   everything and starts the bot. Then run `/manage setup` in Discord
>   ([§3](#3-configure-the-bot)).

---

## Download

Pre-built native installers are attached to every GitHub release:

[**Latest release →**](https://github.com/mob5824m-wq/Sentinel/releases/latest)

| Platform | Architecture | File | Notes |
|----------|--------------|------|-------|
| **macOS**   | Apple Silicon (`arm64`) | `Sentinel-X.Y.Z-arm64.dmg`      | Open the `.dmg`, drag the `.app` into `/Applications` |
| **macOS**   | Intel (`x86_64`)        | `Sentinel-X.Y.Z-x86_64.dmg`     | Same, on an Intel Mac |
| **Linux**   | x86-64 (`amd64`)        | `sentinel_X.Y.Z_amd64.deb`      | `sudo dpkg -i ...` and you're done |
| **Linux**   | ARM64 (`arm64`)         | `sentinel_X.Y.Z_arm64.deb`      | 64-bit Raspberry Pi OS (Bookworm or newer), Graviton, Ampere, … |
| **Windows** | x64 (`amd64`)           | `Sentinel-Setup-X.Y.Z.exe`       | Run the installer; it adds the bot to your Start Menu |
| **Windows** | ARM64 (`arm64`)         | `Sentinel-Setup-X.Y.Z-arm64.exe` | Windows on ARM (Snapdragon X, Surface Pro X, …) |
| **Source**  | —                       | `Source code (zip)` / `Source code (tar.gz)` | For everyone who'd rather run from source |

**Linux (.deb): copy & paste.** Download the `.deb` above, then paste this
whole block into a terminal — it picks the file matching your CPU for you,
installs it, runs the setup prompts and starts the service:

```bash
cd ~/Downloads
sudo dpkg -i sentinel_*_"$(dpkg --print-architecture)".deb
sudo sentinel --install
sudo systemctl start sentinel
```

(Adjust `~/Downloads` if you saved the file elsewhere. Re-run the same block
with a newer `.deb` to upgrade.) On macOS open the `.dmg` and drag the app
into `/Applications`; on Windows run the Setup `.exe`. Then start the app
once and answer the prompts — see [Run it](#4-run-it) for what to expect.

Each installer is built natively on a runner of its own architecture — an arm64
installer cannot be produced by an amd64 machine, because PyInstaller does not
cross-compile — so an arm64 machine downloads the file matching its CPU rather
than an emulated build. Filenames always say which is which.

**Raspberry Pi** — `sentinel_X.Y.Z_arm64.deb` is a native aarch64 package: it
runs on **64-bit Raspberry Pi OS** (Bookworm or Trixie; Pi 3, 4, 5, Zero 2 W
with the 64-bit image) and on other ARM64 Linux boards. It is built and tested
against Debian 12, i.e. exactly what Raspberry Pi OS 64-bit "Bookworm" is
based on, and the package's `Depends: libc6` states the glibc version it was
measured against, so an older system refuses the install instead of failing at
run time. The **32-bit** Raspberry Pi OS (`armhf`) is *not* covered by the
pre-built `.deb` — that needs an entirely separate 32-bit ARM build — so run
[from source](#2-setup-macos-linux-windows) there; the bot supports Python 3.9+
and has no compiled dependencies beyond `aiohttp`.

Releases are produced automatically by GitHub Actions whenever a
`v*` tag is pushed. See `.github/workflows/release.yml` for the
build pipeline, and the `VERSION` file for where the number comes from.

Every merge to `main` is published too, without waiting for a version bump:

| Release | What it is |
|---------|------------|
| [`latest-build`](https://github.com/mob5824m-wq/Sentinel/releases/tag/latest-build) | Rolling prerelease whose six installers (three platforms × two architectures) are replaced on every merge — one URL always has the newest build from `main` |
| `v<VERSION>-build.<run>` | One prerelease per merge (e.g. `v3.0.0-build.42`), so a specific build stays downloadable afterwards |

Both are marked *prerelease*, so
[`releases/latest`](https://github.com/mob5824m-wq/Sentinel/releases/latest)
keeps pointing at the newest versioned release rather than at an unreleased
build. `.github/workflows/merge-release.yml` runs this after each merge, and
`build.yml` sanity-checks the same build on pull requests.

> **Current release: v3.0.0** — the Sentinel rename. Read
> [Upgrading from Punishment Manager (v2)](#upgrading-from-punishment-manager-v2)
> before you update an install that already holds a config or database.
>
> Older note: the v1.0.0 and v2.0.0 installers crash on first launch on a
> packaged install (`PermissionError: [Errno 13] Permission denied:
> '/opt/sentinel/_internal/data'`), because they tried to create their
> database and log inside the read-only install directory. v2.1.0 stores that
> state in a writable per-platform location instead.

### Upgrading from Punishment Manager (v2)

Sentinel is Punishment Manager with a new name, one `/manage` command tree and
a reskinned dashboard. v3.0.0 is a clean cut rather than an in-place
migration, so give it five minutes:

| What changed                        | What to do |
|-------------------------------------|------------|
| Commands moved under `/manage`      | Nothing: the new tree is synced to every connected server at startup. `/punish apply` is now `/manage punish`, `/setup` is `/manage setup`, `/rules …` is `/manage rules …`. |
| Data moved to a `sentinel` directory (`/var/lib/sentinel`, `~/Library/Application Support/Sentinel`, `%LOCALAPPDATA%\Sentinel`, …) | Copy the old `config.json` and `punishments.db` into the new data dir, or re-run `sentinel --install`. |
| Service names are `sentinel` / `com.arena.sentinel` | Uninstall the old service first (`sudo punishment-manager --uninstall-service`), then `sudo sentinel --install-service`. |
| Environment variables are `SENTINEL_*` (were `PUNISHMENT_MANAGER_*`) | Update service units, containers and scripts. |

`sentinel --paths` prints the exact locations this build uses, and the log
names any location it had to fall back to.

---

## 1. Requirements

* Python **3.9 or newer** (developed and tested on 3.11; works on 3.9+).
* A Discord application + bot token — see
  <https://discord.com/developers/applications>.
* The bot must be invited with at minimum:
  * **Manage Roles** *(for punishment and rules-acceptance roles)*
  * **Moderate Members** *(not required: moderation is gated by the staff
    role. It is the fallback rule on servers that have not set one yet.)*
  * **Send Messages** *(for staff-channel embeds and rules posts)*
  * **Embed Links** and **Add Reactions** *(for rules posts)*
  * **Use Application Commands**

Enable **Server Members Intent** in the Discord Developer Portal under
*Bot → Privileged Gateway Intents*; the bot uses it for member/role updates.

> **Role order matters.** Drag the bot's role *above* the punish, post-punish,
> and rules-acceptance roles in *Server Settings → Roles*, otherwise it cannot
> give or take them.

---

## 2. Setup (macOS, Linux, Windows)

**Copy, paste, done.** Paste the *one* block for your OS into a terminal and
press Enter. It downloads the code, creates a virtualenv, installs the
dependencies, runs the interactive installer and starts the bot — there is
nothing else to run for a first install. (Using a packaged release instead?
See [Download](#download); the prompts are the same.)

Before you paste, have these ready:

1. A bot token: <https://discord.com/developers/applications> → your app →
   **Bot** → **Token**. Also turn on **Server Members Intent** under
   *Bot → Privileged Gateway Intents* (see [Requirements](#1-requirements)).
2. In Discord, enable *Settings → Advanced → Developer Mode* so you can
   right-click roles/channels and choose **Copy … ID** when the installer asks.
3. The roles/channels themselves can also be set later from Discord with
   `/manage setup` — every installer prompt except the token can be skipped
   with Enter (see [Configure the bot](#3-configure-the-bot)).

### macOS — copy & paste (Terminal)

```bash
git clone https://github.com/mob5824m-wq/Sentinel.git
cd Sentinel
./scripts/run_mac.sh
```

No `git` or `python3` yet? macOS will offer the Command Line Tools the first
time you run `git` — accept, wait for it to finish, then paste the block
again. (Python also comes from <https://www.python.org/downloads/>.)

### Linux — copy & paste (Terminal)

On Debian / Ubuntu (including Raspberry Pi OS), paste this whole block — the
first two lines install anything missing, the rest is the same as macOS:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
git clone https://github.com/mob5824m-wq/Sentinel.git
cd Sentinel
./scripts/run_linux.sh
```

On another distro, install the equivalents of `git`, `python3` and the
`venv` module with your package manager, then paste just the last three lines.

### Windows — copy & paste (PowerShell)

```powershell
git clone https://github.com/mob5824m-wq/Sentinel.git
cd Sentinel
.\scripts\run_windows.bat
```

In **Command Prompt** the only difference is the last line, which needs no
`.\` prefix: `scripts\run_windows.bat`.

If `git` or `python` is "not recognized", paste this first, then close and
reopen PowerShell and paste the block above again:

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.11 -e
```

(No `winget`? Install Git from <https://git-scm.com/download/win> and Python
from <https://www.python.org/downloads/> — tick **Add python.exe to PATH** —
then reopen the terminal.)

### No git? Already downloaded the source `.zip`?

Extract it, open a terminal *in the extracted `Sentinel` folder*, and paste
only the last line of your OS block above (`./scripts/run_mac.sh`,
`./scripts/run_linux.sh` or `.\scripts\run_windows.bat`). The same applies if
you already have the folder: `cd` into it and paste that one line.

### What happens next

The helper script does the boring parts (virtualenv + dependencies) and then
the interactive installer (`installer.py`) asks for:

| Field             | Where to find it                                      |
|-------------------|-------------------------------------------------------|
| `bot_token`       | Discord Developer Portal -> your app -> Bot -> Token (typing is hidden; paste and press Enter) |
| `server_id`       | Optional for command sync. Right-click the server icon -> Copy Server ID |
| `punish_role_id`  | Right-click the role -> Copy Role ID                  |
| `post_role_id`    | Right-click the role -> Copy Role ID                  |
| `staff_role_id`   | Optional. Members with this role (and any user with admin/moderator permissions) cannot be punished. |
| `staff_channel_id`| Right-click the channel -> Copy Channel ID (optional) |
| `dm_user`         | y / n (default y)                                     |

Press Enter on anything you don't have yet — skipped fields are left as they
are, re-running is safe, and roles can be set afterwards with `/manage setup`.
The installer writes `config.json` with `0600` permissions on Unix so the
token isn't world-readable.

The bot then starts straight away (see [Run it](#4-run-it) for the log lines
to expect). Press **Ctrl+C** to stop it; to start it again, `cd` back into
the `Sentinel` folder and re-paste your OS's last line — your config is kept,
so there are no prompts.

**Skip the token prompt (optional).** If `DISCORD_TOKEN` is set, the launcher
uses it instead of asking. Paste one of these *instead of* the last line of
your OS block, with your token between the quotes:

macOS / Linux:

```bash
DISCORD_TOKEN="paste-your-token-here" ./scripts/run_linux.sh
```

(Use `./scripts/run_mac.sh` on macOS.) Windows (PowerShell):

```powershell
$env:DISCORD_TOKEN="paste-your-token-here"; .\scripts\run_windows.bat
```

### Manual setup (without the helper script)

Same result, step by step. Each block is complete and pasteable as-is — run
it from inside the `Sentinel` folder:

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python installer.py
python bot.py
```

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python installer.py
python bot.py
```

If PowerShell refuses `Activate.ps1` ("running scripts is disabled"), paste
this once and then re-run the activation line:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Windows (Command Prompt): same block, but activate with
`.venv\Scripts\activate.bat` instead of `Activate.ps1`.

---

## 3. Configure the bot

You can configure the bot two ways: with the interactive installer
(recommended for first-time setup), or with the in-Discord `/manage setup`
command (for tweaking things later).

### Option A — interactive installer

To (re)run the installer from a source checkout, paste the block for your OS
from inside the `Sentinel` folder. This writes `config.json` with everything
the bot needs:

macOS / Linux:

```bash
source .venv/bin/activate
python installer.py
```

Windows (PowerShell):

```powershell
.venv\Scripts\Activate.ps1
python installer.py
```

(In Command Prompt, activate with `.venv\Scripts\activate.bat` instead. If
you never stopped the helper script, just re-pasting
`./scripts/run_linux.sh` / `./scripts/run_mac.sh` / `.\scripts\run_windows.bat`
works too — it re-runs the installer whenever no token is configured.)

### Option B — in-Discord `/manage setup` (easiest to change roles later)

1. Create the punish and post-punish roles in your server, e.g. `Punished`, `Suspended`.
2. (Optional) Create a `Staff` role - anyone with this role will be protected from punishment.
3. Create a "staff-logs" text channel and make sure the bot can post in it.
4. As a server administrator, run:

   ```
   /manage setup punish_role:@Punished post_role:@Suspended
              staff_role:@Staff staff_channel:#staff-logs dm_user:true
   ```

   `staff_role`, `staff_channel`, and `dm_user` are optional; the two
   role arguments are required.

### Configure rules and a reaction role

1. Create a basic `Verified` / `Member` role. Keep it below the bot's role and
   do not give it moderation or server-management permissions.
2. As a server administrator, publish the rules:

   ```text
   /manage rules publish channel:#rules role:@Verified rules_text:"1. Be respectful. 2. No spam or harassment."
   ```

   The bot posts this message, an embed containing the rules, and adds a ✅
   reaction:

   > By reacting to this you acknowledge the rules and will abide by them.

   Members who react receive the configured role; removing their reaction
   removes that role. Publishing again under the same name replaces that set's
   post. Existing role assignments are not changed by republishing; if you
   change the acceptance role, remove the old role from existing members as
   needed. `/manage rules disable` turns off reaction handling for a set and leaves
   assignments unchanged.

#### Several rule sets on one server

A server can run more than one rule set — a full "Zone rules" post plus a
short "Event rules" or "Contest rules" post, each with its own channel, role
and text. Give each set a name and it stays independent:

```text
/manage rules publish channel:#rules role:@Verified name:"Zone rules" rules_text:"1. Be respectful. 2. No spam."
/manage rules publish channel:#events role:@Events name:"Event rules" rules_text:"1. Keep chat on topic. 2. No spoilers."
```

* `/manage rules publish` with an existing name (case-insensitive) replaces only that
  set's post; other sets are untouched. The default name — used when you leave
  the name out — is `Zone rules`, so an unnamed post is the default set.
* **A name you type is kept exactly as written.** Nothing normalises it towards
  the default: name a set `Server rules` (the default of older versions) and it
  stays `Server rules`, stored, listed and titled as "Your Server — Server
  rules". Only a name matching the current default, `Zone rules`, gets the
  short "Your Server Rules" title.
* `/manage rules disable name:"Event rules"` disables one set — its post is marked
  disabled and its ✅ is removed, but roles already granted are left alone.
  With only one set published, `name` can be omitted.
* `/manage rules list` shows every set, its channel, its role and its message id.
* Reactions are routed by message, so ✅ on the "Event rules" post grants the
  events role and never the server-rules role.
* Up to 25 sets per server.

The prompt sentence lives in `rules.py` as `RULES_POST_CONTENT`, so
`/manage rules publish` and the dashboard's publish button always post the same
wording (edit it there to change it everywhere). Rules text can be up to
4,096 characters. The bot stores every set per server in `config.json`; no
manual config edit is needed, and a config written by an older version (a
single object instead of a list, no names) keeps working as one set named
`Zone rules` — sets that do carry a name keep it untouched.

The rules post is sent with mentions disabled, so no `@` in the rules can ping
anyone, and the post is only edited or deleted by the bot itself.

In the dashboard, the **Rules & reactions** page lists every published rule set
with **Edit** (rename it, move it to another channel, change the role, or
rewrite the text — the message is updated in place) and **Disable**, and the
editor below publishes a new one.

Need more than one reaction role per post, or a custom message of your own?
See [Reaction role menus](#reaction-role-menus-dashboard) below.

#### Markdown in the rules

The rules text is regular Discord Markdown, rendered by Discord's own client.
Anything Discord supports inside an embed works:

| Syntax | Result |
|--------|--------|
| `**bold**`, `*italic*` / `_italic_`, `__underline__`, `~~strikethrough~~`, `\|\|spoiler\|\|` | inline formatting |
| `# Heading`, `## Heading`, `### Heading` | headings (`####` and more are shown as text) |
| `- item`, `* item`, `1. item` | bulleted / numbered lists |
| `> quote`, `>>> quote` | block quotes (the second quotes everything after it) |
| `` `code` ``, ```` ```code``` ```` | inline code and code blocks |
| `[label](https://example.com)`, `<https://example.com>` | clickable links |
| `-# small note` | subtext |
| `<@user>`, `<@&role>`, `<#channel>` | mentions (rendered, never pinged) |

Discord does **not** render tables, images, task lists, horizontal rules
(`---`), `####`+ headings or nested lists inside an embed — those are shown
literally, so the bot's preview does not pretend otherwise.

**In the dashboard.** The Rules page has a formatting toolbar (bold, italic,
underline, strikethrough, spoiler, headings, lists, quote, code, code block,
link — with `Ctrl`/`Cmd` + `B`, `I`, `E` shortcuts) and a **live preview** that
shows the whole post exactly as Discord renders it: the prompt message, the
embed body, the embed title and the footer. The preview is rendered by the bot
(`discord_markdown.py`) rather than the browser, so what you see is what the
published post looks like. While you
type, the editor also flags syntax Discord would show as plain text — an
unclosed `**`, a `####` heading, a missing space after `#`, or a non-http link.

<sub>Want to verify the renderer without the browser?</sub>

```bash
python3 -c "from discord_markdown import render_markdown_html; print(render_markdown_html('# Rules\n**be kind**'))"
```

### Reaction role menus (dashboard)

The rules post grants one role for one ✅. For anything more — a ping-picker, a
colour menu, an events role — open the dashboard's **Rules & reactions** page
and use the **Reaction role posts** section. There is no limit on the number of
posts; each one is a separate bot message with its own mapping.

1. Pick the **post channel**.
2. Choose the **post style**: an embed (optional title + text) or a plain
   message.
3. Write the **message** members will read. It is regular Discord Markdown —
   the same formatting toolbar, live preview and linter as the rules editor —
   and it is posted with mentions disabled, so nothing in it can ping anyone.
   Leave it empty to publish the default prompt
   (`React with an emoji below to add or remove a role.`).
4. Add the **emoji → role pairs** (up to 20 per post). Type or paste any emoji,
   or a custom emoji as `name:id` / `<:name:id>`; the quick-add row and
   **Insert emoji list** button write the role key into the message for you.
5. Pick what each pair **does** when someone reacts:

   | Action | Reacting | Un-reacting |
   |--------|----------|-------------|
   | **Give role** (default) | hands the member the role | takes it back |
   | **Remove role** | strips the role from the member | hands it back |

   *Give* is the ping-picker case. *Remove* is the opt-out case: an
   "🔕 react to stop being pinged for events" emoji, or a "clear my own
   access" reaction. Give and remove pairs mix freely on one post, and a
   member who never reacts is never touched — the bot only ever changes the
   role of the person who reacted.

6. Press **Publish post**. The bot posts the message and adds every reaction.

Un-reacting reverses whatever the pair did, whether it gave or removed the
role, unless **Undo the change when a member removes their reaction** is
cleared for that post (then reactions are one-way: they apply once and
un-reacting does nothing). Roles are validated exactly like the acceptance
role: they must sit below the bot's role, must not be managed by an
integration, and must not carry moderation or server-management permissions.

Each post listed under **Published reaction role posts** has **Edit** (change
the text, the pairs, or the style — the message is updated in place and the
reactions are re-synced) and **Remove** (drops the configuration and deletes
the bot's message). Moving a post to another channel reposts it. Members keep
any role they already picked, so removing a pair or a post never strips roles —
take them off manually if that is what you want. If the Discord message is
deleted, the next **Save changes** posts a fresh one under the same entry.

The mapping is stored per server in `config.json` under `reaction_roles`, next
to `rules`, so it survives restarts:

```json
"reaction_roles": {
  "987654321098765432": [
    {
      "post_id": "6f1c0b3a",
      "channel_id": 111111111111111111,
      "message_id": 222222222222222222,
      "title": "Choose your roles",
      "message": "React below to pick your pings.",
      "use_embed": true,
      "remove_on_unreact": true,
      "entries": [
        { "emoji": "🎮", "role_id": 333333333333333333, "action": "add" },
        { "emoji": "🔕", "role_id": 444444444444444444, "action": "remove" }
      ]
    }
  ]
}
```

`action` is `add` (give the role on react) or `remove` (take it away on
react); an entry without the key is treated as `add`, so menus written by
older versions keep working unchanged.

### Option C — edit `config.json` directly

```json
{
  "bot_token":        "YOUR-BOT-TOKEN",
  "server_id":        987654321098765432,
  "punish_role_id":   222222222222222222,
  "post_role_id":     333333333333333333,
  "staff_role_id":    555555555555555555,
  "staff_channel_id": 444444444444444444,
  "dm_user":          true,
  "rules":            {},
  "tickets":          {},
  "applications":     {},
  "dashboard_enabled": true,
  "dashboard_host":    "127.0.0.1",
  "dashboard_port":    8765
}
```

The `bot_token`, `server_id`, and role ids go at the top level
(single-server shape). `server_id` is optional for command syncing; if it is
omitted, use `/manage setup` to associate role settings with each server. The bot
fills the `rules` map when a rule set is published (a list of named sets per
server), the `reaction_roles` map when a reaction-role post is published from
the dashboard, `tickets` when a ticket panel or category is configured, and
`applications` when an application form is created — all per server. It also
generates `dashboard_token` on first startup. Keep `config.json` private; it contains
credentials. The `guilds`, `token`, and `log_channel_id` keys are a legacy
multi-server shape and are still respected for backwards compatibility.

Publishing rule sets from Discord or the dashboard fills `rules` like this:

```json
"rules": {
  "987654321098765432": [
    {
      "ruleset_id": "6f1c0b3a",
      "name": "Zone rules",
      "channel_id": 111111111111111111,
      "message_id": 222222222222222222,
      "role_id": 333333333333333333,
      "rules_text": "1. Be respectful."
    }
  ]
}
```

To find ids: enable Developer Mode in *Settings -> Advanced*, then
right-click the server/role/channel and choose "Copy ... ID".

---

## 4. Run it

Starting again later is the same one line as setup — paste it from inside
the `Sentinel` folder (your config is kept, so there are no prompts), or
`sentinel` by itself for a packaged install:

macOS:

```bash
./scripts/run_mac.sh
```

Linux:

```bash
./scripts/run_linux.sh
```

Windows (PowerShell):

```powershell
.\scripts\run_windows.bat
```

Windows (Command Prompt): `scripts\run_windows.bat`. Press **Ctrl+C** to stop
the bot.

You should see:

```
[INFO] sentinel: Database initialised at data/punishments.db
[INFO] sentinel: Global command registry was already empty (startup; commands are registered per guild).
[INFO] sentinel: Logged in as YourBot (id=...)
[INFO] sentinel: Synced 1 command(s) to guild X (instant).
```

### Server-side web dashboard

When the bot is running, open **<http://127.0.0.1:8765>** on the bot host.
The dashboard covers connected servers, active punishments, warnings,
pardon/apply actions, the rules post, multi-role reaction-role menus, tickets,
application forms and submissions, server role/channel settings,
slash-command sync, and punishment history. The **Tickets** page publishes the
panel, edits the categories and modes, and lists every ticket with
close/reopen/delete; the **Applications** page builds forms (questions, review
channel, accept/remove roles), publishes Apply panels, and reads submissions
with Approve/Deny and a decision note.

**Finding the dashboard key.** The first startup generates a private
dashboard key and saves it as `dashboard_token` in the bot's `config.json`.
Until you paste that key into the login screen, the dashboard stays locked.
To retrieve it, open a terminal **on the machine that runs the bot** and run
the command for your install:

| How the bot is installed | Command |
|--------------------------|---------|
| Packaged build (`.dmg`, `.deb`, Windows Setup) | `sentinel --dashboard-token` |
| Running from source | `python bot.py --dashboard-token` (with the `.venv` activated, see below) |

The command prints the key on a single line (and creates it if it doesn't
exist yet). From source, paste this in a second terminal (from the `Sentinel`
folder) so the virtualenv's Python is used:

macOS / Linux:

```bash
source .venv/bin/activate
python bot.py --dashboard-token
```

Windows (PowerShell):

```powershell
.venv\Scripts\Activate.ps1
python bot.py --dashboard-token
```

`sentinel --dashboard` then shows how the dashboard is reachable
and what to fix if it isn't; `sentinel --duckdns` tests a DuckDNS update.
Not sure where `config.json` lives? `sentinel --paths`
(or `python bot.py --paths`, with the `.venv` activated) prints the resolved
config, database, and log paths. The dashboard key is **not** the Discord
bot token — pasting the bot token into the dashboard will not work.

The default listener is loopback-only; the key has access to **every server
connected to this bot**, so treat it like a bot-owner credential. For a remote
server, prefer an SSH tunnel rather than opening a port:

```bash
ssh -L 8765:127.0.0.1:8765 user@your-server
```

Then open <http://127.0.0.1:8765> on your workstation. Restrict it with a
firewall, and **never expose the plain-HTTP dashboard directly to the
internet**. Dashboard moderation entries are tagged `[Dashboard]`; since the
dashboard uses a host key rather than Discord OAuth, its moderator ID is
recorded as the server owner.

Two commands answer "where is it, and why can't I reach it?":

| Command | What it prints |
|---------|----------------|
| `sentinel --dashboard` | The URLs, the TLS/proxy settings, and one line per thing to fix for remote access |
| `sentinel --duckdns` | Sends one DuckDNS update now and reports the address it recorded |

#### Reaching it from outside the house (e.g. DuckDNS)

To check the dashboard from a phone or from work, put HTTPS in front of it and
give it a name. Sentinel keeps a free [DuckDNS](https://www.duckdns.org) name
pointed at your current home address by itself:

```json
{
  "duckdns_domain": "myhome",
  "duckdns_token": "the-account-token-from-duckdns.org",
  "dashboard_allowed_hosts": ["myhome.duckdns.org"],
  "dashboard_public_url": "https://myhome.duckdns.org",
  "dashboard_secure_cookie": true,
  "dashboard_trusted_proxies": ["127.0.0.1", "::1"]
}
```

Keep `dashboard_host` on `127.0.0.1` and let a reverse proxy on the same
machine (Caddy, nginx) terminate TLS and forward to it; then forward 80/443 at
the router, never 8765. The `dashboard_allowed_hosts` entry is required - the
dashboard answers only for names you list, to block DNS-rebinding attacks, and
a name that is missing gets `400 Unrecognized Host header`.

Full walkthrough, including serving TLS directly from Sentinel, SSH tunnels,
`X-Forwarded-For` behind a proxy, and a symptom-to-fix table:
**[docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md)**.

Slash commands are registered **per server only** — that is the scope that
appears immediately, so they show up without waiting and without setting
`server_id`. Newly joined servers are synced as soon as they become
available.

Discord keeps *two* independent command registries per app (the global one
and the per-guild one) and the Discord client lists a command from each of
them, so registering a command in both is exactly what makes every
`/manage` entry appear **twice**. That is why the bot never
uploads commands globally; if an older version left any behind, startup
deletes them (you will see a "Removed N duplicate global command(s)" line),
and the admin-only `/manage fixcommands` command does the same thing on demand.

---

## 5. Commands

Everything lives under `/manage`. Reaction-role menus are configured from
the dashboard (see
[Reaction role menus](#reaction-role-menus-dashboard)) rather than a command,
since they need a message box, a live preview, and one role picker per emoji.

| Command              | Who can use it                  | What it does |
|----------------------|---------------------------------|--------------|
| `/manage punish`      | Staff role (or administrators) | Adds the punish role for the configured duration. Posts a staff embed and DMs the member. |
| `/manage warn`       | Staff role (or administrators) | Records a warning (reason + moderator + time). No role is changed. Posts a staff embed and DMs the member. |
| `/manage warnings`   | Staff role (or administrators) | Lists a member's recorded warnings. Pass `clear:true` to delete them all (this is logged to the staff channel). |
| `/manage pardon`     | Staff role (or administrators) | Ends the punishment early and removes the punish / post-punish role. Posts a staff embed and DMs the member. |
| `/manage status`     | Staff role (or administrators) | Shows the server configuration and a list of active punishments. Pass a `user` to see that member's active status, history, and warnings. |
| `/manage rules publish`     | Server administrators           | Posts a named rule set and sets its ✅ acceptance reaction role. Publishing the same name replaces that set. |
| `/manage rules disable`     | Server administrators           | Stops handling reactions for one rule set (pass `name` when several exist). Existing roles are unchanged. |
| `/manage rules list`        | Server administrators           | Lists the published rule sets with their channel, role and message. |
| `/manage tickets panel`     | Server administrators           | Sets the options (mode, log channel, ticket category) and posts or refreshes the panel. |
| `/manage tickets category`  | Server administrators           | Lists the panel buttons when called bare; with a `label` it adds or edits one, and `remove:true` deletes it. |
| `/manage tickets console`   | Staff (staff role or moderation permissions) | The ticket queue: a summary and a select. Pick a ticket to read it and claim, close or reopen it. |
| `/manage tickets view`      | Staff                           | Shows one ticket's details and controls by ticket number or channel id. |
| `/manage tickets claim`     | Staff                           | Accepts responsibility for a ticket, joins its private thread or channel, and updates its status. |
| `/manage applications form` | Server administrators           | Lists the forms when called bare; with a `name` it creates or edits one, and `remove:true` deletes it. |
| `/manage applications panel` | Server administrators          | Posts (or refreshes) an Apply panel for one form. |
| `/manage applications review` | Staff                          | The review queue: a summary and a select. Pick a submission to read the answers and approve or deny it. |
| `/manage applications view` | Staff                           | Shows one application's full answers and review controls by submission id. |
| `/manage applications accept` | Staff                         | Approves one pending application by id, with an optional note. |
| `/manage applications decide` | Staff                          | Approves or denies one submission by id, with an optional note. |
| `/manage setup`             | Server administrators           | Configures the punishment roles, staff channel, and DM behavior. |
| `/manage fixcommands`       | Server administrators           | Removes duplicated slash commands (e.g. doubled `/manage` entries) and re-syncs this server. |
| `/apply`                    | **Every member**                | Opens the application modal. It cannot list, view or edit anything. |
| `/ticket`                   | **Every member**                | Opens a ticket: choose a category, answer the subject box. It cannot read or edit any ticket. |

### A small command surface, on purpose

Staff can work either from the queues or directly by id:

* **Browse a queue.** `/manage tickets console` and
  `/manage applications review` return private queue messages. Picking a record
  shows its details and controls (Claim / Close / Reopen, or Approve / Deny).
* **Use direct commands.** `/manage tickets view` shows a ticket card, and
  `/manage tickets claim` accepts responsibility for it. `/manage applications view`
  shows a submission's full answers, while `/manage applications accept`
  approves it directly. Use `/manage applications decide` when you need to deny
  an application or choose the decision by id. These commands use the same
  permission-checked ticket/application handlers.
* **List and edit share a command.** `/manage tickets category` with no options
  lists the categories; naming one adds or edits it, and anything left out is
  kept — so `/manage applications form name:"Staff" allow_multiple:true` changes
  one setting without touching the questions or the review channel.
* **Options are optional.** `/manage tickets panel` without a channel only saves
  the settings, and with a channel only refreshes the panel, so an
  administrator never has to restate what they set last time.

No row above is enforced by Discord: `/manage` carries no permission gate, so
every check happens *when the command runs*. A member without the staff role
who tries `/manage punish` gets an ephemeral refusal naming the role, and a
moderator who tries `/manage setup` gets an "administrators only" reply. (The
group is left visible on purpose — a role cannot be expressed as a Discord
permission, and hiding the group behind *Moderate Members* would hide it from
the staff-role holders the server wants moderating.)

### `/manage punish` options

* `user` — the member to punish.
* `duration` — how long. Examples: `30m`, `2h`, `1d`, `1d12h`, `90`
  *(bare numbers are interpreted as minutes)*.
* `reason` — optional, shown in the staff embed, the DM embed, and the DB.

The maximum duration is 30 days. The minimum is 5 seconds.

### Warnings

`/manage warn <user> <reason>` is the light-weight option: unlike
`/manage punish` it changes **no roles** and has no timer. Each warning is
stored permanently in the bot's database with its reason, moderator, and
timestamp, so a member's record survives restarts and pardons.

* `/manage warnings <user>` lists a member's warnings (newest first) with
  the running total. Moderators can send the same command with `clear:true` to
  delete every warning for that member; the clear is announced in the staff
  channel so it is never silent.
* `/manage status <user>` includes the warning total and the three most recent
  warnings next to the punishment history.
* The dashboard's **Moderation** page has the same two actions:
  a *Warn a member* form and a *Recent warnings* table with a **Clear**
  button per member. Dashboard warnings are tagged `[Dashboard]` and are
  attributed to the server owner, exactly like dashboard punishments.

Protected members (admins, moderators, staff role holders, bots, and anyone
above the bot's role) cannot be warned, and nobody can warn themselves.
Warnings are DMed to the member and posted to the staff channel using the same
`dm_user` / `staff_channel_id` settings as punishments.


### Tickets

Members open a ticket from a panel button, or with `/ticket`; the conversation
stays between them and staff. There are two shapes and a server can use
**both** — the default is chosen once, and any category can override it:

| Mode | What the member gets | When to use it |
|------|----------------------|----------------|
| **Private thread** (default) | A private thread under the panel channel. Invite-only: the opener is added, and so is everyone holding the category's staff role (up to 20). Staff outside that group press **Claim** on the staff notice and are added. | Servers whose staff team fits in a private thread. Keeps everything in one channel, so the panel channel doubles as the ticket list. |
| **Private channel** | Its own channel, `@everyone` denied, with the opener, the staff role and the bot allowed. | Busier servers: a role grant reaches every staff member at once, and long transcripts do not pile up in one channel. |

Set it up in two steps:

```text
/manage tickets category label:"General help" emoji:❓
/manage tickets panel channel:#open-a-ticket mode:"Private thread" log:#staff-log
```

`/manage tickets panel` is the one settings command: `mode` picks how new
tickets open, `log` is where staff get the notice, and `parent` files
channel-mode tickets under a Discord category. Every option is optional —
running it with only a channel just refreshes the panel's buttons, and running
it with only a mode changes that setting and leaves the panel where it is.

* **Categories** are the panel buttons. `/manage tickets category` on its own
  lists them; naming one adds or edits it, and options you leave out are kept,
  so `/manage tickets category label:"General help" emoji:🎫` re-badges one
  button without touching the rest. Each one can name its own
  `staff_role`, its own `mode`, a description (shown in the panel's category
  list), and whether to ask "what is this about?" before opening
  (`ask_subject`, on by default). Up to 25 per server.
* The **panel** is one bot message. Publishing again edits that same message,
  so a server never accumulates stale panels. A button whose category has since
  been deleted explains that instead of failing silently.
* **Staff notices** go to `/manage tickets`' log channel, or — if none is
  set — to the staff channel from `/manage setup`. The notice carries **Claim**
  and **Close** buttons, so a staff member who is not in a private thread can
  join it with one click.
* Inside the ticket, the header shows who opened it, the category, the mode,
  the status, who claimed and closed it and why, and carries **Claim**,
  **Close** and **Reopen**.
* **Closing** locks the ticket (`closed-…`, the opener can no longer post),
  posts the reason, DMs the opener when `dm_user` is on, and keeps the record.
  **Reopening** undoes it. Closing and reopening are recorded with whoever did
  it.
* **Claiming** marks the ticket as taken, adds the claimer to the thread and
  announces it, so two staff members do not answer the same question.
* Tickets are stored in SQLite with per-server numbers (`#0007`), so
  `/manage tickets console` still lists one whose thread has been renamed,
  archived or deleted by hand.

Staff can either browse the queue or use an individual ticket number directly:

```text
/manage tickets console
/manage tickets view ticket:#0007
/manage tickets claim ticket:#0007
```

`view` returns a private ticket card with a link to its channel and staff
controls. `claim` accepts the ticket for the caller, adds them to a private
thread when needed, and records who took it.

#### What a normal member can do

| | Member who opened it | Other members | Staff |
|---|---|---|---|
| Open a ticket (panel button or `/ticket`) | ✅ | ✅ | ✅ |
| See that ticket | ✅ | ❌ | ✅ |
| Claim / Reopen | ❌ | ❌ | ✅ |
| Close | ✅ (withdraw their own) | ❌ | ✅ |
| List or view **any** ticket | ❌ — there is no command or button that shows a ticket list to a member | | ✅ |

Members never see a ticket they did not open, and nothing lets them edit,
search or browse tickets. `/ticket` only creates one; staff can read from
`/manage tickets console` or `/manage tickets view`, and accept responsibility
with `/manage tickets claim`. These commands are visible in the `/manage` tree
but re-check staff permission whenever they run; non-staff callers only receive
an ephemeral refusal. The dashboard can list and act on every ticket, and it
requires the dashboard token.

### Applications

An application form is a short set of questions members answer in a modal; the
answers go to staff for a decision.

```text
/manage applications form name:"Staff application" review_channel:#staff-apply questions:"Why do you want to join? | short:Timezone | Experience"
/manage applications panel form:"Staff application" channel:#apply
```

`/manage applications form` on its own lists the forms; naming one creates it,
or edits it when it already exists — and editing only changes what you pass, so
`/manage applications form name:"Staff application" allow_multiple:true`
flips one setting and leaves the questions, the review channel and the roles
exactly as they were. `remove:true` deletes a form (submissions already
received stay readable).

Staff can browse the review queue or work directly with a submission id:

```text
/manage applications review
/manage applications view application:12
/manage applications accept application:12 note:"Welcome aboard!"
```

`view` shows the full answers privately. `accept` approves the pending
submission, records the note, grants the configured accept role when possible,
and sends the applicant the outcome. Use `/manage applications decide` to deny
by id instead.

* **Questions** — one to five (Discord's modal limit), each a one-line box or a
  paragraph. In the slash command, separate them with ` | ` and prefix
  `short:` for a one-line box; the dashboard has a proper editor with the same
  limits.
* **Submission** stores every answer next to the question that was asked (so
  editing the form later never rewrites what somebody was asked), posts a
  review card in the form's **review channel** with **Approve** and **Deny**
  buttons, and DMs the applicant a confirmation.
* **Decisions** can be made from those buttons, from
  `/manage applications review` (pick a submission, then Approve or Deny),
  from `/manage applications decide` by id, or from the dashboard. The decision is written
  once (a second attempt is refused), the review card loses its buttons and
  shows the outcome, the applicant is DM'd the note, and the form's *accept*
  role is added (or its *remove* role taken away) when the bot is able to.
* A member can have one **pending** application per form at a time; the
  dashboard can allow repeats (`allow_multiple`) if the server wants them.
* The dashboard's **Applications** page lists submissions and shows the full
  answers, so staff can decide without scrolling back through a channel.

#### What a normal member can do

Members can **submit** an application and nothing else. `/apply` (and the panel
button) opens the modal; `/manage applications review`, `view`, `accept` and
`decide` read or decide submissions and re-check staff permission whenever
they run. There is no command, button or dashboard page that shows a member
their own — or anyone else's — answers back, and the dashboard itself is behind
the staff token.

---

## 6. Embeds

**Staff channel embed** (on `/manage punish`):

* Title: "Member punished"
* Color: orange
* Fields: User (mention + id), Moderator, Duration, Reason, Started
  (Discord timestamp), Ends (Discord timestamp + relative)
* Thumbnail: the punished user's avatar
* Footer: "User ID: ..."

**Punished-user DM embed** (on `/manage punish`):

* Title: "You've been punished in `<server name>`"
* Color: red
* Fields: Duration, Reason, Started, Ends, Issued by
* Friendly message pointing the member to talk to a mod if they think it
  was a mistake

**Staff channel embed** (on timer expiry):

* Title: "Punishment timer expired"
* Color: blue
* Description showing the member moved from punish -> post role
* Started (relative time)

**Staff channel embed** (on `/manage pardon`):

* Title: "Member pardoned"
* Color: green
* Fields: User, Moderator, Outcome

**Pardoned-user DM embed**:

* Title: "Your punishment in `<server name>` has been lifted"
* Color: green
* Description confirming roles are restored
* "Issued by" field

**Staff channel embed** (on `/manage warn`):

* Title: "Member warned"
* Color: gold
* Fields: User (mention + id), Moderator, Total warnings, Reason, Time
* Thumbnail: the warned user's avatar
* Footer: "User ID: ..."

**Warned-user DM embed** (on `/manage warn`):

* Title: "You've been warned in `<server name>`"
* Color: gold
* Fields: Reason, Total warnings, Time, Issued by
* Friendly message about repeated warnings and talking to a moderator

**Staff channel embed** (on `/manage warnings clear:true` and the dashboard's
**Clear** button):

* Title: "Warnings cleared"
* Color: grey
* Fields: User, Moderator, Warnings removed

---

## 7. Building native installers

The bot can be packaged as a `.dmg` (macOS), `.exe` installer (Windows),
or `.deb` (Linux), for both **amd64/x86_64** and **arm64** machines. Each
platform *and* architecture must be built on its own host — PyInstaller does
not cross-compile — so every build script resolves the architecture from the
machine it runs on (`uname -m`; `PROCESSOR_ARCHITECTURE` in the Windows batch
file) and names its output after it:

| Platform | Built on | Output |
|----------|----------|--------|
| macOS    | Apple Silicon | `dist/Sentinel-1.0.0-arm64.dmg` |
| macOS    | Intel         | `dist/Sentinel-1.0.0-x86_64.dmg` |
| Linux    | x86-64        | `dist/sentinel_1.0.0_amd64.deb` |
| Linux    | ARM64         | `dist/sentinel_1.0.0_arm64.deb` |
| Windows  | x64           | `dist\Sentinel-Setup-1.0.0.exe` |
| Windows  | Windows on ARM | `dist\Sentinel-Setup-1.0.0-arm64.exe` |

Setting `SENTINEL_TARGET_ARCH=amd64` or `SENTINEL_TARGET_ARCH=arm64` overrides
the detected architecture — that is what CI does on each runner. The value
must match the machine: the spec refuses to build a target it cannot produce
instead of emitting a mislabelled bundle. Every script then checks its own
output too, by reading the ELF / Mach-O / PE header of the binary it just built
(`scripts/check_arch.py`, plus the `.deb`'s `Architecture` field and the NSIS
`/DARCH` metadata) and aborting if it is not the architecture that was asked
for. A build that silently produced the wrong CPU is not a theoretical worry —
an x64 Python on Windows on ARM, or an Intel Python in an arm64 CI job, both
happily "succeed" until someone on an arm64 machine runs the installer.

All three build scripts flow through `build/pyinstaller.spec`, which bundles
`bot.py` and its imported modules, plus `installer.py` and `dashboard.html`,
into a self-contained build before wrapping it in the OS-native installer
format.

CI builds all six combinations in parallel
(`.github/workflows/build-installers.yml`, one matrix per platform): amd64 and
arm64 Linux runners, an Apple Silicon and an Intel macOS runner, and x64 and
ARM64 Windows runners. GitHub's arm64 runners are free for public repositories;
`macos-15-intel` is the free Intel image and is scheduled to be retired in
August 2027.

### Cutting a release

The release workflow is fully automated. The version lives in one place -
the `VERSION` file at the project root - which every build script reads
(`build_linux.sh`, `build_macos.sh`, `build_windows.bat`,
`build/pyinstaller.spec`), so the `.deb`/`.dmg`/`.exe` filenames, the `.app`
plist, the NSIS metadata and `sentinel --version` can never disagree
with the release they belong to.

Bump it, then push a semver tag from the `main` branch:

```bash
echo 3.0.0 > VERSION
git commit -am "chore: bump version to 3.0.0"
git push origin main

./scripts/make_release.sh 3.0.0     # or: ./scripts/make_release.sh 3.1.0-rc1
```

The script checks that the tag matches `VERSION`, validates the working
tree, creates an annotated `v3.0.0` tag, and pushes it. Pushing the tag triggers `.github/workflows/release.yml`,
which builds all three platforms - both architectures each - in parallel and
attaches the six installers to a new GitHub Release.

Main doesn't have to wait for that, though: every merge to `main` builds the
same installers through `merge-release.yml` and publishes them as the rolling
`latest-build` prerelease plus a `v<VERSION>-build.<run>` prerelease for that
merge (see [Download](#download)). Those generated tags are skipped by
`release.yml`, so only a tag you push can produce a versioned release.

All three workflows call the same reusable build
(`.github/workflows/build-installers.yml`), so the `.deb`/`.dmg`/`.exe` steps
exist in exactly one file.

You can also just run the same commands by hand:

```bash
git tag -a v3.0.0 -m "Release 3.0.0"
git push origin v3.0.0
```

Either way, the release page appears at
`https://github.com/mob5824m-wq/Sentinel/releases/tag/v3.0.0`
a few minutes later with the `.dmg`, `.deb`, and `.exe` ready to
download.

### macOS

Requirements: Python 3.9+, `pyinstaller`, optionally `create-dmg`
(`brew install create-dmg`) for a styled `.dmg` window. Otherwise
`hdiutil` is used as a fallback. Build on the architecture you are shipping to:
an Apple Silicon Mac produces the `arm64` `.dmg`, an Intel Mac the `x86_64`
one.

```bash
build/build_macos.sh                            # this Mac's architecture
SENTINEL_TARGET_ARCH=arm64 build/build_macos.sh # only on an Apple Silicon Mac
open dist/Sentinel-1.0.0-arm64.dmg
```

The result is a real `.app` bundle (`Sentinel.app`) inside a
`.dmg` that users can drag into `/Applications`. The bundle id is
`com.arena.sentinel` and the binary is at
`Sentinel.app/Contents/MacOS/sentinel`.

To codesign, uncomment the `codesign` lines in `build_macos.sh` and
set `CODESIGN_IDENTITY` to your Developer ID.

### Linux (.deb)

Requirements: Python 3.9+, `pyinstaller`, `dpkg`, `fakeroot`,
`lintian` (optional). The `.deb` is built for the machine's own architecture
(`dpkg --print-architecture`), so run it on an amd64 host for `amd64` and on
an arm64 host for `arm64`.

How old a distribution the result runs on is decided by the *build* machine:
PyInstaller bundles that machine's CPython runtime, so the runner's glibc
becomes the package's floor. CI builds the `.deb` on Ubuntu 22.04 (glibc 2.35),
which covers Debian 12 / Raspberry Pi OS 64-bit "Bookworm" and newer;
`scripts/check_glibc.py` reads the requirement back out of the finished bundle
and fails the build if it exceeds 2.36, and the same number is written into the
package's `Depends: libc6 (>= …)` line. Building on a newer distribution
raises the floor (Ubuntu 24.04 → glibc 2.39) and quietly drops support for
older targets, which is why the runner is pinned and checked:

```bash
build/build_linux.sh             # names the .deb after this host's architecture
sudo dpkg -i dist/sentinel_1.0.0_amd64.deb    # on an amd64 host
sudo dpkg -i dist/sentinel_1.0.0_arm64.deb    # on an arm64 host
sudo systemctl start sentinel
```

The package installs the bot to `/opt/sentinel/`, symlinks
the binary into `/usr/bin/`, registers a desktop entry, and installs
a systemd unit (`/lib/systemd/system/sentinel.service`).
The unit is enabled (not started) by `postinst`; the user runs the
bot once to configure it, then enables the service.

### Windows

Requirements: Python 3.9+, `pyinstaller`, NSIS 3.x in PATH. On Windows on ARM
the build needs a **native ARM64 Python** — the script checks
(`python scripts\check_arch.py host`), and installs one from python.org via
`.github\scripts\install-windows-deps.ps1` if the interpreter on PATH is the
emulated x64 one, which would otherwise produce an x64 installer under an
`-arm64` name.

```
build\build_windows.bat
dist\Sentinel-Setup-1.0.0.exe            :: x64
dist\Sentinel-Setup-1.0.0-arm64.exe      :: Windows on ARM
```

The NSIS installer copies the PyInstaller output to
`%ProgramFiles64%\Sentinel`, creates Start Menu and Desktop
shortcuts, adds the install dir to `PATH`, and registers an
uninstaller in Add/Remove Programs.

To sign with `signtool`, uncomment the `signtool sign` line in
`build_windows.bat`.

### Background service

After install, the bot can be configured to start automatically. Paste the
block for your OS:

Linux (after `sudo dpkg -i ...`):

```bash
sudo sentinel --install-service
sudo systemctl start sentinel
sudo systemctl enable sentinel
```

macOS:

```bash
sudo sentinel --install-service
launchctl load -w ~/Library/LaunchAgents/com.arena.sentinel.plist
```

Windows (Command Prompt, run as Administrator):

```bat
sentinel.exe --install-service
sc start Sentinel
```

The `--install-service` command registers the bot with the OS service
manager (systemd / launchd / NSSM). The bot will then start on boot
and restart automatically if it crashes. `--uninstall-service`
removes the registration.

For development, you can also just run the binary directly with no
arguments — it will auto-run the installer on first launch.

## 8. Files

### Where config, database and logs live

Running from a source checkout keeps everything in the repo (`./data`,
`./config.json`). A **packaged install must not write next to the binary** -
`/opt/sentinel`, `C:\Program Files\Sentinel` and the
macOS `.app` are read-only (and world-readable, which would leak the token),
so `paths.py` picks a writable location at startup:

| Install          | Data (db + log)                                             | Config read from                                        |
|------------------|-------------------------------------------------------------|----------------------------------------------------------|
| source checkout  | `./data/`                                                    | `./config.json`                                          |
| Linux (`.deb`)   | `/var/lib/sentinel`, else `$XDG_STATE_HOME/sentinel`, else `~/.local/state/sentinel` | `~/.local/state/.../config.json`, then `/etc/sentinel/config.json` |
| macOS (`.dmg`)   | `~/Library/Application Support/Sentinel`           | there, else `/Library/Application Support/Sentinel` |
| Windows          | `%LOCALAPPDATA%\Sentinel`, else the install dir    | there, else `config.json` next to `sentinel.exe` |

The first writable candidate wins; if none is writable it falls back to a
temp dir and says so in the log. A config that exists but is read-only (the
`.deb` ships one in `/etc`, mode `0640 root:sentinel`) is read from
there, and the first save copies it to the writable data dir - which then
takes precedence. A candidate the current user can't even look into (for
example the `.deb`'s `0750` `/etc/sentinel` when a portable build is
run by a user outside the `sentinel` group) is skipped with a note
in the log, and the later candidates - such as `config.json` next to the
executable - are still tried (v2.1.1; v2.1.0 stopped at the first such
candidate).

Print the resolved locations any time:

```bash
sentinel --paths        # or, from source with the .venv activated: python bot.py --paths
sentinel --version      # which build is actually installed
```

Or pin them explicitly (useful for containers and custom service units):

```bash
SENTINEL_HOME=/srv/sentinel sentinel              # data + config base
SENTINEL_DATA=/srv/sentinel/data ...             # db + log dir only
SENTINEL_CONFIG=/etc/sentinel/config.json ...    # config.json path
```

Because the systemd service runs as the `sentinel` user, configure
it with `sudo` so the file lands where the service can read it:

```bash
sudo sentinel --install     # writes /var/lib or /etc, service-visible
sudo systemctl start sentinel
```

Running the installer as your own user only configures *your* user (the
installer tells you when that's the case).

```
Sentinel/
├── bot.py                  # bot entry point and command registration
├── command_tree.py         # the shared /manage group + admin check
├── settings.py             # per-guild config accessors (staff role, DM, ...)
├── store.py                # shared SQLite helpers (execute/fetch/insert)
├── tickets.py              # ticket panels, private threads/channels, claim/close
├── applications.py         # application forms, modals, review and decisions
├── rules.py                # rules publishing and the acceptance reaction role
├── reaction_roles.py       # dashboard-published reaction-role menus
├── dashboard.py            # authenticated server-side dashboard/API
├── dashboard.html          # dashboard UI
├── discord_markdown.py     # Markdown rendering/linting for the editors
├── duckdns.py              # optional DuckDNS dynamic-DNS updates
├── installer.py            # interactive first-run installer
├── paths.py                # where config/db/logs live at runtime
├── requirements.txt
├── config.json             # token + per-guild role config
├── .gitignore
├── scripts/                # dev launchers
│   ├── run_mac.sh
│   ├── run_linux.sh
│   └── run_windows.bat
├── build/                  # native installer build artifacts
│   ├── pyinstaller.spec
│   ├── build_macos.sh
│   ├── build_linux.sh
│   ├── build_windows.bat
│   ├── linux/
│   │   ├── sentinel.service
│   │   ├── sentinel.desktop
│   │   ├── postinst
│   │   ├── prerm
│   │   └── postrm
│   ├── macos/
│   │   ├── Info.plist
│   │   └── com.arena.sentinel.plist
│   └── windows/
│       └── installer.nsi
├── tests/
│   ├── test_applications.py     # application forms, permissions, review cards
│   ├── test_arch.py             # packaged-build architecture checks
│   ├── test_commands.py         # slash-command descriptions and guild sync
│   ├── test_dashboard.py        # dashboard login/session security + endpoints
│   ├── test_duckdns.py          # DuckDNS update handling
│   ├── test_glibc.py            # Linux build baseline checks
│   ├── test_markdown.py         # Discord Markdown renderer/linter
│   ├── test_paths.py            # packaged-install path resolution (read-only app dir)
│   ├── test_reaction_roles.py   # reaction-role menus (storage, emoji, give/remove)
│   ├── test_release_workflow.py # merge/tag release publishing (workflows + scripts)
│   ├── test_rules.py            # rules acceptance/reaction-role behavior
│   ├── test_tickets.py          # ticket panels, private threads, claim/close/reopen
│   ├── test_version.py          # VERSION plumbing (build scripts, tag, --version)
│   └── test_warnings.py         # warning escalation behavior
└── data/                    # created at runtime, source checkouts only
    ├── punishments.db
    └── bot.log
```

```bash
python3 -m pytest -q                 # run the full test suite
python3 tests/test_dashboard.py      # run dashboard auth tests alone
```

## 9. Troubleshooting

* **`PermissionError: [Errno 13] Permission denied:
  '/opt/sentinel/_internal/data'`** at startup, usually followed by
  `[PYI-...:ERROR] Failed to execute script 'bot'` — that build predates
  `paths.py` and tried to create its data directory inside the read-only
  install tree. Update to a build that ships `paths.py` (state then lives in
  `/var/lib/sentinel`, or your user's state dir). On the old build
  you can work around it:

  ```bash
  sudo mkdir -p /var/lib/sentinel
  sudo chown sentinel:sentinel /var/lib/sentinel
  sudo SENTINEL_DATA=/var/lib/sentinel sentinel
  ```

  `sentinel --paths` prints where the current build keeps its
  files.
* **"Installer exited without saving a config"** — re-run
  `python3 installer.py` and answer the prompts. If your terminal hides
  input (e.g. when piping from a file), the token will be read as empty
  and you'll be asked again.
* **`Missing Permissions`** when running `/manage punish` — the bot's
  role isn't above the configured roles. Move it up in
  *Server Settings → Roles*.
* **`/manage rules publish` can't post or react** — grant the bot **View Channel**,
  **Send Messages**, **Embed Links**, and **Add Reactions** in the selected
  channel. The acceptance role must be below the bot's role and must not have
  moderation or server-management permissions.
* **A rule set doesn't grant its role** — check `/manage rules list`: the set's
  message id changes when you republish or move it, so an old post stops
  reacting on purpose. Reactions are matched per message, and each set grants
  only its own role. If `/manage rules disable` says nothing was configured, the set
  was already removed from `config.json`.
* **A reaction-role post doesn't grant roles** — the same limits as the
  acceptance role apply: the role must sit below the bot's role, the bot needs
  **Manage Roles**, and the role must not be managed by an integration. The
  message must also still exist (a deleted message can't receive reactions
  any more), so press **Edit** → **Save changes** on that post and the
  dashboard reposts it under the same entry. `data/bot.log` names the failed
  check (`Cannot manage reaction role …`, `Configured reaction role … is
  missing`).
* **Dashboard won't open** — it binds to `127.0.0.1:8765` by default, so open
  it on the bot host or use the documented SSH tunnel. Check `data/bot.log`
  for a port or config error; remote reverse-proxy hosts must be in
  `dashboard_allowed_hosts`. `sentinel --dashboard` prints the effective
  settings and what to change for remote access.
* **Dashboard answers `400 Unrecognized Host header`** — the name you used is
  not in `dashboard_allowed_hosts`. That check is the DNS-rebinding defence,
  not a network failure: add the public name (or set `dashboard_public_url` to
  that URL, whose host is allowed implicitly). See
  [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md).
* **Login succeeds but bounces back to the login screen** — the browser
  refused to store the session cookie. `dashboard_secure_cookie` is `true` but
  the page is being served over plain HTTP. Terminate HTTPS in front of it (or
  serve TLS with `dashboard_tls_cert`/`dashboard_tls_key`), or set
  `dashboard_secure_cookie` to `false`.
* **DuckDNS name resolves but the dashboard is unreachable** — the record is
  current, but the dashboard is loopback-only: run a reverse proxy on the same
  machine and point it at `127.0.0.1:8765`, or set `dashboard_host` to
  `0.0.0.0` and forward the port. `sentinel --duckdns` reports the update
  result; `sentinel --dashboard` reports the rest.
* **Everyone gets `429 Too many attempts` on the login screen** — the
  dashboard sees one client (the proxy). Add the proxy's address or CIDR to
  `dashboard_trusted_proxies` so it may report visitors via
  `X-Forwarded-For`.
* **Dashboard login says "Invalid dashboard key"** — you are entering the
  wrong credential. The login key is printed by
  `sentinel --dashboard-token` (packaged build) or
  `python bot.py --dashboard-token` (source, `.venv` activated) and is stored as
  `dashboard_token` in the bot's `config.json`; the Discord **bot token**
  will not work. `--paths` shows where `config.json` is.
* **The member never receives the DM** — they have DMs disabled or the
  bot is blocked. Set `dm_user: false` in `/manage setup` to suppress the DM
  attempt, or ask the member to enable DMs.
* **Slash commands don't appear** — the bot syncs instantly to every
  connected server, so `server_id` is not required. Make sure the bot was
  invited with the `applications.commands` scope and check `data/bot.log`
  for a "Synced N command(s) to guild X" line for your server.
* **Slash commands appear twice / doubled** (`/manage` listed two or three
  times) — Discord is showing the same command from both of its
  command registries. This is what older versions of the bot caused by
  syncing globally *and* per server; it is fixed now. Startup deletes the
  duplicate global registrations automatically (look for a "Removed N
  duplicate global command(s)" line in `data/bot.log`), or an admin can run
  `/manage fixcommands` to do it on the spot. Discord can take a few minutes to
  refresh the command list — restarting Discord (Ctrl+R) shows it
  immediately. If the duplicates come back, another copy of the bot is still
  running with the same token; stop it and run `/manage fixcommands` again.
* **No token / Login failed** — make sure `DISCORD_TOKEN` is set or
  `config.json` has a non-empty `bot_token` (or the legacy `token`).
* **`.deb` build complains about `dpkg-deb` or `fakeroot`** — install
  them with `sudo apt install fakeroot dpkg`.
* **A build fails with "cannot build a arm64 bundle on a amd64 host"** — the
  architecture you asked for (`SENTINEL_TARGET_ARCH`) is not the machine's.
  PyInstaller cannot cross-compile, so build on a host of that architecture
  (or drop the variable and let the script use the host's).
* **`version 'GLIBC_2.35' not found` when starting `sentinel`** — the system is
  older than the distribution the `.deb` was built on. The arm64 and amd64
  packages are built on Ubuntu 22.04 and run on Debian 12 / Raspberry Pi OS
  64-bit "Bookworm" or newer; on something older (Debian 11, Raspberry Pi OS
  64-bit "Bullseye") run
  [from source](#2-setup-macos-linux-windows) instead, or rebuild the package
  on that distribution with `build/build_linux.sh`.
* **Raspberry Pi: `dpkg: package architecture (arm64) does not match system
  (armhf)`** — the pre-built package is 64-bit only. Either flash the 64-bit
  Raspberry Pi OS image (Pi 3/4/5, Zero 2 W) or run
  [from source](#2-setup-macos-linux-windows) on 32-bit Raspberry Pi OS; there
  is no 32-bit ARM installer, because PyInstaller ships no 32-bit ARM Linux
  bootloader for it to build with.
* **The installer won't run on an arm64 machine** ("bad CPU type", or an
  ARM64 Windows error) — you have the other architecture's file. The names say
  which is which: `...-arm64.dmg`, `..._arm64.deb` and
  `Sentinel-Setup-...-arm64.exe` are for Apple Silicon, ARM64 Linux and
  Windows on ARM; `...-x86_64.dmg`, `..._amd64.deb` and
  `Sentinel-Setup-....exe` are for Intel/AMD machines.
* **NSIS errors with `MUI2.nsh` not found** — install NSIS 3.x
  (https://nsis.sourceforge.io) and ensure `${NSISDIR}` is set.
* **The `.dmg` says "this app is from an unidentified developer"** —
  that means the `.app` isn't codesigned. Either sign it with a
  Developer ID or right-click the `.app` and choose "Open" the
  first time to bypass Gatekeeper.

## 10. License

MIT.
# 10. License

MIT.
