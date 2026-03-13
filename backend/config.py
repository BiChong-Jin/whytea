from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    youtube_api_key: str
    anthropic_api_key: str
    poll_interval_seconds: int = 5
    analysis_interval_seconds: int = 30
    max_comments_per_batch: int = 200
    claude_model: str = "claude-haiku-4-5-20251001"

    class Config:
        env_file = ".env"


settings = Settings()
