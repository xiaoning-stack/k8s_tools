from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
from typing import Optional
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="jwt")

# ========== 定义参数模型 ==========
class PodInput(BaseModel):
    pod_name: str = Field(description="Pod 的完整名称")
    namespace: Optional[str] = Field(default="default", description="命名空间")

class PodLogsInput(BaseModel):
    pod_name: str = Field(description="Pod 的完整名称")
    namespace: Optional[str] = Field(default="default", description="命名空间")
    lines: Optional[int] = Field(default=50, description="日志行数")

class NamespaceInput(BaseModel):
    namespace: Optional[str] = Field(default="default", description="命名空间")
class LegacyYamlFileInput(BaseModel):
    file_path: str = Field(description="YAML 文件的绝对路径")
    write_content: Optional[str] = Field(default=None, description="要写入文件的内容。仅在用户明确确认修改时才传入此参数，否则不传")
    apply: Optional[bool] = Field(default=False,description="用户是否应用改文件")
# ========== 加载 K8s 配置 ==========
try:
    config.load_kube_config()
    print("✓ 已加载 kubeconfig")
except Exception:
    try:
        config.load_incluster_config()
        print("✓ 使用集群内配置")
    except Exception:
        print("✗ 无法加载 K8s 配置,请确保已配置 kubeconfig")

v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

# ========== 定义 K8s 工具 ==========

def list_pods(namespace: str = "default") -> str:
    """列出指定命名空间下的所有 Pod"""
    try:
        pods = v1.list_namespaced_pod(namespace)
        if not pods.items:
            return f"命名空间 '{namespace}' 下没有找到 Pod"
        
        result = f"命名空间 '{namespace}' 下的 Pod:\n"
        result += f"{'NAME':<50} {'STATUS':<10} {'IP':<15} {'NODE':<20}\n"
        result += "-" * 95 + "\n"
        
        for pod in pods.items:
            name = pod.metadata.name
            status = pod.status.phase
            ip = pod.status.pod_ip or "N/A"
            node = pod.spec.node_name or "N/A"
            result += f"{name:<50} {status:<10} {ip:<15} {node:<20}\n"
        
        return result
    except ApiException as e:
        return f"错误: 获取 Pod 列表失败 - {e.reason}"


# ========== 添加 describe_pod 工具函数 ==========

def _get_container_state(pod, container_name):
    """获取容器状态"""
    if pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.name == container_name:
                if cs.state.running:
                    return f"Running (started: {cs.state.running.started_at})"
                elif cs.state.waiting:
                    return f"Waiting ({cs.state.waiting.reason}: {cs.state.waiting.message})"
                elif cs.state.terminated:
                    return f"Terminated (exit code: {cs.state.terminated.exit_code})"
    return "Unknown"

def _get_container_id(pod, container_name):
    """获取容器 ID"""
    if pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.name == container_name and cs.container_id:
                # 简化容器 ID 显示
                return cs.container_id.split("//")[-1][:20] + "..."
    return "N/A"

def _get_container_ready(pod, container_name):
    """获取容器就绪状态"""
    if pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.name == container_name:
                return str(cs.ready)
    return "False"

def _get_restart_count(pod, container_name):
    """获取重启次数"""
    if pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.name == container_name:
                return str(cs.restart_count)
    return "0"

def _format_ports(ports):
    """格式化端口信息"""
    if not ports:
        return "<none>"
    return ", ".join([f"{p.container_port}/{p.protocol}" for p in ports])

def _get_owner_references(pod):
    """获取控制器信息"""
    if pod.metadata.owner_references:
        owners = []
        for owner in pod.metadata.owner_references:
            owners.append(f"{owner.kind}/{owner.name}")
        return ", ".join(owners)
    return "<none>"

def describe_pod(pod_name: str, namespace: str = "default") -> str:
    """获取 Pod 的详细描述（类似 kubectl describe pod）"""
    try:
        pod = v1.read_namespaced_pod(pod_name, namespace)
        
        # 基本信息
        result = f"""Name:             {pod_name}
Namespace:        {namespace}
Priority:         {pod.spec.priority or 0}
Node:             {pod.spec.node_name or 'N/A'}
Start Time:       {pod.status.start_time}
Labels:           {pod.metadata.labels or '<none>'}
Annotations:      {pod.metadata.annotations or '<none>'}
Status:           {pod.status.phase}
IP:               {pod.status.pod_ip or 'N/A'}
IPs:\n  IP:             {pod.status.pod_ip or 'N/A'}\nControlled By:    {_get_owner_references(pod)}\n"""
        
        # 容器信息
        if pod.spec.containers:
            result += "\nContainers:\n"
            for container in pod.spec.containers:
                result += f"  {container.name}:\n"
                result += f"    Container ID:   {_get_container_id(pod, container.name)}\n"
                result += f"    Image:          {container.image}\n"
                result += f"    Port:           {_format_ports(container.ports)}\n"
                result += f"    State:          {_get_container_state(pod, container.name)}\n"
                result += f"    Ready:          {_get_container_ready(pod, container.name)}\n"
                result += f"    Restart Count:  {_get_restart_count(pod, container.name)}\n"
                
                # 资源限制
                if container.resources:
                    if container.resources.limits:
                        result += f"    Limits:\n"
                        for k, v in container.resources.limits.items():
                            result += f"      {k}: {v}\n"
                    if container.resources.requests:
                        result += f"    Requests:\n"
                        for k, v in container.resources.requests.items():
                            result += f"      {k}: {v}\n"
                
                # 环境变量（简化显示）
                if container.env and len(container.env) > 0:
                    result += f"    Environment:\n"
                    for env in container.env[:3]:  # 只显示前3个
                        result += f"      {env.name}: {env.value or '<from secret/configmap>'}\n"
                    if len(container.env) > 3:
                        result += f"      ... and {len(container.env) - 3} more\n"
        
        # 初始化容器
        if pod.spec.init_containers:
            result += "\nInit Containers:\n"
            for container in pod.spec.init_containers:
                result += f"  {container.name}:\n"
                result += f"    Container ID:   {_get_container_id(pod, container.name)}\n"
                result += f"    Image:          {container.image}\n"
                result += f"    State:          {_get_container_state(pod, container.name)}\n"
                result += f"    Restart Count:  {_get_restart_count(pod, container.name)}\n"
        
        # Pod 状态
        if pod.status.conditions:
            result += "\nConditions:\n"
            result += "  Type              Status\n"
            for condition in pod.status.conditions:
                result += f"  {condition.type:<17} {condition.status}\n"
        
        # 卷信息
        if pod.spec.volumes:
            result += "\nVolumes:\n"
            for volume in pod.spec.volumes:
                result += f"  {volume.name}:\n"
                if volume.config_map:
                    result += f"    Type:    ConfigMap (a volume populated by a ConfigMap)\n"
                    result += f"    Name:    {volume.config_map.name}\n"
                elif volume.secret:
                    result += f"    Type:    Secret (a volume populated by a Secret)\n"
                    result += f"    SecretName: {volume.secret.secret_name}\n"
                elif volume.empty_dir:
                    result += f"    Type:    EmptyDir (a temporary directory)\n"
                elif volume.host_path:
                    result += f"    Type:    HostPath (bare host directory volume)\n"
                    result += f"    Path:    {volume.host_path.path}\n"
                elif volume.persistent_volume_claim:
                    result += f"    Type:    PersistentVolumeClaim\n"
                    result += f"    ClaimName: {volume.persistent_volume_claim.claim_name}\n"
        
        # QoS 等级
        if pod.status.qos_class:
            result += f"\nQoS Class:                   {pod.status.qos_class}\n"
        
        # Node Selectors
        if pod.spec.node_selector:
            result += f"Node-Selectors:              {pod.spec.node_selector}\n"
        else:
            result += "Node-Selectors:              <none>\n"
        
        # 容忍度
        if pod.spec.tolerations:
            result += "Tolerations:                 "
            tolerations = []
            for tol in pod.spec.tolerations:
                tolerations.append(f"{tol.key}={tol.value}:{tol.effect} op={tol.operator}")
            result += "\n                             ".join(tolerations) + "\n"
        
        # 事件
        try:
            events = v1.list_namespaced_event(
                namespace, 
                field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod"
            )
            if events.items:
                result += "\nEvents:\n"
                result += "  Type    Reason     Age   From               Message\n"
                result += "  ----    ------     ----  ----               -------\n"
                for event in sorted(events.items, key=lambda e: e.last_timestamp or ""):
                    age = "N/A"
                    if event.last_timestamp:
                        from datetime import datetime
                        try:
                            delta = datetime.now(event.last_timestamp.tzinfo) - event.last_timestamp
                            age = f"{int(delta.total_seconds() / 60)}m"
                        except:
                            pass
                    msg = (event.message or "").replace("\n", " ")
                    if len(msg) > 50:
                        msg = msg[:47] + "..."
                    result += f"  {event.type:<7} {event.reason:<9} {age:<5} {event.source.component or 'N/A':<17} {msg}\n"
        except ApiException:
            pass  # 如果获取事件失败，忽略
        
        return result
        
    except ApiException as e:
        return f"错误: 获取 Pod '{pod_name}' 详细描述失败 - {e.reason}"

def get_pod_status(pod_name: str, namespace: str = "default") -> str:
    
    try:
        pod = v1.read_namespaced_pod(pod_name, namespace)
        result = f"Pod: {pod_name}\n"
        result += f"命名空间: {namespace}\n"
        result += f"状态: {pod.status.phase}\n"
        result += f"IP: {pod.status.pod_ip}\n"
        result += f"节点: {pod.spec.node_name}\n"
        result += f"创建时间: {pod.metadata.creation_timestamp}\n"
        
        if pod.status.container_statuses:
            result += "\n容器状态:\n"
            for cs in pod.status.container_statuses:
                result += f"  - {cs.name}: Ready={cs.ready}, RestartCount={cs.restart_count}\n"
        
        return result
    except ApiException as e:
        return f"错误: 获取 Pod '{pod_name}' 状态失败 - {e.reason}"

def get_pod_logs(pod_name: str, namespace: str = "default", lines: int = 50) -> str:
    
    try:
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=lines
        )
        return f"Pod '{pod_name}' 最近 {lines} 行日志:\n{logs}"
    except ApiException as e:
        return f"错误: 获取 Pod '{pod_name}' 日志失败 - {e.reason}"
