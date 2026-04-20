# k8s-Agent

`k8s-Agent` 是一个基于 `LangChain + LangGraph + Kubernetes Python Client` 的命令行运维助手，用自然语言执行常见的 Kubernetes 查询与运维操作。

当前项目主要提供两类能力：

- 查询类操作：查看 Pod、日志、Deployment 状态、版本历史、诊断异常 Pod
- 变更类操作：通过 YAML 文件修改资源，或对 Deployment 执行扩缩容、回滚、重启、暂停更新、继续更新

对危险操作，项目内置了待确认机制，不会在收到请求后立刻执行。

## 功能概览

### Pod 相关

- 列出指定命名空间下的 Pod
- 查看 Pod 日志
- 查看 Pod 详细描述
- 诊断异常 Pod

### Deployment 相关

- 列出 Deployment
- 查看 Deployment 详情
- 查看滚动更新状态
- 查看版本历史
- 扩缩容
- 回滚
- 滚动重启
- 暂停更新
- 继续更新

### YAML 文件相关

- 读取和校验 Kubernetes YAML 文件
- 写入修改后的 YAML 内容
- `dry-run` 校验
- `kubectl apply`
- 自动备份被修改的 YAML 文件

## 运行要求

- Python 3.9+
- 可访问 Kubernetes 集群
- 已配置 `kubeconfig`，或运行在集群内并可使用 `incluster config`
- 已安装 `kubectl`，如果你要使用 YAML apply 功能

## 安装

推荐直接使用 `requirements.txt` 安装依赖：

```bash
pip install -r requirements.txt
```

## 配置

当前代码使用 `ChatOpenAI`，并通过下面的环境变量读取模型配置：

```bash
export OPENAI_MODEL="gpt-5.4"
export OPENAI_API_KEY="your_api_key"
export OPENAI_API_BASE="https://right.codes/codex"
```

如果缺少上述环境变量，程序会在启动时直接报错。

## 快速开始

### 1. 单条命令模式

```bash
python cli.py -m "查看 default 命名空间下的 Pod"
```

### 2. 交互模式

```bash
python cli.py -i
```

### 3. 指定会话

```bash
python cli.py -m "查看 myapp-deploy 的滚动更新状态" -s prod
```

### 4. 指定 YAML 工作目录

```bash
python cli.py -m "检查 /data/k8s/myapp.yaml" -d /data/k8s
```

## 常用命令

### 查看 Deployment 状态

```bash
python cli.py -m "查看 default 命名空间下的 Deployment"
python cli.py -m "查看 myapp-deploy 的详情"
python cli.py -m "查看 myapp-deploy 的滚动更新状态"
python cli.py -m "查看 myapp-deploy 的版本历史"
```

### 暂停和继续更新

```bash
python cli.py -m "暂停 myapp-deploy 的更新"
python cli.py -m "确认"

python cli.py -m "继续 myapp-deploy 的更新"
python cli.py -m "确认"
```

### 扩缩容、回滚、重启

```bash
python cli.py -m "将 myapp-deploy 扩容到 5 个副本"
python cli.py -m "确认"

python cli.py -m "将 myapp-deploy 回滚到上一个版本"
python cli.py -m "确认"

python cli.py -m "滚动重启 myapp-deploy"
python cli.py -m "确认"
```

### YAML 文件检查和应用

读取 YAML：

```bash
python cli.py -m "检查 /data/k8s/myapp-deploy.yaml"
```

如果你要修改镜像、环境变量或副本数，当前推荐通过 YAML 文件路径进行：

```bash
python cli.py -m "把 /data/k8s/myapp-deploy.yaml 里的镜像改成 wangyanglinux/myapp:v2.0"
```

当助手创建待确认操作后，再执行：

```bash
python cli.py -m "确认"
```

## 待确认操作机制

为了避免误操作，以下类型的动作会先进入待确认队列：

- YAML 写入
- YAML apply
- Deployment 扩缩容
- Deployment 回滚
- Deployment 重启
- Deployment 暂停更新
- Deployment 继续更新

### 查看待确认操作

```bash
python cli.py -m "pending"
```

### 执行待确认操作

```bash
python cli.py -m "确认"
```

或者指定操作 ID：

```bash
python cli.py -m "confirm op_123456abcdef"
```

### 取消待确认操作

```bash
python cli.py -m "取消"
```

或者指定操作 ID：

```bash
python cli.py -m "cancel op_123456abcdef"
```

## 会话管理

会话数据默认保存在用户目录下：

- 记忆数据库：`~/.k8s-agent/memory.db`
- 默认 YAML 工作目录：`~/k8s-yaml`

常用会话命令：

```bash
python cli.py --sessions
python cli.py --history -s default
python cli.py --clear -s default
python cli.py --clear-all
```

## 项目结构

```text
.
├── cli.py
├── requirements.txt
├── setup.py
├── archive/
│   └── k8s_tools_legacy.py
└── k8s_tools/
    ├── __init__.py
    ├── confirmation.py
    ├── core.py
    ├── deployment.py
    ├── diagnose.py
    └── yaml_checker.py
```

说明：

- `cli.py`：CLI 入口、Agent 构建、会话管理、待确认操作逻辑
- `k8s_tools/core.py`：Pod 相关查询工具
- `k8s_tools/deployment.py`：Deployment 查询与变更工具
- `k8s_tools/diagnose.py`：Pod 异常诊断
- `k8s_tools/yaml_checker.py`：YAML 读写、校验、dry-run、apply
- `k8s_tools/confirmation.py`：待确认操作的本地持久化和执行逻辑
- `archive/k8s_tools_legacy.py`：历史遗留实现，当前不参与主流程

## 当前限制

- 当前没有“直接修改 Deployment 镜像”的专用 API 工具，修改镜像建议走 YAML 文件路径
- `pause/resume` 是对 Deployment rollout 的控制，不是流量层面的灰度发布
- `setup.py` 仍是较旧的打包配置，日常使用建议优先 `python cli.py` 或 `pip install -r requirements.txt`

## 典型场景

### 场景 1：查看异常 Pod

```bash
python cli.py -m "诊断 default 命名空间下的 myapp-pod"
```

### 场景 2：通过暂停更新保留两版 Pod

```bash
python cli.py -m "查看 myapp-deploy 的滚动更新状态"
python cli.py -m "暂停 myapp-deploy 的更新"
python cli.py -m "确认"
```

此时如果 Deployment 正在滚动更新中，集群里可以同时存在部分旧版本 Pod 和部分新版本 Pod。之后再执行：

```bash
python cli.py -m "继续 myapp-deploy 的更新"
python cli.py -m "确认"
```

## 备注

如果你只是想清理项目目录，通常可以安全删除下面这些生成物：

- `build/`
- `dist/`
- `k8s_agent.egg-info/`
- `__pycache__/`
- `k8s_tools/__pycache__/`
- `archive/__pycache__/`

这些目录不是源码，都是构建缓存、运行缓存或临时测试数据。
