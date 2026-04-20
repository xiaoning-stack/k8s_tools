from kubernetes.client.rest import ApiException
from .core import v1

def diagnose_pod(pod_name:str,namespace:str="default"):
    """
    一件诊断异常pod
    这个工具会自动收集日志和Pod详细状态，近期事件和末尾日志，提供给大模型进行分析
    """
    diagnostic_report = f"=== 正在诊断 Pod:{pod_name}({namespace}) ===\n\n"
    try:
        #获取Pod详细状态
        pod = v1.read_namespaced_pod(pod_name,namespace)
        diagnostic_report += "【1.Pod基本状态】\n"
        diagnostic_report += f"Phase: {pod.status.phase}\n"
        #检查所有容器状态
        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                diagnostic_report += f"容器{cs.name}:\n"
                diagnostic_report += f" - Ready: {cs.ready}\n"
                diagnostic_report += f" - RestartCount: {cs.restart_count}\n"
                if cs.state.waiting:
                    diagnostic_report += f" - 状态: Waiting(Reason: {cs.state.waiting.reason},Message: {cs.state.waiting.message})\n"
                elif cs.state.terminated:
                    diagnostic_report += f" - 状态: Terminated(Reason: {cs.state.terminated.reason},Message: {cs.state.terminated.message})\n"
                elif cs.state.running:
                    diagnostic_report += f" - 状态: Running\n"
                else:
                    diagnostic_report += f" - 状态: {cs.state.status}\n"
        diagnostic_report += "\n"
        diagnostic_report += "【2.近期时间】\n"
        try:
            events = v1.list_namespaced_event(namespace,field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod")
            if events.items:
                #按照时间排序取最近的十条
                sorted_events = sorted(events.items, key=lambda e: e.last_timestamp or "")[-10:]
                for event in sorted_events:
                    diagnostic_report += f"[{event.type}] {event.reason}:{event.message}\n"
            else:
                diagnostic_report += "无相关事件\n"
        except ApiException as e:
            diagnostic_report += f"获取事件失败:{e.reason}\n"
        diagnostic_report += "\n"
        #获取日志
        diagnostic_report += "【3.容器近期日志】\n"
        container_names = []
        if pod.spec.init_containers:
            container_names.extend([c.name for c in pod.spec.init_containers])
        if pod.spec.containers:
            container_names.extend([c.name for c in pod.spec.containers])
        for c_name in container_names:
            diagnostic_report += f"--- 容器[{c_name}] 的日志 ---\n"
            try:
                logs=v1.read_namespaced_pod_log(name=pod_name,namespace=namespace,container=c_name,tail_lines=100)
                diagnostic_report += logs if logs else "日志为空\n"
            except ApiException as e:
                diagnostic_report += f"获取容器日志失败:{e.reason}\n"
            diagnostic_report += "\n"
        
        return diagnostic_report
    except ApiException as e:
        return f"诊断失败，无法找到pod'{pod_name}':{e.reason}"


        
