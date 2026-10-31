
"""Simulation price engine: random movement + news + user order flow."""
from __future__ import annotations

import math
import random
import time
from typing import Any

from services.firebase_store import FirebaseStore
from services.news_engine import NewsEngine
from services.market_math import clamp


class MarketEngine:
    def __init__(self, store: FirebaseStore) -> None:
        self.store = store
        self.news_engine = NewsEngine(store)
        self.pending_news: dict[str, Any] | None = None
        self.active_emergency_events: list[dict[str, Any]] = []

    def generate_scheduled_news(self) -> dict[str, Any] | None:
        """Generate ordinary scheduled news and queue its price impact."""
        companies = self.store.get_companies()

        if not companies:
            self.pending_news = None
            return None

        news = self.news_engine.generate_one(companies)
        self.pending_news = news
        return news

    def generate_emergency_event(
        self,
        scope: str | None = None,
        target: str | None = None,
        direction: str | None = None,
    ) -> dict[str, Any] | None:
        """Create an emergency event and activate its price impact."""
        companies = self.store.get_companies()

        event = self.news_engine.generate_emergency(
            companies,
            scope=scope,
            target=target,
            direction=direction,
        )

        if event is None:
            return None

        self.active_emergency_events.append(dict(event))
        return event

    def tick(self) -> dict[str, Any]:
        """Update prices using normal news and active emergency events."""
        companies = self.store.get_companies()

        if not companies:
            return {
                "updated": 0,
                "index_value": 1000.0,
                "news": None,
                "expired_emergency_events": [],
            }

        state = self.store.get_market_state()

        old_mood = float(state.get("mood", 0.0))
        mood = clamp(
            old_mood * 0.88 + random.gauss(0, 0.0015),
            -0.015,
            0.015,
        )

        # Scheduled news takes priority. Otherwise, normal news may occur.
        news = self.pending_news

        if news is not None:
            self.pending_news = None
        else:
            news = self.news_engine.maybe_emit(companies)

        returns: list[float] = []
        volume_total = 0
        tick_number = int(state.get("tick_count", 0)) + 1

        self.active_emergency_events = [
            event for event in self.active_emergency_events
            if int(event.get("ticks_remaining", 0)) > 0
        ]

        for symbol, cached in companies.items():
            random_shock = random.gauss(
                0,
                float(cached.get("volatility", 0.01)),
            )

            # Ordinary, one-tick news impact
            news_impact = 0.0

            if news:
                if news.get("symbol") == symbol:
                    news_impact = float(news.get("impact", 0.0))
                elif news.get("symbol") == "MARKET":
                    news_impact = float(news.get("impact", 0.0)) * 0.25

            # Emergency events remain active for several ticks.
            emergency_impact = 0.0
            company_sector = str(cached.get("sector", "기타"))

            for event in self.active_emergency_events:
                duration = max(
                    1,
                    int(event.get("duration_ticks", 5)),
                )
                remaining = max(
                    0,
                    int(event.get("ticks_remaining", 0)),
                )

                # 5 ticks: 100%, 80%, 60%, 40%, 20%
                decay_factor = remaining / duration
                base_impact = (
                    float(event.get("impact", 0.0)) * decay_factor
                )

                scope = event.get("scope")

                if scope == "market":
                    # Market-wide events affect all stocks, at reduced strength.
                    emergency_impact += base_impact * 0.55

                elif (
                    scope == "company"
                    and event.get("symbol") == symbol
                ):
                    emergency_impact += base_impact

                elif (
                    scope == "sector"
                    and event.get("sector") == company_sector
                ):
                    emergency_impact += base_impact

            # Limit combined emergency influence if events overlap.
            emergency_impact = clamp(
                emergency_impact,
                -0.07,
                0.07,
            )

            result: dict[str, float | int] = {}
            ref = self.store.companies_ref.child(symbol)

            def apply_price(current: Any) -> Any:
                if not isinstance(current, dict):
                    return current

                row = dict(current)
                old_price = max(100, int(row.get("price", 100)))
                buy_pressure = int(row.get("buy_pressure", 0))
                sell_pressure = int(row.get("sell_pressure", 0))

                # Limit the price influence from trading volume.
                flow = 0.012 * math.tanh(
                    (buy_pressure - sell_pressure) / 300.0
                )

                change = clamp(
                    random_shock
                    + mood * 0.45
                    + flow
                    + news_impact
                    + emergency_impact,
                    -0.08,
                    0.08,
                )

                new_price = max(
                    100,
                    int(round(old_price * (1.0 + change))),
                )
                actual_return = (new_price - old_price) / old_price

                result.update({
                    "old_price": old_price,
                    "new_price": new_price,
                    "return": actual_return,
                    "volume": int(row.get("volume", 0)),
                })

                row.update({
                    "previous_price": old_price,
                    "price": new_price,
                    "last_change_pct": round(actual_return * 100, 4),
                    "volume": int(int(row.get("volume", 0)) * 0.85),
                    "buy_pressure": 0,
                    "sell_pressure": 0,
                    "updated_at": time.time(),
                    "history_count": int(
                        row.get("history_count", 0)
                    ) + 1,
                })

                return row

            ref.transaction(apply_price)

            if "new_price" not in result:
                continue

            returns.append(float(result["return"]))
            volume_total += int(result.get("volume", 0))

            self.store.record_price_history(
                symbol,
                int(result["new_price"]),
                int(result.get("volume", 0)),
                prune=(tick_number % 60 == 0),
            )

        # Decrease event duration after the current tick has applied.
        expired_events: list[dict[str, Any]] = []

        for event in self.active_emergency_events:
            event["ticks_remaining"] = (
                int(event.get("ticks_remaining", 0)) - 1
            )

            if int(event.get("ticks_remaining", 0)) <= 0:
                expired_events.append(dict(event))

        self.active_emergency_events = [
            event for event in self.active_emergency_events
            if int(event.get("ticks_remaining", 0)) > 0
        ]

        old_index = float(state.get("index_value", 1000.0))
        avg_return = sum(returns) / len(returns) if returns else 0.0
        new_index = max(
            100.0,
            round(old_index * (1.0 + avg_return), 2),
        )

        new_state = {
            **state,
            "index_value": new_index,
            "previous_index": old_index,
            "mood": round(mood, 6),
            "tick_count": tick_number,
            "last_tick": time.time(),
            "total_volume": volume_total,
            "last_return_pct": round(avg_return * 100, 4),
        }

        self.store.market_ref.set(new_state)

        return {
            "updated": len(returns),
            "index_value": new_index,
            "return_pct": avg_return * 100,
            "news": news,
            "active_emergency_events": len(
                self.active_emergency_events
            ),
            "expired_emergency_events": expired_events,
        }
