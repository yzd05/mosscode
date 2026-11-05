# Claude Code sandbox (`/sandbox`, sandboxed Bash) and `@anthropic-ai/sandbox-runtime` (srt)

Snapshot taken 2026-11-05. Sources were read first-hand on that date:
- Claude Code docs "Configure the sandboxed Bash tool" (https://code.claude.com/docs/en/sandboxing). Version notes inside the page go up to v2.1.310.
- Claude Code settings reference (https://code.claude.com/docs/en/settings-reference).
- Claude Code env-vars doc (https://code.claude.com/docs/en/env-vars).
- "Sandbox environments" doc (https://code.claude.com/docs/en/sandbox-environments).
- Claude Code CHANGELOG, latest entry 2.1.318 (https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md).
- Anthropic engineering blog post "Beyond permission prompts", 2025-10-20 (https://www.anthropic.com/engineering/claude-code-sandboxing).
- The sandbox-runtime repo at main (https://github.com/anthropics/sandbox-runtime). It was last pushed 2026-10-30. The latest npm release is 0.0.81, published 2026-10-20 (https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime).

Throughout, "CC" means Claude Code and "srt" means sandbox-runtime.

---

## 1. Isolation mechanism per platform (macOS Seatbelt, Linux bubblewrap, WSL2, native Windows)

### Takeaway
CC's built-in sandbox uses Seatbelt (`sandbox-exec` with a profile generated per command) on macOS, and bubblewrap + seccomp + a network namespace on Linux and WSL2.

On native Windows, CC officially does not sandbox: "On native Windows, Claude Code runs commands unsandboxed." The standalone srt library has, however, shipped an **alpha native-Windows backend** (`srt-win.exe`, written in Rust). It runs commands as a dedicated local user account, uses a WFP egress filter keyed on that account's SID, a write-restricted token, NTFS ACEs and a job object. This is the most directly relevant prior art for MossCode on Windows 11.

### Cited Findings

**General and CC-level**
- CC's statement: "The sandbox runs on macOS, Linux, and WSL2. On native Windows, Claude Code runs commands unsandboxed. To use the sandbox on a Windows machine, run Claude Code inside a WSL2 distribution." — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- The sandbox limits apply to the commands of the Bash, PowerShell and Monitor tools "and the processes they start". — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- The sandbox is off by default. Turn it on with `/sandbox` or with `sandbox.enabled: true`. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- The "Sandbox environments" page: "This option does not support native Windows. On Windows hosts, use WSL2 or one of the container or VM approaches below." — [Sandbox environments](https://code.claude.com/docs/en/sandbox-environments)
- Managed-settings guidance for Windows: "The sandbox doesn't run on native Windows, so with `failIfUnavailable` set, Claude Code exits at startup on those machines." Admins are told to deliver the config only to macOS and Linux machines, or to move Windows users into WSL2 or a container. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- When the sandbox can't start (unsupported platform or a failed dependency check), CC falls back to running unsandboxed by default. `sandbox.failIfUnavailable: true` makes it exit instead. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- History:
  - The sandbox first shipped in CC 2.0.24: "Sandbox: Releasing a sandbox mode for the BashTool on Linux & Mac". — [CC CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
  - The blog post announcing it (2025-10-20) says it is built "on top of OS level primitives such as Linux bubblewrap and MacOS seatbelt". It covers "any scripts, programs, or subprocesses that are spawned by the command". — [Anthropic engineering blog](https://www.anthropic.com/engineering/claude-code-sandboxing)

**macOS (Seatbelt)**
- srt generates a Seatbelt profile and runs the command under `sandbox-exec`. — [srt README](https://github.com/anthropics/sandbox-runtime)
- Profile skeleton, from `generateSandboxProfile` in [macos-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/macos-sandbox-utils.ts):
  - It starts with `(version 1)` and `(deny default (with message "<logTag>"))`. The comment says it is "based on Chrome sandbox policy".
  - Process rules: `(allow process-exec)`, `(allow process-fork)`, `(allow process-info* (target same-sandbox))`, `(allow signal (target same-sandbox))`.
  - An explicit list of Mach services, with no wildcard: logd, fonts, opendirectoryd, securityd.xpc, launchservicesd, and others.
  - `ipc-posix-shm` and `ipc-posix-sem` are allowed (the semaphores are for Python multiprocessing).
  - A whitelist of `sysctl-read` names.
  - `AF_SYSTEM` sockets only.
- Reads are allowed by default (`(allow file-read*)`). `denyRead` entries become `(deny file-read* …)`, and `allowRead` re-allows paths inside them. — [macos-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/macos-sandbox-utils.ts)
- Network rules on macOS when the network is restricted. — [macos-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/macos-sandbox-utils.ts)
  - Only `network-bind/inbound/outbound` to `localhost:<httpProxyPort>` and to the SOCKS port are allowed.
  - Unix sockets are blocked unless `allowUnixSockets` (a list of paths) or `allowAllUnixSockets` is set.
  - `allowLocalBinding` adds `(allow network-bind (local ip "*:*"))` and `(allow network-outbound (remote ip "localhost:*"))`.
- Weakening switches on macOS:
  - `enableWeakerNetworkIsolation` allows `com.apple.trustd.agent`. Go TLS verification needs this.
  - `allowAppleEvents` adds `(allow appleevent-send)`, `(allow lsopen)` and the coreservices Mach lookups.
  - `allowPty` adds `(allow pseudo-tty)`.
  - `allowMachLookup` admits services you name.
  - Sources: [macos-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/macos-sandbox-utils.ts); [srt README](https://github.com/anthropics/sandbox-runtime).
- On macOS, a deny path that passes through a symlink is also denied at its real location. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- macOS path rules accept git-style globs (`*`, `**`, `?`, `[abc]`). — [srt README](https://github.com/anthropics/sandbox-runtime)

**Linux and WSL2 (bubblewrap)**
- How the `bwrap` argv is built in `wrapCommandWithSandboxLinux` ([linux-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/linux-sandbox-utils.ts)):
  - It starts with `--new-session --die-with-parent`.
  - When the network is restricted it adds `--unshare-net`.
  - Filesystem binds come next: `--ro-bind / /`, then `--bind` for write roots and `--ro-bind` or `--tmpfs` for denies.
  - Then `--dev /dev`, `--unshare-pid` and `--unshare-user`, with capability arguments starting from `--cap-drop ALL`.
  - Then `--proc /proc` for a fresh procfs. Under `enableWeakerNestedSandbox` it uses `--bind /proc /proc` instead.
  - It ends with `--unsetenv` / `--setenv` for credential environment variables and proxy variables.
- A code comment states: "If we don't have --unshare-pid, it is possible to escape the sandbox." — [linux-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/linux-sandbox-utils.ts)
- Seccomp layer (`vendor/seccomp-src`), as described in the [srt README](https://github.com/anthropics/sandbox-runtime):
  - A static `apply-seccomp` helper runs inside bwrap. It creates a nested user + PID + mount namespace, acts as a non-dumpable PID 1, and applies a BPF filter before it execs the user command.
  - The filter blocks `socket(AF_UNIX, …)`, and also `io_uring_setup/enter/register` because `IORING_OP_SOCKET` would otherwise get around the socket rule.
  - Prebuilt binaries exist for x64 and arm64 only.
- A second seccomp filter stops the command from escaping its mount namespace ([seccomp-unix-block.c](https://github.com/anthropics/sandbox-runtime/blob/main/vendor/seccomp-src/seccomp-unix-block.c); [srt README](https://github.com/anthropics/sandbox-runtime)):
  - It refuses with `EPERM`: `unshare` and `clone` with `CLONE_NEWUSER|CLONE_NEWNS`, `setns`, `mount`, `umount2`, `pivot_root`, `open_tree`, `move_mount`, `fsopen`, `fsconfig`, `fsmount`, `fspick`, `mount_setattr`.
  - It refuses `clone3` with `ENOSYS`, which makes libc fall back to `clone`.
  - Rationale from the README: "Every write deny on Linux is a read-only bind mount … Creating a user namespace … gives its creator a full capability set over a private copy of the mount tree, from which the binds can be removed".
- Without the seccomp helper, bwrap's `--disable-userns` (bwrap ≥0.8.0) is used instead. — [srt README](https://github.com/anthropics/sandbox-runtime)
- Supported bubblewrap versions: ≥0.4.0, with ≥0.5.0 needed for some file binds. — [srt README](https://github.com/anthropics/sandbox-runtime)
- CC dependencies on Linux and WSL2:
  - `bubblewrap` and `socat` are required.
  - Ripgrep is bundled with the native CC binary.
  - The seccomp filter is optional ("adds Unix domain socket blocking"). Install it with `npm install -g @anthropic-ai/sandbox-runtime`.
  - Source: [CC sandboxing](https://code.claude.com/docs/en/sandboxing).
- Ubuntu 24.04+ sets `kernel.apparmor_restrict_unprivileged_userns=1`, which stops bwrap. The fixes are an AppArmor profile for `/usr/bin/bwrap` (the doc gives one) or `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- Inside unprivileged containers, bwrap can't mount a fresh `/proc`. `enableWeakerNestedSandbox` bind-mounts the existing `/proc` instead, and the docs say it "considerably weakens security". — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)

**WSL2**
- WSL2 uses bubblewrap, the same as Linux. WSL1 is rejected with "Sandboxing requires WSL2". — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- On WSL2, launching a Windows binary (`cmd.exe`, `powershell.exe`, anything under `/mnt/c/`) goes to the host over a Unix socket. Whether that is blocked therefore depends on the seccomp Unix-socket filter being installed, and `allowAllUnixSockets` re-allows it. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- On `/mnt/c` (a 9p mount), Windows ACLs decide what can be created, so srt creates the mount-point files for deny paths itself at wrap time. — [srt README](https://github.com/anthropics/sandbox-runtime)

**Native Windows in srt (alpha). CC does not use it, per its docs.**
- "Runs the sandboxed process under a dedicated `srt-sandbox` local user account, with a Windows Filtering Platform egress fence keyed on that account's SID, a write-restricted token, and explicit ACEs on the working tree". — [srt README](https://github.com/anthropics/sandbox-runtime)
- One-time setup is an elevated `npx @anthropic-ai/sandbox-runtime windows-install` (one UAC prompt). It:
  - creates the `srt-sandbox` user, with a random password stored DPAPI-encrypted under `HKLM\SOFTWARE\sandbox-runtime`;
  - creates a `sandbox-runtime-users` group;
  - installs machine-wide WFP filters.
  - It is idempotent and needs no logout.
  - Source: [srt README](https://github.com/anthropics/sandbox-runtime).
- Launch is a two-hop process. — [srt README](https://github.com/anthropics/sandbox-runtime)
  - The broker calls `CreateProcessWithLogonW` to start a runner as `srt-sandbox`.
  - The runner uses `CreateProcessAsUserW` to start the target under a write-restricted token inside a job object.
  - The child gets the sandbox account's own profile, `%TEMP%` and `HKCU`, and "a fresh environment overlaid with only the broker's `PATH` and the generated proxy variables".
- Job object details: `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` plus UI limits (desktop, clipboard read/write, global atoms, handles, exitwindows, …). A per-exec isolated desktop is created on the broker's window station. — [job.rs](https://github.com/anthropics/sandbox-runtime/blob/main/vendor/srt-win-src/src/job.rs); [winsta.rs](https://github.com/anthropics/sandbox-runtime/blob/main/vendor/srt-win-src/src/winsta.rs); [launch.rs](https://github.com/anthropics/sandbox-runtime/blob/main/vendor/srt-win-src/src/launch.rs)
- If `srt-win exec` sees the process that started it die, it kills the command tree and exits with code 26 (`session_ended`). — [srt README](https://github.com/anthropics/sandbox-runtime)
- Why a separate user SID: it "structurally closes the surrogate-spawn class of escape (Task Scheduler, `PROC_THREAD_ATTRIBUTE_PARENT_PROCESS` …, BITS, out-of-process COM …)". Any process spawned out-of-band still carries the sandbox SID. — [srt README](https://github.com/anthropics/sandbox-runtime)
- Why a write-restricted token: `srt-sandbox` is a member of `BUILTIN\Users`, so with a normal token it could write wherever Users or Authenticated Users can (`C:\` subfolders, data drives, PATH directories). — [srt README](https://github.com/anthropics/sandbox-runtime)
  - The restricting SIDs are the sandbox account, `Everyone`, `RESTRICTED`, and one SID per `allowWrite` root.
  - "Reads are unaffected by the token."

### Inferences
- For MossCode on Windows, srt's Windows design is the only first-party Anthropic blueprint. It is heavy: it needs admin install, a machine-wide WFP filter set, a dedicated user account and ACL rewriting over the user's profile. It is also explicitly alpha. CC itself has not adopted it, and its docs tell Windows users to use WSL2 or a container.
- A Python agent could call `srt` as an external CLI (it is Node ≥22.12) rather than reimplement it. The Windows path would then still depend on the elevated `windows-install` step.

### Gaps
- I found no official statement or roadmap on when or whether CC will use srt's Windows backend. The CC docs fetched 2026-11-05 still say native Windows runs unsandboxed. One changelog line, "Fixed a spurious 'sandbox dependencies missing' startup warning on Windows", hints at Windows-side plumbing but does not say a sandbox exists there. — [CC CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
- The task mentioned the GitHub org `anthropic-experimental`. The live repo and the CC docs both point to `anthropics/sandbox-runtime`, created 2025-10-20 (GitHub API). I did not verify whether an `anthropic-experimental` URL redirects there.

---

## 2. Filesystem policy (defaults, deny lists, configuration, protecting config files)

### Takeaway
- **Writes** follow an allow-list: the working directory, a per-user temp directory (`$TMPDIR` is pointed at it) and added directories.
- **Reads** are open by default. Credential files such as `~/.ssh` and `~/.aws/credentials` are readable unless the user denies them, and CC has no built-in credential deny list.
- A hard-coded set of "protected paths" (CC) or "mandatory deny paths" (srt) stays write-denied even inside writable roots, so a sandboxed command can't grant itself more power. That set covers agent config, hooks, MCP config, shell rc files, `.git/hooks` and `.git/config`, and IDE directories.

### Cited Findings
- Defaults table. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - Writes: the working directory, a per-user temp directory, and directories added with `--add-dir`, `/add-dir` or `permissions.additionalDirectories`.
  - Reads: "Most of the machine, including credential files such as `~/.ssh` and `~/.aws/credentials`".
  - Settings that change them: `filesystem.allowWrite`, `filesystem.denyWrite`, `filesystem.denyRead`, `filesystem.allowRead`, and `credentials`.
- "There is no built-in credential deny list, so only the files and variables you list are restricted." — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- Precedence rules. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing); [srt README](https://github.com/anthropics/sandbox-runtime)
  - For reads, the narrower path wins: `denyRead: ["~/"]` with `allowRead: ["~/projects"]` re-opens projects. A deny inside a wider allow still holds, e.g. `allowRead ~/` with `denyRead ~/**/.env`.
  - In srt, `allowRead` beats `denyRead`, while `denyWrite` beats `allowWrite`: "intentionally opposite".
- Path syntax differs from CC's own permission rules. Sandbox paths use `/abs` and `~/x`, while Read/Edit permission rules use `//abs` and `/project-relative`. A relative `.` resolves against the directory of the settings file it appears in. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- Permission rules are merged into the sandbox config. `Edit` allow rules feed `allowWrite`, `Edit` deny rules feed `denyWrite`, `Read` deny rules feed `denyRead`, and `WebFetch(domain:…)` rules feed the domain lists. — [CC settings reference](https://code.claude.com/docs/en/settings-reference)
- Arrays are merged across settings scopes rather than replaced. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- `permissions.blockReadsOutsideWorkingDirectories` denies sandboxed reads of home directories and mounted volumes. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- `sandbox.filesystem.disabled: true` turns the filesystem layer off and keeps network isolation. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - Only user settings, managed settings or `--settings` can set it; project settings cannot.
  - The docs warn that with it off, a command "can write files that later commands run or read, such as shell startup files, executables on `$PATH`, or `~/.claude/settings.json`, and use them to widen its own access".
- **CC protected paths** stay write-denied even under `allowWrite` or an Edit allow rule. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - In the working directory and every directory above it: the `.claude` settings files, `.claude/skills`, `.claude/agents`, `.claude/commands`, `.claude/hooks`, `.mcp.json`, `.claude/workflows` and `.claude/scheduled_tasks.json`.
  - In the working directory only: `.bashrc`, `.zshrc` and similar, `.gitconfig`, `.vscode`, `.idea`, and `hooks` and `config` inside `.git`.
  - Files that would turn the working directory into a bare git repository: `HEAD`, `objects` and `refs` at the top level. Newly created ones are deleted after the command.
  - Most of `~/.claude` (or `CLAUDE_CONFIG_DIR`), plus `~/.claude.json` and `.credentials.json`.
  - If a symlink appears at a protected settings path during the session, the file it points to is also denied, starting with the next command.
  - Stated reason: "A command that could edit those files could grant itself permissions, or add a hook or MCP server that Claude Code runs outside the sandbox."
- In a linked git worktree, writes to the main repository's `.git` are allowed except `hooks/` and `config`. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- **srt mandatory deny paths** apply even with `allowWrite: ["."]`. — [srt README](https://github.com/anthropics/sandbox-runtime)
  - Files: `.bashrc`, `.bash_profile`, `.zshrc`, `.zprofile`, `.profile`, `.gitconfig`, `.gitmodules`, `.ripgreprc`, `.mcp.json`.
  - Directories: `.vscode/`, `.idea/`, `.claude/commands/`, `.claude/agents/`, `.git/hooks/`, `.git/config`.
  - On Linux, ripgrep scans `mandatoryDenySearchDepth` levels deep (default 3, range 1–10). macOS uses glob patterns instead.
- A few paths are always writable without being listed: the child's stdio, `/tmp/claude`, `~/.npm/_logs` and `~/.claude/debug`. — [srt README](https://github.com/anthropics/sandbox-runtime)
- **Linux mechanics.** — [srt README](https://github.com/anthropics/sandbox-runtime)
  - Write denies are read-only bind mounts.
  - A `denyWrite` path that doesn't exist yet gets a 0-byte read-only placeholder file on the host, removed afterwards. If the session is killed with SIGKILL, the placeholders are left behind; CC's `claude doctor` reports them. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - Denied directories become a tmpfs, which looks like an empty writable directory inside the sandbox.
  - Globs are expanded only to paths that exist at wrap time.
  - Ancestors of protected paths are "pinned" as mountpoints so `mv` or `rmdir` fail with `EBUSY`.
- **Windows (srt alpha) mechanics.** — [srt README](https://github.com/anthropics/sandbox-runtime)
  - `allowWrite` becomes an inheriting MODIFY ALLOW ACE with `FILE_DELETE_CHILD` withheld.
  - `allowRead` becomes READ|EXECUTE.
  - Denies become an inheriting DENY ACE plus a `FILE_DELETE_CHILD` DENY on the parent, which stops rename or delete through the parent.
  - Write grants on project roots are kept between sessions because re-ACLing large trees is slow.
  - An optional "profile read grant" makes `%USERPROFILE%` readable, minus a long built-in exclusion list: `.ssh`, `.aws`, `.azure`, `.kube`, `.docker`, `.claude`, `.config\gh`, `.git-credentials`, `.netrc`, `.pypirc`, NTUSER.DAT, browser profiles, password managers, `%APPDATA%\Microsoft\Credentials|Protect|Vault|Crypto`, TokenBroker, `%LOCALAPPDATA%\Packages`, and more.

### Inferences
- The protected-path list is the key design idea for MossCode. Any file the agent loads as config or code (MossCode's equivalents of `.claude/settings*.json`, hooks, MCP config, `~/.mosscode/`, plugin directories) must be write-denied to sandboxed commands, or the sandbox can be escaped on the next run.
- Read-open-by-default is a deliberate usability trade-off. CC pushes users toward an explicit `credentials` deny list instead.

### Gaps
- The exact CC-side list of "denied directories" that applies to reads by default ("except certain denied directories") is not listed on the page. The docs point to `/sandbox` → Config tab for the list on your own machine.

---

## 3. Network policy (proxy design, allowlist, enforcement, approving new domains, proxy-ignoring tools)

### Takeaway
- Each sandboxed command has **no direct route out**.
- On Linux and WSL2 it runs in an empty network namespace (`--unshare-net`). The only way out is a Unix socket, bind-mounted into the sandbox, that `socat` bridges to HTTP and SOCKS5 proxies running on the host.
- On macOS, Seatbelt allows outbound connections only to the proxies' localhost ports.
- The proxies enforce a domain allow/deny list. Domains not on the list cause a user prompt in Manual mode.
- Tools that ignore `HTTP(S)_PROXY` simply fail. srt adds special shims for `git` over ssh and for the JVM.

### Cited Findings
- "Linux and WSL2: the command runs in a separate network namespace that has no connection to your network. macOS: the Seatbelt sandbox framework by default blocks connections other than the one to the sandbox proxy." — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- The proxy runs outside the sandbox. CC points commands at it with `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and related variables, and the proxy checks each hostname against the allowed and denied lists. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- Linux bridge architecture, from a code comment in [linux-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/linux-sandbox-utils.ts):
  - "Host side: Run socat bridges that listen on Unix sockets and forward to host proxy servers".
  - "Sandbox side: Bind the Unix sockets into the isolated namespace and run socat listeners — HTTP listener on port 3128 … SOCKS listener on port 1080".
  - `HTTP_PROXY=http://localhost:3128`, and `GIT_SSH_COMMAND` uses socat through SOCKS.
  - The socket files are `$TMPDIR/claude-http-<random>.sock` and its SOCKS counterpart.
- The blog describes the design as "only allowing internet access through a unix domain socket connected to a proxy server running outside the sandbox. This proxy server enforces restrictions on the domains … and handles user confirmation for newly requested domains." — [Anthropic engineering blog](https://www.anthropic.com/engineering/claude-code-sandboxing)
- On Windows (srt alpha), WFP `ALE_AUTH_CONNECT_V4/V6` filters do the work. — [srt README](https://github.com/anthropics/sandbox-runtime)
  - They PERMIT loopback to the proxy port range (default 60080–60089) and BLOCK every other connect whose token is `srt-sandbox`.
  - "the WFP filter is the boundary — a process that ignores or unsets them is still fenced."
- What gets through. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - Proxy-aware tools (curl, npm, git over HTTPS) work for allowed hosts.
  - "Plain `ssh`, most database drivers, and similar tools can't connect, even to an allowed host."
  - "UDP, HTTP/3 over QUIC, and ICMP tools such as `ping` can't leave the sandbox."
  - Failures look like `Network is unreachable` on Linux, or a name-resolution error.
- Order of decisions in the srt filter. — [srt README](https://github.com/anthropics/sandbox-runtime)
  1. A `deniedDomains` match denies.
  2. An `allowedDomains` match allows.
  3. `strictAllowlist` denies.
  4. The per-command list allows.
  5. Otherwise the ask callback decides; with no callback, the connection is denied.
- Allowlist entries take an optional `:port`. Wildcards are `*.example.com`. IPv6 must be bracketed. — [srt README](https://github.com/anthropics/sandbox-runtime)
- **Resolved-address check (anti-DNS-rebinding / SSRF).** After an allowed hostname resolves, the proxy refuses to dial it when it resolves only to: — [CC sandboxing](https://code.claude.com/docs/en/sandboxing); [srt README](https://github.com/anthropics/sandbox-runtime)
  - loopback, link-local, multicast or unspecified addresses;
  - cloud metadata addresses (`169.254.169.254`, `100.100.100.200`, `168.63.129.16`, …);
  - the host's own interface addresses.
  - Private RFC1918 ranges are not refused by default. They can be added with `deniedResolvedAddresses`.
  - The refusal is a `403` whose body names the class of address, e.g. "resolved to a loopback address".
  - CC adopted this in v2.1.284.
- **Approving new domains in CC.** — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - The allowlist starts empty. When a command hits a host that isn't on it, the command "stays in the sandbox and waits for a decision".
  - Manual and acceptEdits modes: the user gets a prompt. "Yes" allows the host for the session. "Yes, and don't ask again" saves a `WebFetch(domain:…)` allow rule to local settings.
  - `bypassPermissions` mode: allowed without a prompt, unless `strictAllowlist` or `allowManagedDomainsOnly` is on.
  - Auto mode: refused, unless the command listed the host in its per-command `allowed_domains` and the classifier approved it.
  - `dontAsk` mode: refused.
  - `deniedDomains` always refuses.
- In the srt library, prompting is implemented as `SandboxManager.initialize(config, askCallback)`. The callback returns `true`, `false` or `{allow:false, reason}`. — [srt README](https://github.com/anthropics/sandbox-runtime)
- **Per-command allowed domains (auto mode, v2.1.271+).** Bash, PowerShell and Monitor calls can carry a host list that a classifier reviews together with the command; the list applies to that command only. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - srt binds a connection to its command through the proxy username, which carries a ≥128-bit random `commandId`.
  - The README says this is "attribution, not a boundary between concurrent commands of one session". — [srt README](https://github.com/anthropics/sandbox-runtime)
- Proxy authentication: srt's proxies require a token passed in the proxy URL. On Windows this is `http_proxy=http://srt:<token>@127.0.0.1:…`, and the token is visible on the runner's command line. — [srt README](https://github.com/anthropics/sandbox-runtime)
- **Shims for tools that ignore proxy variables.** — [srt README](https://github.com/anthropics/sandbox-runtime); [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - git over SSH: `GIT_SSH_COMMAND` with a `ProxyCommand` that does HTTP CONNECT through the proxy (socat on Linux, `nc` on macOS). The CC docs say this works on Linux but fails on macOS.
  - JVM: a `-javaagent` injected through `JAVA_TOOL_OPTIONS` sets `https.proxyHost` and installs an Authenticator.
  - Node on Windows: `NODE_USE_ENV_PROXY=1`.
  - Everything else (DB drivers, plain ssh) has to be moved out of the sandbox with `excludedCommands`.
- The srt README lists as future work "Proxychains support … `LD_PRELOAD` on Linux". — [srt README](https://github.com/anthropics/sandbox-runtime)
- `localhost` on Linux: the sandbox has a private loopback, so host dev servers are unreachable directly and `allowLocalBinding` has no effect. CC sets `NO_PROXY` for localhost. On macOS, `allowLocalBinding` opens every localhost port. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- Corporate proxy: set `HTTPS_PROXY` in CC's environment. CC enforces the allowlist and then tunnels allowed connections upstream. Alternatively, `httpProxyPort` and `socksProxyPort` replace the built-in proxy entirely, and then CC's own domain controls stop applying. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- TLS is not inspected by default. Experimental `network.tlsTerminate` makes the proxy mint leaf certificates from a MITM CA, which is exposed to clients through `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO` and similar variables. It exists mainly for credential masking. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing); [srt README](https://github.com/anthropics/sandbox-runtime)
- Windows DNS caveat: `getaddrinfo()` is serviced by the Dnscache service, so name resolution succeeds even though `connect()` is blocked. "This mirrors the macOS behaviour." — [srt README](https://github.com/anthropics/sandbox-runtime)

### Inferences
- For a Python agent, the Linux pattern (an empty netns plus a Unix socket bridge to a host proxy) is reproducible with bwrap and socat, and the proxy can be written in Python asyncio.
- On Windows there is no namespace equivalent. Kernel-level enforcement needs WFP, which means admin rights and an install step. Proxy environment variables alone would be advisory only.

### Gaps
- None material for the design.

---

## 4. Secret protection (environment, API key, ~/.ssh, keychains)

### Takeaway
- By default, sandboxed commands **inherit CC's full environment, secrets included**, and can **read `~/.ssh` and `~/.aws`**.
- Protection is opt-in, through three mechanisms:
  - `sandbox.credentials` with `deny` (unset the variable, or read-deny the file) or `mask` (a sentinel inside the sandbox, swapped for the real value by the TLS-terminating proxy only on requests to allowed hosts);
  - the global `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` (heuristic credential stripping);
  - read denies.
- The macOS keychain is partly reachable via allowed Mach services (`com.apple.SecurityServer`, `securityd.xpc`). I found no explicit statement about keychain reads from the sandbox.

### Cited Findings
- Defaults table: "Environment variables — Inherited from Claude Code, including any secrets in its environment". — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- `credentials.envVars` with `mode: "deny"` unsets the variable before each sandboxed command. `credentials.files` with `mode: "deny"` read-denies the file. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - Example: deny `~/.aws/credentials`, `~/.ssh`, `GITHUB_TOKEN` and `NPM_TOKEN`.
  - `deny` entries merge across scopes, and no scope can remove one.
- On Linux this is implemented with bwrap `--unsetenv NAME` and `--setenv NAME <sentinel>`. They are emitted before the proxy `--setenv` flags so srt's own proxy variables survive. — [linux-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/linux-sandbox-utils.ts)
- **Masking.** — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - The sandboxed command sees a per-session sentinel. The proxy substitutes the real value on outbound requests to the `injectHosts` list (by default every allowed domain).
  - It requires `network.tlsTerminate`, and is honored only from user, managed or `--settings` scope, never from repository settings.
  - AWS SigV4 requests are re-signed by the proxy.
  - Masked files get a sentinel copy on Linux and are simply denied on macOS. On srt's Windows backend, masked files are also denied ([srt README](https://github.com/anthropics/sandbox-runtime)).
  - Falls back to `deny` for a directory, a glob, a file over 8 MiB or a non-UTF-8 file.
- `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` strips credentials from Bash, hooks and stdio MCP server environments, matching on variable name or value shape. — [CC env-vars](https://code.claude.com/docs/en/env-vars)
  - It removes `ANTHROPIC_API_KEY`, `AWS_SECRET_ACCESS_KEY`, `*_TOKEN` and `*_PASSWORD`.
  - It deliberately keeps `GITHUB_TOKEN`/`GH_TOKEN` and the proxy variables.
  - On Linux it also runs Bash in an isolated PID namespace "so they can't read host process environments through `/proc`".
  - With it set, every command runs sandboxed (no `excludedCommands`, no `!` bypass) and auto-allow is turned off. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing); [CC settings reference](https://code.claude.com/docs/en/settings-reference)
- `apiKeyHelper`, hooks, MCP servers and the status line run outside the sandbox with full access. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- ssh-agent: "the sandbox can block the `ssh-agent` socket" (Unix sockets are blocked by default). — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
- Fresh `/proc` plus `--unshare-pid` hide host processes on Linux. Without `--proc`, "it is possible to read host /proc and leak information about code running outside the sandbox". — [linux-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/linux-sandbox-utils.ts)
- Windows (srt alpha): the child gets a **fresh environment**, made of the broker's PATH plus the proxy variables, and runs under the sandbox account's own profile and `HKCU`. Credential stores are excluded from the profile read grant (`%APPDATA%\Microsoft\Credentials`, `Protect` (DPAPI), `Vault`, TokenBroker, browser profiles, …). — [srt README](https://github.com/anthropics/sandbox-runtime)
- The blog claims that with sandboxing "a compromised Claude Code can't steal your SSH keys, or phone home". — [Anthropic engineering blog](https://www.anthropic.com/engineering/claude-code-sandboxing)
  - This is contradicted in nuance by the current docs: default reads include `~/.ssh` unless denied. The blog's claim depends on the network allowlist stopping exfiltration rather than on blocking the read. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)

### Inferences
- The model API key in CC's own environment is inherited by sandboxed commands unless scrubbed or denied. MossCode already keeps keys in the credential manager (commit f54d4b5). The lesson is to pass sandboxed children an allowlisted or scrubbed environment by default. srt's Windows backend effectively does this (a fresh environment) by design.
- On Windows, running as a different user SID makes DPAPI-protected secrets of the real user (Credential Manager, browser cookies) unreadable by construction. That is a strong argument for the separate-account approach.

### Gaps
- I found no explicit documentation on whether macOS Keychain items can be read from inside the Seatbelt profile. The profile allows `mach-lookup` for `com.apple.SecurityServer` and `com.apple.securityd.xpc` ([macos-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/macos-sandbox-utils.ts)). Whether keychain reads succeed is unverified.

---

## 5. Relationship to permissions and approval (auto-allow, excludedCommands, dangerouslyDisableSandbox, violation reporting)

### Takeaway
The sandbox replaces per-command prompts with an OS boundary ("auto-allow mode", `autoAllowBashIfSandboxed`, default `true`). Deny rules, content-scoped ask rules and critical-path `rm` still prompt.

Commands escape the sandbox through:
- `excludedCommands`, which still go through the normal permission flow;
- a model-initiated retry with `dangerouslyDisableSandbox: true`, which prompts as "Bash command (unsandboxed)" in Manual mode and can be disabled with `allowUnsandboxedCommands: false` (strict mode).

Violations (denied paths and hosts) are collected and appended to the tool result in a `<sandbox_violations>` block, so the model knows why a command failed.

### Cited Findings
- **Two modes**, with identical restrictions in both. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - Auto-allow: sandboxed commands run without a prompt.
  - Regular permissions: every command goes through the normal permission flow.
  - Auto-allow works independently of the permission mode, except in plan mode, auto-mode per-command domains and classifier review: "Bash commands that modify files within the sandbox boundaries execute without prompting, even in Manual mode".
- **Still enforced under auto-allow.** — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - Bash deny rules.
  - `rm`/`rmdir` on critical paths.
  - Content-scoped ask rules such as `Bash(git push *)`.
  - Monitor tool commands are not auto-approved.
  - Commands with a leading variable assignment (`FOO=bar npm test`), `make CC=clang` and `eval` go through the permission flow but still run sandboxed.
- **`excludedCommands`** take matching calls out of the sandbox entirely, with no filesystem or network limits. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing); [CC settings reference](https://code.claude.com/docs/en/settings-reference)
  - Every command in a chained call must match.
  - Matching is on the call text: a script that calls `docker` doesn't match.
  - These calls stay sandboxed whatever the entries say: anything starting with `sudo`, `eval` or `xargs`; any `cd`/`pushd`/`popd`; command substitution, subshells and if/for blocks; redirects other than fd-dup; a command name taken from a variable; `git clone/init/worktree` with an absolute, `~` or `..` path.
  - Excluded commands still go through the permission flow.
  - "Exclusion is a convenience, not a security boundary."
- **Unsandboxed retry.** — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - "When the sandbox blocks a network connection, Claude Code names the denied host in the command's result … Claude analyzes the failure and may retry the command with the `dangerouslyDisableSandbox` parameter."
  - Who approves it:
    - `bypassPermissions` mode: no prompt.
    - Manual and acceptEdits modes: a prompt titled "Bash command (unsandboxed)".
    - Auto mode: the classifier.
    - `dontAsk` mode: denied.
    - A matching allow rule also approves the retry.
    - An ask rule `Bash(dangerouslyDisableSandbox:true)` forces a prompt.
- **Strict mode.** `sandbox.allowUnsandboxedCommands: false` makes CC ignore the parameter. Set in managed settings or `--settings`, it also makes the sandbox "admin-required". — [CC sandboxing](https://code.claude.com/docs/en/sandboxing); [CC settings reference](https://code.claude.com/docs/en/settings-reference)
  - Admin-required means a repository's `.claude/settings*.json` can no longer loosen the sandbox: its `excludedCommands`, `allowedDomains`, `allowWrite`, Unix socket settings and proxy ports are ignored.
- History. — [CC CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
  - `allowUnsandboxedCommands` was added in 2.0.30.
  - 2.1.34 fixed excluded commands bypassing the Bash ask rule under auto-allow.
  - 2.1.113 fixed `dangerouslyDisableSandbox` "running commands outside the sandbox without a permission prompt".
  - 2.1.224 fixed violation details not reaching the model: "Claude now sees which file or network access was denied and why".
- **Trust of settings sources.** Several weakening keys are honored only from user, managed or CLI settings, never from a checked-out repository: `filesystem.disabled`, `allowAppleEvents`, `allowNestedUserNamespaces`, `mask`, `tlsTerminate`, and `strictAllowlist`. — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - srt's README likewise tells embedders to take `allowAppleEvents` and `allowLocalBinding` "never from project-local files". — [srt README](https://github.com/anthropics/sandbox-runtime)
- **What runs outside the sandbox.** — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - The Read, Edit, Write, WebFetch and WebSearch tools; these follow permission rules instead.
  - Hooks, local MCP servers, LSP servers, the status line and `apiKeyHelper`.
  - User `!` shell-mode commands, in most sessions.
  - Subagents share the parent's sandbox config.
- **Violation reporting (srt).** — [srt README](https://github.com/anthropics/sandbox-runtime); [sandbox-manager.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/sandbox-manager.ts)
  - Violations are stored per command key (`commandId`).
  - `annotateStderrWithSandboxFailures(key, stderr)` appends `<sandbox_violations>` … `</sandbox_violations>` with one sanitized line per violation.
  - The store keeps the last 100. — [sandbox-violation-store.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/sandbox-violation-store.ts)
- Violation sources by platform:
  - macOS: `log stream --predicate '(eventMessage ENDSWITH "<session suffix>")'` over the system sandbox log, keyed by the profile's `(with message "<logTag>")`. — [macos-sandbox-utils.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/macos-sandbox-utils.ts)
  - Linux: a seccomp `USER_NOTIF` observer in `apply-seccomp` reports write-intent syscalls to a Unix socket. A path counts as a violation if it is outside the allowed write roots or inside denyWrite. — [linux-violation-monitor.ts](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/linux-violation-monitor.ts)
  - Proxy denials are recorded as lines such as `deny network-outbound host:port (...)`.
  - The older README section says Linux has no built-in reporting and suggests `strace`. That is outdated relative to the observer code. — [srt README](https://github.com/anthropics/sandbox-runtime)
- `sandbox.ignoreViolations` (a map from command substring to violation substrings) suppresses reports only; the access is still blocked. — [CC settings reference](https://code.claude.com/docs/en/settings-reference)
- `deniedDomainReasons` adds a model-facing reason to the violation line, e.g. "SSH pushes to GitHub are blocked; use an https:// remote". — [srt README](https://github.com/anthropics/sandbox-runtime)
- Measured impact: "In our internal usage, we've found that sandboxing safely reduces permission prompts by 84%." — [Anthropic engineering blog](https://www.anthropic.com/engineering/claude-code-sandboxing)

### Inferences
- MossCode's existing command risk tiers and approval prompts map naturally onto this design: keep them for unsandboxed execution and skip them for sandboxed execution. MossCode would also need:
  - an explicit "unsandboxed" approval prompt;
  - a model-visible `dangerously_disable_sandbox` parameter, policy-disablable;
  - violation text appended to tool output.
- The "stays sandboxed" shape list for `excludedCommands` (cd, redirects, substitutions, `sudo`/`eval`/`xargs`) is a ready-made set of bypass-direction test cases. MossCode's AGENTS.md requires such tests for security-boundary changes.

### Gaps
- The exact wording of the tool-result text CC shows the model, and the Bash tool description that explains `dangerouslyDisableSandbox`, are not published in the docs. Only the srt `<sandbox_violations>` format is visible in source.

---

## 6. Known limitations, escape risks, compatibility problems

### Takeaway
Anthropic says plainly: "Sandboxing reduces risk but is not a complete isolation boundary." The main risks are:
- domain fronting and exfiltration through broad allowed domains, since TLS is not inspected;
- allowed Unix sockets, e.g. `docker.sock`;
- broad write grants (PATH directories, rc files);
- weakening flags: `enableWeakerNestedSandbox`, `allowNestedUserNamespaces`, `allowAppleEvents`, `enableWeakerNetworkIsolation`.

Compatibility breakages are documented: docker, watchman/jest, Go TLS on macOS, ssh/DB clients, localhost servers on Linux, Chromium and rootless Podman, clipboard tools, and git operations touching protected files. On Windows (srt alpha), Schannel-based tools (System32 curl, PowerShell `Invoke-WebRequest`, .NET `HttpClient`/NuGet, winget, cargo) do not work.

### Cited Findings
- **Security limitations.** — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - Network: the proxy decides from the client-supplied hostname, so domain fronting can reach hosts that are not allowed; "Allowing broad domains such as `github.com` can create paths for data exfiltration".
  - Unix sockets: "allowing access to `/var/run/docker.sock` effectively grants access to the host system".
  - Filesystem: write permissions on `$PATH` directories or rc files lead to code execution.
  - `enableWeakerNestedSandbox` "considerably weakens security".
  - `allowAppleEvents` "removes code-execution isolation".
  - `allowMachLookup` services are not confined.
  - `allowNestedUserNamespaces` means "code from a sandboxed command can end up running outside the sandbox".
- "Effective sandboxing requires both filesystem and network isolation." — [CC sandboxing](https://code.claude.com/docs/en/sandboxing); [Anthropic engineering blog](https://www.anthropic.com/engineering/claude-code-sandboxing)
- Install-location risk: if srt sits in `<project>/node_modules` inside a writable project, a sandboxed command can rewrite it, "and the next `srt` there runs the result unsandboxed". — [srt README](https://github.com/anthropics/sandbox-runtime)
- Linux seccomp limitation: it does not stop use of Unix socket fds that were inherited or passed via `SCM_RIGHTS`. Without the seccomp helper (non-x64/arm64 architectures, or `allowAllUnixSockets`), Unix sockets are unrestricted and only a warning is shown. — [srt README](https://github.com/anthropics/sandbox-runtime)
- Root caller on Linux: needs `CAP_SETFCAP` in its bounding set. — [srt README](https://github.com/anthropics/sandbox-runtime)
- Large configurations on Linux: bwrap parses at most about 9000 arguments (~3000 mounts), and each nested git repository costs about 4 mounts. — [srt README](https://github.com/anthropics/sandbox-runtime)
- **Documented compatibility problems.** — [CC sandboxing](https://code.claude.com/docs/en/sandboxing)
  - "`watchman` is incompatible with the sandbox. Run `jest --no-watchman`."
  - "`docker` is incompatible with the sandbox"; use `excludedCommands`.
  - Go CLIs (`gh`, `gcloud`, `terraform`) fail TLS verification on macOS.
  - `open` and `osascript` fail with -600 on macOS.
  - Chromium ("No usable sandbox!"), rootless Podman and `unshare` fail on Linux.
  - `pbcopy`, `xclip` and `wl-copy` fail.
  - git `unable to unlink old` on protected paths.
  - git over SSH fails on macOS.
  - DB clients and ssh fail everywhere.
  - Localhost servers are unreachable on Linux.
  - Leftover 0-byte placeholder files after SIGKILL on Linux.
- **Windows srt known limitations.** — [srt README](https://github.com/anthropics/sandbox-runtime)
  - Schannel clients fail under a restricted token, with "no workaround" inside the sandbox.
  - Per-user tool installs (nvm, Scoop, `pip --user`, `%LOCALAPPDATA%\Programs`) are unreadable without the profile grant.
  - Write grants are kept between sessions, and on shared machines another user's sandboxed session can read kept roots.
  - SMB and WSL traffic carried by system services is not fenced.
  - A thread impersonating the anonymous token, or a derived deny-only or restricted copy of its own token, before creating a socket is **not fenced by WFP**. These are reported as "residuals". "Treat the network filter as stopping commands that connect directly, not code written to defeat it."
  - DNS through Dnscache is not fenced.
  - `allowLocalBinding` is a machine-wide install-time mode.

### Inferences
- If MossCode adopts srt's Windows approach, its main Windows user (Windows Terminal) would lose the native tools PowerShell users rely on: PowerShell web cmdlets, winget, dotnet restore. Those commands would need to be routed through an `excludedCommands`-style unsandboxed path with approval.
- Git for Windows still works because srt forces `http.sslBackend=openssl`.

### Gaps
- None beyond those noted in earlier sections.

---

## 7. sandbox-runtime packaging: license, language, invocation, dependencies

### Takeaway
srt is **Apache-2.0** (MIT-compatible, so it can be used from MossCode). It is TypeScript (Node ≥22.12, also runs under Bun), with a C seccomp helper and a Rust Windows helper (`srt-win`). It is labelled a "Beta Research Preview", at version 0.0.81 as of 2026-10-20. You use it as the CLI `srt <cmd>`, as a library (`SandboxManager.initialize` / `wrapWithSandbox`), or as `srt proxy` (proxy only, with an external decider).

### Cited Findings
- Repository metadata from the GitHub API, fetched 2026-11-05: [anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime).
  - License: Apache-2.0. Language: TypeScript. Created 2025-10-20. Last pushed 2026-10-30. About 6.98k stars.
  - Description: "A lightweight sandboxing tool for enforcing filesystem and network restrictions on arbitrary processes at the OS level, without requiring a container."
- `package.json`: [package.json](https://github.com/anthropics/sandbox-runtime/blob/main/package.json)
  - Name `@anthropic-ai/sandbox-runtime`; `bin: { srt: dist/cli.js }`; engines `node >=22.12.0`.
  - Dependencies: `@pondwader/socks5-server`, `commander`, `node-forge`, `zod`.
- npm: latest is 0.0.81 (published 2026-10-20). The package was first published 2025-10-20. — [npm registry](https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime)
- README banner: "Beta Research Preview … APIs and configuration formats may evolve." — [srt README](https://github.com/anthropics/sandbox-runtime)
- **CLI.** — [srt README](https://github.com/anthropics/sandbox-runtime)
  - `srt [--debug] [--settings file] <command>`.
  - The config lives in `~/.srt-settings.json`, with `network` and `filesystem` (plus `credentials` and others).
  - An invalid config file is an error; srt does not fall back to defaults.
  - `--control-fd N` hot-reloads network lists, one JSON object per line.
  - The README shows using `srt` to wrap MCP servers in `.mcp.json`.
- **Library.** — [srt README](https://github.com/anthropics/sandbox-runtime)
  - `await SandboxManager.initialize(config, askCallback?)` starts the proxies.
  - `wrapWithSandbox(cmd, …, {commandId, commandText})` returns a wrapped shell string, which you run with `spawn(..., {shell:true})`.
  - Then `annotateStderrWithSandboxFailures`, `getViolationsForCommand`, `registerCommandNetworkLists`, `reset()`.
  - Windows install helpers: `installWindowsSandbox()` / `uninstallWindowsSandbox()`.
- `srt proxy` runs only the HTTP proxy. It takes an inherited listening socket and asks a separate "decider" process over another fd. — [srt README](https://github.com/anthropics/sandbox-runtime); [docs/srt-proxy.md](https://github.com/anthropics/sandbox-runtime/blob/main/docs/srt-proxy.md)
- **Platform dependencies.** — [srt README](https://github.com/anthropics/sandbox-runtime)
  - macOS: none, plus `ripgrep` for deny-path detection.
  - Linux: `bubblewrap`, `socat` and `ripgrep`; `gcc` and `libseccomp-dev` only on architectures without the prebuilt seccomp helper.
  - Windows: the bundled `srt-win.exe` (x64 and arm64) and a one-time elevated `windows-install`.
- Source layout: `src/sandbox/{sandbox-manager, http-proxy, socks-proxy, linux-sandbox-utils, macos-sandbox-utils, windows-sandbox-utils, tls-terminate-proxy, credential-*}.ts`, `vendor/seccomp-src/*.c`, `vendor/srt-win-src/src/*.rs` (acl, wfp, logon, token, job, winsta, profile, …) and `vendor/java-proxy-agent-src`. — [repo tree](https://github.com/anthropics/sandbox-runtime)
- CC's docs suggest running the whole CC process inside srt to also cover hooks, MCP and file tools. Writes must be allowed to `~/.claude`, `~/.claude.json` and the temp directory, and network access to `api.anthropic.com`, `claude.ai` and `platform.claude.com`. — [Sandbox environments](https://code.claude.com/docs/en/sandbox-environments)

### Inferences
- A Python 3.12 agent could integrate in either of two ways:
  - shell out to `srt` as a subprocess wrapper, which needs Node ≥22.12 on the user's machine and the `srt` settings file or `--control-fd` for live domain approvals;
  - reimplement the per-platform wrappers natively: Seatbelt profile generation, bwrap argv plus a Python asyncio HTTP CONNECT/SOCKS proxy with a Unix socket bridge, and, for Windows, a much larger native effort.
- The Apache-2.0 license allows copying the profile and argv design, with attribution and a NOTICE.

### Gaps
- No standalone Python binding exists.
- I did not verify whether `srt` works when wrapping commands for an embedder that is not Node (it should, since it is a CLI).
