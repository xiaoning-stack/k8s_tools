#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
K8s AI 运维助手 - CLI 入口

使用方式:
  k8s-agent -m "查看 default 命名空间下的 Pod"
  k8s-agent -m "查看它的日志" -s my_session
  k8s-agent -i
  k8s-agent --sessions
  k8s-agent --history
  k8s-agent --clear
"""

import argparse
import sys
import os
import re
import sqlite3
from typing import Optional

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="jwt")

# ========== 导入工具 ==========
from k8s_tools.confirmation import (
    cancel_pending_operation as cancel_pending_operation_executor,
    clear_operations as clear_operations_executor,
    create_pending_operation as create_pending_operation_executor,
    execute_pending_operation as execute_pending_operation_executor,
    list_pending_operations as list_pending_operations_executor,
)
from k8s_tools.diagnose import diagnose_pod
from k8s_tools.yaml_checker import YamlFileInput, yaml_file_operation as yaml_file_operation_executor
from k8s_tools.core import (
    list_pods,
    get_pod_status,
    get_pod_logs,
    describe_pod,
    NamespaceInput,
    PodInput,
    PodLogsInput,
)
from k8s_tools.deployment import (
    list_deployments,
    describe_deployment,
    scale_deployment as scale_deployment_executor,
    rollout_status,
    rollout_history,
    rollback_deployment as rollback_deployment_executor,
    restart_deployment as restart_deployment_executor,
    pause_deployment as pause_deployment_executor,
    resume_deployment as resume_deployment_executor,
    DeploymentInput,
    DeploymentScaleInput,
    DeploymentNamespaceInput,
    DeploymentRollbackInput,
)

# ========== 配置 ==========

# 数据目录，存放 SQLite 记忆文件
DATA_DIR = os.path.join(os.path.expanduser("~"), ".k8s-agent")
DB_PATH = os.path.join(DATA_DIR, "memory.db")
DEFAULT_SESSION = "default"
DEFAULT_YAML_DIR = os.path.join(os.path.expanduser("~"), "k8s-yaml")  # 默认 YAML 工作目录


# ========== 构建 Agent ==========

CONFIRM_KEYWORDS = {"确认", "confirm", "yes", "y", "是", "执行"}
CANCEL_KEYWORDS = {"取消", "cancel", "no", "n", "否"}
PENDING_LIST_KEYWORDS = {"pending", "/pending", "待确认", "待确认操作", "待执行"}


def _build_pending_handlers():
    return {
        "yaml_file_operation": yaml_file_operation_executor,
        "scale_deployment": scale_deployment_executor,
        "rollback_deployment": rollback_deployment_executor,
        "restart_deployment": restart_deployment_executor,
        "pause_deployment": pause_deployment_executor,
        "resume_deployment": resume_deployment_executor,
    }


def _extract_operation_id(text: str) -> Optional[str]:
    match = re.search(r"op_[0-9a-f]{12}", text.lower())
    if match:
        return match.group(0)
    return None


def _format_pending_operations(pending_operations) -> str:
    if not pending_operations:
        return "当前会话没有待确认操作"

    lines = ["当前会话的待确认操作:"]
    for operation in pending_operations:
        lines.append(
            f"- {operation['operation_id']}: {operation['summary']} "
            f"(created: {operation['created_at']}, expires: {operation['expires_at']})"
        )
    lines.append("回复“确认”执行唯一待确认操作，或回复“confirm op_xxx”/“cancel op_xxx”指定操作。")
    return "\n".join(lines)


def _list_pending_operations_for_session(session_id: str) -> str:
    return _format_pending_operations(list_pending_operations_executor(DB_PATH, session_id))


def _resolve_pending_operation_id(session_id: str, operation_id: Optional[str]):
    pending_operations = list_pending_operations_executor(DB_PATH, session_id)

    if operation_id:
        return operation_id, None

    if not pending_operations:
        return None, "当前会话没有待确认操作"

    if len(pending_operations) > 1:
        return None, "当前会话有多个待确认操作，请指定 operation_id。\n" + _format_pending_operations(pending_operations)

    return pending_operations[0]["operation_id"], None


def _queue_pending_operation(session_id: str, kind: str, payload: dict, summary: str) -> str:
    operation_id = create_pending_operation_executor(
        DB_PATH,
        session_id,
        kind,
        payload,
        summary,
    )
    return (
        f"已创建待确认操作: {operation_id}\n"
        f"摘要: {summary}\n"
        "该操作尚未执行。\n"
        "回复“确认”或“confirm <operation_id>”执行；"
        "回复“取消”或“cancel <operation_id>”取消。"
    )


def _confirm_pending_operation_for_session(session_id: str, operation_id: Optional[str] = None) -> str:
    resolved_operation_id, error = _resolve_pending_operation_id(session_id, operation_id)
    if error:
        return error

    return execute_pending_operation_executor(
        DB_PATH,
        session_id,
        resolved_operation_id,
        _build_pending_handlers(),
    )


def _cancel_pending_operation_for_session(session_id: str, operation_id: Optional[str] = None) -> str:
    resolved_operation_id, error = _resolve_pending_operation_id(session_id, operation_id)
    if error:
        return error

    return cancel_pending_operation_executor(DB_PATH, session_id, resolved_operation_id)


def _handle_pending_operation_input(message: str, session_id: str) -> Optional[str]:
    stripped = message.strip()
    if not stripped:
        return None

    lowered = stripped.lower()
    operation_id = _extract_operation_id(stripped)
    pending_operations = list_pending_operations_executor(DB_PATH, session_id)
    has_pending_operations = bool(pending_operations)

    if stripped in PENDING_LIST_KEYWORDS or lowered in PENDING_LIST_KEYWORDS:
        return _format_pending_operations(pending_operations)

    is_explicit_confirm = lowered == "confirm" or lowered.startswith("confirm ") or lowered.startswith("/confirm")
    is_explicit_cancel = lowered == "cancel" or lowered.startswith("cancel ") or lowered.startswith("/cancel")

    if is_explicit_confirm:
        return _confirm_pending_operation_for_session(session_id, operation_id)

    if is_explicit_cancel:
        return _cancel_pending_operation_for_session(session_id, operation_id)

    if (
        has_pending_operations
        and (
            stripped in CONFIRM_KEYWORDS
            or lowered in CONFIRM_KEYWORDS
            or stripped.startswith("确认")
        )
    ):
        return _confirm_pending_operation_for_session(session_id, operation_id)

    if (
        has_pending_operations
        and (
            stripped in CANCEL_KEYWORDS
            or lowered in CANCEL_KEYWORDS
            or stripped.startswith("取消")
        )
    ):
        return _cancel_pending_operation_for_session(session_id, operation_id)

    return None


def build_tools(session_id: str):
    """构建所有工具"""

    def yaml_file_operation(
        file_path: str,
        write_content: Optional[str] = None,
        apply: bool = False,
        dry_run: bool = False,
    ) -> str:
        requires_confirmation = write_content is not None or (apply and not dry_run)
        if not requires_confirmation:
            return yaml_file_operation_executor(
                file_path=file_path,
                write_content=write_content,
                apply=apply,
                dry_run=dry_run,
            )

        if write_content is not None and apply and not dry_run:
            summary = f"写入并应用 YAML 文件 '{file_path}'"
        elif write_content is not None and dry_run:
            summary = f"写入 YAML 文件 '{file_path}' 并执行 dry-run 校验"
        elif write_content is not None:
            summary = f"写入 YAML 文件 '{file_path}'"
        else:
            summary = f"应用 YAML 文件 '{file_path}'"

        return _queue_pending_operation(
            session_id,
            "yaml_file_operation",
            {
                "file_path": file_path,
                "write_content": write_content,
                "apply": apply,
                "dry_run": dry_run,
            },
            summary,
        )

    def scale_deployment(name: str, namespace: str = "default", replicas: int = 1) -> str:
        return _queue_pending_operation(
            session_id,
            "scale_deployment",
            {
                "name": name,
                "namespace": namespace,
                "replicas": replicas,
            },
            f"将命名空间 '{namespace}' 中的 Deployment '{name}' 副本数调整为 {replicas}",
        )

    def rollback_deployment(name: str, namespace: str = "default", revision: int = 0) -> str:
        revision_text = "上一版本" if revision == 0 else f"revision {revision}"
        return _queue_pending_operation(
            session_id,
            "rollback_deployment",
            {
                "name": name,
                "namespace": namespace,
                "revision": revision,
            },
            f"将命名空间 '{namespace}' 中的 Deployment '{name}' 回滚到 {revision_text}",
        )

    def restart_deployment(name: str, namespace: str = "default") -> str:
        return _queue_pending_operation(
            session_id,
            "restart_deployment",
            {
                "name": name,
                "namespace": namespace,
            },
            f"滚动重启命名空间 '{namespace}' 中的 Deployment '{name}'",
        )

    def pause_deployment(name: str, namespace: str = "default") -> str:
        return _queue_pending_operation(
            session_id,
            "pause_deployment",
            {
                "name": name,
                "namespace": namespace,
            },
            f"暂停命名空间 '{namespace}' 中的 Deployment '{name}' 滚动更新",
        )

    def resume_deployment(name: str, namespace: str = "default") -> str:
        return _queue_pending_operation(
            session_id,
            "resume_deployment",
            {
                "name": name,
                "namespace": namespace,
            },
            f"继续命名空间 '{namespace}' 中的 Deployment '{name}' 滚动更新",
        )

    def list_pending_operations_tool() -> str:
        return _list_pending_operations_for_session(session_id)

    def confirm_pending_operation(operation_id: Optional[str] = None) -> str:
        return _confirm_pending_operation_for_session(session_id, operation_id)

    def cancel_pending_operation(operation_id: Optional[str] = None) -> str:
        return _cancel_pending_operation_for_session(session_id, operation_id)

    return [
        StructuredTool.from_function(
            name="list_pods",
            func=list_pods,
            description="列出指定命名空间下的所有 Pod。输入参数: namespace (命名空间名称)",
            args_schema=NamespaceInput,
        ),
        StructuredTool.from_function(
            name="get_pod_logs",
            func=get_pod_logs,
            description="获取指定 Pod 的日志。输入参数: pod_name, namespace, lines",
            args_schema=PodLogsInput,
        ),
        StructuredTool.from_function(
            name="describe_pod",
            func=describe_pod,
            description="获取 Pod 详细信息（类似 kubectl describe pod）。输入参数: pod_name, namespace",
            args_schema=PodInput,
        ),
        StructuredTool.from_function(
            name="diagnose_pod",
            func=diagnose_pod,
            description="一键收集异常 Pod 的全面诊断信息（包括状态、事件和日志）。当发现 Pod 状态异常时优先使用此工具。",
            args_schema=PodInput,
        ),
        StructuredTool.from_function(
            name="yaml_file_operation",
            func=yaml_file_operation,
            description="读取 YAML 会直接执行；写入文件或 apply 会先创建待确认操作。输入参数: file_path, write_content, apply, dry_run",
            args_schema=YamlFileInput,
        ),
        StructuredTool.from_function(
            name="list_pending_operations",
            func=list_pending_operations_tool,
            description="列出当前会话中所有待确认操作。",
        ),
        StructuredTool.from_function(
            name="confirm_pending_operation",
            func=confirm_pending_operation,
            description="执行待确认操作。可传 operation_id；如果当前会话只有一个待确认操作，也可以不传。",
        ),
        StructuredTool.from_function(
            name="cancel_pending_operation",
            func=cancel_pending_operation,
            description="取消待确认操作。可传 operation_id；如果当前会话只有一个待确认操作，也可以不传。",
        ),
        # ===== Deployment 管理工具 =====
        StructuredTool.from_function(
            name="list_deployments",
            func=list_deployments,
            description="列出指定命名空间下的所有 Deployment。输入参数: namespace (命名空间名称)",
            args_schema=DeploymentNamespaceInput,
        ),
        StructuredTool.from_function(
            name="describe_deployment",
            func=describe_deployment,
            description="获取 Deployment 的详细描述信息，包括副本状态、更新策略、Pod模板、事件等（类似 kubectl describe deployment）。输入参数: name, namespace",
            args_schema=DeploymentInput,
        ),
        StructuredTool.from_function(
            name="scale_deployment",
            func=scale_deployment,
            description="扩缩容 Deployment，会先创建待确认操作。输入参数: name, namespace, replicas (目标副本数)",
            args_schema=DeploymentScaleInput,
        ),
        StructuredTool.from_function(
            name="rollout_status",
            func=rollout_status,
            description="查看 Deployment 的滚动更新状态和进度。输入参数: name, namespace",
            args_schema=DeploymentInput,
        ),
        StructuredTool.from_function(
            name="rollout_history",
            func=rollout_history,
            description="查看 Deployment 的版本历史记录，包括每个版本的镜像和副本信息。输入参数: name, namespace",
            args_schema=DeploymentInput,
        ),
        StructuredTool.from_function(
            name="rollback_deployment",
            func=rollback_deployment,
            description="回滚 Deployment，会先创建待确认操作。revision=0 表示回滚到上一个版本。输入参数: name, namespace, revision",
            args_schema=DeploymentRollbackInput,
        ),
        StructuredTool.from_function(
            name="restart_deployment",
            func=restart_deployment,
            description="滚动重启 Deployment，会先创建待确认操作。输入参数: name, namespace",
            args_schema=DeploymentInput,
        ),
        StructuredTool.from_function(
            name="pause_deployment",
            func=pause_deployment,
            description="暂停 Deployment 的滚动更新，会先创建待确认操作。输入参数: name, namespace",
            args_schema=DeploymentInput,
        ),
        StructuredTool.from_function(
            name="resume_deployment",
            func=resume_deployment,
            description="继续 Deployment 的滚动更新，会先创建待确认操作。输入参数: name, namespace",
            args_schema=DeploymentInput,
        ),
    ]


def build_system_prompt(yaml_dir: str) -> str:
    """构建 system prompt，注入 YAML 工作目录"""
    return f"""你是一个 K8s 运维助手，帮助用户管理 Kubernetes 集群。

重要交互规则：
1. 当用户要求审查YAML文件时，先提供详细的审查报告
2. 如果发现可改进的地方，主动提供修改后的YAML示例
3. 在提供修改示例后，必须询问："是否要应用这些修改？请回复 '是' 或 '否'"
4. 只有当用户明确回复'是'时，才执行文件替换操作
5. 执行替换前，必须告知用户将创建备份文件
6. 可以给用户应用yaml文件
7. 当用户执行扩缩容、回滚、重启等危险操作时，先确认再执行
8. 当用户要求暂停或继续 Deployment 的滚动更新时，分别使用 pause_deployment 和 resume_deployment 工具

【YAML 文件路径规则（非常重要，必须严格遵守）】
- 默认 YAML 工作目录: {yaml_dir}
- 当用户要求创建新的 YAML 文件时，如果用户没有指定路径，必须将文件创建在默认工作目录下: {yaml_dir}/<文件名>.yaml
- 当用户明确指定了目录或路径时，必须使用用户指定的路径，不要擅自修改
- 文件名应有意义，例如 nginx-deployment.yaml、redis-service.yaml
- 创建文件后，必须告知用户文件的完整路径
- 读取或修改已有文件时，使用用户提供的路径

你可以帮助用户：

【Pod 管理】
- 查看 Pod 列表：使用 list_pods 工具
- 查看 Pod 状态：使用 get_pod_status 工具
- 查看 Pod 日志：使用 get_pod_logs 工具
- 查看 Pod 详细信息：使用 describe_pod 工具
- 诊断 Pod 异常：使用 diagnose_pod 工具

【Deployment 管理】
- 查看 Deployment 列表：使用 list_deployments 工具
- 查看 Deployment 详情：使用 describe_deployment 工具
- 扩缩容 Deployment：使用 scale_deployment 工具（修改副本数）
- 查看滚动更新状态：使用 rollout_status 工具
- 查看版本历史：使用 rollout_history 工具
- 回滚 Deployment：使用 rollback_deployment 工具（revision=0 回滚到上一版本）
- 滚动重启 Deployment：使用 restart_deployment 工具
- 暂停 Deployment 更新：使用 pause_deployment 工具
- 继续 Deployment 更新：使用 resume_deployment 工具

【YAML 文件管理】
- 检查/分析 YAML 文件：使用 yaml_file_operation 工具
- 应用 YAML 修改：使用 yaml_file_operation 工具（用户确认后）
当你使用yaml_file_operation工具的时候，查看文件不需要传入write_content参数，当用户确认修改文件后，将你给出的更新的k8s内容传入write_content参数,当用户需要应用yaml文件的时候传入apply=True

函数的返回值是什么就是什么，不要自己编造，按照返回值来，若返回值和用户描述不一致，给出错误就好。
"""


def create_k8s_agent(checkpointer, session_id: str = DEFAULT_SESSION, yaml_dir: str = DEFAULT_YAML_DIR):
    """创建 K8s 运维 Agent"""
    # 确保 YAML 工作目录存在
    os.makedirs(yaml_dir, exist_ok=True)

    model_name = os.environ.get("OPENAI_MODEL")
    api_key = os.environ.get("OPENAI_API_KEY")
    api_base = os.environ.get("OPENAI_API_BASE")

    missing_env_vars = [
        env_name
        for env_name, env_value in (
            ("OPENAI_MODEL", model_name),
            ("OPENAI_API_KEY", api_key),
            ("OPENAI_API_BASE", api_base),
        )
        if not env_value
    ]
    if missing_env_vars:
        missing = ", ".join(missing_env_vars)
        raise RuntimeError(f"缺少必要的环境变量: {missing}")

    llm = ChatOpenAI(
        model=model_name,
        temperature=0,
        openai_api_key=api_key,
        openai_api_base=api_base,
    )

    agent = create_agent(
        llm,
        build_tools(session_id),
        system_prompt=build_system_prompt(yaml_dir),
        checkpointer=checkpointer,
    )
    return agent


# ========== 核心调用 ==========

def invoke_agent(message: str, session_id: str = DEFAULT_SESSION, yaml_dir: str = DEFAULT_YAML_DIR):
    """发送单条消息并获取回复"""
    os.makedirs(DATA_DIR, exist_ok=True)

    pending_response = _handle_pending_operation_input(message, session_id)
    if pending_response is not None:
        return pending_response

    # 使用 SqliteSaver 持久化记忆
    with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
        agent = create_k8s_agent(checkpointer, session_id, yaml_dir)

        result = agent.invoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": session_id}},
        )

        # 提取 AI 最后的回复
        if "messages" in result:
            for msg in reversed(result["messages"]):
                if hasattr(msg, "type") and msg.type == "ai":
                    if hasattr(msg, "content") and msg.content:
                        return msg.content
    return "未获取到回复"


def interactive_mode(session_id: str = DEFAULT_SESSION, yaml_dir: str = DEFAULT_YAML_DIR):
    """交互式对话模式"""
    print("=" * 60)
    print("K8s AI 运维助手 (交互模式)")
    print(f"会话: {session_id}")
    print(f"YAML 目录: {yaml_dir}")
    print("=" * 60)
    print("\n输入 'quit' 退出 | 输入 '/clear' 清除当前会话记忆\n")

    os.makedirs(DATA_DIR, exist_ok=True)

    with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
        agent = create_k8s_agent(checkpointer, session_id, yaml_dir)

        while True:
            try:
                user_input = input("\n🔧 > ").strip()

                if user_input.lower() in ("quit", "exit", "q"):
                    print("再见!")
                    break

                if user_input == "/clear":
                    clear_session(session_id)
                    print(f"✅ 会话 '{session_id}' 的记忆已清除")
                    # 重新创建 agent 以刷新 checkpointer
                    agent = create_k8s_agent(checkpointer, session_id, yaml_dir)
                    continue

                if not user_input:
                    continue

                pending_response = _handle_pending_operation_input(user_input, session_id)
                if pending_response is not None:
                    print(pending_response)
                    continue

                print("🤔 正在处理...", end="", flush=True)

                result = agent.invoke(
                    {"messages": [{"role": "user", "content": user_input}]},
                    config={"configurable": {"thread_id": session_id}},
                )

                print("\r" + " " * 20 + "\r", end="")

                if "messages" in result:
                    for msg in reversed(result["messages"]):
                        if hasattr(msg, "type") and msg.type == "ai":
                            if hasattr(msg, "content") and msg.content:
                                print(msg.content)
                                break

            except KeyboardInterrupt:
                print("\n再见!")
                break
            except Exception as e:
                print(f"\n❌ 错误: {e}")
                import traceback
                traceback.print_exc()


# ========== 会话管理 ==========

def list_sessions():
    """列出所有会话"""
    if not os.path.exists(DB_PATH):
        print("暂无会话记录")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        # langgraph SQLite checkpointer 的表结构
        cursor.execute("""
            SELECT DISTINCT thread_id
            FROM checkpoints
            ORDER BY thread_id
        """)
        rows = cursor.fetchall()

        if not rows:
            print("暂无会话记录")
            return

        print("=" * 40)
        print("所有会话:")
        print("=" * 40)
        for i, (thread_id,) in enumerate(rows, 1):
            marker = " (默认)" if thread_id == DEFAULT_SESSION else ""
            print(f"  {i}. {thread_id}{marker}")
        print(f"\n共 {len(rows)} 个会话")

    except sqlite3.OperationalError:
        print("暂无会话记录（数据库表尚未创建）")
    finally:
        conn.close()


def show_history(session_id: str = DEFAULT_SESSION):
    """查看指定会话的对话历史"""
    if not os.path.exists(DB_PATH):
        print("暂无对话历史")
        return

    with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
        config = {"configurable": {"thread_id": session_id}}
        checkpoint = checkpointer.get(config)

        if not checkpoint or "channel_values" not in checkpoint:
            print(f"会话 '{session_id}' 暂无对话历史")
            return

        messages = checkpoint["channel_values"].get("messages", [])
        if not messages:
            print(f"会话 '{session_id}' 暂无对话历史")
            return

        print("=" * 60)
        print(f"会话 '{session_id}' 的对话历史:")
        print("=" * 60)

        for msg in messages:
            if hasattr(msg, "type"):
                if msg.type == "human":
                    print(f"\n{'👤 用户:'}")
                    print(f"  {msg.content}")
                elif msg.type == "ai" and msg.content:
                    print(f"\n{'🤖 助手:'}")
                    # 截断过长的回复
                    content = msg.content
                    if len(content) > 500:
                        content = content[:500] + "\n  ... (内容过长已截断)"
                    print(f"  {content}")
                elif msg.type == "tool":
                    pass  # 不显示工具调用细节

        print("\n" + "=" * 60)


def clear_session(session_id: str = None):
    """清除指定会话或所有会话"""
    if not os.path.exists(DB_PATH):
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        if session_id:
            cursor.execute("DELETE FROM checkpoints WHERE thread_id = ?", (session_id,))
            cursor.execute("DELETE FROM writes WHERE thread_id = ?", (session_id,))
        else:
            cursor.execute("DELETE FROM checkpoints")
            cursor.execute("DELETE FROM writes")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 表不存在，无需清除
    finally:
        conn.close()

    clear_operations_executor(DB_PATH, session_id)


# ========== CLI 入口 ==========

def main():
    parser = argparse.ArgumentParser(
        prog="k8s-agent",
        description="K8s AI 运维助手 - 用自然语言管理你的 Kubernetes 集群",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  k8s-agent -m "查看 default 命名空间下的 Pod"
  k8s-agent -m "那个 nginx pod 的日志呢" -s prod
  k8s-agent -i
  k8s-agent -i -s prod-cluster
  k8s-agent --sessions
  k8s-agent --history -s prod
  k8s-agent --clear -s prod
        """,
    )

    # 核心参数
    parser.add_argument(
        "-m", "--message",
        type=str,
        help="发送单条消息（命令模式）",
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="进入交互式对话模式",
    )
    parser.add_argument(
        "-s", "--session",
        type=str,
        default=DEFAULT_SESSION,
        help=f"指定会话 ID（默认: {DEFAULT_SESSION}），同一会话共享上下文记忆",
    )
    parser.add_argument(
        "-d", "--yaml-dir",
        type=str,
        default=DEFAULT_YAML_DIR,
        help=f"YAML 文件的默认工作目录（默认: {DEFAULT_YAML_DIR}）",
    )

    # 会话管理
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="列出所有会话",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="查看当前会话的对话历史",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="清除会话记忆（配合 -s 清除指定会话，否则清除所有）",
    )
    parser.add_argument(
        "--clear-all",
        action="store_true",
        help="清除所有会话记忆",
    )

    args = parser.parse_args()

    # 处理会话管理命令
    if args.sessions:
        list_sessions()
        return

    if args.history:
        show_history(args.session)
        return

    if args.clear_all:
        clear_session(None)
        print("✅ 所有会话记忆已清除")
        return

    if args.clear:
        clear_session(args.session)
        print(f"✅ 会话 '{args.session}' 的记忆已清除")
        return

    # 核心功能
    yaml_dir = os.path.abspath(args.yaml_dir)

    if args.message:
        # 单条消息模式
        try:
            response = invoke_agent(args.message, args.session, yaml_dir)
            print(response)
        except Exception as e:
            print(f"❌ 错误: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.interactive:
        # 交互式模式
        interactive_mode(args.session, yaml_dir)

    else:
        # 没有参数时默认进入交互模式
        interactive_mode(args.session, yaml_dir)


if __name__ == "__main__":
    main()
