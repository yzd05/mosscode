# Cloud / remote sandbox platforms and isolation technologies for AI agent code execution

Research date: 2026-11-05. All pages were read on that date. Where a page shows a "last updated" date it is given in the text. Every claim links to a first-hand source (vendor docs, vendor repo or vendor changelog). Where only a third-party source was found, the claim goes under Gaps.

---

## 1. Isolation technologies: Linux containers, gVisor, Firecracker, Kata Containers, Cloud Hypervisor

### Takeaway
There are three levels of isolation. (1) Containers (namespaces + cgroups + seccomp) share the host kernel. They are cheap, but every kernel bug is a potential escape. (2) gVisor puts a user-space kernel (written in Go) between the workload and the host kernel. The process model stays, but syscall-heavy workloads cost more CPU. (3) MicroVMs (Firecracker, Cloud Hypervisor, Kata, which wraps a VMM) give each workload its own guest kernel behind KVM. They boot in about 125 ms with under 5 MiB of VMM overhead. Every commercial agent-sandbox platform reviewed here uses level 2 or level 3 for untrusted code. None relies on plain containers alone.

### Cited Findings
**Linux containers (namespaces + cgroups + seccomp)**
- Docker's security model has four areas: "the intrinsic security of the kernel and its support for namespaces and cgroups", the daemon's attack surface, container configuration loopholes, and kernel hardening features. Containers share the host kernel. — [Docker Engine security](https://docs.docker.com/engine/security/)
- Docker's default seccomp profile "disables around 44 system calls out of 300+" and "is moderately protective while providing wide application compatibility". It is an allowlist with `defaultAction` `SCMP_ACT_ERRNO`. — [Docker seccomp docs](https://docs.docker.com/engine/security/seccomp/)
- Docker compares the options itself. A "container with socket mount" gives "Partial (namespaces)" isolation. Docker-in-Docker gives "Partial (privileged)". microVM sandboxes give "Full (hypervisor)". Host execution gives "None". — [Docker Sandboxes architecture](https://docs.docker.com/ai/sandboxes/architecture/)

**gVisor (runsc)**
- gVisor is "an application kernel that implements a Linux-like interface". It is "written in a memory-safe language (Go) and runs in userspace". It ships an OCI runtime called `runsc` that works with Docker and Kubernetes. — [gVisor: What is gVisor?](https://gvisor.dev/docs/)
- Positioning: gVisor is not a syscall filter like seccomp and not a VM. It "intercepts application system calls and acts as the guest kernel, without the need for translation through virtualized hardware" ("seccomp on steroids"). The cost is "reduced application compatibility and higher per-system call overhead". — [gVisor docs](https://gvisor.dev/docs/)
- Security model: the Sentry implements the System API, so applications cannot pass crafted arguments directly to the host kernel. The Sentry itself may only use a "restricted set" of host syscalls (defense in depth). gVisor "does not provide protection against hardware side channels". — [gVisor Security Model](https://gvisor.dev/docs/architecture_guide/security/)
- Platforms: KVM ("best performance" on bare metal) and systrap, which uses `SECCOMP_RET_TRAP`, has been the default since mid-2023, and is better inside VMs or without virtualization support. ptrace is deprecated. — [gVisor Platform Guide](https://gvisor.dev/docs/architecture_guide/platforms/)
- Performance: raw memory access has no extra cost. A fixed memory overhead comes from the Sentry. Syscalls traverse extra layers. The network stack is "less CPU efficient". File operations route through the Gofer, and a VFS implementation that "needs improvement" dominates the cost. — [gVisor Performance Guide](https://gvisor.dev/docs/architecture_guide/performance/)

**Firecracker**
- Apache-2.0. Written in Rust. Runs in user space on KVM. Has "only 5 emulated devices". Boots in "<125ms". Can create "up to 150 microVMs per second per host". Uses "<5 MiB overhead per VM". A companion "jailer" adds Linux user-space barriers as "a second line of defense in case the virtualization barrier is ever compromised". Listed users include E2B and Fly.io. — [Firecracker homepage](https://firecracker-microvm.github.io/)
- New in 2026: AWS markets "Lambda MicroVMs", a managed Firecracker sandbox "purpose-built for AI agents". It keeps memory and disk state "for up to 8 hours", suspends and resumes automatically, and bills per second. — [Firecracker homepage](https://firecracker-microvm.github.io/)

**Kata Containers**
- An OCI/CRI-compatible runtime (shimv2) that runs containers inside lightweight VMs, "using hardware virtualization technology as a second layer of defence". It supports QEMU, Cloud Hypervisor and Firecracker. Apache-2.0, stewarded by the OpenInfra Foundation. — [katacontainers.io](https://katacontainers.io/); [Kata architecture doc](https://github.com/kata-containers/kata-containers/blob/main/docs/design/architecture/README.md)
- The workload is isolated from the guest VM by a container environment, and the guest VM is isolated from the host. — [Kata architecture doc](https://github.com/kata-containers/kata-containers/blob/main/docs/design/architecture/README.md)

**Cloud Hypervisor**
- A Rust VMM built on rust-vmm crates. It runs on KVM or the Microsoft Hypervisor (MSHV). Goals: "minimal emulation", "low latency", "small attack surface", 64-bit only, CPU/memory/PCI hotplug and live migration. It mainly uses paravirtualised virtio devices. — [Cloud Hypervisor README](https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/README.md)

### Inferences
- Protection, roughly. Containers stop accidental damage and cross-tenant file access. They do not stop a kernel 0-day. gVisor stops most host-kernel syscall exploits because the attacker must first compromise the Sentry and then a narrower host interface. MicroVMs stop guest-kernel compromise from reaching the host unless a VMM or KVM bug is also present. None of the three stops data exfiltration through allowed network paths. That needs an egress policy (section 3).
- None of these technologies runs natively on Windows hosts. Firecracker, gVisor and Kata need Linux with KVM. Cloud Hypervisor's MSHV support targets Linux-on-Hyper-V setups. So for MossCode on Windows 11, these technologies are only available through WSL2 or Hyper-V, through Docker Desktop or Docker Sandboxes, or through a remote backend.

### Gaps
- I found no current first-hand startup-time number for gVisor. The performance page shows a chart but no text figure in the extracted content. I found none for Kata either.
- I did not find whether Docker Sandboxes uses Hyper-V, WSL2 or another hypervisor on Windows. The architecture page confirms microVMs and virtiofs on "all operating systems" but the extracted text does not name the Windows hypervisor.

---

## 2. Sandbox platforms (APIs you can call): E2B, Daytona, Modal, Cloudflare, Vercel, Fly Machines, Docker Sandboxes

### Takeaway
All of these offer "create sandbox → exec command with streamed stdout/stderr → read/write files → destroy or pause". Most have a Python SDK. Isolation is Firecracker (E2B, Vercel, Cloudflare Containers, Fly), gVisor or a VM (Modal), or a choice between container and VM (Daytona). Egress control is now standard: CIDR and domain allowlists, often SNI-based. The most advanced platforms (E2B, Cloudflare, Docker, plus the hosted agents in section 3) keep secrets outside the sandbox and inject them in a proxy.

### Cited Findings
**E2B**
- Python (`pip install e2b`) and JS SDKs. `Sandbox.create()` then `sandbox.commands.run(...)` returns stdout. Code Interpreter and Desktop SDKs are also available. — [E2B SDK README](https://github.com/e2b-dev/E2B/blob/main/README.md)
- Runtime is open source under Apache-2.0 (Go) as `e2b-dev/runtime`. It includes the control-plane API, the per-node orchestrator driving Firecracker, the in-VM agent `envd`, the edge router and the template builder. "E2B Embed" runs the whole stack on one Linux host with KVM. — [e2b-dev/runtime README](https://github.com/e2b-dev/runtime)
- How it works: "A sandbox is a resumed snapshot". Templates are pre-booted VMs restored lazily through `userfaultfd` with a CoW rootfs. Each sandbox is one Firecracker microVM with its own cgroup and network namespace, "a per-sandbox nftables egress firewall and SNI/Host-inspecting domain allow and deny lists". Other features: pause/resume, forking up to 100 per request, `envd` exposing "processes, PTYs, filesystem operations, file watchers, and port forwarding", and public URLs `https://<port>-<sandbox>.<domain>`. "Secrets are metadata-only on the control plane". Workload identity issues short-lived tokens. — [e2b-dev/runtime README](https://github.com/e2b-dev/runtime)
- Streaming: `on_stdout` / `on_stderr` callbacks on `commands.run()`. Keepalive pings every 50 s. — [E2B streaming docs](https://e2b.dev/docs/commands/streaming)
- Files: `sbx.files.write(path, data)`, `files.write_files()` for batches, and download APIs. Data can also be baked into a custom template. — [E2B upload/download](https://e2b.dev/docs/quickstart/upload-download-files.md)
- Network: internet is on by default and can be turned off with `allow_internet_access=False`. `network={"deny_out": [...], "allow_out": [...]}` accepts IP, CIDR or domain. Domain allowlisting requires denying all other traffic. Domains are not supported in deny lists. — [E2B internet access](https://e2b.dev/docs/sandbox/internet-access.md)
- The docs index lists Secrets ("Injecting secrets into requests"), a PTY, SSH access, volumes, bring-your-own-proxy and guides for running Claude Code and Amp inside E2B. — [E2B docs index](https://e2b.dev/docs/llms.txt)
- Lifetime: up to 24 h continuous on Pro and 1 h on Hobby. Pausing resets the window and keeps state "indefinitely". — [E2B sandbox lifecycle](https://e2b.dev/docs/sandbox)
- Pricing: Hobby is free with a one-time $100 credit, 1 h sessions and 20 concurrent sandboxes. Pro is $150/month plus usage, 24 h sessions and 100 concurrent (expandable to 1,100). Usage is billed per second of running sandbox. Enterprise adds BYOC. — [E2B pricing](https://e2b.dev/pricing)

**Daytona**
- The public repo is archived: "As of June 2026, Daytona's core development has moved to a private codebase". The last public code stays usable under the license at tag v0.190.0. — [daytonaio/daytona README](https://github.com/daytonaio/daytona/blob/main/README.md)
- Sandboxes have "a dedicated kernel, filesystem, network stack, and allocated vCPU, RAM, and disk". SDKs cover Python, TypeScript, Ruby, Go and Java, plus a CLI and REST API. — [Daytona Sandboxes](https://www.daytona.io/docs/en/sandboxes/)
- Isolation classes: **Container** ("dedicated namespaces and enforced resource limits. Code runs as root inside the sandbox"), **VM sandboxes** (Linux VM and Windows, "full virtual machine with its own kernel", which enables pause/resume, fork and hot snapshots), and GPU (a container with exclusive GPUs). — [Daytona isolation](https://www.daytona.io/docs/en/isolation.md)
- Firewall options: `networkAllowList` (IPv4 CIDR), `domainAllowList` (with wildcards), `networkBlockAll` and `outboundProxyUrl`. Tier 1–2 organizations get restricted access that sandboxes cannot override. Tier 3–4 get full access by default, and a sandbox allowlist replaces it strictly. — [Daytona network limits](https://www.daytona.io/docs/en/network-limits/)
- Pricing: vCPU $0.0504/h, memory $0.0162/GiB-h, storage $0.000108/GiB-h after 5 GiB free, Windows $0.0858/vCPU/h. — [Daytona pricing](https://www.daytona.io/pricing)

**Modal Sandboxes**
- Two runtimes. **gVisor** ("strong isolation and is suitable for most workloads") and **VM** ("its own Linux kernel", for Docker-in-sandbox, FUSE or nested cgroups). Selected with `runtime="vm"|"gvisor"`. — [Modal Sandboxes guide](https://modal.com/docs/guide/sandbox)
- API: `modal.Sandbox.create(app=...)`, then `p = sb.exec("bash","-c",...)` and iterate `p.stdout` to stream. SDKs for Python, JS and Go. Default lifetime is 5 minutes, configurable up to 24 hours. — [Modal Sandboxes guide](https://modal.com/docs/guide/sandbox)
- Network: outbound to any public IP by default. Restrictions are `block_network=True`, `outbound_cidr_allowlist` (any protocol) and `outbound_domain_allowlist` (Beta, TLS port 443 only). Inbound is restricted with `inbound_cidr_allowlist` on encrypted tunnels. "Sidecars" and "Secret injection" are Beta features listed in the nav. — [Modal Sandbox networking](https://modal.com/docs/guide/sandbox-networking)
- "Each container runs in its own sandbox, isolated from the host, using primitives like gVisor or microVMs." — [Modal Security](https://modal.com/docs/guide/security)
- The client library `modal-labs/modal-client` is Apache-2.0. — [GitHub API: modal-labs/modal-client](https://api.github.com/repos/modal-labs/modal-client)
- Pricing is per second of CPU and GPU. — [Modal pricing](https://modal.com/pricing)

**Cloudflare Sandboxes / Containers** (overview last updated Sep 30, 2026)
- Two environments. **Containers**: "Each instance is a microVM with its own kernel and network". It starts from an image, `container.exec([...])`, `enableInternet: false`. **Dynamic Workers**: V8-isolate JS/Python/Wasm with `globalOutbound: null`. Requires Workers Paid. — [Cloudflare Sandboxes overview](https://developers.cloudflare.com/sandbox/)
- "Each container instance runs in a Firecracker microVM with its own kernel and network. No other workload on the Cloudflare network shares that kernel." — [Cloudflare Containers architecture](https://developers.cloudflare.com/containers/platform-details/architecture/)
- Security guidance (updated Oct 22, 2026): every process in a container effectively has root capabilities, so "the sandbox is therefore the smallest unit of trust". A token placed in a git clone URL ends up in `.git/config`, where test dependencies can read it. "Credentials are safer outside the sandbox": an outbound handler in the Worker adds the credential after the request leaves. Containers start with `enableInternet: false` by default. — [Cloudflare Sandbox security](https://developers.cloudflare.com/sandbox/concepts/security/)
- Command output can be streamed to a browser as SSE. — [Cloudflare stream command output](https://developers.cloudflare.com/sandbox/commands/stream-command-output/index.md)
- Coding-agent pattern: "Your Worker starts the sandbox, holds the credentials, and decides which hostnames the sandbox can reach". The Worker adds the model API token to each request. There are guides for Claude Code, Codex, Devin, Cursor and OpenCode. — [Cloudflare coding agents](https://developers.cloudflare.com/sandbox/coding-agents/index.md)
- Pricing (updated Oct 5, 2026): billed every 10 ms of activity. Memory $0.0000025/GiB-s, CPU $0.000020/vCPU-s (active), disk $0.00000007/GB-s, after the inclusions in the $5/month Workers Paid plan. Instance types range from lite (1/16 vCPU, 256 MiB) to standard-4 (4 vCPU, 12 GiB). — [Cloudflare Containers pricing](https://developers.cloudflare.com/containers/pricing/)
- Client SDK is TypeScript only (`@cloudflare/sandbox`) and is called from a Worker. — [Cloudflare Sandbox architecture (0.x)](https://developers.cloudflare.com/sandbox/concepts/architecture/)

**Vercel Sandbox**
- "Each sandbox runs in a secure Firecracker microVM with its own filesystem and network". SDKs: `@vercel/sandbox` (JS) and `vercel.sandbox` (Python), plus a `sandbox` CLI. Authentication uses OIDC tokens or access tokens. — [Vercel Sandbox docs](https://vercel.com/docs/vercel-sandbox)
- Pricing is "Active CPU": $0.128/CPU-h, $0.0212/GB-h provisioned memory, $0.60 per 1M creations. Idle time spent waiting on LLM calls is not charged as CPU. Default timeout is 5 min and can be extended. The maximum duration applies per session and resets on stop/resume. — [Vercel Sandbox pricing](https://vercel.com/docs/vercel-sandbox/pricing)
- `vercel/sandbox` repo is Apache-2.0. — [GitHub API: vercel/sandbox](https://api.github.com/repos/vercel/sandbox)

**Fly Machines**
- "Fast-launching VMs" that "can be started and stopped at subsecond speeds", controlled by REST API or flyctl. — [Fly Machines docs](https://fly.io/docs/machines/)
- Fly.io is listed as a Firecracker user. — [Firecracker homepage](https://firecracker-microvm.github.io/)

**Docker Sandboxes (`sbx`)**
- "Docker Sandboxes run AI coding agents in isolated environments on your machine or on Docker-managed cloud infrastructure". The CLI and local compute are free, including for commercial use. Cloud compute is pay-as-you-go. Org-wide network, filesystem and MCP policies are a paid add-on. — [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)
- Architecture: each sandbox is a microVM with its own Docker daemon. Workspaces are either a direct read-write mount (virtiofs, cached on "all operating systems"), `--clone` mode (repo mounted read-only at `/run/sandbox/source`, agent works on a private clone), or mountless. Credentials: the agent sees a "sentinel value", and a host-side proxy replaces it with the real credential after the request leaves the microVM. Outbound traffic must pass a network policy. — [Docker Sandboxes architecture](https://docs.docker.com/ai/sandboxes/architecture/); [Docker Sandboxes security](https://docs.docker.com/ai/sandboxes/security/)

### Inferences
- For MossCode, E2B is the closest fit for a remote backend. It has a mature Python SDK with streaming exec, a PTY, files, egress allow/deny and pause/resume, and an Apache-2.0 self-hostable runtime so there is no lock-in. Modal and Vercel also have Python SDKs. Cloudflare needs a TypeScript Worker in the middle, so it does not suit a pure-Python CLI.
- Docker Sandboxes is the most relevant local option for Windows users. It already solves microVM isolation, workspace mounting/clone mode and credential injection on the developer's own machine, so MossCode could support it as an optional backend instead of building VM isolation itself.
- Daytona's move to closed source (June 2026) makes it a weaker choice than E2B as a self-hostable backend.

### Gaps
- Fly Machines' exec API (endpoint name, streaming support) could not be extracted from the API page in this session.
- I could not confirm the license of the Cloudflare Sandbox SDK: GitHub reports `NOASSERTION` for `cloudflare/sandbox-sdk`. Same for Daytona's archived license (the README links to LICENSE at v0.190.0; the file was not read).
- I did not verify how Daytona and Modal handle secrets in detail: whether they inject env vars or use a proxy.

---

## 3. How hosted coding agents run: OpenAI Codex Cloud, Claude Code cloud sessions, GitHub Copilot, Devin, Jules

### Takeaway
All hosted agents follow the same design: one ephemeral, isolated Linux VM or container per task. The repo is cloned in, and a setup script runs with internet access. The agent phase runs under a restricted egress allowlist. Results come out as a branch or PR, never as a direct write to the user's machine. The 2026 trend is to keep secrets out of the sandbox entirely: the agent sees a placeholder, and a proxy substitutes the real credential only for allowed HTTPS destinations. Codex, Claude Code and Docker do this. GitHub uses the same pattern for its local sandbox.

### Cited Findings
**OpenAI Codex Cloud**
- Current docs: each task gets "its own isolated workspace from the published environment". Environment variables are passed directly to programs. A **network secret** works differently: "Programs receive a placeholder; the proxy substitutes the real value for allowed destinations", on HTTPS port 443 only, and "it doesn't put the raw credential in a local process or file". Internet access is either "Enable all public internet access" or restricted to package managers plus additional domains. Saved state keeps uncommitted changes per task. — [Codex cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments.md)
- Legacy Codex Cloud ("we plan to deprecate"): "Codex creates a container and checks out your repo". The setup script runs with internet. Agent internet is off by default and can be enabled with a domain allowlist (None / Common dependencies / All) and allowed HTTP methods. Secrets "are only available to setup scripts" and are "removed before the agent phase starts". — [Codex legacy internet access](https://developers.openai.com/codex/cloud/internet-access); [Codex legacy environments](https://developers.openai.com/codex/cloud/environments)

**Claude Code cloud sessions (claude.ai/code)**
- "each session runs in an isolated, Anthropic-managed VM". Network is limited by default. "When running with network access disabled, Claude Code can still communicate with the Anthropic API, which may allow data to exit the VM." Git credentials and signing keys stay outside the sandbox, and a proxy authenticates with scoped credentials. — [Claude Code on the web: Security and isolation](https://code.claude.com/docs/en/claude-code-on-the-web.md)
- Network levels: None, Trusted (default: package registries, GitHub, cloud SDKs), Full and Custom. "Network secrets": "Anthropic's agent proxy adds the key to requests for the hosts you list, after each request leaves the session's VM". The proxy never attaches network secrets to setup-script requests. — [Claude Code cloud environments](https://code.claude.com/docs/en/cloud-environments.md)
- CLI integration: `--cloud` starts a new cloud session for the current repo. `--teleport` pulls a cloud session into the terminal. From the CLI, handoff is one-way: a terminal session cannot be pushed to the cloud. Self-hosted environments are an option, and isolation there is the deployment's responsibility. — [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web.md)

**GitHub Copilot (cloud agent / cloud sandboxes)**
- Changelog of Oct 28, 2026: by default every session "runs in a secure cloud sandbox that starts fast and resumes right where you left off". Cloud sandboxes are GA on paid plans with "no one-hour limit". Billing starts November 2026 and draws from the shared Actions compute entitlement. Actions-based environments are still needed for `copilot-setup-steps.yml` or self-hosted runners. — [GitHub changelog 2026-10-28](https://github.blog/changelog/2026-10-28-work-from-anywhere-with-a-new-connected-copilot-cloud-experience/)
- "A cloud sandbox is an isolated Linux environment". Copilot clones each repo into it. `copilot --cloud` runs an interactive CLI session there (not combinable with `-p`). Snapshots are kept while stopped. Network follows the Copilot firewall settings. — [About cloud sandboxes (github/docs source)](https://github.com/github/docs/blob/main/content/copilot/concepts/security-governance-and-network-settings/about-cloud-sandboxes.md)
- Firewall: always allows GitHub hosts. A recommended allowlist (OS package repos, container registries, language package registries, CAs, Playwright browser hosts) is on by default. Blocked requests are reported in the PR. Limitations: it "only applies to processes started by the agent via its Bash tool", not to MCP servers or setup steps. "Sophisticated attacks may bypass the firewall". — [Copilot firewall docs](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-firewall)
- On GitHub Actions, `.github/workflows/copilot-setup-steps.yml` preinstalls tools and can select larger, self-hosted or Windows runners. Custom Linux hosts can be registered. — [Copilot environment customization](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment)
- Related, local side: Copilot's local sandbox uses Microsoft eXecution Container (MXC): Seatbelt on macOS 15+, bubblewrap (+slirp4netns) on Linux, and a recent Windows 11 feature on Windows. On Windows, proxy enforcement "relies on programs honoring the proxy settings". Credentials are placeholders swapped by a local proxy for approved HTTPS destinations. — [About local sandboxing (github/docs source)](https://github.com/github/docs/blob/main/content/copilot/concepts/security-governance-and-network-settings/about-cloud-and-local-sandboxes.md)

**Devin**
- The cloud session gets "its own VM with a shell, browser, and full repo access". The CLI's `/handoff` sends the repo, branch, conversation context and "uncommitted changes — your work-in-progress diff". Handoff back to local fetches the session's PR branch. — [Devin handoff](https://docs.devin.ai/cli/handoff.md)
- The "brain" always resides in Cognition's cloud. The workspace has a shell, browser and editor. Customer-dedicated deployment and "Outposts" (self-hosted workers) exist. — [Devin enterprise deployment](https://docs.devin.ai/enterprise/deployment/overview.md); [Devin docs index](https://docs.devin.ai/llms.txt)
- Secrets: organization, personal, repo and session secrets, encrypted at rest. The docs recommend a dedicated Devin account and saving its username and password as secrets. — [Devin secrets](https://docs.devin.ai/product-guides/secrets.md)

**Jules (Google)**
- "Jules runs each task inside a secure, short-lived virtual machine (VM)" on Ubuntu with Python 3.12, Node, Go, Java, Rust and Docker preinstalled. An optional setup script is supported. — [Jules environment setup](https://jules.google/docs/environment/)

### Inferences
- Devin's secrets model appears to put secret values into the session, unlike the proxy-injection designs of Codex, Claude Code, Docker and Cloudflare. This is inferred from the docs describing credentials Devin uses to log in. A MossCode remote backend should copy the proxy-injection pattern so that API keys never enter the sandbox. This matches MossCode's existing key-protection direction (commit f54d4b5).
- The Copilot firewall's stated limitations (bash-only scope, bypassable) show that agent-level egress filtering is weaker than VM-level egress filtering such as E2B's nftables+SNI or Claude Code's VM proxy.

### Gaps
- I found no first-hand source for the virtualization technology under Copilot cloud sandboxes. A third-party search result claimed Azure Container Apps Sandboxes, but it was not verified on a GitHub or Microsoft page. The same applies to Claude Code cloud VMs, Codex workspaces and Jules VMs: the hypervisor and runtime are not disclosed in the pages read.
- Jules network policy and secret handling were not on the environment page.

---

## 4. What it would take for MossCode (local CLI) to delegate command execution to a remote backend

### Takeaway
There are two proven patterns. (a) **Session handoff**: move the whole agent session to the cloud (Claude `--cloud`/`--teleport`, Copilot `--cloud`, Devin `/handoff`). Results come back through git. (b) **Remote exec backend**: the agent loop stays local, and each shell command runs in a remote sandbox through an SDK (E2B, Modal, Daytona, Vercel). Pattern (b) needs continuous two-way sync of the working tree and adds network round-trip latency to every command. Interactive commands need a PTY channel. Pattern (a) is simpler and is what every major vendor ships.

### Cited Findings
- Session handoff carries the repo, branch, conversation and uncommitted diff. Results come back as a PR branch. — [Devin handoff](https://docs.devin.ai/cli/handoff.md)
- Claude Code: `--cloud` creates a new cloud session for the current repo. A terminal session cannot be pushed, only new sessions created. `--teleport` pulls a session back down. — [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web.md)
- Copilot `copilot --cloud` runs the whole interactive session in the cloud sandbox. "The commands that Copilot runs execute in the cloud sandbox, not on your local machine". The sandbox "can't read or change the files on your computer". — [About cloud sandboxes](https://github.com/github/docs/blob/main/content/copilot/concepts/security-governance-and-network-settings/about-cloud-sandboxes.md)
- Building blocks available for pattern (b): E2B's `envd` exposes processes, PTYs, file operations, file watchers and port forwarding over Connect RPC/REST. — [e2b-dev/runtime](https://github.com/e2b-dev/runtime). E2B has a PTY and SSH. — [E2B docs index](https://e2b.dev/docs/llms.txt). E2B has streaming callbacks. — [E2B streaming](https://e2b.dev/docs/commands/streaming). Modal has `sb.exec` with stdout iteration. — [Modal Sandboxes](https://modal.com/docs/guide/sandbox)
- Cost of keeping a remote sandbox idle while the LLM thinks. Vercel bills active CPU only. — [Vercel pricing](https://vercel.com/docs/vercel-sandbox/pricing). E2B auto-pauses idle sandboxes. — [e2b-dev/runtime](https://github.com/e2b-dev/runtime). Cloudflare bills only while running and sleeps on timeout. — [Cloudflare Containers pricing](https://developers.cloudflare.com/containers/pricing/)
- Snapshot-based creation makes per-task sandboxes cheap to start. E2B restores pre-booted VM snapshots with lazy memory. — [e2b-dev/runtime](https://github.com/e2b-dev/runtime). Firecracker boots in under 125 ms. — [Firecracker](https://firecracker-microvm.github.io/)
- Credential pattern to reuse: a placeholder in the sandbox, with substitution at a proxy outside it. — [Codex cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments.md); [Docker Sandboxes architecture](https://docs.docker.com/ai/sandboxes/architecture/); [Cloudflare security](https://developers.cloudflare.com/sandbox/concepts/security/)

### Inferences
- **Working-tree sync for pattern (b)**: MossCode's file tools (read/edit) run locally while commands run remotely, so every edit must be pushed before the next command runs. Every remote-side change (build outputs, `git` operations, formatters, codegen) must also be pulled back. Practical options: (1) treat the remote as the source of truth and route MossCode's file tools through the backend's filesystem API as well (E2B `files.*`), i.e. a full remote workspace; (2) git-based sync, sending a `git diff` or bundle up and fetching a diff back, as in Devin's handoff; (3) rsync-like hashing of changed files per command. Option (1) is the most consistent but makes every file read a network call.
- **Latency**: each tool call adds at least one WAN round trip, plus upload of changed files. For test-and-fix loops this is probably acceptable, since LLM latency dominates. For many small `grep`/`ls` calls it adds up, so search tools should run where the files live.
- **Interactive commands**: these need a PTY API (E2B has one; Cloudflare has a browser terminal) and MossCode's TUI must forward keystrokes. Background servers need port forwarding or public preview URLs (E2B `https://<port>-<sandbox>.<domain>`, Modal tunnels).
- **Windows-specific mismatch**: remote sandboxes are Linux (Daytona offers Windows VMs). A Windows user's PowerShell or `.exe`-dependent commands and path semantics will differ, so a remote backend fits Linux-targeted projects best.
- **Security trade-off**: a remote backend protects the user's machine completely, which local OS sandboxes do only partially, especially on Windows according to the Copilot doc. But the repo, and anything synced, leaves the machine and goes to a third party. MossCode should make this explicit and opt-in.
- **Recommended shape**: define a pluggable execution backend interface in MossCode's `tools/` (`run(cmd, cwd, env, stream_cb, pty=False)`, `read/write file`, `sync`). Ship local first. Add a remote E2B backend later, with git-diff sync and proxy-injected secrets. Treat session handoff as a separate, simpler feature.

### Gaps
- I found no published end-to-end latency numbers (per-exec round trip) for any of these platforms. Measuring this would need a benchmark.
- I found no vendor documentation describing continuous bidirectional working-tree sync for a local agent with remote exec. All shipped products use either full handoff or remote-only workspaces.
