from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # WeChat Configuration
    WECHAT_BOT_ID: str = ""
    WECHAT_BOT_SECRET: str = ""
    WECHAT_WSS_URL: str = "wss://openws.work.weixin.qq.com"

    # 环境标识: local or k8s
    ENVIRONMENT: str = "local"
    KUBECONFIG_PATH: str = "/app/kubeConfig/config.yaml"

    # MCP Configuration
    MCP_K8S_COMMAND: list[str] = []
    MCP_FS_COMMAND: list[str] = []
    SYSTEM_PROMPT_PATH: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        import os
        # 获取项目根目录的绝对路径
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        
        # 根据环境初始化 MCP 启动命令
        if self.ENVIRONMENT == "k8s":
            # K8s 模式下，用户明确要求挂载 kubeconfig 以支持多集群管理
            self.MCP_K8S_COMMAND = [
                "/usr/local/bin/kubernetes-mcp-server", 
                "--read-only",
                "--stateless",
                "--cluster-provider", "kubeconfig",
                "--kubeconfig", self.KUBECONFIG_PATH
            ]
            # Filesystem MCP 使用 node 运行集成的代码
            self.MCP_FS_COMMAND = ["node", "/usr/local/lib/mcp-filesystem/index.js", "/projects"]
            # 系统提示词路径 (Docker 映射路径)
            self.SYSTEM_PROMPT_PATH = "/projects/guide.md"
        else:
            # 本地运行，使用 Docker 容器方案
            self.MCP_K8S_COMMAND = [
                "docker", "run", "-i", "--rm", 
                "-v", f"{base_dir}/kubeConfig/config.yaml:/etc/kubernetes/admin.conf:ro", 
                "ghcr.io/containers/kubernetes-mcp-server:latest", 
                "--kubeconfig", "/etc/kubernetes/admin.conf", 
                "--read-only",
                "--stateless"
            ]
            self.MCP_FS_COMMAND = [
                "docker", "run", "-i", "--rm", 
                "-v", f"{base_dir}/doc_path/guide_doc:/projects/guide_doc:ro", 
                "mcp/filesystem", 
                "/projects"
            ]
            # 系统提示词路径 (本地相对路径)
            self.SYSTEM_PROMPT_PATH = os.path.join(base_dir, "doc_path", "guide_doc", "guide.md")

    # OpenAI Configuration 
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.modelverse.cn/v1"
    OPENAI_MODEL: str = "zai-org/glm-5"
    MAX_HISTORY_ROUNDS: int = 10

    # RAG Configuration
    RAG_ENABLED: bool = False
    RAG_EMBEDDING_MODEL: str = "Qwen3-Embedding-8B"
    RAG_EMBEDDING_API_BASE: str = "http://117.50.226.140:8000/v1"
    RAG_EMBEDDING_API_KEY: str = "none"
    
    RAG_LLM_MODEL: str = "openai/gpt-5.2"
    RAG_LLM_API_KEY: str = ""
    RAG_LLM_API_BASE: str = "https://api.modelverse.cn/v1"

    # Milvus Configuration
    RAG_MILVUS_HOST: str = "127.0.0.1"
    RAG_MILVUS_PORT: str = "19530"
    RAG_MILVUS_COLLECTION_NAME: str = "sre_small_to_big_hybrid_vstore"
    
    # Rerank Configuration
    RAG_RERANK_MODEL: str = "Qwen3-VL-Reranker-8B"
    RAG_RERANK_API_URL: str = "http://106.75.44.131:8000/rerank"
    RAG_RERANK_TIMEOUT: int = 10

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
