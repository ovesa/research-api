from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """
    Application configuration loaded from environment variables or .env file.

    Attributes:

        database_url (str): Full PostgreSQL connection string. Includes
            username, password, host, port, and database name. Defaults
            to the local Docker container created by docker-compose.

        redis_url (str): Redis connection string. Defaults to the local
            Docker container created by docker-compose.

        app_name (str): Human-readable name for the application. Used in
            API docs and log output.

        debug (bool): Enables debug mode. Never set to True in production
            as it exposes internal error details in API responses.

        cache_ttl_seconds (int): How long to keep a paper cached in Redis
            before expiring it. Defaults to 86400 (24 hours). Papers are
            stable documents so a long TTL is safe and keeps external API
            calls to a minimum.

        external_api_timeout (int): How long in seconds to wait for a
            response from CrossRef, arXiv, or Semantic Scholar before
            giving up. Defaults to 10 seconds.
    """

    # Database
    database_url: str = (
        "postgresql://researchapi:researchapi@localhost:5432/researchapi"
    )

    # Redis
    redis_url: str = "redis://localhost:6379"

    # App
    app_name: str = "Research Paper Metadata API"
    debug: bool = False

    # Cache TTL in seconds (papers don't change often so cache aggressively)
    # Updated once a day
    cache_ttl_seconds: int = 86400  # 24 hours

    # External API timeout in seconds
    external_api_timeout: int = 10
    
    # NASA ADS
    ads_api_token: str = ""
    anthropic_api_key: str = "" 

    class Config:
        env_file = ".env"
        extra = "ignore" 


settings = Settings()
