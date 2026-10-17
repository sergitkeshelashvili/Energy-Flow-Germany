"""Centralized application settings loaded from environment variables via Pydantic v2.

Supports the standard EnergyFlowGermany environment variables (`MONGO_URI`, `DB_NAME`,
`ENERGY_CHARTS_*`, `OPEN_METEO_BASE_URL`, `WEATHER_*`, `WEATHER_LOCATIONS`, `API_*`, `DASHBOARD_PORT`,
`ENERGY_POLL_INTERVAL_SECONDS`, `WEATHER_POLL_INTERVAL_SECONDS`, `HTTP_TIMEOUT_SECONDS`).
"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WeatherLocation(BaseModel):
    """One Open-Meteo forecast point configured by coordinates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location_id: str = Field(min_length=1)
    location_name: str = Field(min_length=1)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    timezone: str = Field(default="Europe/Berlin", min_length=1)


def _default_weather_locations() -> list[WeatherLocation]:
    return [
        WeatherLocation(
            location_id="DE-DUS",
            location_name="Düsseldorf, Germany",
            latitude=51.2277,
            longitude=6.7735,
        ),
        WeatherLocation(
            location_id="DE-BER",
            location_name="Berlin, Germany",
            latitude=52.5200,
            longitude=13.4050,
        ),
        WeatherLocation(
            location_id="DE-HAM",
            location_name="Hamburg, Germany",
            latitude=53.5511,
            longitude=9.9937,
        ),
        WeatherLocation(
            location_id="DE-MUC",
            location_name="Munich, Germany",
            latitude=48.1372,
            longitude=11.5756,
        ),
        WeatherLocation(
            location_id="DE-FRA",
            location_name="Frankfurt, Germany",
            latitude=50.1109,
            longitude=8.6821,
        ),
    ]


class Settings(BaseSettings):
    """Environment-backed configuration for EnergyFlowGermany."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Environment & Logging ---
    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- MongoDB 7 Connection ---
    mongo_uri_override: str | None = Field(default=None, alias="MONGO_URI")
    mongo_initdb_root_username: str | None = Field(
        default=None,
        alias="MONGO_INITDB_ROOT_USERNAME",
    )
    mongo_initdb_root_password: SecretStr | None = Field(
        default=None,
        alias="MONGO_INITDB_ROOT_PASSWORD",
    )
    mongo_host: str = Field(default="mongodb", alias="MONGO_HOST")
    mongo_port: int = Field(default=27017, alias="MONGO_PORT", ge=1, le=65535)
    mongo_db_name: str = Field(
        default="energy_flow_germany",
        validation_alias=AliasChoices("DB_NAME", "MONGO_DB_NAME"),
    )
    mongo_auth_source: str = Field(default="admin", alias="MONGO_AUTH_SOURCE")
    mongo_connect_max_retries: int = Field(
        default=10,
        alias="MONGO_CONNECT_MAX_RETRIES",
        ge=1,
    )
    mongo_connect_retry_delay_seconds: float = Field(
        default=2.0,
        alias="MONGO_CONNECT_RETRY_DELAY_SECONDS",
        gt=0.0,
    )

    # --- MongoDB Collection Names ---
    mongo_collection_electricity_prices: str = Field(
        default="electricity_prices",
        alias="MONGO_COLLECTION_ELECTRICITY_PRICES",
    )
    mongo_collection_weather_forecasts: str = Field(
        default="weather_forecasts",
        alias="MONGO_COLLECTION_WEATHER_FORECASTS",
    )
    mongo_collection_checkpoints: str = Field(
        default="ingestion_checkpoints",
        alias="MONGO_COLLECTION_CHECKPOINTS",
    )
    mongo_collection_dlq: str = Field(
        default="dead_letter_records",
        alias="MONGO_COLLECTION_DLQ",
    )

    # --- Public Data Sources (No API Keys Required) ---
    # Source 1: Fraunhofer ISE Energy-Charts
    energy_charts_base_url: str = Field(
        default="https://api.energy-charts.info",
        alias="ENERGY_CHARTS_BASE_URL",
    )
    energy_charts_bidding_zone: str = Field(
        default="DE-LU",
        alias="ENERGY_CHARTS_BIDDING_ZONE",
    )

    # Source 2: Open-Meteo (Default: five German city coordinates)
    open_meteo_base_url: str = Field(
        default="https://api.open-meteo.com/v1/forecast",
        alias="OPEN_METEO_BASE_URL",
    )
    open_meteo_location_id: str = Field(
        default="DE-DUS",
        alias="OPEN_METEO_LOCATION_ID",
    )
    open_meteo_location_name: str = Field(
        default="Düsseldorf, Germany",
        alias="OPEN_METEO_LOCATION_NAME",
    )
    open_meteo_latitude: float = Field(
        default=51.2277,
        validation_alias=AliasChoices("WEATHER_LATITUDE", "OPEN_METEO_LATITUDE"),
        ge=-90.0,
        le=90.0,
    )
    open_meteo_longitude: float = Field(
        default=6.7735,
        validation_alias=AliasChoices("WEATHER_LONGITUDE", "OPEN_METEO_LONGITUDE"),
        ge=-180.0,
        le=180.0,
    )
    open_meteo_timezone: str = Field(
        default="Europe/Berlin",
        validation_alias=AliasChoices("WEATHER_TIMEZONE", "OPEN_METEO_TIMEZONE"),
    )
    weather_locations: list[WeatherLocation] = Field(
        default_factory=_default_weather_locations,
        alias="WEATHER_LOCATIONS",
    )

    @field_validator("weather_locations")
    @classmethod
    def validate_weather_locations(cls, value: list[WeatherLocation]) -> list[WeatherLocation]:
        if not value:
            raise ValueError("At least one weather location must be configured.")
        ids = [location.location_id for location in value]
        if len(ids) != len(set(ids)):
            raise ValueError("Weather location IDs must be unique.")
        return value

    # --- HTTP Client & Retry Settings ---
    http_timeout_seconds: float = Field(
        default=20.0,
        alias="HTTP_TIMEOUT_SECONDS",
        gt=0.0,
    )
    http_max_retries: int = Field(
        default=3,
        alias="HTTP_MAX_RETRIES",
        ge=0,
    )
    http_backoff_base_seconds: float = Field(
        default=0.5,
        alias="HTTP_BACKOFF_BASE_SECONDS",
        gt=0.0,
    )
    http_backoff_max_seconds: float = Field(
        default=30.0,
        alias="HTTP_BACKOFF_MAX_SECONDS",
        gt=0.0,
    )

    # --- Ingestion Worker Scheduling & Rate-Limit Protection ---
    energy_poll_interval_seconds: int = Field(
        default=900,
        validation_alias=AliasChoices(
            "ENERGY_POLL_INTERVAL_SECONDS",
            "INGESTION_POLL_INTERVAL_SECONDS",
        ),
        ge=1,
    )
    weather_poll_interval_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices(
            "WEATHER_POLL_INTERVAL_SECONDS",
            "INGESTION_MIN_FETCH_INTERVAL_SECONDS",
        ),
        ge=1,
    )
    ingestion_run_once: bool = Field(
        default=False,
        alias="INGESTION_RUN_ONCE",
    )

    # --- Service Networking ---
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT", ge=1, le=65535)
    streamlit_server_port: int = Field(
        default=8501,
        validation_alias=AliasChoices("DASHBOARD_PORT", "STREAMLIT_SERVER_PORT"),
        ge=1,
        le=65535,
    )
    api_internal_url: str = Field(
        default="http://api:8000",
        validation_alias=AliasChoices("API_BASE_URL", "API_INTERNAL_URL"),
    )

    @property
    def mongo_uri(self) -> str:
        """Return configured `MONGO_URI` or build URI from host/port/optional credentials."""
        if self.mongo_uri_override and self.mongo_uri_override.strip():
            return self.mongo_uri_override.strip()
        if (
            self.mongo_initdb_root_username
            and self.mongo_initdb_root_username.strip()
            and self.mongo_initdb_root_password
            and self.mongo_initdb_root_password.get_secret_value().strip()
        ):
            user = quote_plus(self.mongo_initdb_root_username.strip())
            password = quote_plus(self.mongo_initdb_root_password.get_secret_value().strip())
            return (
                f"mongodb://{user}:{password}@{self.mongo_host}:{self.mongo_port}/"
                f"?authSource={quote_plus(self.mongo_auth_source)}"
            )
        return f"mongodb://{self.mongo_host}:{self.mongo_port}/"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
