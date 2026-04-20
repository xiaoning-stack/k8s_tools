import os
import subprocess
import tempfile
import yaml
from typing import Optional
from pydantic import BaseModel, Field


# ========== 参数模型 ==========

class YamlFileInput(BaseModel):
    file_path: str = Field(description="YAML 文件的路径（创建新文件时为目标路径）")
    write_content: Optional[str] = Field(
        default=None,
        description="要写入文件的 YAML 内容。创建新文件或修改已有文件时传入，仅读取时不传"
    )
    apply: Optional[bool] = Field(
        default=False,
        description="是否执行 kubectl apply 将文件应用到集群"
    )
    dry_run: Optional[bool] = Field(
        default=False,
        description="是否使用 --dry-run=client 模式进行试运行，不实际应用到集群"
    )


# ========== K8s 资源必要字段校验规则 ==========

REQUIRED_FIELDS = {
    "Deployment": {
        "top": ["apiVersion", "kind", "metadata", "spec"],
        "metadata": ["name"],
        "spec": ["selector", "template"],
        "spec.template.spec": ["containers"],
    },
    "Service": {
        "top": ["apiVersion", "kind", "metadata", "spec"],
        "metadata": ["name"],
        "spec": ["selector", "ports"],
    },
    "ConfigMap": {
        "top": ["apiVersion", "kind", "metadata"],
        "metadata": ["name"],
    },
    "Pod": {
        "top": ["apiVersion", "kind", "metadata", "spec"],
        "metadata": ["name"],
        "spec": ["containers"],
    },
    "StatefulSet": {
        "top": ["apiVersion", "kind", "metadata", "spec"],
        "metadata": ["name"],
        "spec": ["selector", "template", "serviceName"],
    },
    "DaemonSet": {
        "top": ["apiVersion", "kind", "metadata", "spec"],
        "metadata": ["name"],
        "spec": ["selector", "template"],
    },
    "Job": {
        "top": ["apiVersion", "kind", "metadata", "spec"],
        "metadata": ["name"],
        "spec": ["template"],
    },
    "CronJob": {
        "top": ["apiVersion", "kind", "metadata", "spec"],
        "metadata": ["name"],
        "spec": ["schedule", "jobTemplate"],
    },
    "Ingress": {
        "top": ["apiVersion", "kind", "metadata", "spec"],
        "metadata": ["name"],
        "spec": ["rules"],
    },
}


# ========== 主函数 ==========

def yaml_file_operation(
    file_path: str,
    write_content: str = None,
    apply: bool = False,
    dry_run: bool = False
) -> str:
    """
    K8s YAML 文件的读取、创建、修改、校验和应用。

    使用方式:
    1. 读取已有文件:        传入 file_path
    2. 创建新文件:          传入 file_path + write_content（文件可以不存在）
    3. 修改已有文件:        传入 file_path + write_content（会自动备份原文件）
    4. 应用到集群:          传入 file_path + apply=True
    5. 创建/修改并应用:     传入 file_path + write_content + apply=True
    6. 试运行（不实际应用）: 传入 file_path + dry_run=True
    """

    file_exists = os.path.exists(file_path)

    # ---- 情况一：纯读取 ----
    if write_content is None and not apply and not dry_run:
        if not file_exists:
            return f"错误: 找不到文件 '{file_path}'，请确认路径是否正确"
        return _read_and_validate(file_path)

    # ---- 情况二：写入内容（创建或修改） ----
    if write_content is not None:
        # 先校验 YAML 格式
        validation = _validate_yaml_content(write_content)
        if validation["errors"]:
            error_msg = "YAML 内容校验失败，未写入文件:\n"
            for err in validation["errors"]:
                error_msg += f"  - {err}\n"
            if validation["warnings"]:
                error_msg += "\n另外还有以下警告:\n"
                for warn in validation["warnings"]:
                    error_msg += f"  - {warn}\n"
            return error_msg

        # 执行写入
        write_result = _write_file(file_path, write_content, file_exists)
        if write_result["success"]:
            result_parts = [write_result["message"]]

            # 如果有警告，附上
            if validation["warnings"]:
                result_parts.append("\n注意事项:")
                for warn in validation["warnings"]:
                    result_parts.append(f"  - {warn}")

            # 写入后是否需要 apply
            if apply:
                apply_result = _apply_file(file_path, dry_run=False)
                result_parts.append("\n" + apply_result)
            elif dry_run:
                dry_result = _apply_file(file_path, dry_run=True)
                result_parts.append("\n" + dry_result)

            return "\n".join(result_parts)
        else:
            return write_result["message"]

    # ---- 情况三：仅 apply 或 dry_run（不写入） ----
    if apply or dry_run:
        if not file_exists:
            return f"错误: 找不到文件 '{file_path}'，无法应用。请先创建文件"

        # 先校验文件内容
        validation_msg = _read_and_validate(file_path)
        apply_result = _apply_file(file_path, dry_run=dry_run)
        return f"{validation_msg}\n\n{apply_result}"

    return "错误: 参数组合无效，请检查输入"


# ========== 读取并校验 ==========

def _read_and_validate(file_path: str) -> str:
    """读取 YAML 文件并进行结构校验"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return f"错误: 读取文件失败 - {str(e)}"

    if len(content) > 15000:
        return "错误: 文件过大（超过15000字符），无法进行审查"

    if not content.strip():
        return f"警告: 文件 '{file_path}' 内容为空"

    result = f"=== 文件: {file_path} ===\n{content}\n=== 文件内容结束 ===\n"

    # YAML 解析校验
    validation = _validate_yaml_content(content)
    if validation["errors"]:
        result += "\n校验错误:\n"
        for err in validation["errors"]:
            result += f"  ❌ {err}\n"
    if validation["warnings"]:
        result += "\n校验警告:\n"
        for warn in validation["warnings"]:
            result += f"  ⚠️ {warn}\n"
    if not validation["errors"] and not validation["warnings"]:
        result += "\n✅ YAML 格式校验通过\n"

    return result


# ========== YAML 内容校验 ==========

def _validate_yaml_content(content: str) -> dict:
    """
    对 YAML 内容进行多级校验:
    1. YAML 语法是否合法
    2. K8s 必要字段是否齐全
    3. 常见配置问题检查
    """
    errors = []
    warnings = []

    # 第一级: YAML 语法解析
    try:
        docs = list(yaml.safe_load_all(content))
        docs = [d for d in docs if d is not None]
    except yaml.YAMLError as e:
        errors.append(f"YAML 语法错误: {str(e)}")
        return {"errors": errors, "warnings": warnings}

    if not docs:
        errors.append("YAML 文件内容为空或无有效文档")
        return {"errors": errors, "warnings": warnings}

    # 对每个文档进行校验
    for i, doc in enumerate(docs):
        prefix = f"[文档 {i+1}] " if len(docs) > 1 else ""

        if not isinstance(doc, dict):
            errors.append(f"{prefix}文档内容不是有效的 K8s 资源（应为键值对结构）")
            continue

        # 基础字段检查
        if "apiVersion" not in doc:
            errors.append(f"{prefix}缺少 'apiVersion' 字段")
        if "kind" not in doc:
            errors.append(f"{prefix}缺少 'kind' 字段")
            continue

        kind = doc.get("kind", "")

        # 第二级: 资源特定字段校验
        if kind in REQUIRED_FIELDS:
            rules = REQUIRED_FIELDS[kind]

            # 顶层字段
            for field in rules.get("top", []):
                if field not in doc:
                    errors.append(f"{prefix}{kind} 缺少顶层字段 '{field}'")

            # metadata 字段
            metadata = doc.get("metadata", {})
            if isinstance(metadata, dict):
                for field in rules.get("metadata", []):
                    if field not in metadata:
                        errors.append(f"{prefix}{kind} 的 metadata 缺少 '{field}'")

            # spec 字段
            spec = doc.get("spec", {})
            if isinstance(spec, dict):
                for field in rules.get("spec", []):
                    if field not in spec:
                        errors.append(f"{prefix}{kind} 的 spec 缺少 '{field}'")

                # spec.template.spec 字段
                template_spec = _safe_get(spec, "template", "spec")
                if template_spec and isinstance(template_spec, dict):
                    for field in rules.get("spec.template.spec", []):
                        if field not in template_spec:
                            errors.append(f"{prefix}{kind} 的 spec.template.spec 缺少 '{field}'")

        # 第三级: 常见问题检查
        _check_common_issues(doc, kind, prefix, warnings)

    return {"errors": errors, "warnings": warnings}


def _check_common_issues(doc: dict, kind: str, prefix: str, warnings: list):
    """检查常见的配置问题"""
    spec = doc.get("spec", {})
    if not isinstance(spec, dict):
        return

    # Deployment / StatefulSet 相关检查
    if kind in ("Deployment", "StatefulSet", "DaemonSet"):
        template_spec = _safe_get(spec, "template", "spec")

        if isinstance(template_spec, dict):
            containers = template_spec.get("containers", [])

            if isinstance(containers, list):
                for c in containers:
                    if not isinstance(c, dict):
                        continue
                    name = c.get("name", "unnamed")

                    # 镜像标签检查
                    image = c.get("image", "")
                    if image and ":" not in image:
                        warnings.append(f"{prefix}容器 '{name}' 的镜像 '{image}' 没有指定标签，将使用 latest（不推荐）")
                    elif image.endswith(":latest"):
                        warnings.append(f"{prefix}容器 '{name}' 使用了 latest 标签（生产环境不推荐）")

                    # 资源限制检查
                    resources = c.get("resources")
                    if not resources:
                        warnings.append(f"{prefix}容器 '{name}' 没有设置资源限制 (resources)，建议添加 requests 和 limits")
                    else:
                        if not resources.get("limits"):
                            warnings.append(f"{prefix}容器 '{name}' 没有设置 resources.limits")
                        if not resources.get("requests"):
                            warnings.append(f"{prefix}容器 '{name}' 没有设置 resources.requests")

                    # 健康检查
                    if not c.get("livenessProbe") and not c.get("readinessProbe"):
                        warnings.append(f"{prefix}容器 '{name}' 没有配置健康检查探针 (livenessProbe/readinessProbe)")

                    # 安全上下文
                    if not c.get("securityContext"):
                        warnings.append(f"{prefix}容器 '{name}' 没有设置 securityContext")

        # 副本数检查
        replicas = spec.get("replicas")
        if kind == "Deployment" and replicas is not None:
            if not isinstance(replicas, int):
                warnings.append(f"{prefix}replicas 应该是整数，当前值: {replicas}")
            elif replicas == 1:
                warnings.append(f"{prefix}replicas 为 1，单副本在生产环境中无法保证高可用")

        # selector 和 template labels 一致性检查
        selector_labels = _safe_get(spec, "selector", "matchLabels")
        template_labels = _safe_get(spec, "template", "metadata", "labels")
        if selector_labels and template_labels:
            for k, v in selector_labels.items():
                if template_labels.get(k) != v:
                    warnings.append(f"{prefix}selector.matchLabels 中的 '{k}={v}' 与 template.metadata.labels 不匹配")

    # Service 相关检查
    if kind == "Service":
        ports = spec.get("ports", [])
        if isinstance(ports, list):
            for p in ports:
                if isinstance(p, dict) and not p.get("name") and len(ports) > 1:
                    warnings.append(f"{prefix}Service 有多个端口但未设置 port name")


# ========== 文件写入 ==========

def _write_file(file_path: str, content: str, file_exists: bool) -> dict:
    """写入文件，已有文件自动备份"""
    try:
        # 确保目录存在
        dir_path = os.path.dirname(file_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)

        # 已有文件 -> 备份
        backup_path = None
        if file_exists:
            backup_path = f"{file_path}.backup"
            # 如果已有备份，加上序号
            counter = 1
            while os.path.exists(backup_path):
                backup_path = f"{file_path}.backup.{counter}"
                counter += 1
            try:
                import shutil
                shutil.copy2(file_path, backup_path)
            except Exception as e:
                return {
                    "success": False,
                    "message": f"错误: 创建备份失败 - {str(e)}"
                }

        # 先写入临时文件再移动，保证原子性
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', delete=False,
            dir=os.path.dirname(file_path) or '.', encoding='utf-8'
        ) as tmp:
            tmp.write(content)
            temp_path = tmp.name

        # 替换目标文件
        if os.path.exists(file_path):
            os.remove(file_path)
        os.rename(temp_path, file_path)

        if file_exists:
            msg = (
                f"文件修改成功!\n"
                f"  文件路径: {file_path}\n"
                f"  备份文件: {backup_path}"
            )
        else:
            msg = (
                f"文件创建成功!\n"
                f"  文件路径: {file_path}"
            )

        return {"success": True, "message": msg}

    except Exception as e:
        # 清理临时文件
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.unlink(temp_path)
        return {"success": False, "message": f"错误: 文件写入失败 - {str(e)}"}


# ========== kubectl apply ==========

def _apply_file(file_path: str, dry_run: bool = False) -> str:
    """执行 kubectl apply"""
    cmd = ["kubectl", "apply", "-f", file_path]
    mode = "试运行"
    if dry_run:
        cmd.append("--dry-run=client")
        mode = "试运行 (dry-run)"
    else:
        mode = "应用"

    result_msg = f"正在{mode}: kubectl apply -f {file_path}"
    if dry_run:
        result_msg += " --dry-run=client"
    result_msg += "\n"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            result_msg += f"✅ {mode}成功:\n{result.stdout}"
        else:
            result_msg += f"❌ {mode}失败:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        result_msg += "❌ 执行超时（30秒）"
    except FileNotFoundError:
        result_msg += "❌ 找不到 kubectl 命令，请确认已安装并配置 PATH"
    except Exception as e:
        result_msg += f"❌ 执行失败: {str(e)}"

    return result_msg


# ========== 辅助函数 ==========

def _safe_get(d: dict, *keys):
    """安全地获取嵌套字典的值"""
    current = d
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current
