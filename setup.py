from setuptools import setup, find_packages

setup(
    name="k8s-agent",
    version="0.1.0",
    description="K8s AI 运维助手 - 用自然语言管理 Kubernetes 集群",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "langchain>=0.1.0",
        "langchain-openai>=0.0.5",
        "langgraph>=0.0.1",
        "langgraph-checkpoint-sqlite>=1.0.0",
        "kubernetes>=28.1.0",
        "openai>=1.3.0",
    ],
    entry_points={
        "console_scripts": [
            "k8s-agent=cli:main",
        ],
    },
)
