# Native Windows technologies for sandboxing coding-agent child processes (as of Nov 2026)

Scope: per-command sandbox for MossCode (Python 3.12, Windows 11, Git Bash / PowerShell, already uses a Job Object for kill-tree). Target policy: read most of the system, write only project + temp, network off or only via a local proxy, no reading secrets (~/.ssh, agent config, parent env).

Note on the dev machine: the environment this research ran in reports "Windows 11 Home China 10.0.26300". Edition matters a lot below (Windows Sandbox / Hyper-V options are not available on Home).

Method note: WebFetch was unavailable; pages were retrieved with curl (learn.microsoft.com HTML, raw.githubusercontent.com, GitHub API) and, for openai.com, developers.openai.com and blogs.windows.com, via the r.jina.ai text proxy. Every claim below cites the original URL.

---

## Q0. Big new development: Microsoft Execution Containers (MXC), GA Oct 2026

### Takeaway
Microsoft now ships a first-party, MIT-licensed, policy-driven agent sandbox (MXC) whose Windows "process container" is built on AppContainer, needs no admin setup, and is already used by Codex as its *recommended* Windows sandbox. This changes the calculus for MossCode more than any of the classic primitives below.

### Cited Findings
- MXC is "a policy-driven execution layer for untrusted code or dynamically generated workloads"; developers declare files and network destinations, and "the policy remains outside the agent workload's control". GA announced 2026-10-07. — [Windows Developer Blog](https://blogs.windows.com/windowsdeveloper/2026/10/07/microsoft-execution-containers-policy-driven-containment-for-ai-agents/)
- Backends: **Process container** (Windows 11/macOS/Linux; "AppContainer on Windows, Seatbelt on macOS, and Bubblewrap on Linux"); **Session container** (Windows 11 only; "distinct Windows account and session", separate desktop/clipboard/UI/input); **WSL container (WSLc)** (Windows 11 only); **MicroVM** (Windows 11 + Linux, experimental). — [Windows Developer Blog](https://blogs.windows.com/windowsdeveloper/2026/10/07/microsoft-execution-containers-policy-driven-containment-for-ai-agents/)
- Policy areas: file system (modify / read-only / no access), network (inbound/outbound "including whether the workload can connect to services through the host's loopback interface"), UI. Modes: Enforcement, Learning (blocked + JSON activity report), Permissive (allowed + recorded); activity report is Windows-only. — [Windows Developer Blog](https://blogs.windows.com/windowsdeveloper/2026/10/07/microsoft-execution-containers-policy-driven-containment-for-ai-agents/)
- Adopters listed: GitHub Copilot, OpenClaw, OpenAI Codex, Replit, LM Studio, Unsloth; "Anthropic Claude Code" listed among those "releasing support". — [Windows Developer Blog](https://blogs.windows.com/windowsdeveloper/2026/10/07/microsoft-execution-containers-policy-driven-containment-for-ai-agents/)
- Repo `microsoft/mxc` is MIT-licensed, written in Rust; SDKs are **Rust, .NET, Node** (no Python SDK). "Non-SDK consumption: Platform-specific executor binaries, such as `wxc-exec.exe`, accept JSON container-creation requests"; `wxc-exec.exe --audit policy.json` exists. Node sample: `network: { egress: { default: 'deny' } }`. Network policy includes "Proxy support, outbound controls". README has a telemetry section ("Required diagnostic data and administrative controls"). — [microsoft/mxc README](https://github.com/microsoft/mxc/blob/main/README.md), [GitHub API repo metadata](https://api.github.com/repos/microsoft/mxc)
- Codex docs: `mxc` is "The recommended sandbox on compatible Windows devices. Uses native process isolation without administrator-approved setup, additional Windows accounts, or local firewall rules." `elevated` is now "the preferred legacy fallback"; `unelevated` "a legacy fallback". — [Codex Windows docs](https://developers.openai.com/codex/windows)
- "Microsoft introduced MXC process isolation in Windows 11 24H2 (build 26100.9278) and 25H2 (build 26200.9278)." Codex test command: `codex -c windows.sandbox=mxc sandbox ... -- cmd.exe /d /c echo MXC_OK`. — [Codex Windows docs](https://developers.openai.com/codex/windows)
- Codex-documented MXC limits: "MXC permits connections to and from services on host loopback" (managed networking requires `allow_local_binding = true`); "Remaining child processes stop when the foreground command exits"; deny-read globs are snapshotted at process start. — [Codex Windows docs](https://developers.openai.com/codex/windows)
- Secondary reporting (not first-party): MXC Process Isolation shipped to 24H2/25H2 in the August 2026 update KB5120998; Intune policy "coming soon". — [Windows Report](https://windowsreport.com/microsoft-execution-containers-mxc-is-now-generally-available-on-windows-11/) (secondary; KB number not verified against Microsoft)

### Inferences
- For MossCode (Python), the realistic integration is shelling out to `wxc-exec.exe` with a JSON policy, or a small Rust/Node helper; there is no Python SDK. Distributing `wxc-exec.exe` alongside a pip package needs checking (binary size, signing, telemetry consent).
- MXC's loopback permission matches MossCode's "network only via local proxy" design: deny egress, allow loopback, point HTTP(S)_PROXY at a MossCode-owned proxy.
- "Remaining child processes stop when the foreground command exits" overlaps MossCode's existing Job Object kill-tree behavior and would break MossCode background shells/dev servers unless they run as their own long-lived MXC container.
- Because the Windows backend is AppContainer, the AppContainer compatibility caveats (Q1) presumably apply, but Microsoft appears to have done the ACL/capability plumbing; Codex adopting it as default is strong evidence it works for git/shell/python workloads.

### Gaps
- Whether MXC process containers work on Windows 11 **Home** and on Insider build 26300 (the user's machine) — not stated in the sources found.
- Exact semantics of "read-only" vs "no access" (e.g., can it deny reading `~/.ssh` while allowing the rest of the profile?), registry and named-pipe handling, ConPTY/console behaviour for Git Bash/MSYS2 — not verified; read `microsoft/mxc/docs` and schemas.
- License/redistribution terms of the binary executor and the telemetry defaults — README mentions required diagnostic data; details not read.

---

## Q1. AppContainer and Less Privileged AppContainer (LPAC)

### Takeaway
Real OS boundary with built-in network denial and no admin requirement, but the default-deny model breaks arbitrary developer toolchains unless you lay down ACLs/capabilities; OpenAI explicitly rejected raw AppContainer for this reason (before MXC packaged it). LPAC is even stricter and needs ACLs on every file it touches.

### Cited Findings
- AppContainer isolates "processes and windows belonging to other applications as well as from devices, files/directories, registry keys, the network, and credentials"; AppContainers run at Low IL; tokens carry a package SID plus capability SIDs; "without the network capability, an AppContainer" cannot use the network. — [Implementing an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- File isolation: "Read-write access can be granted to specific persistent files and registry keys. Read-only access is less restricted." Network: "Granular access can be granted for Internet access, Intranet access, and acting as a server." — [AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)
- Launch steps: derive capability SIDs (`DeriveCapabilitySidsFromName`), create a profile, pass process/thread attributes (SECURITY_CAPABILITIES) to CreateProcess. "Regular AppContainers are granted access to certain system files/directories, common registry keys and COM objects, however, LPAC needs specific capabilities". — [Implementing an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- Chromium: "network checks are enforced if the token is a Low Box token and the `INTERNET_CLIENT` Capability is not present"; Chromium adds Low Box attributes to its restricted token "without granting any Capabilities" to get no-network. — [Chromium sandbox design](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/design/sandbox.md)
- LPAC: access by default only to objects marked `ALL RESTRICTED APPLICATION PACKAGES` or a specific package SID (vs `ALL APPLICATION PACKAGES`); "all locations in the filesystem and registry that the LPAC process will access during its lifetime need to have the right ACLs on them", typically laid down by the installer or `icacls`; `registryRead`, `lpacCom` etc. are named capabilities. — [Chromium sandbox design](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/design/sandbox.md)
- OpenAI on AppContainer: "a capability-based isolation model built for apps that know, up front, exactly what they need to access"; rejected because Codex "drives open-ended developer workflows: shells, Git, Python, package managers, build tools ... that made AppContainer the wrong shape for the problem." — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)
- MIC doc: "Windows ensures that processes running with a low integrity level cannot obtain access to a process which is associated with an app container." — [Mandatory Integrity Control](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control)

### Inferences
- Admin: none needed to create an AppContainer profile and launch (it is a per-user API); granting the container write access to the project dir means adding an ACE for the package SID (or a capability SID) to the project dir — a persistent ACL change, inheritable to new files.
- Read access: user-profile files (e.g. `%USERPROFILE%\.gitconfig`, node/npm caches, Python user site, MSYS2 home) are not readable by AppContainers by default, so Git Bash/node/pip will misbehave unless ACLs are added — this is the "wrong shape" OpenAI described. Conversely this default-deny is exactly what protects `~/.ssh` and MossCode's config.
- Loopback: AppContainers are commonly blocked from loopback by default (CheckNetIsolation loopback exemptions); this would break a local-proxy design unless an exemption/capability is configured. MXC explicitly allows loopback, suggesting it handles this.
- From Python: doable with ctypes (CreateAppContainerProfile, DeriveAppContainerSidFromAppContainerName, InitializeProcThreadAttributeList + PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, CreateProcessW). `subprocess.Popen` cannot pass arbitrary attribute lists, so pipes/ConPTY must be wired manually.

### Gaps
- No first-party source found on AppContainer + MSYS2/Cygwin runtime compatibility (Cygwin's fork emulation, shared memory, `/tmp` mapping) or ConPTY inside an AppContainer.
- Loopback default for desktop (non-UWP) AppContainers not verified against a Microsoft page in this session.

---

## Q2. Restricted tokens (CreateRestrictedToken, deny-only SIDs, restricting SIDs, WRITE_RESTRICTED) and low integrity (MIC)

### Takeaway
A WRITE_RESTRICTED token plus a synthetic SID ACL'd onto writable roots gives precise write confinement with no admin rights (Codex "unelevated" mode), but it cannot restrict reads or network; low IL is simpler but turns writable roots into a sink for every low-IL process on the machine.

### Cited Findings
- CreateRestrictedToken supports disabled (deny-only) SIDs, deleted privileges and restricting SIDs; with restricting SIDs "The system performs two access checks ... Access is granted only if both access checks allow"; flag `WRITE_RESTRICTED`: "restricting SIDs that are considered only when evaluating write access"; other flags `DISABLE_MAX_PRIVILEGE`, `SANDBOX_INERT`, `LUA_TOKEN`. Deny-only "absence of a deny-only SID does not allow access". — [CreateRestrictedToken](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-createrestrictedtoken)
- Codex unelevated design: synthetic SID `sandbox-write` granted write/execute/delete on cwd and `writable_roots`; explicitly denied write on `<cwd>/.git`, `<cwd>/.codex`, `<cwd>/.agents`; commands run under a write-restricted token with restricted SIDs `[Everyone, logon session SID, sandbox-write]`. — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)
- Codex source uses `CreateRestrictedToken` with `DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED`, sets `TokenDefaultDacl`, and per-workspace capability SIDs ("Returns the workspace-specific capability SID for `cwd`, creating and persisting it if missing"). — [codex token.rs](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/token.rs), [codex cap.rs](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/cap.rs)
- Unelevated tradeoffs per OpenAI: ACL application can be slow on large workspaces; real ACLs on the user's filesystem; semantics hard to change; network protection "advisory" (env-var proxy poisoning: `HTTPS_PROXY=http://127.0.0.1:9`, `ALL_PROXY`, `GIT_HTTPS_PROXY`, etc.). — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)
- Codex docs: `unelevated` "doesn't support denied read paths" and has "weaker network isolation". — [Codex Windows docs](https://developers.openai.com/codex/windows)
- Firewall cannot target restricted tokens: "Windows doesn't allow matching a firewall rule to the non-principal identity of a restricted token." — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)
- Chromium's strictest token: Logon SID mandatory, all other SIDs deny-only, restricting SID `S-1-0-0` (NULL), no privileges, Untrusted IL; such a token can access almost nothing, so Chromium brokers handles. The token cannot protect FAT/FAT32 volumes (null security) or "some unlabelled objects". — [Chromium sandbox design](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/design/sandbox.md)
- MIC: low IL "cannot write to an object with a medium integrity level, even if that object's DACL allows"; unlabeled objects are treated as medium; default policy is NO_WRITE_UP (reads allowed). — [Mandatory Integrity Control](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control)
- Low IL can still: read most files, write `AppData\LocalLow` and `HKCU\Software\AppDataLow`, use TCP/IP sockets, RPC, named pipes with LI labels. — [Chromium sandbox design](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/design/sandbox.md)
- OpenAI rejected MIC: relabeling the workspace low-integrity means "low-integrity processes in general can write there", turning "the user's actual checkout into a low-integrity sink for the host". — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)

### Inferences
- For MossCode, this is the lowest-effort *no-admin* option for write confinement: ctypes CreateRestrictedToken + CreateProcessAsUserW (allowed for a restricted version of your own token without SeAssignPrimaryTokenPrivilege) + one-time ACE grants for a synthetic SID on project/temp dirs. ACEs persist and need cleanup/refresh logic.
- It does nothing for secret *reads* (same user principal can read `~/.ssh`) or network; secrets protection would require deny ACEs on the real user, which would also hit the real user and is risky. Parent-env leakage is solved separately by passing a scrubbed environment block (independent of the sandbox).
- Temp dirs: the default `%TEMP%` is under the user profile; give the sandbox a per-session temp dir inside an ACL'd location and set TEMP/TMP.

### Gaps
- No source on MSYS2/Git Bash behaviour under WRITE_RESTRICTED tokens (e.g., writes to `/tmp`, `~/.bash_history`, MSYS2 shared memory); Codex runs PowerShell primarily.

---

## Q3. Dedicated low-privilege local user accounts (+ workspace ACLs) and WFP/firewall per user

### Takeaway
Running as a separate local user is what makes per-command firewall/WFP blocking and deny-read possible; Codex "elevated" mode does exactly this but needs a UAC-elevated one-time setup, user creation (blocked on some enterprise devices), DPAPI-stored passwords, and read ACLs granted on the real user's profile.

### Cited Findings
- Codex elevated: still a write-restricted token, but principal is one of two local users `CodexSandboxOffline` (targeted by firewall rules) / `CodexSandboxOnline`; setup creates users, stores credentials with DPAPI "in a place where the sandbox users cannot actually read", creates firewall rules blocking all outbound for the offline user. — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)
- Read access problem: other users cannot read the real user's profile by default, so setup grants read ACLs to `C:\Users\<real-user>`, `C:\Windows`, `Program Files`, `Program Files (x86)`, `ProgramData`, asynchronously because it's expensive. — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)
- Spawn flow: `LogonUserW` + `CreateProcessAsUserW` from the real user hit a "privilege wall"; instead `codex.exe` uses `CreateProcessWithLogonW` to launch `codex-command-runner.exe` as the sandbox user, which builds the restricted token and spawns the child. Separate `codex-windows-sandbox-setup.exe` handles elevated setup. — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)
- Codex source adds persistent WFP filters scoped with `FWPM_CONDITION_ALE_USER_ID` under its own provider/sublayer (installed by the elevated setup helper), blocking e.g. ICMP, DNS 53/853 and SMB 445/139 at `FWPM_LAYER_ALE_AUTH_CONNECT_V4/V6` and `ALE_RESOURCE_ASSIGNMENT`. — [codex wfp.rs](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/wfp.rs), [codex filter_specs.rs](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/wfp/filter_specs.rs)
- Deny-read in elevated mode: deny-read ACEs applied to explicit paths for the sandbox principal; missing paths are pre-created so the sandbox cannot create-then-read; reparse targets are also covered; roots rejected; ACEs revoked on failure. — [codex deny_read_acl.rs](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/deny_read_acl.rs)
- Codex also hides sandbox users' profile dirs (HIDDEN|SYSTEM) and uses a private desktop by default in both legacy modes. — [codex hide_users.rs](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/hide_users.rs), [Codex Windows docs](https://developers.openai.com/codex/windows)
- Common setup failures: declined UAC, enterprise policy blocking local user/group creation, firewall config, "sandbox-user logon rights"; IT can pre-provision with `codex sandbox setup --elevated --user ...`. — [Codex Windows docs](https://developers.openai.com/codex/windows)
- Firewall without separate principal is the wrong shape: program-path rules block all `python.exe`, user rules hit the real user, port rules wrong policy. — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)

### Inferences
- Works on Home edition (local users and Windows Firewall/WFP exist on all editions) — inference, not verified in a source.
- A WFP filter keyed on the sandbox user's SID could *permit* loopback to a MossCode proxy while blocking everything else — this is the cleanest native "network only via local proxy" mechanism, but requires admin to install.
- Running as another user changes `%USERPROFILE%`, HKCU, git global config, credential helpers, Python user-site, npm caches; tools may create a fresh profile on first logon. Git also has `safe.directory` ownership checks that fire when a different user operates on a repo owned by the real user (inference from git behaviour; not sourced here).
- Implementation from Python: significant (helper runner exe or Python runner launched via CreateProcessWithLogonW, password management with DPAPI via `win32crypt`, elevated setup script, WFP via `fwpuclnt.dll` ctypes or `New-NetFirewallRule -LocalUser` in an elevated PowerShell). Very high effort; Codex's crate is ~100 files.

### Gaps
- Whether `New-NetFirewallRule -LocalUser` (Windows Firewall, SDDL-based) alone is sufficient versus raw WFP; Codex uses both but reasons for the raw WFP additions were not documented.

---

## Q4. Job Objects

### Takeaway
Job Objects are resource/UI/lifetime controls, not an access-control boundary: they cannot restrict file, registry, or network access (only bandwidth shaping), but they are a required layer (kill-tree, active-process limits, UI restrictions) under any of the other options.

### Cited Findings
- Jobs enforce limits such as working set, priority, end-of-job time; children created with CreateProcess join the job, but "Child processes created using Win32_Process.Create are not associated with the job"; breakaway via `JOB_OBJECT_LIMIT_BREAKAWAY_OK` / `SILENT_BREAKAWAY_OK`; nested jobs since Windows 8; "Security limits must be set individually for each process". — [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- `JOBOBJECT_NET_RATE_CONTROL_INFORMATION`: `MaxBandwidth` for outgoing traffic and a `DscpTag`; can be set on only one job in a nested hierarchy; Windows 10+. No blocking/allow-list semantics. — [JOBOBJECT_NET_RATE_CONTROL_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_net_rate_control_information)
- Chromium job restrictions: no SystemParametersInfo changes, no desktop creation/switch, no display changes, no clipboard, no broadcasts, no global hooks, no global atoms, no USER handles outside job, active-process limit; could also limit CPU/memory/IO. — [Chromium sandbox design](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/design/sandbox.md)
- Windows sharing the same desktop are "effectively in the same security context" (shatter attacks); Chromium uses an alternate desktop (~4MB). — [Chromium sandbox design](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/design/sandbox.md)

### Inferences
- MossCode should add `JOB_OBJECT_UILIMIT_*` restrictions (clipboard, globalatoms, handles, desktop) and deny breakaway to its existing job; cheap via ctypes, no admin.
- The WMI escape (`Win32_Process.Create`) means a job alone cannot guarantee process containment; a token/user boundary is needed too.

### Gaps
- None material.

---

## Q5. Windows Sandbox (Hyper-V, .wsb, `wsb` CLI)

### Takeaway
Strongest isolation, but unusable for MossCode's main user: not available on Home, only one instance at a time, fresh image without host tools, and the CLI's `exec` returns no stdout/stderr.

### Cited Findings
- Hypervisor-isolated disposable VM; "Host-installed software isn't available in the sandbox"; networking enabled by default; "doesn't allow multiple instances to run simultaneously"; supported on Pro/Enterprise/Education; "not supported on Windows Home edition". — [Windows Sandbox overview](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-overview)
- `.wsb` config: `<Networking>Disable</Networking>`, `<MappedFolders>` with read-only option, `<LogonCommand>`, vGPU toggle. — [Configure using .wsb](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)
- CLI (Windows 11 24H2+): `wsb start --config`, `list`, `exec`, `share --allow-write`, `connect`, `ip`, `stop`. For `exec`: "there is no support for process I/O meaning that there is no way to retrieve the output"; `ExistingLogin` requires an active session established via `connect`. — [Windows Sandbox CLI](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli)
- OpenAI rejected it: Codex must act on the user's real checkout/tools, would need host/guest bridging, and "Windows Sandbox isn't even available on Windows Home SKUs". — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)

### Inferences
- Getting output would require a side channel (shared folder files, or a network listener reachable via `wsb ip`), plus installing git/python/node in each fresh instance — impractical for per-command use.

### Gaps
- None material.

---

## Q6. WSL2 (Linux + bubblewrap)

### Takeaway
Gives Linux-grade sandboxing (bubblewrap/seccomp) and per-VM firewalling, but commands run against Linux tools, not the user's Windows Git Bash/PowerShell toolchain; cross-filesystem access to the Windows checkout is via /mnt/c with known performance and permission costs. Good as an optional mode, not as the default for a Windows-native agent.

### Cited Findings
- Codex recommends the native Windows sandbox by default and WSL "when you need Linux-native tooling, your workflow already lives in WSL2, or the available native Windows implementations don't meet your needs." — [Codex Windows docs](https://developers.openai.com/codex/windows)
- MXC offers a WSL container backend (WSLc) for "Linux-first agent toolchains"; MXC's Linux process container uses Bubblewrap. — [Windows Developer Blog](https://blogs.windows.com/windowsdeveloper/2026/10/07/microsoft-execution-containers-policy-driven-containment-for-ai-agents/)
- WSL defaults to NAT networking; mirrored mode (Win11 22H2+) lets host and WSL reach each other via localhost. — [WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)
- Hyper-V firewall (Win11 22H2+) filters inbound/outbound traffic for WSL (VMCreatorId `{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}`). — [Hyper-V firewall](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/hyper-v-firewall)

### Inferences
- WSL is available on Home; installing it needs admin once. Inside WSL, MossCode could reuse its Linux bubblewrap implementation (if one exists) unchanged.
- Windows drives mounted at /mnt/c are fully visible inside WSL by default (including `C:\Users\<user>\.ssh`), so bubblewrap must hide them explicitly.

### Gaps
- No first-party numbers on /mnt/c I/O overhead were collected in this session.

---

## Q7. Windows containers / Hyper-V isolated containers

### Takeaway
Wrong shape: requires Docker/containerd plus Windows container images (Server Core/Nano), separate filesystem, and Hyper-V on Pro+; host tools and the real checkout need bind mounts. Not suitable for interactive per-command use.

### Cited Findings
- Two modes: process isolation (namespaces, resource controls, shared kernel) and Hyper-V isolation (each container "runs inside of a highly optimized virtual machine and effectively gets its own kernel"), selected via `docker run --isolation=hyperv|process` with images like `mcr.microsoft.com/windows/servercore`. — [Isolation modes](https://learn.microsoft.com/en-us/virtualization/windowscontainers/manage-containers/hyperv-container)

### Inferences
- Startup cost (seconds) per command and the need for a container runtime make this unsuitable as a default. Process-isolated Windows containers on client SKUs are restricted (not verified here).

### Gaps
- Edition requirements for Windows containers on Windows 11 (Home vs Pro) were not verified from a first-party page in this session.

---

## Q8. Win32 App Isolation (MSIX-based)

### Takeaway
Designed for packaging a *specific app* into an AppContainer-like sandbox with declared capabilities; preview status, requires MSIX packaging with Visual Studio. Not a mechanism for sandboxing arbitrary child commands. MXC is effectively Microsoft's agent-oriented successor for that use case.

### Cited Findings
- Win32 app isolation "provides a sandbox environment that can be integrated into Win32 apps"; "This feature is in preview"; requires Windows 11 24H2 (build 26100)+, Visual Studio 17.10.2+ packaging; Application Capability Profiler helps identify capabilities. — [Win32 app isolation overview](https://learn.microsoft.com/en-us/windows/win32/secauthz/app-isolation-overview)
- Codex's sandbox crate now contains `app_package.rs` / `package_identity.rs` ("Preserve the caller's OS-assigned package identity for sandboxed descendants"; "Registered runners still require OS package identity and the exact staged runner image"). — [codex app_package.rs](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/app_package.rs)

### Inferences
- Codex's package-identity code is likely tied to its MXC/packaged-runner path; MossCode (a pip-installed Python app) has no package identity, which is another reason to call MXC's executor rather than reimplement this.

### Gaps
- Exact role of package identity in Codex's MXC path not confirmed.

---

## Q9. Third-party / open-source references (Sandboxie-Plus, Python libs)

### Takeaway
Sandboxie-Plus is a kernel-driver-based virtualization sandbox requiring driver installation (admin) with a custom/GPL-family licence situation; not embeddable in an MIT project. No mature Python library for Windows sandboxing was found.

### Cited Findings
- Sandboxie "creates a secure operating environment in which applications can be run or installed without permanently modifying local & mapped drives or the Windows registry"; ships a driver and service; Plus uses a custom licence, Classic a separate licence. — [Sandboxie-Plus README](https://github.com/sandboxie-plus/Sandboxie/blob/master/README.md)
- Chromium's sandbox is a broker/target design relying on restricted token + job + alternate desktop + integrity levels, plus process mitigation policies; "there is no practical way to prevent code in the sandbox from calling a system service", and AV DLL injection adds attack surface. — [Chromium sandbox design](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/design/sandbox.md)

### Inferences
- Sandboxie Classic is GPLv3 (from memory of the project; badge shows a "Classic license" without naming it here) — violates MossCode's no-GPL rule; do not depend on it.

### Gaps
- No Python library offering AppContainer/restricted-token process launch was identified (search budget exhausted); assume ctypes/pywin32 hand-rolled code.

---

## Q10. Suitability ranking for MossCode

### Takeaway
Recommended path: (1) MXC process container via `wxc-exec.exe` when available; (2) fallback = restricted-token (WRITE_RESTRICTED + synthetic-SID ACLs, scrubbed env, hardened Job Object, private desktop) with env-based proxy steering and honest "network is advisory" labelling; (3) optional admin-setup mode with a dedicated user + WFP filters for real network/secret-read enforcement. Windows Sandbox, Windows containers and Win32 App Isolation are unsuitable.

### Cited Findings
- Codex's own ordering: "Use MXC when the device and policy support it. Otherwise, prefer `elevated`, with `unelevated` as a secondary fallback." — [Codex Windows docs](https://developers.openai.com/codex/windows)
- Codex positions security for a coding agent as balancing "compatibility with agentic workloads against real enforcement". — [OpenAI engineering blog](https://openai.com/index/building-codex-windows-sandbox/)

### Inferences (rating: write confinement / read-secret protection / network / admin / Home / effort)
| Option | Writes | Secret reads | Network | Admin | Home | Effort from Python | Fit |
|---|---|---|---|---|---|---|---|
| MXC process container | yes | yes (policy) | deny egress, loopback allowed | no | unverified | low-medium (spawn `wxc-exec.exe` JSON) | **Best, if available** |
| AppContainer (raw) | yes | yes (default-deny) | yes (no capability) | no | yes | high (ACL plumbing, ctypes attribute lists, loopback) | Medium; MXC packages it better |
| LPAC | yes | yes | yes | no | yes | very high | Poor |
| Restricted token (WRITE_RESTRICTED) | yes | **no** | **no** (env only) | no | yes | medium | Good fallback |
| Low IL / MIC | partly | no | no | no | yes | low | Poor (sink problem) |
| Dedicated user + ACL + WFP | yes | yes (deny ACEs) | yes (WFP per user SID; can allow loopback proxy) | yes, one-time | yes (inferred) | very high | Opt-in strict mode |
| Job Object | no | no | bandwidth only | no | yes | done | Required layer, not a boundary |
| WFP / Firewall alone | no | no | only per user/program | yes | yes | medium | Only combined with separate user |
| Windows Sandbox | yes (VM) | yes | yes | feature enable | **no** | high; no exec output | Unsuitable |
| WSL2 + bubblewrap | yes | yes (if /mnt hidden) | yes | install once | yes | medium (reuse Linux code) | Optional Linux mode |
| Windows/Hyper-V containers | yes | yes | yes | yes | unverified | high | Unsuitable |
| Win32 App Isolation | — | — | — | — | — | n/a (MSIX app packaging) | Unsuitable |

- Regardless of option, MossCode should independently: pass a scrubbed environment block (no API keys/parent secrets), set per-command TEMP/TMP, keep the Job Object kill-tree, and treat env-var proxy steering as advisory unless backed by WFP or MXC egress policy.

### Gaps
- Must verify MXC availability on the user's actual machine (Windows 11 Home, build 26300) by running the MXC check; if unavailable on Home, the restricted-token fallback becomes the default.
