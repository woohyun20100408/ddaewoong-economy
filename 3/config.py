"""Environment-based configuration for the Discord stock-market bot."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    news_channel_id: int | None
    discord_token: str
    firebase_database_url: str
    firebase_service_account_json: str
    google_application_credentials: str
    guild_id: int | None
    starting_cash: int = 100_000
    trading_fee_rate: float = 0.0015
    market_tick_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
            news_channel_raw = os.getenv("NEWS_CHANNEL_ID", "").strip()

            starting_cash = int(os.getenv("STARTING_CASH", "100000"))
            fee_rate = float(os.getenv("TRADING_FEE_RATE", "0.0015"))
            tick_seconds = int(os.getenv("MARKET_TICK_SECONDS", "60"))

            guild_id = int(guild_raw) if guild_raw else None
            news_channel_id = (
                int(news_channel_raw) if news_channel_raw else None
            )

        except ValueError as exc:
            raise ValueError(
                "환경 변수의 숫자 형식을 확인해 주세요."
            ) from exc

        if starting_cash <= 0:
            raise ValueError("STARTING_CASH는 0보다 커야 합니다.")

        if not 0 <= fee_rate < 0.1:
            raise ValueError(
                "TRADING_FEE_RATE는 0 이상 0.1 미만이어야 합니다."
            )

        if tick_seconds < 15:
            raise ValueError(
                "MARKET_TICK_SECONDS는 최소 15초로 설정해 주세요."
            )

        return cls(
            news_channel_id=news_channel_id,
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            firebase_database_url=os.getenv(
                "FIREBASE_DATABASE_URL", ""
            ).strip(),
            firebase_service_account_json=os.getenv(
                "FIREBASE_SERVICE_ACCOUNT_JSON", ""
            ).strip(),
            google_application_credentials=os.getenv(
                "GOOGLE_APPLICATION_CREDENTIALS", ""
            ).strip(),
            guild_id=guild_id,
            starting_cash=starting_cash,
            trading_fee_rate=fee_rate,
            market_tick_seconds=tick_seconds,
        )

    def validate_for_startup(self) -> None:
        missing = []

        if not self.discord_token:
            missing.append("DISCORD_TOKEN")

        if not self.firebase_database_url:
            missing.append("FIREBASE_DATABASE_URL")

        if not (
            self.firebase_service_account_json
            or self.google_application_credentials
        ):
            missing.append(
                "FIREBASE_SERVICE_ACCOUNT_JSON 또는 "
                "GOOGLE_APPLICATION_CREDENTIALS"
            )

        if missing:
            raise ValueError(
                "필수 환경 변수가 없습니다: " + ", ".join(missing)
            )