# 在容器中运行

MossCode 的权限检查是应用层的（见 [权限](permissions.md#路径限制)）：确认过的命令以你的系统权限运行，能读写项目以外的文件。Windows 11 上可以打开 [命令沙箱](sandbox.md)，由系统限制命令的写入、读取和网络；其他系统，或想让 MossCode 整个碰不到主机上的其他文件、凭据和进程，在 Docker 容器或 Dev Container 中运行它。

## 容器能隔离什么

| 方面 | 在容器中 |
| --- | --- |
| 文件 | 命令只能看到挂载进容器的目录。项目目录是读写挂载，改动直接写到主机上：开始前提交或备份，用 git 查看改动 |
| 主机的凭据 | 主机的 `~/.ssh`、`~/.mosscode`、浏览器数据等不挂载就看不到 |
| 进程 | 命令看不到主机上的其他进程；结束容器即结束所有子进程 |
| 网络 | 不隔离。MossCode 要连接模型服务，容器不能断网，命令同样可以访问网络（包括把项目文件发出去）。限制出站需要自己配置防火墙或代理 |
| API 密钥 | 容器中的 MossCode 需要密钥。命令不继承 `api_key_env` 指向的变量和其他形似密钥的变量（见 [密钥保护](providers.md#密钥保护)），但同一用户的命令仍可能读到它，例如 `/proc/<pid>/environ` 或挂载进来的配置文件。给容器单独的密钥，并在服务商处设置额度 |
| 资源 | 默认不限制；需要时用 `docker run` 的 `--memory`、`--cpus` |

容器中通常没有系统凭据管理器，`api_key_keyring` 不可用，用 `api_key_env` 并通过 `-e` 传入变量。

## Docker

在一个空目录中保存下面的 `Dockerfile`：

```dockerfile
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir mosscode
ARG UID=1000
RUN useradd --create-home --uid ${UID} moss
USER moss
# The mounted project is owned by another user inside the container; let git work in it anyway.
RUN git config --global --add safe.directory /work && mkdir /home/moss/.mosscode
WORKDIR /work
ENTRYPOINT ["moss"]
```

构建镜像：

```bash
docker build -t mosscode .
```

Linux 上主机用户的 uid 不是 1000 时，加 `--build-arg UID=$(id -u)`，容器中写出的文件才属于你。要用开发中的版本，把 `pip install --no-cache-dir mosscode` 换成从源码安装（先 `COPY` 进镜像，再 `pip install` 这个目录）。

在项目目录中启动（PowerShell；bash 中把行尾的 `` ` `` 换成 `\`，`${PWD}` 换成 `$PWD`）：

```powershell
docker run --rm -it `
  -v "${PWD}:/work" `
  -v mosscode-home:/home/moss/.mosscode `
  -e DEEPSEEK_API_KEY `
  mosscode
```

- `-v "${PWD}:/work"`：只把当前项目挂载进去。
- `-v mosscode-home:/home/moss/.mosscode`：命名卷，保存配置、会话和记忆，删除容器后仍在；不想保留就去掉这一行。
- `-e DEEPSEEK_API_KEY`：只写变量名，Docker 从当前环境取值，密钥不出现在命令行中。

第一次启动时卷中没有配置，`moss` 进入[引导](cli.md#setup)：凭据选“环境变量”，变量名与 `-e` 传入的一致。之后的参数直接写在镜像名后面，例如 `mosscode --version`、`mosscode -p "列出 src 下的模块"`（无界面模式不需要 `-t`，用 `-i` 可以接管道输入）。

也可以不用卷，把一份只含 `api_key_env` 的配置只读挂载进去：`-v "<配置路径>:/etc/mosscode.yaml:ro"`，再在镜像名后加 `--config /etc/mosscode.yaml`。

镜像中没有 tmux，Agent 团队的成员在进程内运行（见 [团队](teams.md)）。

## Dev Container

用 VS Code 或其他支持 [Dev Container](https://containers.dev/) 的编辑器时，在项目中加 `.devcontainer/devcontainer.json`：

```json
{
  "name": "project-with-mosscode",
  "image": "mcr.microsoft.com/devcontainers/python:3.12",
  "postCreateCommand": "sudo apt-get update && sudo apt-get install -y ripgrep && pip install --user mosscode",
  "remoteEnv": { "DEEPSEEK_API_KEY": "${localEnv:DEEPSEEK_API_KEY}" }
}
```

在容器中打开项目后，在编辑器的终端里运行 `moss`；第一次同样进入引导，凭据选“环境变量”。`remoteEnv` 把主机的变量传进容器，主机上要先设置它，再启动编辑器。

Dev Container 默认挂载整个项目目录，编辑器的扩展也在容器中运行；上一节的边界同样适用。
