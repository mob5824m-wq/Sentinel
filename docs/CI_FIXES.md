# CI Build Status

## State

| Job | Status | Output |
|-----|--------|--------|
| macOS `.dmg` | passing | `dist/Sentinel-X.Y.Z-arm64.dmg`, `dist/Sentinel-X.Y.Z-x86_64.dmg` |
| Linux `.deb` | passing | `dist/sentinel_X.Y.Z_amd64.deb`, `dist/sentinel_X.Y.Z_arm64.deb` |
| Windows `.exe` | passing | `dist/Sentinel-Setup-X.Y.Z.exe`, `dist/Sentinel-Setup-X.Y.Z-arm64.exe` |
| All artifacts | produced | Six installers (three platforms × two architectures) as workflow artifacts on PRs (3-day retention); published as releases on merges to `main` (see fix 16) |

The `build.yml` CI build is fully working on all three platforms and both
architectures (see fix 17), and so is `release.yml`: the matrix bug described under ~~"The open bug"~~ below was
applied to `main` (three explicit per-platform jobs), so pushing a `v*` tag
builds the installers and attaches them to the release by itself - as it did
for v2.0.0, and again for **v2.1.0** (the crash fix below).

## How a release is cut

`VERSION` at the project root is the single source of truth for the version;
every build script reads it (see "Fix 13"), so the artifact names, the `.app`
plist and the NSIS metadata always match the tag.

```bash
# 1. Bump the version and land it on main.
echo 2.1.1 > VERSION
git commit -am "chore: bump version to 2.1.1" && git push origin main

# 2. Tag it - this runs release.yml end to end.
./scripts/make_release.sh 2.1.1     # refuses to tag if VERSION disagrees
```

Versioned releases are for stability, not for shipping: `merge-release.yml`
already built and published the commit you merged (rolling `latest-build` plus
`v<VERSION>-build.<run>`), so a fix is downloadable before the bump. See fix 16.

To fall back to a manual release (only needed if release.yml breaks again):

```bash
# 1. Tag the commit you want to release.
git tag -a v1.0.0 -m "Release 1.0.0"
git push origin v1.0.0

# 2. Create the GitHub Release with release notes.
gh release create v1.0.0 \
    --title "Sentinel 1.0.0" \
    --notes-file /path/to/release-notes.md \
    --target <commit-sha>

# 3. Drag-and-drop the build artifacts onto the release page
#    in the web UI. The artifacts are on the Actions tab of
#    the matching commit, named:
#      sentinel-linux-amd64 / -arm64   (the .deb)
#      sentinel-macos-arm64 / -x86_64  (the .dmg)
#      sentinel-windows-x64 / -arm64   (the .exe)
#    The web-UI upload goes through github.com (reachable from
#    anywhere), so this works in environments where
#    uploads.github.com is firewalled.
```

Once `release.yml` is fixed, releases can go back to the automated
flow:

```bash
./scripts/make_release.sh 1.0.0
# creates an annotated v1.0.0 tag, pushes it, and the
# release.yml workflow builds the installers and attaches them
# to a GitHub Release.
```

A plain version like `1.0.0` becomes a normal release. A
`v1.0.0-rc1` tag (anything with a hyphen) is marked prerelease.

## ~~The open bug: `release.yml` is broken~~ (resolved)

**Resolved.** The corrected file is on `main` (three explicit jobs, no
`matrix.shell`), and v1.0.1 was released through it. The original failure,
kept for context:

`release.yml` triggers on every `v*` tag push, but it never
actually ran. GitHub rejected the workflow file at parse time:

> Invalid workflow file: `.github/workflows/release.yml#L1`
> (Line: 83, Col: 16): Unrecognized named-value: 'matrix'.

The offending line is:

```yaml
- name: Build
  run: ${{ matrix.build_script }}
  shell: ${{ matrix.shell }}   # <-- matrix is not bound here
```

`shell:` on a step is evaluated before the matrix strategy is
resolved, so `${{ matrix.shell }}` is not a valid value. The
correct pattern (already used in `build.yml`) is to replace the
matrix with three explicit per-platform jobs, one per runner.

The agent's GitHub App token could not push files into
`.github/workflows/` (no `workflows` permission), so a maintainer applied the
corrected file; releases are automated again. If the permission is still
missing on the App, `.github/workflows/*` edits must go in through the web
editor or a maintainer's token.

## History of fixes

The CI build was debugged through many rounds. The fixes are
documented below in case you need to understand why the scripts
do what they do.

### 1. Setup Python dev files

**Problem:** PyInstaller's bootloader needs `python3.lib` next
to `python3.dll`, but the slim Python that `actions/setup-python`
installs lacks it.

**Fix:** `.github/scripts/install-windows-deps.ps1` downloads
the official Python MSI from python.org with
`Include_dev=1 Include_lib=1` when the active Python lacks
the dev files. The helper was rewritten during the debug round
to look for `python3.lib` at
`os.path.join(sys.executable_dir, "libs", "python3.lib")` (one
`libs` segment, not two — the earlier two-segment path was a
real bug that left the dev files uninstalled).

### 2. NSIS path discovery

**Problem:** The "Install NSIS" step sets `$env:PATH` inside
its own PowerShell session, but that change doesn't propagate
to the next step's cmd.exe session — cmd.exe can't see
`makensis` even after a successful install.

**Fix:** `install-nsis.ps1` exports the install path via
`$GITHUB_ENV` as `MAKENSIS_PATH`. `build_windows.bat` reads
`%MAKENSIS_PATH%` and uses that as the primary source of
truth, with `C:\nsis-3.10\`, `C:\nsis-3.09\`, `C:\nsis-3.08\`
as fallbacks.

### 3. cmd.exe parens-block parser bug

**Problem:** `if exist "C:\Program Files (x86)\NSIS\..." (`
inside a parenthesized block causes cmd.exe's `if` parser to
misread the path. The `(` inside `(x86)` gets confused with
the `(` of the `if`'s code block, and the whole line dies with
`%\Program Files (x86)\NSIS\... was unexpected at this time.`

**Fix:** Replaced all `if exist ... ( ... )` patterns in
`build_windows.bat` with `if exist ... goto :label` and
`set` + `goto` chains. No parenthesized blocks anywhere in
the build script. This is fragile to write but reliable to
run.

### 4. NSIS 2 commands

**Problem:** The original `installer.nsi` used `SetBrandText`,
which was removed in NSIS 3.

**Fix:** Removed `SetBrandText`. Also removed the `MUI_ICON`
and `MUI_UNICON` references (the icon files don't exist),
`MUI_HEADERIMAGE` and the bitmap paths (Chocolatey's NSIS
doesn't ship `Contrib\Graphics\`). The installer still looks
clean without the cosmetic extras.

### 5. EnVar plugin not bundled

**Problem:** `EnVar::SetHKLM` and `EnVar::AddValue` calls
require the EnVar plugin, which isn't bundled with the
standard NSIS install. The plugin error is:

> Plugin not found, cannot call EnVar::SetHKLM
> Plugin directories: C:\Program Files (x86)\NSIS\Plugins\x86-unicode

**Fix:** Removed the PATH-manipulation lines from both the
Install and Uninstall sections. Users can add the install
dir to their PATH manually if they want command-line access;
the trade-off is worth not depending on a non-bundled plugin.

### 6. NSIS file path resolution

**Problem:** The original `File /r "..\dist\sentinel\*"`
in `installer.nsi` resolved relative to the script's directory
(`build/windows/`), so it was looking for `build/dist/...`
which doesn't exist.

**Fix:** Changed to `File /r "..\..\dist\sentinel\*"`.
The two `..` are required because NSIS resolves the path
relative to the directory containing the script.

### 7. NSIS OUTFILE path

**Problem:** `makensis /OUTFILE="dist\..."` was a relative
path, and NSIS wrote the installer to
`<project_root>\build\windows\dist\...` instead of
`<project_root>\dist\...`.

**Fix:** Pass the absolute project root via the `%CD%` env
var: `makensis /DOUTFILE="%CD%\dist\..."`.

### 8. Chocolatey NSIS version

**Problem:** The original `install-nsis.ps1` did
`choco install nsis --version 3.10`, but the public
`chocolatey.org/packages/nsis` only publishes a three-part
version (`3.10.0`). The shorter version silently no-ops —
the install never happens, the script falls back to the
portable download, and the whole step is flaky as a result.

**Fix:** Pass the full version to Chocolatey:
`choco install nsis --version=3.10.0`. The portable downloads
still use the two-segment `3.10` since the SourceForge zip is
named `nsis-3.10.zip`.

### 9. `install-nsis.ps1` swallowed choco errors

**Problem:** `choco install ... 2>&1 | Out-Null` hid any
Chocolatey error so the script couldn't tell why the install
failed, and `Test-MakensisExists` always returned `$null`,
forcing the script to fall back to the portable download
every time. Also, piping to `Out-Host` in a non-interactive
PowerShell session is unreliable.

**Fix:** Capture choco's output into a variable with the `&`
call operator and log each line through `Write-Host` explicitly.
Now the step log shows exactly what Chocolatey said.

### 10. SourceForge HTML error pages

**Problem:** SourceForge occasionally serves an HTML error
page on a `200 OK` response (a few hundred bytes, not the
expected ~2.4 MB zip). The old `Invoke-WebRequest` would
succeed and `Expand-Archive` would then fail with a confusing
"end of central directory record not found" error.

**Fix:** After downloading, check that the file is at least
100 KB before extracting; retry on size-check failure.

### 11. `release.yml` matrix.shell

**Problem:** As described in "The open bug" above, the
`shell: ${{ matrix.shell }}` on the `Build` step is not valid
GitHub Actions syntax. The matrix context isn't bound at the
point `shell:` is evaluated, so GitHub rejects the workflow
file at parse time. The workflow never runs.

**Fix:** Replace the matrix with three explicit per-platform
jobs, like `build.yml` already does. The agent has the
corrected `release.yml` in the PR branch but cannot push it
(no `workflows` permission on the GitHub App token). A
maintainer needs to apply the change.

### 12. Packaged builds crashed on first launch (read-only app dir)

**Problem:** the installed `.deb` binary died at import time with

```
File "bot.py", line 35, in <module>
File "pathlib.py", line 1116, in mkdir
PermissionError: [Errno 13] Permission denied: '/opt/sentinel/_internal/data'
[PYI-49989:ERROR] Failed to execute script 'bot' due to unhandled exception!
```

`bot.py` derived every path from `Path(__file__).parent`. In a PyInstaller
onedir build that is `_internal/` inside the install tree — root-owned and
read-only (and explicitly read-only for the service, since the unit sets
`ProtectSystem=strict`). So `DATA_DIR.mkdir()` raised before the logger even
existed. The same applied to `config.json` (written into the install dir,
world-readable, with the bot token in it) and to the Windows `.exe` under
`C:\Program Files`. CI did not catch it because the installers are only
*built*, never run.

**Fix:** new `paths.py` resolves the data dir and config file once, at import:
env overrides, then the platform state dir (`/var/lib/sentinel`,
`~/.local/state/sentinel`, `~/Library/Application Support/...`,
`%LOCALAPPDATA%\Sentinel`), then `<app dir>/data`, then a temp dir —
each candidate only used if it is actually creatable and writable. A config
found in a read-only place is copied on write to the writable location. The
systemd unit, launchd agent and NSSM service are now generated against the
running executable (with the packaged resources looked up via
`paths.resource_path`), and `--install-service` chowns the state dirs to the
service user. `bot.py --paths` prints the resolved locations.

**Regression gate:** `tests/test_paths.py` re-creates the packaged layout in a
temp dir (read-only app tree, `sys.frozen`/`_MEIPASS`/`sys.executable`
patched, isolated `HOME`) and asserts `import bot` succeeds and nothing is
written into the install tree. It runs in `build.yml` (all three OSes) and in
`release.yml`'s linux job, so a path regression fails the build instead of the
customer's first launch. Read-only-permission cases skip when running as root
or on Windows, where `chmod` can't emulate them.

### 13. Version drift between the tag and the installers

**Problem:** the version was hardcoded in seven places - `build_linux.sh`,
`build_macos.sh`, `build_windows.bat`, the `pyinstaller.spec` plist, the
macOS `Info.plist`, `installer.nsi` and the generated `.deb` control file -
and nothing tied them to the git tag. Cutting `v1.0.1` would have shipped
`sentinel_1.0.0_amd64.deb` attached to a release named v1.0.1, with
a `.app` reporting a third version, and no way to ask a user which build they
had installed.

**Fix:** a `VERSION` file at the project root is the single source of truth.
`scripts/pm_version.sh` resolves it (`$PM_VERSION` override > `VERSION` file >
matching git tag > `0.0.0+unknown`) for the shell scripts, the `.bat` reads the
file directly, and the spec reads it for the plist *and* bundles a copy so
`sentinel --version` / the startup log line report the *installed*
build rather than a nearby checkout. `make_release.sh` refuses to tag when the
tag and the file disagree. `tests/test_version.py` fails if any of those
scripts hardcodes a version again.

### 14. An inaccessible path candidate ended the search (v2.1.1)

**Problem:** before Python 3.13 (the builds use 3.11), `Path.exists()` /
`is_file()` only swallow `ENOENT`/`ENOTDIR`/`EBADF`/`ELOOP` and raise
everything else - notably `EACCES` for a path under a directory the user may
not traverse. The `.deb`'s `postinst` makes `/etc/sentinel`
`0750 root:sentinel`, so for anyone outside that group (typically a
portable/unzipped build run next to a `.deb` install) the
`/etc/sentinel/config.json` candidate raised `PermissionError` out of
`_pick_config_path()`'s loop. v2.1.0 was still safe to run - the import-time
guard caught it - but every candidate *after* the inaccessible one was dropped
(the portable install's own `<app dir>/config.json` was never tried and an
empty default was created in the data dir instead), and the note was
mislabelled `WARNING: config resolution failed (...)`. The data-dir loop had
the same bug: an inaccessible `$XDG_STATE_HOME` candidate demoted the bot to
the temp-dir fallback with "data directory resolution failed".

**Fix:** `paths._inspect()` classifies a path without raising
(`dir`/`file`/`other`/`missing`/`inaccessible` plus the OS reason) and backs
every probe in the resolution code. Both loops skip an inaccessible candidate
with an accurate per-candidate note (`... cannot be accessed (Permission
denied); using another location` / `Config candidate ... cannot be accessed
(...); skipped`) and carry on; the import-time `except` clauses are a true
last resort again.

**Regression gate:** `tests/test_paths.py::InaccessibleCandidateTests`
re-creates a portable install next to a locked system config dir and a locked
per-user state dir (skipped as root / on Windows like the other permission-bit
fixtures).

### 15. No slash commands were registered: a description over Discord's limit

**Problem:** `/manage status` had a 101-character description. Discord allows
1-100 characters for every command and option description and validates the
whole list at once, so the startup `tree.sync()` failed with HTTP 400 / error
50035 (`In command 'punish status' ... description: Must be between 1 and 100
in length`) and **none** of the commands were registered or updated. The bot
logged `Failed to sync global commands.` and carried on - it has to, the
scheduler that releases punished users runs in the same process - so it looked
healthy. discord.py shortens descriptions it derives itself (docstrings,
`@app_commands.describe(...)`), but a `description=` passed explicitly to
`command()` / `Group()` is sent as-is, and nothing exercised the command tree
before a release.

**Fix:** the description is 94 characters now ("config" instead of
"configuration").

**Regression gate:** `tests/test_commands.py` builds the real command tree in a
sandboxed subprocess - the exact payload `tree.sync()` would upload, no network
or token needed - and fails, naming the command, if any command, group,
subcommand or option description is outside 1-100 characters. It also feeds the
checker the string that broke sync, so the gate can't quietly become a no-op.

### 16. Merges to main produced no downloadable build

**Problem:** the installers only left CI when a maintainer bumped `VERSION` and
pushed a tag, so a merged fix was not downloadable until someone cut a release
— the merge itself built nothing a user could take. And the fix could not just
be "also run the build on main": the platform steps were already duplicated
between `build.yml` and `release.yml`, and a third copy for the merge build
would have meant three places to update (three places for the version-drift bug
of fix 13 to live in).

**Fix:** the platform steps now live in one reusable workflow,
`.github/workflows/build-installers.yml`, called by every workflow that needs
installers (`workflow_call`). On top of it:

- `merge-release.yml` runs on every push to `main` and publishes two
  prereleases: the rolling `latest-build`, whose tag is force-moved to the
  merge commit and whose assets are replaced (stale assets from an earlier
  version are deleted), and `v<VERSION>-build.<run_number>`, one per merge so
  an older build stays downloadable. Both are prereleases, so
  `/releases/latest` still points at the newest *versioned* release.
- `release.yml` keeps handling `v*` tags and skips the generated `-build.` tags
  (`if:` guards on both jobs) so a build tag can never publish a second, empty
  release. Tag pushes made with the workflow's own `GITHUB_TOKEN` do not
  trigger workflows, which the guards make explicit rather than relying on.
- `build.yml` still gates every pull request and `arena/*` push, but no longer
  duplicates the main build.
- Merges queue (`cancel-in-progress: false`) so a burst of merges publishes
  every build instead of cancelling all but the last.

The release plumbing lives in `.github/scripts/publish-merge-release.sh` and
`.github/scripts/publish-tag-release.sh` rather than in inline `run:` blocks,
because scripts can be executed offline.

**Regression gate:** `tests/test_release_workflow.py` checks the workflow
wiring (every workflow that builds calls the reusable one; the platform steps
exist in exactly one file; merge-release triggers on `main` and is a
prerelease-only publisher; `release.yml` still triggers on `v*` and skips
`-build.` tags) and then runs both publish scripts against stubbed `gh`/`git`
binaries, asserting the rolling tag move, the per-merge tag name
(`v<VERSION>-build.<run>`), the asset clobber/upload calls, the stale-asset
sweep, the tag-push fallback and the failures for missing installers or a
missing tag.

### 17. The installers were x86_64-only (v3.0.1+)

**Problem:** every installer was built on the x64 (or, for macOS, the *only*
available) runner, so
`sentinel_X.Y.Z_amd64.deb` and `Sentinel-Setup-X.Y.Z.exe` could not run on an
arm64 Linux box, and on Windows on ARM only under emulation. macOS was the
mirror image: `macos-latest` is Apple Silicon, so the `.dmg` was arm64-only
and Intel Macs had nothing. The build scripts also hard-coded the
architecture (`ARCH="amd64"` in `build_linux.sh`), so no amount of runner
juggling could have produced a second architecture.

**Fix:** build each platform for both architectures, natively:

- `build-installers.yml` keeps one job per platform and runs each as a
  two-entry matrix, so the platform steps still exist in exactly one place:
  `ubuntu-24.04` + `ubuntu-24.04-arm`, `macos-latest` + `macos-15-intel`,
  `windows-latest` + `windows-11-arm`. (GitHub's arm64 runners are free for
  public repositories; `macos-15-intel` is the free Intel image.) Each leg
  sets `SENTINEL_TARGET_ARCH` and `fail-fast: false`, so one architecture's
  failure does not hide the other's.
- `scripts/arch.sh` resolves the architecture (host `uname -m`, or
  `SENTINEL_TARGET_ARCH`) and normalizes `amd64|x86_64|x64` and
  `arm64|aarch64|armv8*`; `scripts/check_arch.py` reads a binary's ELF /
  Mach-O / PE header.
- Artifact and release filenames now carry the architecture:
  `sentinel_<VERSION>_amd64.deb` / `_arm64.deb`,
  `Sentinel-<VERSION>-arm64.dmg` / `-x86_64.dmg`,
  `Sentinel-Setup-<VERSION>.exe` / `-arm64.exe`.
- The Windows ARM64 leg asks `actions/setup-python` for the arm64
  interpreter and, if the tool cache has none, installs the native ARM64
  Python from python.org (`install-windows-deps.ps1`, now architecture-aware).
  NSIS itself runs under emulation; only the payload has to be native.

**Regression gate (the subtle part):** an x64 Python on Windows on ARM (or an
Intel Python in an arm64 CI job) builds "successfully" - and produces a binary
for the *wrong* CPU under an arm64 name, which then crashes on the user's
machine. So every build now verifies its own output instead of trusting the
label: the spec refuses a target that is not the host (PyInstaller cannot
cross-compile), each script checks the ELF/Mach-O/PE header of the binary it
built, `build_linux.sh` re-reads the `.deb`'s `Architecture` field, CI checks
the interpreter's architecture before building, and the Windows batch script
re-runs the Python installer when the interpreter on PATH is the emulated one.
`tests/test_arch.py` covers the resolver, the header reader and that wiring.

### 18. The arm64 .deb would not start on Raspberry Pi OS

**Problem:** fix 17 made the artifact *architecturally* right - an aarch64
`.deb`, built on `ubuntu-24.04-arm` - but an architecture match is not enough
to run. PyInstaller bundles the build machine's CPython runtime, so the
runner's glibc becomes the artifact's floor: built on Ubuntu 24.04 (glibc
2.39) the arm64 `.deb` dies on Raspberry Pi OS 64-bit "Bookworm" (Debian 12,
glibc 2.36) with

```
sentinel: /lib/aarch64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found
```

and no amount of `dpkg -i` changes that. The `.deb` also claimed
`Depends: libc6 (>= 2.31)`, which let it install on distributions that could
never run it.

**Fix:** move both Linux legs to **Ubuntu 22.04** (`ubuntu-22.04` /
`ubuntu-22.04-arm`) and Python 3.10, the newest jammy has a `libpython` for
(`libpython3.11` does not exist in jammy). Jammy's glibc is 2.35, so the
artifacts run on Debian 12 / Raspberry Pi OS 64-bit Bookworm and Trixie, and
on Ubuntu 22.04+, while the app still supports Python 3.9+.

On top of that the floor is now *measured and enforced* rather than assumed:

- `scripts/check_glibc.py` reads the version requirement out of every ELF
  file's `.gnu.version_r` table (ELF32/ELF64, either byte order) and reports
  the bundle's real floor.
- The Linux legs run it with `--max 2.36` (Debian 12 = Raspberry Pi OS 64-bit
  Bookworm) and publish the measured number as a check annotation, so a runner
  or Python bump raises a red build instead of a user's `GLIBC_x.y not found`.
- `build_linux.sh` writes the same number into the package's
  `Depends: libc6 (>= …)` line, so `apt` refuses an install on a distribution
  too old to run it rather than leaving a broken binary behind.
- The finished bundle is executed inside `debian:bookworm-slim` and
  `debian:trixie-slim` - the Raspberry Pi OS 64-bit bases - when Docker is
  available on the runner.

**Still not covered:** 32-bit Raspberry Pi OS (`armhf`). PyInstaller's wheels
carry no 32-bit ARM Linux bootloader (`Linux-64bit-intel` and
`Linux-64bit-arm` are what ship, for amd64 and aarch64), so an armhf package
would be a separate build pipeline, not a matrix entry; 32-bit Pi users run
from source. Raspberry Pi OS 64-bit "Bullseye" (glibc 2.31) is also below the
2.35 floor - Bookworm or newer is what the `.deb` supports.

**Regression gate:** `tests/test_glibc.py` covers the reader (synthetic ELF32,
ELF64, big-endian and version-less fixtures), the CLI exit codes and the
workflow wiring - including that the Linux legs stay off `ubuntu-24.04` and
that the `.deb` derives its `Depends` from the measured floor.

### 19. A red test leg with no visible reason

Both Windows legs failed at "Run the test suite" while every other leg was
green, and the failure text lives in a run log served from an authenticated
blob store - which is not always reachable (a restricted network, a sandbox
without egress, `gh` in CI tooling), leaving a red leg with nothing to act on.
`gh run view --log-failed` and the job-logs API both failed with a connection
error instead of a verdict.

- Each test step now writes `--junitxml=test-results.xml`, and a following
  `if: failure()` step turns the report into `::error file=…,line=…` check-run
  annotations with `.github/scripts/annotate-test-failures.py`. Annotations are
  plain REST data (`/check-runs/<id>/annotations`), so the diagnosis is one API
  call away, and the same numbers appear on the PR's Checks tab.
- The annotator caps itself at GitHub's per-step annotation limit and lists the
  overflow in a warning; it only reports, so it can never turn a red suite
  green. A missing report (a failure before pytest ran) is not an error.
- What the annotations showed: two DuckDNS updater tests that slept for a fixed
  0.3 s and then asserted a 50 ms timer had fired twice and been recorded. On a
  loaded two-core Windows runner that is not guaranteed, and a request still in
  flight when `stop()` cancels the task is counted by the server but produces
  no result - the "5 != 6" in the report. They now poll for the condition with a
  deadline (15 s) and check that `stop()` prevents *new* updates instead of
  requiring none to have been in flight.
- Not a code fault, but worth knowing when reading a red run: `The job was not
  acquired by Runner of type hosted even after multiple attempts` means GitHub
  had no free hosted runner for that leg. The job shows up as *cancelled* with
  no steps and no failing test; re-running the workflow (a fresh push, or
  **Re-run failed jobs** from the UI) is the fix.

## Test gate in CI

Every leg of `build.yml` and of the reusable `build-installers.yml` runs
`python -m pytest tests`, which covers runtime path resolution (fixes 12 and
14), the version plumbing (fix 13), the slash-command definitions (fix 15),
the merge/tag release automation (fix 16), the per-architecture build plumbing
(fix 17), the glibc floor (fix 18) and the test-failure annotations (fix 19).
The suites are plain `unittest`, so they also run standalone:
`python tests/test_paths.py`.

## Other notes

- The build uses no code signing. If you want signed
  installers, set up `signtool` in the build and add a signing
  step at the end.
- The `softprops/action-gh-release@v2` action requires
  `contents: write` on the GITHUB_TOKEN, which `release.yml`
  declares. Once the matrix bug is fixed, this should "just
  work" — all three matrix jobs independently call the action
  to upload their own artifact.
- Build artifacts on the Actions tab are retained for 3 days
  (the `retention-days: 3` in `build.yml`). The previous
  default of 7 days was reduced to keep the artifact budget
  under GitHub's free-tier limit.
