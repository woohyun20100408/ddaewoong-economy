"""Virtual-currency trading, protected by Firebase account transactions."""
from __future__ import annotations

import copy
import logging
import math
import time
import uuid
from typing import Any

from services.firebase_store import FirebaseStore
from services.economy_items import best_owned_item

logger = logging.getLogger(__name__)
MAX_ORDER_QUANTITY = 100_000
MAX_ORDER_VALUE = 1_000_000_000
MAX_RECENT_TRADES = 50

WORK_REWARD = 10_000
WORK_COOLDOWN_SECONDS = 60 * 60
MAX_TRANSFER_AMOUNT = 1_000_000_000


class TradeError(Exception):
    """A trade rejected by game rules; safe to show to the user."""


class TradingService:
    def __init__(self, store: FirebaseStore, starting_cash: int, fee_rate: float) -> None:
        self.store = store
        self.starting_cash = int(starting_cash)
        self.fee_rate = float(fee_rate)

    def ensure_account(self, user_id: int | str, username: str) -> tuple[dict[str, Any], bool]:
        user_key, now = str(user_id), time.time()
        result_flags = {"created": False}
        ref = self.store.players_ref.child(user_key)

        def upsert(current: Any) -> dict[str, Any]:
            if isinstance(current, dict):
                account = copy.deepcopy(current)
                account["username"] = username[:80]
                account["updated_at"] = now
                result_flags["created"] = False
                return account
            result_flags["created"] = True
            return {
                "user_id": user_key,
                "username": username[:80],
                "cash": self.starting_cash,
                "initial_cash": self.starting_cash,
                "holdings": {},
                "realized_pnl": 0,
                "trades_count": 0,
                "recent_trades": [],
                "created_at": now,
                "updated_at": now,
            }

        account = ref.transaction(upsert)
        return dict(account or {}), bool(result_flags["created"])
    
    def claim_work(
        self,
        user_id: int | str,
        username: str,
    ) -> dict[str, Any]:
        """Give a player 10,000 cash once per hour."""
        now = time.time()
        user_key = str(user_id)
        failure: dict[str, str | None] = {"reason": None}
        result: dict[str, Any] = {}

        account_ref = self.store.players_ref.child(user_key)

        def mutate(current: Any) -> Any:
            failure["reason"] = None
            result.clear()

            if not isinstance(current, dict):
                failure["reason"] = (
                    "계좌가 없습니다. 먼저 `/계좌개설`로 계좌를 개설해 주세요."
                )
                return current

            account = copy.deepcopy(current)
            last_work_at = float(account.get("work_last_claim", 0))
            elapsed = now - last_work_at

            if elapsed < WORK_COOLDOWN_SECONDS:
                remaining = max(
                    1,
                    math.ceil(WORK_COOLDOWN_SECONDS - elapsed),
                )
                minutes, seconds = divmod(remaining, 60)

                failure["reason"] = (
                    "아직 일하기 쿨타임이 남았습니다. "
                    f"**{minutes}분 {seconds}초** 후 다시 이용해 주세요."
                )
                return current

            cash = int(account.get("cash", 0))
            new_cash = cash + WORK_REWARD

            account["cash"] = new_cash
            account["work_last_claim"] = now
            account["username"] = username[:80]
            account["updated_at"] = now

            result.update({
                "reward": WORK_REWARD,
                "cash": new_cash,
                "next_work_at": now + WORK_COOLDOWN_SECONDS,
            })

            return account

        account_after = account_ref.transaction(mutate)

        if failure["reason"]:
            raise TradeError(failure["reason"])

        if not result or not isinstance(account_after, dict):
            raise TradeError(
                "일하기 처리에 실패했습니다. 잠시 후 다시 시도해 주세요."
            )

        return dict(result)

    def transfer_cash(
        self,
        sender_id: int | str,
        sender_name: str,
        recipient_id: int | str,
        recipient_name: str,
        amount: int,
    ) -> dict[str, Any]:
        """Transfer cash atomically between existing player accounts."""
        sender_key = str(sender_id)
        recipient_key = str(recipient_id)

        if sender_key == recipient_key:
            raise TradeError("자기 자신에게는 송금할 수 없습니다.")

        if (
            not isinstance(amount, int)
            or not 1 <= amount <= MAX_TRANSFER_AMOUNT
        ):
            raise TradeError(
                f"송금 금액은 1원부터 ₩{MAX_TRANSFER_AMOUNT:,}까지 가능합니다."
            )

        failure: dict[str, str | None] = {"reason": None}
        result: dict[str, Any] = {}
        now = time.time()

        def mutate(current: Any) -> Any:
            failure["reason"] = None
            result.clear()

            if not isinstance(current, dict):
                failure["reason"] = "계좌 데이터를 찾을 수 없습니다."
                return current

            players = copy.deepcopy(current)
            sender = players.get(sender_key)
            recipient = players.get(recipient_key)

            if not isinstance(sender, dict):
                failure["reason"] = (
                    "계좌가 없습니다. 먼저 `/계좌개설`로 계좌를 개설해 주세요."
                )
                return current

            if not isinstance(recipient, dict):
                failure["reason"] = (
                    f"{recipient_name}님은 아직 계좌가 없습니다. "
                    "받는 사람도 `/계좌개설`을 먼저 이용해야 합니다."
                )
                return current

            sender_cash = int(sender.get("cash", 0))
            recipient_cash = int(recipient.get("cash", 0))

            if sender_cash < amount:
                failure["reason"] = (
                    f"잔액이 부족합니다. 현재 잔액은 ₩{sender_cash:,}입니다."
                )
                return current

            sender["cash"] = sender_cash - amount
            recipient["cash"] = recipient_cash + amount

            sender["username"] = sender_name[:80]
            recipient["username"] = recipient_name[:80]
            sender["updated_at"] = now
            recipient["updated_at"] = now

            players[sender_key] = sender
            players[recipient_key] = recipient

            result.update({
                "amount": amount,
                "sender_cash": sender["cash"],
                "recipient_cash": recipient["cash"],
            })

            return players

        # 두 계좌의 잔액을 하나의 Firebase 트랜잭션으로 갱신한다.
        players_after = self.store.players_ref.transaction(mutate)

        if failure["reason"]:
            raise TradeError(failure["reason"])

        if (
            not result
            or not isinstance(players_after, dict)
            or sender_key not in players_after
            or recipient_key not in players_after
        ):
            raise TradeError(
                "송금을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요."
            )

        return dict(result)

    def execute_trade(self, user_id: int | str, username: str, symbol: str, side: str, quantity: int) -> dict[str, Any]:
        symbol, side = symbol.upper().strip(), side.lower().strip()
        if side not in {"buy", "sell"}:
            raise TradeError("매수 또는 매도만 할 수 있습니다.")
        if not isinstance(quantity, int) or quantity < 1 or quantity > MAX_ORDER_QUANTITY:
            raise TradeError(f"수량은 1~{MAX_ORDER_QUANTITY:,}주 사이여야 합니다.")

        company = self.store.get_company(symbol)
        if not company:
            raise TradeError(f"'{symbol}' 회사를 찾을 수 없습니다. `/시장`에서 회사 이름을 확인해 주세요.")
        price = int(company.get("price", 0))
        if price <= 0:
            raise TradeError("현재 종목 가격이 올바르지 않아 거래할 수 없습니다.")
        gross = price * quantity
        if gross > MAX_ORDER_VALUE:
            raise TradeError(f"1회 주문 금액은 ₩{MAX_ORDER_VALUE:,} 이하여야 합니다.")

        base_fee = max(1, math.ceil(gross * self.fee_rate))
        timestamp, trade_id = time.time(), uuid.uuid4().hex[:12]
        failure: dict[str, str | None] = {"reason": None}
        result_trade: dict[str, Any] = {}
        account_ref = self.store.players_ref.child(str(user_id))

        def mutate(current: Any) -> Any:
            failure["reason"] = None
            result_trade.clear()
            if not isinstance(current, dict):
                failure["reason"] = "계좌가 없습니다. 먼저 `/계좌개설`로 계좌를 개설해 주세요."
                return current

            account = copy.deepcopy(current)
            cash = int(account.get("cash", 0))
            holdings = copy.deepcopy(account.get("holdings", {}) or {})
            holding = dict(holdings.get(symbol, {}) or {})
            held_qty = int(holding.get("quantity", 0))
            avg_cost = float(holding.get("average_cost", 0.0))

            # Apply a calculator discount inside the same account transaction
            # that charges the fee, so concurrent commands cannot bypass it.
            fee = base_fee
            calculator = best_owned_item(account, "calculator")
            if calculator:
                fee = max(0, math.ceil(base_fee * (1 - float(calculator["value"]))))

            if side == "buy":
                total_cost = gross + fee
                if cash < total_cost:
                    failure["reason"] = f"잔액이 부족합니다. 필요 ₩{total_cost:,}이고, 현재 ₩{cash:,}입니다."
                    return current
                new_qty = held_qty + quantity
                holdings[symbol] = {
                    "quantity": new_qty,
                    "average_cost": round((avg_cost * held_qty + gross + fee) / new_qty, 2),
                }
                cash_delta = -total_cost
                account["cash"] = cash + cash_delta
                realized_pnl: int | None = None
            else:
                if held_qty < quantity:
                    failure["reason"] = f"보유 수량이 부족합니다. 보유 {held_qty:,}주, 요청 {quantity:,}주입니다."
                    return current
                proceeds = gross - fee
                realized_pnl = int(round(proceeds - avg_cost * quantity))
                remaining = held_qty - quantity
                if remaining:
                    holdings[symbol] = {"quantity": remaining, "average_cost": round(avg_cost, 2)}
                else:
                    holdings.pop(symbol, None)
                cash_delta = proceeds
                account["cash"] = cash + proceeds
                account["realized_pnl"] = int(account.get("realized_pnl", 0)) + realized_pnl

            # 장기 투자 증서를 보유한 상태에서 매수한 종목을 추적한다.
            # 한 번에 한 종목을 추적하며, 추가 매수하면 보유 기간을 다시 계산한다.
            item_state = account.setdefault("item_system", {})
            position = item_state.get("long_term_position")
            certificate = best_owned_item(account, "certificate")
            has_certificate = certificate is not None

            if side == "buy" and has_certificate:
                added_investment = gross + fee
                if isinstance(position, dict) and position.get("symbol") == symbol:
                    position["shares"] = int(position.get("shares", 0)) + quantity
                    position["investment_value"] = (
                        int(position.get("investment_value", 0)) + added_investment
                    )
                    position["next_reward_at"] = timestamp + 3 * 86400
                elif not isinstance(position, dict) or not position.get("symbol"):
                    item_state["long_term_position"] = {
                        "symbol": symbol,
                        "company_name": str(company.get("name", symbol)),
                        "shares": quantity,
                        "investment_value": added_investment,
                        "started_at": timestamp,
                        "next_reward_at": timestamp + 3 * 86400,
                    }
            elif side == "sell" and isinstance(position, dict) and position.get("symbol") == symbol:
                tracked_shares = max(0, int(position.get("shares", 0)))
                removed_tracked = min(quantity, tracked_shares)
                new_tracked_shares = tracked_shares - removed_tracked
                if new_tracked_shares <= 0:
                    item_state["long_term_position"] = None
                else:
                    old_investment = max(0, int(position.get("investment_value", 0)))
                    position["shares"] = new_tracked_shares
                    position["investment_value"] = math.floor(
                        old_investment * new_tracked_shares / max(1, tracked_shares)
                    )

            result_trade.update({
                "trade_id": trade_id, "timestamp": timestamp, "symbol": symbol,
                "company_name": str(company.get("name", symbol)), "side": side,
                "quantity": quantity, "price": price, "gross": gross, "fee": fee,
                "cash_delta": cash_delta, "realized_pnl": realized_pnl,
            })
            recent = list(account.get("recent_trades", []) or [])
            recent.append(dict(result_trade))
            account["recent_trades"] = recent[-MAX_RECENT_TRADES:]
            account["holdings"] = holdings
            account["username"] = username[:80]
            account["trades_count"] = int(account.get("trades_count", 0)) + 1
            account["updated_at"] = timestamp
            return account

        account_after = account_ref.transaction(mutate)
        if failure["reason"]:
            raise TradeError(failure["reason"])
        if not result_trade or not isinstance(account_after, dict):
            raise TradeError("거래를 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.")

        # The account update has succeeded; secondary records must not reverse that success.
        try:
            self.store.add_trade_history(user_id, dict(result_trade))
        except Exception:
            logger.exception("Trade committed, but history append failed: %s", trade_id)
        try:
            self.store.update_company_pressure(symbol, side, quantity)
        except Exception:
            logger.exception("Trade committed, but market-volume update failed: %s", trade_id)
        return dict(result_trade)
