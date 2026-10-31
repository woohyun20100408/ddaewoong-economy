"""Collectible economy items for the stock-market Discord bot.

All passive effects activate automatically when an item is owned.
For passive item categories, only the highest owned tier is active.
Consumables are stored in the same inventory but are not passive.
"""
from __future__ import annotations

import copy
import math
import time
from datetime import datetime
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from services.firebase_store import FirebaseStore


SEOUL = ZoneInfo("Asia/Seoul")

# 장기 투자 증서
LONG_TERM_INITIAL_HOLD_HOURS = 24
LONG_TERM_REWARD_INTERVAL_SECONDS = 60 * 60
LONG_TERM_SCHEDULE_VERSION = 2

# 비상금 서류가방
EMERGENCY_COOLDOWN_DAYS = 3
EMERGENCY_CASH_THRESHOLD = 10_000

# 주가조작권
CONSUMABLE_ITEM_ID = "market_manipulation"
MARKET_MANIPULATION_PRICE = 20_000
MARKET_MANIPULATION_CHANGE = 0.05

CATEGORIES = {
    "calculator": "회계용 계산기",
    "piggybank": "황금 저금통",
    "certificate": "장기 투자 증서",
    "emergency_bag": "비상금 서류가방",
}

TIERS = ["일반", "좋은", "고급", "최고급", "명품"]


CATALOG: dict[str, dict[str, Any]] = {
    "calculator": {
        "base_name": "회계용 계산기",
        "prices": [
            10_000,
            20_000,
            30_000,
            40_000,
            50_000,
        ],
        "values": [
            0.20,
            0.40,
            0.60,
            0.80,
            1.00,
        ],
        "effects": [
            "주식 거래 수수료 20% 할인",
            "주식 거래 수수료 40% 할인",
            "주식 거래 수수료 60% 할인",
            "주식 거래 수수료 80% 할인",
            "주식 거래 수수료 100% 할인",
        ],
    },

    "piggybank": {
        "base_name": "황금 저금통",
        "prices": [
            20_000,
            60_000,
            180_000,
            500_000,
            1_500_000,
        ],
        "values": [
            0.002,
            0.004,
            0.006,
            0.008,
            0.010,
        ],
        "effects": [
            "하루 보유 현금의 0.2% 이자",
            "하루 보유 현금의 0.4% 이자",
            "하루 보유 현금의 0.6% 이자",
            "하루 보유 현금의 0.8% 이자",
            "하루 보유 현금의 1.0% 이자",
        ],
    },

    "certificate": {
        "base_name": "장기 투자 증서",
        "prices": [
            30_000,
            100_000,
            250_000,
            600_000,
            1_500_000,
        ],
        "values": [
            0.0002,
            0.0004,
            0.0006,
            0.0008,
            0.0010,
        ],
        "effects": [
            "주식 24시간 보유 후 매시간 보유 현금의 0.02% 지급",
            "주식 24시간 보유 후 매시간 보유 현금의 0.04% 지급",
            "주식 24시간 보유 후 매시간 보유 현금의 0.06% 지급",
            "주식 24시간 보유 후 매시간 보유 현금의 0.08% 지급",
            "주식 24시간 보유 후 매시간 보유 현금의 0.10% 지급",
        ],
    },

    "emergency_bag": {
        "base_name": "비상금 서류가방",
        "prices": [
            125_000,
            220_000,
            525_000,
            850_000,
            1_750_000,
        ],
        "values": [
            50_000,
            100_000,
            200_000,
            300_000,
            500_000,
        ],
        "effects": [
            "현금 10,000원 이하일 때 50,000원 지급 (3일 대기)",
            "현금 10,000원 이하일 때 100,000원 지급 (3일 대기)",
            "현금 10,000원 이하일 때 200,000원 지급 (3일 대기)",
            "현금 10,000원 이하일 때 300,000원 지급 (3일 대기)",
            "현금 10,000원 이하일 때 500,000원 지급 (3일 대기)",
        ],
    },
}


# =========================================================
# 등급별 아이템 데이터 생성
# =========================================================

ITEMS: dict[str, dict[str, Any]] = {}

for category, spec in CATALOG.items():
    for tier_index, tier_name in enumerate(TIERS):
        item_id = f"{category}_{tier_index + 1}"

        ITEMS[item_id] = {
            "id": item_id,
            "category": category,
            "tier": tier_index,
            "tier_name": tier_name,
            "name": f"{tier_name} {spec['base_name']}",
            "price": int(spec["prices"][tier_index]),
            "value": spec["values"][tier_index],
            "effect": spec["effects"][tier_index],
            "consumable": False,
        }


# =========================================================
# 소모품: 주가조작권
# =========================================================

ITEMS[CONSUMABLE_ITEM_ID] = {
    "id": CONSUMABLE_ITEM_ID,
    "category": "consumable",
    "tier": 0,
    "tier_name": "소모품",
    "name": "주가조작권",
    "price": MARKET_MANIPULATION_PRICE,
    "value": MARKET_MANIPULATION_CHANGE,
    "effect": (
        "무작위 종목 주가 5% 상승 또는 "
        "선택한 종목 주가 5% 하락"
    ),
    "consumable": True,
}


class ItemError(Exception):
    """Expected error that can safely be displayed to a Discord user."""


def _safe_int(value: Any, default: int = 0) -> int:
    """Convert a value to int without raising on malformed stored data."""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default


def best_owned_item(
    account: dict[str, Any],
    category: str,
) -> dict[str, Any] | None:
    """Return the highest-tier owned passive item in a category."""
    if category not in CATEGORIES:
        return None

    inventory = account.get("items", {}) or {}

    if not isinstance(inventory, dict):
        return None

    owned: list[dict[str, Any]] = []

    for item_id, raw_count in inventory.items():
        item = ITEMS.get(item_id) if isinstance(item_id, str) else None
        count = _safe_int(raw_count)

        if (
            item
            and item["category"] == category
            and count > 0
        ):
            owned.append(item)

    return (
        max(owned, key=lambda item: int(item["tier"]))
        if owned
        else None
    )


def active_owned_items(
    account: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return the automatically active highest-tier item per category."""
    active: dict[str, dict[str, Any]] = {}

    for category in CATEGORIES:
        item = best_owned_item(account, category)

        if item:
            active[category] = item

    return active


class EconomyItemService:
    def __init__(self, store: FirebaseStore) -> None:
        self.store = store

    def _player_ref(self, user_id: int | str):
        return self.store.players_ref.child(str(user_id))

    def get_player(
        self,
        user_id: int | str,
    ) -> dict[str, Any] | None:
        data = self._player_ref(user_id).get()

        return dict(data) if isinstance(data, dict) else None

    def get_active_items(
        self,
        user_id: int | str,
    ) -> dict[str, dict[str, Any]]:
        account = self.get_player(user_id) or {}
        return active_owned_items(account)

    # =====================================================
    # 아이템 구매
    # =====================================================

    def buy(
        self,
        user_id: int | str,
        item_id: str,
    ) -> dict[str, Any]:
        item = ITEMS.get(item_id)

        if item is None:
            raise ItemError("존재하지 않는 아이템입니다.")

        result: dict[str, Any] = {"status": None}

        def mutate(current: Any) -> Any:
            result["status"] = None

            if not isinstance(current, dict):
                result["status"] = "no_account"
                return current

            account = copy.deepcopy(current)

            cash = max(
                0,
                _safe_int(account.get("cash", 0)),
            )
            price = int(item["price"])

            if cash < price:
                result["status"] = "no_money"
                return current

            inventory = account.setdefault("items", {})

            if not isinstance(inventory, dict):
                inventory = {}
                account["items"] = inventory

            previous_count = max(
                0,
                _safe_int(inventory.get(item_id, 0)),
            )

            already_owned_category = (
                best_owned_item(account, item["category"]) is not None
            )

            inventory[item_id] = previous_count + 1

            # 황금 저금통을 처음 구매한 날에는
            # 구매 직후 이자가 지급되지 않도록 한다.
            if (
                item["category"] == "piggybank"
                and not already_owned_category
            ):
                state = account.setdefault("item_system", {})

                if isinstance(state, dict):
                    state["last_interest_date"] = (
                        datetime.now(SEOUL).date().isoformat()
                    )

            account["cash"] = cash - price
            account["updated_at"] = time.time()

            result.update({
                "status": "ok",
                "cash": cash - price,
                "item_id": item_id,
            })

            return account

        self._player_ref(user_id).transaction(mutate)

        if result["status"] == "no_account":
            raise ItemError(
                "계좌가 없습니다. 먼저 `/계좌개설`을 이용해 주세요."
            )

        if result["status"] == "no_money":
            raise ItemError(
                f"현금이 부족합니다. 필요한 금액은 ₩{item['price']:,}입니다."
            )

        if result["status"] != "ok":
            raise ItemError(
                "아이템 구매에 실패했습니다. 잠시 후 다시 시도해 주세요."
            )

        return {**item, **result}

    # =====================================================
    # 소모품 수량 확인
    # =====================================================

    def get_item_count(
        self,
        user_id: int | str,
        item_id: str,
    ) -> int:
        """Return the player's current quantity of an item."""
        account = self.get_player(user_id) or {}
        inventory = account.get("items", {})

        if not isinstance(inventory, dict):
            return 0

        return max(0, _safe_int(inventory.get(item_id, 0)))

    # =====================================================
    # 소모품 차감
    # =====================================================

    def consume_one(
        self,
        user_id: int | str,
        item_id: str,
    ) -> None:
        """Atomically consume one item using a Firebase transaction."""
        item = ITEMS.get(item_id)

        if item is None or not item.get("consumable"):
            raise ItemError("사용할 수 있는 소모품이 아닙니다.")

        result: dict[str, Any] = {"status": None}

        def mutate(current: Any) -> Any:
            result["status"] = None

            if not isinstance(current, dict):
                result["status"] = "no_account"
                return current

            inventory = current.get("items", {})

            if not isinstance(inventory, dict):
                result["status"] = "no_item"
                return current

            count = max(
                0,
                _safe_int(inventory.get(item_id, 0)),
            )

            if count < 1:
                result["status"] = "no_item"
                return current

            account = copy.deepcopy(current)
            new_inventory = dict(account.get("items", {}))

            if count == 1:
                new_inventory.pop(item_id, None)
            else:
                new_inventory[item_id] = count - 1

            account["items"] = new_inventory
            account["updated_at"] = time.time()

            result["status"] = "ok"
            result["remaining"] = count - 1

            return account

        self._player_ref(user_id).transaction(mutate)

        if result["status"] == "no_account":
            raise ItemError("계좌를 찾을 수 없습니다.")

        if result["status"] == "no_item":
            raise ItemError(
                f"{item['name']}을(를) 보유하고 있지 않습니다."
            )

        if result["status"] != "ok":
            raise ItemError(
                "아이템 차감에 실패했습니다. 잠시 후 다시 시도해 주세요."
            )

    # =====================================================
    # 소모품 반환
    # 효과 적용이 실패한 경우 호출
    # =====================================================

    def restore_one(
        self,
        user_id: int | str,
        item_id: str,
    ) -> None:
        """Restore one consumed item after an operation fails."""
        item = ITEMS.get(item_id)

        if item is None or not item.get("consumable"):
            raise ItemError("반환할 수 있는 소모품이 아닙니다.")

        result: dict[str, Any] = {"status": None}

        def mutate(current: Any) -> Any:
            result["status"] = None

            if not isinstance(current, dict):
                result["status"] = "no_account"
                return current

            account = copy.deepcopy(current)
            inventory = account.get("items", {})

            if not isinstance(inventory, dict):
                inventory = {}

            inventory = dict(inventory)

            current_count = max(
                0,
                _safe_int(inventory.get(item_id, 0)),
            )

            inventory[item_id] = current_count + 1

            account["items"] = inventory
            account["updated_at"] = time.time()

            result["status"] = "ok"
            return account

        self._player_ref(user_id).transaction(mutate)

        if result["status"] != "ok":
            raise ItemError(
                "아이템 반환에 실패했습니다. 관리자 확인이 필요합니다."
            )

    # =====================================================
    # 회계용 계산기: 주식 거래 수수료 할인
    # =====================================================

    def apply_calculator_discount(
        self,
        user_id: int | str,
        original_fee: int | float,
    ) -> int:
        account = self.get_player(user_id) or {}
        calculator = best_owned_item(account, "calculator")

        fee = max(0, _safe_int(original_fee))

        if not calculator:
            return fee

        discount_rate = float(calculator["value"])

        discounted_fee = math.ceil(
            fee * (1.0 - discount_rate)
        )

        return max(0, discounted_fee)

    # =====================================================
    # 장기 투자 증서: 매수 기록
    # 매수 성공 후 기존 TradingService에서 호출해야 한다.
    # =====================================================

    def on_stock_bought(
        self,
        user_id: int | str,
        stock_name: str,
        quantity: int,
        unit_price: int,
    ) -> None:
        """Record a stock purchase for the long-term certificate.

        The player must own a certificate when the purchase occurs.
        A single position is tracked at a time.

        Buying more shares of the tracked stock adds to the position
        and restarts the 24-hour waiting period.
        """
        quantity = max(0, _safe_int(quantity))
        unit_price = max(0, _safe_int(unit_price))

        if quantity <= 0 or unit_price <= 0:
            return

        now = datetime.now(SEOUL).timestamp()
        added_investment = quantity * unit_price
        ref = self._player_ref(user_id)

        def mutate(current: Any) -> Any:
            if not isinstance(current, dict):
                return current

            account = copy.deepcopy(current)

            certificate = best_owned_item(
                account,
                "certificate",
            )

            if certificate is None:
                return current

            state = account.setdefault("item_system", {})

            if not isinstance(state, dict):
                state = {}
                account["item_system"] = state

            position = state.get("long_term_position")

            if isinstance(position, dict):
                tracked_name = position.get("stock_name")

                # 이미 다른 종목을 추적 중이라면 교체하지 않는다.
                if tracked_name != stock_name:
                    return current

                old_shares = max(
                    0,
                    _safe_int(position.get("shares", 0)),
                )

                old_investment = max(
                    0,
                    _safe_int(position.get("investment_value", 0)),
                )

                position["shares"] = old_shares + quantity
                position["investment_value"] = (
                    old_investment + added_investment
                )

                # 같은 종목 추가 매수 시 24시간 대기를 다시 시작한다.
                position["started_at"] = now
                position["next_reward_at"] = (
                    now + LONG_TERM_INITIAL_HOLD_HOURS * 3600
                )
                position["schedule_version"] = (
                    LONG_TERM_SCHEDULE_VERSION
                )

                account["updated_at"] = time.time()
                return account

            state["long_term_position"] = {
                "stock_name": str(stock_name),
                "shares": quantity,
                "investment_value": added_investment,
                "started_at": now,
                "next_reward_at": (
                    now + LONG_TERM_INITIAL_HOLD_HOURS * 3600
                ),
                "schedule_version": LONG_TERM_SCHEDULE_VERSION,
            }

            account["updated_at"] = time.time()
            return account

        ref.transaction(mutate)

    # =====================================================
    # 장기 투자 증서: 매도 기록
    # 매도 성공 후 기존 TradingService에서 호출해야 한다.
    # =====================================================

    def on_stock_sold(
        self,
        user_id: int | str,
        stock_name: str,
        remaining_quantity: int,
    ) -> None:
        """Update the tracked position after a successful sale.

        remaining_quantity should be the player's share count after sale.
        """
        remaining_quantity = max(
            0,
            _safe_int(remaining_quantity),
        )

        ref = self._player_ref(user_id)

        def mutate(current: Any) -> Any:
            if not isinstance(current, dict):
                return current

            account = copy.deepcopy(current)
            state = account.get("item_system", {})

            if not isinstance(state, dict):
                return current

            position = state.get("long_term_position")

            if not isinstance(position, dict):
                return current

            if position.get("stock_name") != stock_name:
                return current

            old_shares = max(
                0,
                _safe_int(position.get("shares", 0)),
            )

            old_investment = max(
                0,
                _safe_int(position.get("investment_value", 0)),
            )

            if remaining_quantity <= 0 or old_shares <= 0:
                state["long_term_position"] = None
            else:
                ratio = min(
                    1.0,
                    remaining_quantity / old_shares,
                )

                position["shares"] = remaining_quantity
                position["investment_value"] = math.floor(
                    old_investment * ratio
                )

                # 일부 매도 시에는 보유 기간을 초기화하지 않는다.

            account["updated_at"] = time.time()
            return account

        ref.transaction(mutate)

    # =====================================================
    # 자동 효과 처리
    # 외부의 주기적 작업에서 플레이어별로 호출해야 한다.
    # =====================================================

    def process_player(self, user_id: int | str) -> None:
        """Apply passive item effects based on ownership."""
        now_dt = datetime.now(SEOUL)
        now = now_dt.timestamp()
        today = now_dt.date().isoformat()

        ref = self._player_ref(user_id)

        def mutate(current: Any) -> Any:
            if not isinstance(current, dict):
                return current

            account = copy.deepcopy(current)
            owned = active_owned_items(account)

            if not owned:
                return current

            state = account.setdefault("item_system", {})

            if not isinstance(state, dict):
                state = {}
                account["item_system"] = state

            cash = max(
                0,
                _safe_int(account.get("cash", 0)),
            )

            dirty = False

            # -------------------------------------------------
            # 1. 황금 저금통
            # 서울 날짜 기준 하루 한 번, 보유 현금 기준
            # -------------------------------------------------

            piggybank = owned.get("piggybank")

            if (
                piggybank
                and state.get("last_interest_date") != today
            ):
                interest = math.floor(
                    cash * float(piggybank["value"])
                )

                interest = max(0, interest)
                cash += interest

                state["last_interest_date"] = today
                state["last_interest_amount"] = interest
                state["last_interest_at"] = now

                dirty = True

            # -------------------------------------------------
            # 2. 장기 투자 증서
            # 주식을 24시간 보유한 뒤 매시간 현재 현금의
            # 등급별 비율만큼 지급한다.
            # 누락된 시간도 계산해 정산한다.
            # -------------------------------------------------

            certificate = owned.get("certificate")
            position = state.get("long_term_position")

            if certificate and isinstance(position, dict):
                shares = max(
                    0,
                    _safe_int(position.get("shares", 0)),
                )

                next_reward_at = float(
                    position.get("next_reward_at", 0) or 0
                )

                schedule_version = _safe_int(
                    position.get("schedule_version", 0)
                )

                # 이전 버전 기록은 새 규칙으로 전환한다.
                # 전환 시점부터 24시간 대기한다.
                if schedule_version != LONG_TERM_SCHEDULE_VERSION:
                    position["schedule_version"] = (
                        LONG_TERM_SCHEDULE_VERSION
                    )
                    position["started_at"] = now
                    position["next_reward_at"] = (
                        now + LONG_TERM_INITIAL_HOLD_HOURS * 3600
                    )
                    dirty = True

                elif (
                    shares > 0
                    and next_reward_at > 0
                    and now >= next_reward_at
                ):
                    elapsed_periods = (
                        int(
                            (
                                now - next_reward_at
                            ) // LONG_TERM_REWARD_INTERVAL_SECONDS
                        )
                        + 1
                    )

                    reward_rate = float(certificate["value"])
                    total_reward = 0

                    # 각 시간의 보상을 순서대로 계산한다.
                    # 앞선 보상으로 늘어난 현금도 다음 계산에 포함한다.
                    for _ in range(elapsed_periods):
                        reward = math.floor(cash * reward_rate)
                        reward = max(0, reward)

                        cash += reward
                        total_reward += reward

                    position["next_reward_at"] = (
                        next_reward_at
                        + elapsed_periods
                        * LONG_TERM_REWARD_INTERVAL_SECONDS
                    )

                    position["last_reward"] = total_reward
                    position["last_reward_periods"] = elapsed_periods
                    position["last_reward_at"] = now

                    dirty = True

            # -------------------------------------------------
            # 3. 비상금 서류가방
            # 현금 10,000원 이하일 때 지급, 3일 쿨타임
            # -------------------------------------------------

            emergency_bag = owned.get("emergency_bag")

            if (
                emergency_bag
                and cash <= EMERGENCY_CASH_THRESHOLD
            ):
                next_allowed = float(
                    state.get("emergency_next_at", 0) or 0
                )

                if now >= next_allowed:
                    reward = max(
                        0,
                        _safe_int(emergency_bag["value"]),
                    )

                    cash += reward

                    state["emergency_next_at"] = (
                        now + EMERGENCY_COOLDOWN_DAYS * 86400
                    )

                    state["last_emergency_reward"] = reward
                    state["last_emergency_reward_at"] = now

                    dirty = True

            if not dirty:
                return current

            account["cash"] = cash
            account["updated_at"] = time.time()

            return account

        ref.transaction(mutate)