from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    youtube_api_key: str
    deepseek_api_key: str
    poll_interval_seconds: int = 5
    analysis_interval_seconds: int = 30
    max_comments_per_batch: int = 200
    analysis_model: str = "deepseek-chat"
    jwt_secret_key: str
    jwt_token_expire_hours: int = 24
    # Comma-separated origins, e.g. "https://app.example.com,https://www.example.com"
    cors_origins: str = "*"

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                "JWT_SECRET_KEY must be at least 32 characters. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v

    class Config:
        env_file = ".env"


settings = Settings()
