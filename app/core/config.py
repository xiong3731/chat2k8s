from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # WeChat Configuration
    WECHAT_CORP_ID: str = ""
    WECHAT_CORP_SECRET: str = ""
    WECHAT_AGENT_ID: int = 0
    WECHAT_TOKEN: str = ""
    WECHAT_ENCODING_AES_KEY: str = ""

    # MCP Configuration
    MCP_SERVER_URL: str = "http://localhost:5678/sse"

    # OpenAI Configuration
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.modelverse.cn/v1"
    OPENAI_MODEL: str = "zai-org/glm-5"
    MAX_HISTORY_ROUNDS: int = 10

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
