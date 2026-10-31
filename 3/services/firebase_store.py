"""Firebase Realtime Database data-access layer."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import firebase_admin
from firebase_admin import credentials, db

logger = logging.getLogger(__name__)
ROOT_PATH = "stock_market"
MAX_HISTORY_POINTS = 720  # keep at most 12 hours at a 1-minute tick


class FirebaseStore:
    def __init__(self, database_url: str, service_account_json: str = "", service_account_file: str = "") -> None:
        if not database_url:
            raise ValueError("FIREBASE_DATABASE_URL이 설정되지 않았습니다.")
        if firebase_admin._apps:
            app = firebase_admin.get_app()
        else:
            if service_account_json:
                try:
                    credential = credentials.Certificate(json.loads(service_account_json))
                except json.JSONDecodeError as exc:
                    raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON이 올바른 JSON인지 확인해 주세요.") from exc
            elif service_account_file:
                if not Path(service_account_file).is_file():
                    raise FileNotFoundError("GOOGLE_APPLICATION_CREDENTIALS 경로에 인증 파일이 없습니다.")
                credential = credentials.Certificate(service_account_file)
            else:
                raise ValueError("Firebase 서비스 계정 인증 정보가 필요합니다.")
            app = firebase_admin.initialize_app(credential, {"databaseURL": database_url})

        self.app = app
        self.root = db.reference(ROOT_PATH, app=app)
        self.companies_ref = self.root.child("companies")
        self.players_ref = self.root.child("players")
        self.market_ref = self.root.child("market")
        self.news_ref = self.root.child("news")
        self.history_ref = self.root.child("price_history")
        self.trade_history_ref = self.root.child("trade_history")

    def ensure_seed_data(self, seed_companies: dict[str, dict[str, Any]]) -> None:
        now = time.time()
        # Migrate the original demo tickers to the user's Korean company list. Existing
        # prices, chart history, and player holdings are carried over when possible.
        legacy_map = {
            "BLOOM": "KAPI",
            "GREEN": "WOONG",
            "NEOG": "YBGM",
            "FOOD": "YJFD",
            "ORBIT": "DBMB",
            "NOVA": "WBTK",
            "CLOUD": "HJSC",
            "LUXE": "SPLT",
        }
        legacy_names = {
            "NOVA": "노바테크", "NEOG": "네오게임즈", "GREEN": "그린에너지",
            "ORBIT": "오빗모빌리티", "BLOOM": "블룸바이오", "CLOUD": "클라우드웨이브",
            "FOOD": "테이블랩", "LUXE": "루미에르",
        }
        existing = self.companies_ref.get() or {}

        for old_symbol, new_symbol in legacy_map.items():
            old_row = existing.get(old_symbol)
            seed = seed_companies.get(new_symbol)
            if not isinstance(old_row, dict) or not isinstance(seed, dict):
                continue
            current_target = self.companies_ref.child(new_symbol).get()
            if not isinstance(current_target, dict):
                migrated = dict(old_row)
                migrated.update({key: seed[key] for key in ("name", "sector", "description", "volatility") if key in seed})
                migrated["price"] = max(100, int(old_row.get("price", seed.get("price", 100))))
                migrated.setdefault("previous_price", migrated["price"])
                migrated.setdefault("volume", 0)
                migrated.setdefault("buy_pressure", 0)
                migrated.setdefault("sell_pressure", 0)
                migrated.setdefault("last_change_pct", 0.0)
                migrated.setdefault("updated_at", now)
                migrated.setdefault("history_count", 0)
                self.companies_ref.child(new_symbol).set(migrated)
            old_history = self.history_ref.child(old_symbol).get()
            if old_history and not self.history_ref.child(new_symbol).get():
                self.history_ref.child(new_symbol).set(old_history)

        # Move any legacy holdings to the new keys and retain weighted average cost.
        players = self.players_ref.get() or {}
        for user_id, original in players.items():
            if not isinstance(original, dict):
                continue
            old_holdings = original.get("holdings", {}) or {}
            old_recent_trades = original.get("recent_trades", []) or []
            has_old_holding = any(old_symbol in old_holdings for old_symbol in legacy_map)
            has_old_trade = any(
                isinstance(trade, dict) and trade.get("symbol") in legacy_map
                for trade in old_recent_trades
            )
            if not has_old_holding and not has_old_trade:
                continue

            def migrate_account(current: Any) -> Any:
                if not isinstance(current, dict):
                    return current
                account = dict(current)
                holdings = dict(account.get("holdings", {}) or {})
                for old_symbol, new_symbol in legacy_map.items():
                    old_holding = holdings.pop(old_symbol, None)
                    if not isinstance(old_holding, dict):
                        continue
                    old_qty = max(0, int(old_holding.get("quantity", 0)))
                    if old_qty <= 0:
                        continue
                    current_holding = dict(holdings.get(new_symbol, {}) or {})
                    current_qty = max(0, int(current_holding.get("quantity", 0)))
                    old_cost = float(old_holding.get("average_cost", 0))
                    current_cost = float(current_holding.get("average_cost", 0))
                    total_qty = old_qty + current_qty
                    holdings[new_symbol] = {
                        "quantity": total_qty,
                        "average_cost": round((old_qty * old_cost + current_qty * current_cost) / total_qty, 2),
                    }
                account["holdings"] = holdings
                trades = list(account.get("recent_trades", []) or [])
                for trade in trades:
                    if not isinstance(trade, dict):
                        continue
                    old_symbol = trade.get("symbol")
                    if old_symbol in legacy_map:
                        new_symbol = legacy_map[old_symbol]
                        trade["symbol"] = new_symbol
                        trade["company_name"] = str(seed_companies.get(new_symbol, {}).get("name", new_symbol))
                account["recent_trades"] = trades
                account["updated_at"] = now
                return account

            self.players_ref.child(str(user_id)).transaction(migrate_account)

        # Keep older trade/history entries readable under the renamed companies.
        trade_users = self.trade_history_ref.get() or {}
        for user_id, user_trades in trade_users.items():
            if not isinstance(user_trades, dict):
                continue
            for trade_id, trade in user_trades.items():
                if not isinstance(trade, dict) or trade.get("symbol") not in legacy_map:
                    continue
                new_symbol = legacy_map[str(trade["symbol"])]
                self.trade_history_ref.child(str(user_id)).child(str(trade_id)).update({
                    "symbol": new_symbol,
                    "company_name": str(seed_companies.get(new_symbol, {}).get("name", new_symbol)),
                })

        news_items = self.news_ref.get() or {}
        for news_id, item in news_items.items():
            if not isinstance(item, dict) or item.get("symbol") not in legacy_map:
                continue
            old_symbol = str(item["symbol"])
            new_symbol = legacy_map[old_symbol]
            new_name = str(seed_companies.get(new_symbol, {}).get("name", new_symbol))
            headline = str(item.get("headline", ""))
            if legacy_names.get(old_symbol):
                headline = headline.replace(legacy_names[old_symbol], new_name)
            self.news_ref.child(str(news_id)).update({
                "symbol": new_symbol,
                "company_name": new_name,
                "headline": headline,
            })

        # Remove retired demo ticker records only after their holdings/history are migrated.
        for old_symbol in legacy_map:
            if old_symbol in existing:
                self.companies_ref.child(old_symbol).delete()
            self.history_ref.child(old_symbol).delete()

        existing = self.companies_ref.get() or {}
        for symbol, seed in seed_companies.items():
            defaults = {
                **seed,
                "previous_price": int(seed["price"]),
                "volume": 0,
                "buy_pressure": 0,
                "sell_pressure": 0,
                "last_change_pct": 0.0,
                "updated_at": now,
                "history_count": 0,
            }
            if symbol not in existing:
                self.companies_ref.child(symbol).set(defaults)
                self.record_price_history(symbol, int(seed["price"]), 0, prune=False)
            else:
                current = dict(existing[symbol] or {})
                # Company metadata comes from data/companies.json so the owner can rename
                # firms or sectors without resetting the current market price.
                for key in ("name", "sector", "description", "volatility"):
                    if key in seed:
                        current[key] = seed[key]
                for key, value in defaults.items():
                    current.setdefault(key, value)
                self.companies_ref.child(symbol).set(current)
                if not self.history_ref.child(symbol).get():
                    self.record_price_history(symbol, int(current.get("price", seed["price"])), 0, prune=False)

        def initialize_market(current: Any) -> dict[str, Any]:
            if current:
                current = dict(current)
                current.setdefault("index_value", 1000.0)
                current.setdefault("previous_index", current["index_value"])
                current.setdefault("mood", 0.0)
                current.setdefault("tick_count", 0)
                current.setdefault("last_tick", now)
                current.setdefault("total_volume", 0)
                return current
            return {
                "index_value": 1000.0,
                "previous_index": 1000.0,
                "mood": 0.0,
                "tick_count": 0,
                "last_tick": now,
                "total_volume": 0,
            }

        self.market_ref.transaction(initialize_market)
        logger.info("Firebase seed data checked: %d companies", len(seed_companies))

    def get_companies(self) -> dict[str, dict[str, Any]]:
        data = self.companies_ref.get() or {}
        return {str(symbol): dict(value) for symbol, value in data.items() if isinstance(value, dict)}

    def get_company(self, symbol: str) -> dict[str, Any] | None:
        data = self.companies_ref.child(symbol.upper()).get()
        return dict(data) if isinstance(data, dict) else None

    def get_player(self, user_id: int | str) -> dict[str, Any] | None:
        data = self.players_ref.child(str(user_id)).get()
        return dict(data) if isinstance(data, dict) else None

    def get_all_players(self) -> dict[str, dict[str, Any]]:
        data = self.players_ref.get() or {}
        return {str(uid): dict(value) for uid, value in data.items() if isinstance(value, dict)}

    def get_market_state(self) -> dict[str, Any]:
        return dict(self.market_ref.get() or {})

    def record_price_history(self, symbol: str, price: int, volume: int, prune: bool = False) -> None:
        ref = self.history_ref.child(symbol)
        ref.push({"timestamp": time.time(), "price": int(price), "volume": int(volume)})
        if prune:
            entries = ref.get() or {}
            if len(entries) > MAX_HISTORY_POINTS:
                ordered = sorted(entries.items(), key=lambda pair: float((pair[1] or {}).get("timestamp", 0)))
                ref.update({key: None for key, _ in ordered[:len(ordered) - MAX_HISTORY_POINTS]})

    def get_price_history(self, symbol: str, limit: int = 240) -> list[dict[str, Any]]:
        entries = self.history_ref.child(symbol.upper()).get() or {}
        points = [dict(value) for value in entries.values() if isinstance(value, dict)]
        points.sort(key=lambda point: float(point.get("timestamp", 0)))
        return points[-max(2, min(int(limit), MAX_HISTORY_POINTS)):]

    def add_news(self, news_item: dict[str, Any]) -> str:
        return self.news_ref.push(news_item).key or ""

    def get_recent_news(self, limit: int = 8) -> list[dict[str, Any]]:
        data = self.news_ref.get() or {}
        items = [dict(value) for value in data.values() if isinstance(value, dict)]
        items.sort(key=lambda item: float(item.get("timestamp", 0)), reverse=True)
        return items[:max(1, min(int(limit), 20))]

    def add_trade_history(self, user_id: int | str, trade: dict[str, Any]) -> None:
        self.trade_history_ref.child(str(user_id)).push(trade)

    def get_trade_history(self, user_id: int | str, limit: int = 10) -> list[dict[str, Any]]:
        data = self.trade_history_ref.child(str(user_id)).get() or {}
        items = [dict(value) for value in data.values() if isinstance(value, dict)]
        items.sort(key=lambda item: float(item.get("timestamp", 0)), reverse=True)
        return items[:max(1, min(int(limit), 30))]

    def update_company_pressure(self, symbol: str, side: str, quantity: int) -> None:
        ref = self.companies_ref.child(symbol.upper())

        def update(current: Any) -> Any:
            if not isinstance(current, dict):
                return current
            current = dict(current)
            current["volume"] = int(current.get("volume", 0)) + quantity
            pressure_key = "buy_pressure" if side == "buy" else "sell_pressure"
            current[pressure_key] = int(current.get(pressure_key, 0)) + quantity
            return current

        ref.transaction(update)
