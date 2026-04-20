
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone

from .core import v1, apps_v1

# ========== 定义参数模型 ==========

class DeploymentInput(BaseModel):
    name: str = Field(description="Deployment 的名称")
    namespace: Optional[str] = Field(default="default", description="命名空间")

class DeploymentScaleInput(BaseModel):
    name: str = Field(description="Deployment 的名称")
    namespace: Optional[str] = Field(default="default", description="命名空间")
    replicas: int = Field(description="目标副本数")

class DeploymentNamespaceInput(BaseModel):
    namespace: Optional[str] = Field(default="default", description="命名空间")

class DeploymentRollbackInput(BaseModel):
    name: str = Field(description="Deployment 的名称")
    namespace: Optional[str] = Field(default="default", description="命名空间")
    revision: Optional[int] = Field(default=0, description="回滚到的版本号，0 表示回滚到上一个版本")


# ========== Deployment 工具函数 ==========

def list_deployments(namespace: str = "default") -> str:
    """列出指定命名空间下的所有 Deployment"""
    try:
        deployments = apps_v1.list_namespaced_deployment(namespace)
        if not deployments.items:
            return f"命名空间 '{namespace}' 下没有找到 Deployment"

        result = f"命名空间 '{namespace}' 下的 Deployment:\n"
        result += f"{'NAME':<40} {'READY':<12} {'UP-TO-DATE':<12} {'AVAILABLE':<12} {'AGE':<10}\n"
        result += "-" * 90 + "\n"

        for dep in deployments.items:
            name = dep.metadata.name
            desired = dep.spec.replicas or 0
            ready = dep.status.ready_replicas or 0
            updated = dep.status.updated_replicas or 0
            available = dep.status.available_replicas or 0
            age = _format_age(dep.metadata.creation_timestamp)

            result += f"{name:<40} {ready}/{desired:<10} {updated:<12} {available:<12} {age:<10}\n"

        return result
    except ApiException as e:
        return f"错误: 获取 Deployment 列表失败 - {e.reason}"


def describe_deployment(name: str, namespace: str = "default") -> str:
    """获取 Deployment 的详细描述信息"""
    try:
        dep = apps_v1.read_namespaced_deployment(name, namespace)

        desired = dep.spec.replicas or 0
        ready = dep.status.ready_replicas or 0
        updated = dep.status.updated_replicas or 0
        available = dep.status.available_replicas or 0
        unavailable = dep.status.unavailable_replicas or 0

        result = f"""Name:               {name}
Namespace:          {namespace}
CreationTimestamp:  {dep.metadata.creation_timestamp}
Labels:             {_format_labels(dep.metadata.labels)}
Annotations:        {_format_labels(dep.metadata.annotations)}
Selector:           {_format_labels(dep.spec.selector.match_labels) if dep.spec.selector else '<none>'}
Replicas:           {desired} desired | {updated} updated | {ready} ready | {available} available | {unavailable} unavailable
"""

        # 更新策略
        strategy = dep.spec.strategy
        if strategy:
            result += f"StrategyType:       {strategy.type}\n"
            if strategy.type == "RollingUpdate" and strategy.rolling_update:
                result += f"  Max Unavailable:  {strategy.rolling_update.max_unavailable}\n"
                result += f"  Max Surge:        {strategy.rolling_update.max_surge}\n"

        # Pod 模板信息
        template = dep.spec.template
        if template.spec.containers:
            result += "\nPod Template:\n"
            result += f"  Labels:           {_format_labels(template.metadata.labels)}\n"
            for container in template.spec.containers:
                result += f"\n  Container: {container.name}\n"
                result += f"    Image:          {container.image}\n"
                result += f"    Ports:          {_format_ports(container.ports)}\n"

                # 资源限制
                if container.resources:
                    if container.resources.requests:
                        result += f"    Requests:\n"
                        for k, v in container.resources.requests.items():
                            result += f"      {k}: {v}\n"
                    if container.resources.limits:
                        result += f"    Limits:\n"
                        for k, v in container.resources.limits.items():
                            result += f"      {k}: {v}\n"

                # 环境变量
                if container.env:
                    result += f"    Environment:\n"
                    for env in container.env[:5]:
                        val = env.value or '<from secret/configmap>'
                        result += f"      {env.name}: {val}\n"
                    if len(container.env) > 5:
                        result += f"      ... and {len(container.env) - 5} more\n"

                # 探针
                if container.liveness_probe:
                    result += f"    Liveness:       {_format_probe(container.liveness_probe)}\n"
                if container.readiness_probe:
                    result += f"    Readiness:      {_format_probe(container.readiness_probe)}\n"
                if container.startup_probe:
                    result += f"    Startup:        {_format_probe(container.startup_probe)}\n"

                # 挂载
                if container.volume_mounts:
                    result += f"    Mounts:\n"
                    for vm in container.volume_mounts:
                        ro = " (ro)" if vm.read_only else ""
                        result += f"      {vm.mount_path} from {vm.name}{ro}\n"

        # Conditions
        if dep.status.conditions:
            result += "\nConditions:\n"
            result += f"  {'TYPE':<25} {'STATUS':<10} {'REASON':<30} MESSAGE\n"
            result += "  " + "-" * 90 + "\n"
            for cond in dep.status.conditions:
                msg = (cond.message or "")[:50]
                result += f"  {cond.type:<25} {cond.status:<10} {cond.reason or 'N/A':<30} {msg}\n"

        # 关联的 ReplicaSet
        result += "\n" + _get_replicasets(name, namespace)

        # 事件
        result += "\n" + _get_deployment_events(name, namespace)

        return result
    except ApiException as e:
        return f"错误: 获取 Deployment '{name}' 详细描述失败 - {e.reason}"


def scale_deployment(name: str, namespace: str = "default", replicas: int = 1) -> str:
    """扩缩容 Deployment 的副本数"""
    try:
        # 先获取当前状态
        dep = apps_v1.read_namespaced_deployment(name, namespace)
        old_replicas = dep.spec.replicas or 0

        if old_replicas == replicas:
            return f"Deployment '{name}' 当前已经是 {replicas} 个副本，无需调整"

        # 执行扩缩容
        body = {"spec": {"replicas": replicas}}
        apps_v1.patch_namespaced_deployment(name, namespace, body)

        action = "扩容" if replicas > old_replicas else "缩容"
        return (
            f"Deployment '{name}' {action}成功!\n"
            f"  命名空间: {namespace}\n"
            f"  副本数: {old_replicas} -> {replicas}\n"
            f"  提示: 可以使用 rollout_status 工具查看更新进度"
        )
    except ApiException as e:
        return f"错误: 扩缩容 Deployment '{name}' 失败 - {e.reason}"


def rollout_status(name: str, namespace: str = "default") -> str:
    """查看 Deployment 的滚动更新状态"""
    try:
        dep = apps_v1.read_namespaced_deployment(name, namespace)

        desired = dep.spec.replicas or 0
        updated = dep.status.updated_replicas or 0
        ready = dep.status.ready_replicas or 0
        available = dep.status.available_replicas or 0
        unavailable = dep.status.unavailable_replicas or 0

        result = f"=== Deployment '{name}' 滚动更新状态 ===\n\n"
        result += f"  期望副本数:     {desired}\n"
        result += f"  已更新副本数:   {updated}\n"
        result += f"  就绪副本数:     {ready}\n"
        result += f"  可用副本数:     {available}\n"
        result += f"  不可用副本数:   {unavailable}\n\n"

        # 判断更新状态
        if updated == desired and ready == desired and available == desired:
            result += f"  状态: Deployment '{name}' 已成功完成滚动更新\n"
        elif unavailable > 0:
            result += f"  状态: 正在更新中... 还有 {unavailable} 个副本未就绪\n"
        else:
            result += f"  状态: 更新进行中 ({updated}/{desired} 已更新, {ready}/{desired} 已就绪)\n"

        # Conditions 详情
        if dep.status.conditions:
            result += "\n  最新状态:\n"
            for cond in dep.status.conditions:
                result += f"    [{cond.type}] {cond.status} - {cond.reason}: {cond.message or 'N/A'}\n"

        # 关联的 ReplicaSet 状态
        result += "\n" + _get_replicasets(name, namespace)

        return result
    except ApiException as e:
        return f"错误: 获取 Deployment '{name}' 滚动更新状态失败 - {e.reason}"


def rollout_history(name: str, namespace: str = "default") -> str:
    """查看 Deployment 的版本历史（通过 ReplicaSet 推导）"""
    try:
        dep = apps_v1.read_namespaced_deployment(name, namespace)
        selector = dep.spec.selector.match_labels
        label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])

        rs_list = apps_v1.list_namespaced_replica_set(namespace, label_selector=label_selector)

        if not rs_list.items:
            return f"Deployment '{name}' 没有找到历史版本"

        # 按 revision 排序
        rs_items = []
        for rs in rs_list.items:
            # 只保留属于该 Deployment 的 ReplicaSet
            if not _is_owned_by(rs, dep.metadata.uid):
                continue
            revision = rs.metadata.annotations.get("deployment.kubernetes.io/revision", "0") if rs.metadata.annotations else "0"
            rs_items.append((int(revision), rs))

        rs_items.sort(key=lambda x: x[0])

        result = f"=== Deployment '{name}' 版本历史 ===\n\n"
        result += f"{'REVISION':<12} {'REPLICAS':<15} {'IMAGE':<50} {'AGE':<10}\n"
        result += "-" * 90 + "\n"

        for revision, rs in rs_items:
            replicas = f"{rs.status.ready_replicas or 0}/{rs.spec.replicas or 0}"
            images = ", ".join([c.image for c in rs.spec.template.spec.containers]) if rs.spec.template.spec.containers else "N/A"
            age = _format_age(rs.metadata.creation_timestamp)

            # 标记当前活跃版本
            marker = " <-- 当前版本" if (rs.spec.replicas or 0) > 0 else ""
            result += f"{revision:<12} {replicas:<15} {images:<50} {age:<10}{marker}\n"

        return result
    except ApiException as e:
        return f"错误: 获取 Deployment '{name}' 版本历史失败 - {e.reason}"


def rollback_deployment(name: str, namespace: str = "default", revision: int = 0) -> str:
    """回滚 Deployment 到指定版本（默认回滚到上一个版本）"""
    try:
        dep = apps_v1.read_namespaced_deployment(name, namespace)
        selector = dep.spec.selector.match_labels
        label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])

        # 找到目标 ReplicaSet
        rs_list = apps_v1.list_namespaced_replica_set(namespace, label_selector=label_selector)
        rs_items = []
        for rs in rs_list.items:
            if not _is_owned_by(rs, dep.metadata.uid):
                continue
            rev = rs.metadata.annotations.get("deployment.kubernetes.io/revision", "0") if rs.metadata.annotations else "0"
            rs_items.append((int(rev), rs))

        rs_items.sort(key=lambda x: x[0])

        if len(rs_items) < 2 and revision == 0:
            return f"Deployment '{name}' 只有一个版本，无法回滚"

        # 确定目标版本
        if revision == 0:
            # 回滚到上一个版本（倒数第二个）
            target_rs = rs_items[-2][1]
            target_rev = rs_items[-2][0]
        else:
            target_rs = None
            for rev, rs in rs_items:
                if rev == revision:
                    target_rs = rs
                    target_rev = rev
                    break
            if target_rs is None:
                available = [str(r) for r, _ in rs_items]
                return f"错误: 找不到版本 {revision}，可用版本: {', '.join(available)}"

        # 用目标 ReplicaSet 的 Pod 模板替换当前 Deployment 的模板
        target_template = target_rs.spec.template
        current_images = ", ".join([c.image for c in dep.spec.template.spec.containers])
        target_images = ", ".join([c.image for c in target_template.spec.containers])

        body = {
            "spec": {
                "template": target_template.to_dict()
            }
        }
        apps_v1.patch_namespaced_deployment(name, namespace, body)

        return (
            f"Deployment '{name}' 回滚成功!\n"
            f"  命名空间:   {namespace}\n"
            f"  目标版本:   revision {target_rev}\n"
            f"  镜像变更:   {current_images}\n"
            f"          ->  {target_images}\n"
            f"  提示: 可以使用 rollout_status 工具查看回滚进度"
        )
    except ApiException as e:
        return f"错误: 回滚 Deployment '{name}' 失败 - {e.reason}"


def restart_deployment(name: str, namespace: str = "default") -> str:
    """滚动重启 Deployment（等效于 kubectl rollout restart）"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now
                        }
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(name, namespace, body)

        return (
            f"Deployment '{name}' 滚动重启已触发!\n"
            f"  命名空间: {namespace}\n"
            f"  重启时间: {now}\n"
            f"  提示: 可以使用 rollout_status 工具查看重启进度"
        )
    except ApiException as e:
        return f"错误: 重启 Deployment '{name}' 失败 - {e.reason}"


def pause_deployment(name: str, namespace: str = "default") -> str:
    """暂停 Deployment 的滚动更新（等效于 kubectl rollout pause）"""
    try:
        dep = apps_v1.read_namespaced_deployment(name, namespace)

        if dep.spec.paused:
            return (
                f"Deployment '{name}' 当前已经处于暂停状态\n"
                f"  命名空间: {namespace}\n"
                f"  提示: 可以使用 rollout_status 工具查看当前更新进度"
            )

        body = {"spec": {"paused": True}}
        apps_v1.patch_namespaced_deployment(name, namespace, body)

        return (
            f"Deployment '{name}' 已暂停滚动更新!\n"
            f"  命名空间: {namespace}\n"
            f"  提示: 当前新旧版本 Pod 会维持在现有状态，"
            f"可使用 rollout_status 工具查看进度，后续使用 resume_deployment 继续更新"
        )
    except ApiException as e:
        return f"错误: 暂停 Deployment '{name}' 失败 - {e.reason}"


def resume_deployment(name: str, namespace: str = "default") -> str:
    """继续 Deployment 的滚动更新（等效于 kubectl rollout resume）"""
    try:
        dep = apps_v1.read_namespaced_deployment(name, namespace)

        if not dep.spec.paused:
            return (
                f"Deployment '{name}' 当前未处于暂停状态\n"
                f"  命名空间: {namespace}\n"
                f"  提示: 可以使用 rollout_status 工具查看当前更新进度"
            )

        body = {"spec": {"paused": False}}
        apps_v1.patch_namespaced_deployment(name, namespace, body)

        return (
            f"Deployment '{name}' 已继续滚动更新!\n"
            f"  命名空间: {namespace}\n"
            f"  提示: 可以使用 rollout_status 工具查看继续更新后的进度"
        )
    except ApiException as e:
        return f"错误: 继续 Deployment '{name}' 更新失败 - {e.reason}"


# ========== 内部辅助函数 ==========

def _format_age(creation_timestamp) -> str:
    """格式化资源存在时间"""
    if not creation_timestamp:
        return "N/A"
    try:
        now = datetime.now(timezone.utc)
        if creation_timestamp.tzinfo is None:
            creation_timestamp = creation_timestamp.replace(tzinfo=timezone.utc)
        delta = now - creation_timestamp
        total_seconds = int(delta.total_seconds())

        if total_seconds < 60:
            return f"{total_seconds}s"
        elif total_seconds < 3600:
            return f"{total_seconds // 60}m"
        elif total_seconds < 86400:
            return f"{total_seconds // 3600}h"
        else:
            return f"{total_seconds // 86400}d"
    except Exception:
        return "N/A"


def _format_labels(labels: dict) -> str:
    """格式化标签"""
    if not labels:
        return "<none>"
    return ", ".join([f"{k}={v}" for k, v in labels.items()])


def _format_ports(ports) -> str:
    """格式化端口"""
    if not ports:
        return "<none>"
    return ", ".join([f"{p.container_port}/{p.protocol}" for p in ports])


def _format_probe(probe) -> str:
    """格式化探针信息"""
    if probe.http_get:
        return f"http-get {probe.http_get.path}:{probe.http_get.port} (delay={probe.initial_delay_seconds}s, period={probe.period_seconds}s)"
    elif probe.tcp_socket:
        return f"tcp-socket :{probe.tcp_socket.port} (delay={probe.initial_delay_seconds}s, period={probe.period_seconds}s)"
    elif probe.exec:
        cmd = " ".join(probe.exec.command) if probe.exec.command else ""
        return f"exec [{cmd}] (delay={probe.initial_delay_seconds}s, period={probe.period_seconds}s)"
    return "unknown"


def _is_owned_by(rs, owner_uid: str) -> bool:
    """检查 ReplicaSet 是否属于指定 Deployment"""
    if rs.metadata.owner_references:
        for ref in rs.metadata.owner_references:
            if ref.uid == owner_uid:
                return True
    return False


def _get_replicasets(deploy_name: str, namespace: str) -> str:
    """获取 Deployment 关联的 ReplicaSet 信息"""
    try:
        dep = apps_v1.read_namespaced_deployment(deploy_name, namespace)
        selector = dep.spec.selector.match_labels
        label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])

        rs_list = apps_v1.list_namespaced_replica_set(namespace, label_selector=label_selector)

        result = "ReplicaSets:\n"
        for rs in rs_list.items:
            if not _is_owned_by(rs, dep.metadata.uid):
                continue
            revision = rs.metadata.annotations.get("deployment.kubernetes.io/revision", "?") if rs.metadata.annotations else "?"
            desired = rs.spec.replicas or 0
            ready = rs.status.ready_replicas or 0
            result += f"  {rs.metadata.name} (revision {revision}): {ready}/{desired} replicas ready\n"

        return result
    except ApiException:
        return "ReplicaSets: <获取失败>\n"


def _get_deployment_events(deploy_name: str, namespace: str) -> str:
    """获取 Deployment 相关事件"""
    try:
        events = v1.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={deploy_name},involvedObject.kind=Deployment"
        )
        if not events.items:
            return "Events: <none>"

        result = "Events:\n"
        result += f"  {'TYPE':<10} {'REASON':<20} {'AGE':<8} {'MESSAGE'}\n"
        result += "  " + "-" * 80 + "\n"

        sorted_events = sorted(events.items, key=lambda e: e.last_timestamp or "")[-15:]
        for event in sorted_events:
            age = _format_age(event.last_timestamp)
            msg = (event.message or "").replace("\n", " ")
            if len(msg) > 60:
                msg = msg[:57] + "..."
            result += f"  {event.type:<10} {event.reason:<20} {age:<8} {msg}\n"

        return result
    except ApiException:
        return "Events: <获取失败>\n"
