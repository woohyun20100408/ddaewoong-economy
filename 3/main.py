"""Discord virtual stock market bot for DisHost."""
from __future__ import annotations
import random
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import Settings
from services.chart_service import render_price_chart
from services.firebase_store import FirebaseStore

from services.economy_items import (
    CATALOG,
    CATEGORIES,
    ITEMS,
    EconomyItemService,
    ItemError,
    active_owned_items,
    CONSUMABLE_ITEM_ID,
    MARKET_MANIPULATION_CHANGE,
)

from services.market_engine import MarketEngine
from services.trading_service import TradeError, TradingService

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "companies.json"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("discord-stock-bot")


def money(value: int | float) -> str:
    return f"₩{int(round(value)):,}"


def pct(value: int | float) -> str:
    number = float(value)
    return f"{'+' if number > 0 else ''}{number:.2f}%"


def sign_color(value: float) -> discord.Color:
    if value > 0:
        # 상승: 빨간색
        return discord.Color.from_rgb(220, 53, 69)
    if value < 0:
        # 하락: 파란색
        return discord.Color.from_rgb(45, 115, 205)
    return discord.Color.blurple()

def apply_stock_manipulation(
    store,
    symbol: str,
    change_ratio: float,
    company_count: int,
) -> dict[str, Any]:
    """한 종목의 가격을 변경하고 가격 기록과 시장 지수를 갱신한다."""
    result: dict[str, Any] = {}
    company_ref = store.companies_ref.child(symbol)

    def update_company(current: Any) -> Any:
        result.clear()

        if not isinstance(current, dict):
            return current

        row = dict(current)
        old_price = max(100, int(row.get("price", 100)))
        new_price = max(
            100,
            int(round(old_price * (1.0 + change_ratio))),
        )

        if new_price == old_price:
            result["error"] = "price_floor"
            return current

        actual_change = (new_price - old_price) / old_price
        volume = int(row.get("volume", 0))

        row.update({
            "previous_price": old_price,
            "price": new_price,
            "last_change_pct": round(actual_change * 100, 4),
            "updated_at": time.time(),
            "history_count": int(row.get("history_count", 0)) + 1,
        })

        result.update({
            "symbol": symbol,
            "old_price": old_price,
            "new_price": new_price,
            "actual_change_pct": actual_change * 100,
            "volume": volume,
        })

        return row

    company_ref.transaction(update_company)

    if "new_price" not in result:
        raise RuntimeError(
            "종목 가격을 변경하지 못했습니다. "
            "종목이 삭제되었거나 가격 하한에 도달했을 수 있습니다."
        )

    # 가격 차트 기록은 가격 변경과 별도로 처리한다.
    try:
        store.record_price_history(
            symbol,
            result["new_price"],
            result["volume"],
        )
    except Exception:
        logger.exception("주가조작 후 차트 기록 실패: %s", symbol)

    # 해당 종목의 변동이 시장 지수에도 일부 반영되도록 한다.
    try:
        count = max(1, int(company_count))
        actual_return = float(result["actual_change_pct"]) / 100.0

        def update_market(current: Any) -> Any:
            if not isinstance(current, dict):
                return current

            state = dict(current)
            old_index = float(state.get("index_value", 1000.0))
            new_index = max(
                100.0,
                round(old_index * (1.0 + actual_return / count), 2),
            )

            state["previous_index"] = old_index
            state["index_value"] = new_index
            return state

        store.market_ref.transaction(update_market)

    except Exception:
        logger.exception("주가조작 후 시장 지수 갱신 실패")

    return result

SECTOR_ICONS = {
    "바이오·헬스케어": "🧬",
    "에너지": "⚡",
    "게임·엔터테인먼트": "🎮",
    "식품·유통": "🍱",
    "모빌리티·자동차": "🚗",
    "IT·기술": "💻",
    "과학·연구개발": "🔬",
    "항공·우주기술": "🪐",
    "로봇·자동화": "🤖",
    "신재생에너지": "🌱",
    "통신·네트워크": "📡",
    "첨단과학·양자기술": "⚛️",
}

def resolve_symbol(companies: dict[str, dict[str, Any]], user_input: str) -> str | None:
    """Accept an internal symbol or the Korean company name typed by a user."""
    query = user_input.strip().casefold()
    if not query:
        return None
    for symbol, company in companies.items():
        if symbol.casefold() == query or str(company.get("name", "")).strip().casefold() == query:
            return symbol
    return None




class StockBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.settings = settings
        self.store: FirebaseStore | None = None
        self.trading: TradingService | None = None
        self.market_engine: MarketEngine | None = None
        self.item_service: EconomyItemService | None = None

        self.market_lock = asyncio.Lock()
        self._market_task: asyncio.Task[None] | None = None
        self._news_task: asyncio.Task[None] | None = None
        self._emergency_task: asyncio.Task[None] | None = None
        self._item_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        self.settings.validate_for_startup()

        try:
            companies = json.loads(
                DATA_FILE.read_text(encoding="utf-8")
            )
            self.store = await asyncio.to_thread(
                FirebaseStore,
                self.settings.firebase_database_url,
                self.settings.firebase_service_account_json,
                self.settings.google_application_credentials,
            )
            await asyncio.to_thread(
                self.store.ensure_seed_data, companies
            )
        except Exception as exc:
            logger.exception("Firebase initialization failed")
            raise RuntimeError(
                "Firebase 초기화에 실패했습니다. Firebase URL, "
                "서비스 계정 JSON, 데이터베이스 활성화 여부를 확인하세요."
            ) from exc

        self.trading = TradingService(
            self.store,
            self.settings.starting_cash,
            self.settings.trading_fee_rate,
        )
        self.market_engine = MarketEngine(self.store)
        self.item_service = EconomyItemService(self.store)


        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %d application commands to guild %s",
                len(synced),
                self.settings.guild_id,
            )
            logger.info(
                "Registered commands: %s",
                ", ".join(command.name for command in synced),
            )
        else:
            synced = await self.tree.sync()
            logger.info(
                "Synced %d global application commands", len(synced)
            )

        self._market_task = asyncio.create_task(
            self.market_loop(), name="market-price-loop"
        )
        self._item_task = asyncio.create_task(
            self.item_effect_loop(), name="economy-item-effect-loop"
        )

        if self.settings.news_channel_id:
            self._news_task = asyncio.create_task(
                self.news_loop(), name="automatic-news-loop"
            )
            self._emergency_task = asyncio.create_task(
                self.emergency_news_loop(),
                name="emergency-news-loop",
            )
            logger.info(
                "News posting enabled in channel %s",
                self.settings.news_channel_id,
            )
        else:
            logger.warning(
                "Automatic news disabled: NEWS_CHANNEL_ID is not configured."
            )

    async def close(self) -> None:
        tasks = (
            self._market_task,
            self._news_task,
            self._emergency_task,
            self._item_task,
        )

        for task in tasks:
            if task is not None and not task.done():
                task.cancel()

        for task in tasks:
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception(
                        "Background task stopped with an error"
                    )

        await super().close()

    async def get_news_channel(self) -> Any | None:
        channel_id = self.settings.news_channel_id

        if not channel_id:
            return None

        channel = self.get_channel(channel_id)

        if channel is None:
            channel = await self.fetch_channel(channel_id)

        return channel if hasattr(channel, "send") else None

    async def publish_news_item(
        self,
        item: dict[str, Any],
        *,
        emergency: bool = False,
        fallback_channel: Any | None = None,
    ) -> bool:
        """경제 뉴스, 긴급 뉴스, 주가조작 뉴스를 게시한다."""

        is_manipulation = item.get("type") == "item_manipulation"

        # 주가조작 뉴스는 NEWS_CHANNEL_ID에 지정된 채널에만 게시한다.
        channel = await self.get_news_channel()

        # 주가조작 뉴스는 대체 채널로 보내지 않는다.
        if channel is None and not is_manipulation:
            channel = fallback_channel

        if channel is None or not hasattr(channel, "send"):
            logger.error(
                "뉴스 게시 채널이 없습니다. NEWS_CHANNEL_ID를 확인하세요."
            )
            return False

        impact_pct = float(item.get("impact", 0.0)) * 100

        if item.get("scope") == "market" or item.get("symbol") == "MARKET":
            affected = "시장 전체"
        elif item.get("scope") == "sector":
            affected = str(
                item.get("sector")
                or item.get("target_label")
                or "관련 업종"
            )
        else:
            affected = str(
                item.get("company_name")
                or item.get("target_label")
                or item.get("symbol", "기업")
            )

        if is_manipulation:
            prefix = "시장 속보"
        elif emergency:
            prefix = "긴급 뉴스"
        else:
            prefix = "경제 뉴스"

        embed = discord.Embed(
            title=f"{prefix} | {item.get('headline', '시장 소식')}",
            description=str(item.get("body", "")),
            color=sign_color(impact_pct),
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="영향 대상",
            value=affected,
            inline=True,
        )

        if is_manipulation:
            embed.add_field(
                name="실제 주가 변동",
                value=pct(impact_pct),
                inline=True,
            )
            embed.add_field(
                name="변경 전 → 변경 후",
                value=(
                    f"{money(item.get('old_price', 0))} → "
                    f"{money(item.get('new_price', 0))}"
                ),
                inline=False,
            )
            embed.set_footer(
                text="출처 불명 · 의문의 세력 개입 추정"
            )

        elif emergency:
            embed.add_field(
                name="초기 예상 영향",
                value=pct(impact_pct),
                inline=True,
            )
            embed.add_field(
                name="영향 지속",
                value=(
                    f"최대 {int(item.get('duration_ticks', 5))}"
                    "회 시세 갱신"
                ),
                inline=True,
            )
            embed.set_footer(
                text="긴급 이벤트 영향은 시세 갱신마다 점차 감소합니다."
            )

        else:
            embed.add_field(
                name="초기 예상 영향",
                value=pct(impact_pct),
                inline=True,
            )
            embed.set_footer(text="자동 등록 뉴스")

        try:
            await channel.send(embed=embed)
            return True

        except Exception:
            logger.exception(
                "뉴스 게시 실패: %s",
                item.get("headline", "제목 없음"),
            )
            return False

    async def publish_event_end(
        self,
        item: dict[str, Any],
    ) -> None:
        """Announce that an emergency event's extra price effect ended."""
        try:
            channel = await self.get_news_channel()
            if channel is None:
                return

            embed = discord.Embed(
                title="긴급 이벤트 종료",
                description=(
                    f"**{item.get('headline', '긴급 뉴스')}**\n"
                    "이벤트의 추가 주가 영향이 종료되었습니다."
                ),
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            await channel.send(embed=embed)

        except Exception:
            logger.exception(
                "Could not post emergency-event end notice"
            )

    async def market_loop(self) -> None:
        await self.wait_until_ready()

        while not self.is_closed():
            expired_events: list[dict[str, Any]] = []

            try:
                async with self.market_lock:
                    if self.market_engine:
                        result = await asyncio.to_thread(
                            self.market_engine.tick
                        )
                        expired_events = result.get(
                            "expired_emergency_events", []
                        )

                        logger.info(
                            "Market tick: updated=%s index=%s "
                            "active_events=%s",
                            result.get("updated"),
                            result.get("index_value"),
                            result.get("active_emergency_events", 0),
                        )

                for event in expired_events:
                    await self.publish_event_end(event)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Market tick failed; will retry on the next interval"
                )

            await asyncio.sleep(
                self.settings.market_tick_seconds
            )

    async def news_loop(self) -> None:
        """Generate one ordinary news item every ten minutes."""
        await self.wait_until_ready()

        if not self.settings.news_channel_id:
            return

        while not self.is_closed():
            try:
                await asyncio.sleep(300)

                if self.market_engine is None:
                    continue

                async with self.market_lock:
                    item = await asyncio.to_thread(
                        self.market_engine.generate_scheduled_news
                    )

                if item:
                    await self.publish_news_item(
                        item, emergency=False
                    )
                    logger.info(
                        "Scheduled news posted: %s",
                        item.get("headline"),
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Scheduled news failed; will retry in ten minutes"
                )

    async def emergency_news_loop(self) -> None:
        """Generate an emergency event every random 1–3 hours."""
        await self.wait_until_ready()

        if not self.settings.news_channel_id:
            return

        while not self.is_closed():
            try:
                delay = random.randint(150, 300)
                logger.info(
                    "Next automatic emergency news in %d seconds",
                    delay,
                )
                await asyncio.sleep(delay)

                if self.market_engine is None:
                    continue

                async with self.market_lock:
                    item = await asyncio.to_thread(
                        self.market_engine.generate_emergency_event
                    )

                if item:
                    await self.publish_news_item(
                        item, emergency=True
                    )
                    logger.info(
                        "Emergency event generated: %s",
                        item.get("headline"),
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic emergency news failed")

    async def item_effect_loop(self) -> None:
        """Apply passive collectible-item effects to existing player accounts."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                if self.store is not None and self.item_service is not None:
                    players = await asyncio.to_thread(self.store.get_all_players)
                    for user_id, account in players.items():
                        if not active_owned_items(account):
                            continue
                        await asyncio.to_thread(
                            self.item_service.process_player, user_id
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Economy item effect processing failed")
            await asyncio.sleep(60)


settings = Settings.from_env()
bot = StockBot(settings)


def get_services() -> tuple[FirebaseStore, TradingService]:
    if bot.store is None or bot.trading is None:
        raise RuntimeError("봇 서비스가 아직 준비되지 않았습니다.")
    return bot.store, bot.trading


def get_item_service() -> EconomyItemService:
    if bot.item_service is None:
        raise RuntimeError("아이템 서비스가 아직 준비되지 않았습니다.")
    return bot.item_service


async def process_item_effects_now(user_id: int) -> None:
    """Check passive item effects after user actions that change cash."""
    if bot.item_service is None:
        return
    try:
        await asyncio.to_thread(bot.item_service.process_player, user_id)
    except Exception:
        logger.exception("Immediate economy item effect processing failed for %s", user_id)


class ItemShopSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = []
        for item_id, item in ITEMS.items():
            description = f"₩{item['price']:,} · {item['effect']}"
            options.append(
                discord.SelectOption(
                    label=item["name"],
                    value=item_id,
                    description=description[:100],
                )
            )
        super().__init__(
            placeholder="구매할 아이템과 등급을 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="economy_item_shop_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ItemShopView):
            await interaction.response.send_message(
                "상점 화면을 새로 열어 주세요.", ephemeral=True
            )
            return
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "이 상점은 명령어를 실행한 사용자만 조작할 수 있습니다.",
                ephemeral=True,
            )
            return
        view.selected_item_id = self.values[0]
        await interaction.response.edit_message(
            embed=view.make_embed(), view=view
        )


class ItemShopView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.selected_item_id: str | None = None
        self.add_item(ItemShopSelect())

    def make_embed(self, notice: str | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="경제 아이템 상점",
            description=(
                "아래 메뉴에서 아이템을 선택하고 **선택한 아이템 구매**를 누르세요.\n"
                "아이템 효과는 구매 후 자동으로 적용됩니다. 같은 종류는 가장 높은 보유 등급만 적용됩니다."
            ),
            color=discord.Color.gold(),
        )
        for category, spec in CATALOG.items():
            lines = []
            for tier_index in range(5):
                item = ITEMS[f"{category}_{tier_index + 1}"]
                lines.append(
                    f"**{item['name']}** — ₩{item['price']:,}\n{item['effect']}"
                )
            embed.add_field(
                name=CATEGORIES[category],
                value="\n\n".join(lines),
                inline=False,
            )

        consumable = ITEMS.get(CONSUMABLE_ITEM_ID)

        if consumable:
            embed.add_field(
                name="소모품",
                value=(
                    f"**{consumable['name']}** — "
                    f"₩{consumable['price']:,}\n"
                    f"{consumable['effect']}\n"
                    "`/아이템사용` 명령어로 사용할 수 있습니다."
                ),
                inline=False,
            )

        if self.selected_item_id:
            item = ITEMS[self.selected_item_id]
            embed.add_field(
                name="선택한 아이템",
                value=f"**{item['name']}** · ₩{item['price']:,}\n{item['effect']}",
                inline=False,
            )
            
        if notice:
            embed.set_footer(text=notice[:200])
        return embed

    @discord.ui.button(
        label="선택한 아이템 구매",
        style=discord.ButtonStyle.success,
        emoji="🛒",
        custom_id="economy_item_shop_purchase",
    )
    async def purchase_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "이 상점은 명령어를 실행한 사용자만 조작할 수 있습니다.",
                ephemeral=True,
            )
            return
        if not self.selected_item_id:
            await interaction.response.send_message(
                "먼저 드롭다운에서 구매할 아이템을 선택해 주세요.",
                ephemeral=True,
            )
            return

        try:
            purchased = await asyncio.to_thread(
                get_item_service().buy,
                interaction.user.id,
                self.selected_item_id,
            )
        except ItemError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception("Item shop purchase failed")
            await interaction.response.send_message(
                "구매 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                ephemeral=True,
            )
            return

        await process_item_effects_now(interaction.user.id)
        account = await asyncio.to_thread(
            get_item_service().get_player, interaction.user.id
        )
        remaining_cash = int((account or {}).get("cash", 0) or 0)
        await interaction.response.send_message(
            f"**{purchased['name']}** 구매 완료!\n"
            f"결제 금액: ₩{purchased['price']:,}\n"
            f"남은 현금: ₩{remaining_cash:,}\n"
            f"효과: {purchased['effect']}\n",
            ephemeral=True,
        )


@bot.tree.command(name="상점", description="경제 아이템을 확인하고 구매합니다.")
async def item_shop(interaction: discord.Interaction) -> None:
    view = ItemShopView(interaction.user.id)
    await interaction.response.send_message(
        embed=view.make_embed(), view=view, ephemeral=True
    )


@bot.tree.command(name="내아이템", description="보유한 경제 아이템 목록을 확인합니다.")
async def my_items(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    service = get_item_service()
    account = await asyncio.to_thread(service.get_player, interaction.user.id)
    if not account:
        await interaction.followup.send(
            "계좌가 없습니다. 먼저 `/계좌개설`을 이용해 주세요.",
            ephemeral=True,
        )
        return

    inventory = account.get("items", {}) or {}
    active = active_owned_items(account)
    active_ids = {item["id"] for item in active.values()}
    lines = []
    for item_id, count in inventory.items():
        item = ITEMS.get(item_id)
        if not item or int(count or 0) <= 0:
            continue
        marker = " · 효과 적용 중" if item_id in active_ids else " · 보유 중(효과 비활성)"
        lines.append(
            f"**{item['name']}** × {int(count)}{marker}\n{item['effect']}"
        )

    embed = discord.Embed(title="내 아이템", color=discord.Color.blurple())
    embed.description = (
        "\n\n".join(lines)
        if lines
        else "보유한 아이템이 없습니다. `/상점`에서 아이템을 구매해 보세요."
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="보유효과", description="보유 아이템으로 현재 적용 중인 효과를 확인합니다.")
async def owned_effects(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    service = get_item_service()
    account = await asyncio.to_thread(service.get_player, interaction.user.id)
    if not account:
        await interaction.followup.send(
            "계좌가 없습니다. 먼저 `/계좌개설`을 이용해 주세요.",
            ephemeral=True,
        )
        return

    active = active_owned_items(account)
    embed = discord.Embed(
        title="보유 효과",
        description=(
            "아이템은 보유하는 즉시 자동 적용됩니다.\n"
            "같은 종류를 여러 등급 보유하면 가장 높은 등급 하나만 적용됩니다."
        ),
        color=discord.Color.green(),
    )
    for category, category_name in CATEGORIES.items():
        item = active.get(category)
        if item:
            embed.add_field(
                name=f"{category_name} · {item['tier_name']}",
                value=f"**{item['name']}**\n{item['effect']}",
                inline=False,
            )
        else:
            embed.add_field(
                name=category_name,
                value="보유 아이템 없음",
                inline=False,
            )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def require_account(interaction: discord.Interaction) -> dict[str, Any] | None:
    store, _ = get_services()
    account = await asyncio.to_thread(store.get_player, interaction.user.id)
    if not account:
        await interaction.response.send_message(
            "계좌가 없습니다. 먼저 `/계좌개설`로 투자 계좌를 개설해 주세요.", ephemeral=True
        )
        return None
    return account


@bot.tree.command(name="계좌개설", description="주식시장 계좌를 개설합니다.")
async def start(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    _, trading = get_services()
    account, created = await asyncio.to_thread(
        trading.ensure_account, interaction.user.id, interaction.user.display_name
    )
    if created:
        text = (
            f"투자 계좌를 개설했습니다!\n시작 자금 **{money(account['cash'])}**이 지급되었습니다.\n"
            "`/시장`에서 종목을 살펴보고 `/매수`로 매수해 보세요."
        )
    else:
        text = f"이미 계좌가 있습니다. 현재 현금 잔액은 **{money(account.get('cash', 0))}**입니다."
    await interaction.followup.send(text, ephemeral=True)


@bot.tree.command(name="시장", description="전체 종목과 현재 주가를 확인합니다.")
async def market(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)

    store, _ = get_services()
    companies = await asyncio.to_thread(store.get_companies)
    state = await asyncio.to_thread(store.get_market_state)

    if not companies:
        await interaction.followup.send(
            "시장 데이터가 아직 없습니다. 관리자에게 문의해 주세요."
        )
        return

    index = float(state.get("index_value", 1000.0))
    change = float(state.get("last_return_pct", 0.0))

    ordered = sorted(
        companies.items(),
        key=lambda pair: str(pair[1].get("name", pair[0]))
    )

    embed = discord.Embed(
        title="📈 종목 시세",
        description=(
            f"**종합지수** `{index:,.2f}`"
            f"　·　**시장 변동** `{pct(change)}`"
        ),
        color=sign_color(change),
        timestamp=discord.utils.utcnow(),
    )

    rows = []

    for symbol, row in ordered:
        name = row.get("name", symbol)
        price = money(row.get("price", 0))
        change_pct = float(row.get("last_change_pct", 0.0))

        if change_pct > 0:
            marker = "🔴"
        elif change_pct < 0:
            marker = "🔵"
        else:
            marker = "⚪"

        rows.append(
            f"**{name}** · **{price}** · "
            f"{marker} `{pct(change_pct)}`"
        )

    embed.add_field(
        name="회사 · 현재가 · 등락률",
        value="\n".join(rows),
        inline=False,
    )

    embed.set_footer(
        text=(
            f"주가 갱신 주기 {settings.market_tick_seconds}초"
            " · 상세 정보는 /종목정보"
        )
    )

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="종목정보", description="회사 이름으로 현재 주가와 기업 정보를 확인합니다.")
@app_commands.rename(symbol="회사명")
@app_commands.describe(symbol="회사 이름을 입력하거나 자동완성 목록에서 선택")
async def quote(interaction: discord.Interaction, symbol: str) -> None:
    await interaction.response.defer(thinking=True)
    store, _ = get_services()
    companies = await asyncio.to_thread(store.get_companies)
    key = resolve_symbol(companies, symbol)
    row = companies.get(key) if key else None
    if not row or not key:
        await interaction.followup.send("회사를 찾을 수 없습니다. `/시장`에서 종목 이름을 확인해 주세요.")
        return
    change = float(row.get("last_change_pct", 0.0))
    embed = discord.Embed(
        title=f"{row.get('name', key)} 주가 정보",
        description=str(row.get("description", "기업")),
        color=sign_color(change),
    )
    embed.add_field(name="현재 주가", value=f"**{money(row.get('price', 0))}**", inline=True)
    embed.add_field(name="직전 변동", value=pct(change), inline=True)
    embed.add_field(name="업종", value=str(row.get("sector", "기타")), inline=True)
    embed.add_field(name="최근 거래량", value=f"{int(row.get('volume', 0)):,}주", inline=True)
    embed.add_field(name="시세 갱신", value=f"<t:{int(row.get('updated_at', time.time()))}:R>", inline=True)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="매수", description="주식을 매수합니다.")
@app_commands.rename(symbol="회사명", quantity="수량")
@app_commands.describe(symbol="회사 이름을 입력하거나 자동완성 목록에서 선택", quantity="매수할 주식 수")
async def buy(interaction: discord.Interaction, symbol: str, quantity: app_commands.Range[int, 1, 100000]) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    store, trading = get_services()
    companies = await asyncio.to_thread(store.get_companies)
    key = resolve_symbol(companies, symbol)
    if not key:
        await interaction.followup.send("회사를 찾을 수 없습니다. `/시장`에서 이름을 확인하거나 자동완성 목록을 사용해 주세요.", ephemeral=True)
        return
    try:
        async with bot.market_lock:
            trade = await asyncio.to_thread(
                trading.execute_trade, interaction.user.id, interaction.user.display_name,
                key, "buy", quantity,
            )
    except TradeError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    except Exception:
        logger.exception("Buy command failed")
        await interaction.followup.send("매수 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return
    await process_item_effects_now(interaction.user.id)
    company = trade["company_name"]
    embed = discord.Embed(title="매수 체결", color=discord.Color.from_rgb(36, 164, 100))
    embed.description = f"**{company}** {trade['quantity']:,}주를 매수했습니다."
    embed.add_field(name="체결 단가", value=money(trade["price"]), inline=True)
    embed.add_field(name="주문 금액", value=money(trade["gross"]), inline=True)
    embed.add_field(name="수수료", value=money(trade["fee"]), inline=True)
    embed.add_field(name="총 차감액", value=money(-trade["cash_delta"]), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="매도", description="보유한 주식을 매도합니다.")
@app_commands.rename(symbol="회사명", quantity="수량")
@app_commands.describe(symbol="회사 이름을 입력하거나 자동완성 목록에서 선택", quantity="매도할 주식 수")
async def sell(interaction: discord.Interaction, symbol: str, quantity: app_commands.Range[int, 1, 100000]) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    store, trading = get_services()
    companies = await asyncio.to_thread(store.get_companies)
    key = resolve_symbol(companies, symbol)
    if not key:
        await interaction.followup.send("회사를 찾을 수 없습니다. `/시장`에서 이름을 확인하거나 자동완성 목록을 사용해 주세요.", ephemeral=True)
        return
    try:
        async with bot.market_lock:
            trade = await asyncio.to_thread(
                trading.execute_trade, interaction.user.id, interaction.user.display_name,
                key, "sell", quantity,
            )
    except TradeError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    except Exception:
        logger.exception("Sell command failed")
        await interaction.followup.send("매도 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return
    await process_item_effects_now(interaction.user.id)
    embed = discord.Embed(title="매도 체결", color=discord.Color.blurple())
    embed.description = f"**{trade['company_name']}** {trade['quantity']:,}주를 매도했습니다."
    embed.add_field(name="체결 단가", value=money(trade["price"]), inline=True)
    embed.add_field(name="매도 금액", value=money(trade["gross"]), inline=True)
    embed.add_field(name="수수료", value=money(trade["fee"]), inline=True)
    embed.add_field(name="현금 증가", value=money(trade["cash_delta"]), inline=True)
    embed.add_field(name="실현 손익", value=money(trade.get("realized_pnl") or 0), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(
    name="일하기",
    description="일해서 10,000원을 벌어요. 쿨타임은 1시간입니다.",
)
async def work(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    _, trading = get_services()
    await process_item_effects_now(interaction.user.id)

    try:
        result = await asyncio.to_thread(
            trading.claim_work,
            interaction.user.id,
            interaction.user.display_name,
        )
    except TradeError as exc:
        await interaction.followup.send(
            str(exc),
            ephemeral=True,
        )
        return
    except Exception:
        logger.exception("Work command failed")
        await interaction.followup.send(
            "일하기 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="일하기 완료",
        description=(
            f"수고했어요! 보상 **{money(result['reward'])}**을 받았습니다."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="현재 현금 잔액",
        value=f"**{money(result['cash'])}**",
        inline=True,
    )
    embed.add_field(
        name="다음 이용 가능",
        value=f"<t:{int(result['next_work_at'])}:R>",
        inline=True,
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
    )

@bot.tree.command(name="잔액", description="투자 계좌의 현금 잔액을 확인합니다.")
async def balance(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    store, _ = get_services()
    account = await asyncio.to_thread(store.get_player, interaction.user.id)
    if not account:
        await interaction.followup.send("먼저 `/계좌개설`로 계좌를 개설해 주세요.", ephemeral=True)
        return
    embed = discord.Embed(title=f"{interaction.user.display_name}님의 현금 잔액", color=discord.Color.blurple())
    embed.description = f"## {money(account.get('cash', 0))}"
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(
    name="송금",
    description="다른 이용자에게 현금을 송금합니다.",
)
@app_commands.rename(recipient="받는사람", amount="금액")
@app_commands.describe(
    recipient="송금받을 서버 멤버",
    amount="송금할 금액(원)",
)
async def transfer(
    interaction: discord.Interaction,
    recipient: discord.Member,
    amount: app_commands.Range[int, 1, 1_000_000_000],
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    if recipient.bot:
        await interaction.followup.send(
            "봇 계정에는 송금할 수 없습니다.",
            ephemeral=True,
        )
        return

    if recipient.id == interaction.user.id:
        await interaction.followup.send(
            "자기 자신에게는 송금할 수 없습니다.",
            ephemeral=True,
        )
        return

    _, trading = get_services()

    try:
        result = await asyncio.to_thread(
            trading.transfer_cash,
            interaction.user.id,
            interaction.user.display_name,
            recipient.id,
            recipient.display_name,
            amount,
        )
    except TradeError as exc:
        await interaction.followup.send(
            str(exc),
            ephemeral=True,
        )
        return
    except Exception:
        logger.exception("Transfer command failed")
        await interaction.followup.send(
            "송금 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )
        return

    await process_item_effects_now(interaction.user.id)
    embed = discord.Embed(
        title="송금 완료",
        description=(
            f"{recipient.mention}님에게 "
            f"**{money(result['amount'])}**을 송금했습니다."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="송금 후 내 잔액",
        value=f"**{money(result['sender_cash'])}**",
        inline=True,
    )
    embed.add_field(
        name="송금 수수료",
        value=money(0),
        inline=True,
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )

@bot.tree.command(name="내자산", description="보유 주식, 평가손익, 총자산과 수익률을 확인합니다.")
async def portfolio(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    store, _ = get_services()
    account = await asyncio.to_thread(store.get_player, interaction.user.id)
    if not account:
        await interaction.followup.send("먼저 `/계좌개설`로 계좌를 개설해 주세요.", ephemeral=True)
        return
    companies = await asyncio.to_thread(store.get_companies)
    holdings = account.get("holdings", {}) or {}
    cash = int(account.get("cash", 0))
    total_value = 0
    unrealized = 0.0
    lines: list[str] = []
    for symbol, holding in holdings.items():
        quantity = int((holding or {}).get("quantity", 0))
        if quantity <= 0:
            continue
        company = companies.get(symbol, {})
        price = int(company.get("price", 0))
        average_cost = float((holding or {}).get("average_cost", 0))
        value = quantity * price
        pnl = value - quantity * average_cost
        total_value += value
        unrealized += pnl
        lines.append(
            f"**{company.get('name', symbol)}** — {quantity:,}주 | "
            f"평가액 {money(value)} | 평가손익 {money(pnl)}"
        )
    equity = cash + total_value
    initial = max(1, int(account.get("initial_cash", settings.starting_cash)))
    total_return = (equity - initial) / initial * 100
    embed = discord.Embed(title=f"{interaction.user.display_name}님의 포트폴리오", color=sign_color(total_return))
    embed.add_field(name="현금", value=money(cash), inline=True)
    embed.add_field(name="주식 평가액", value=money(total_value), inline=True)
    embed.add_field(name="총자산", value=f"**{money(equity)}**", inline=True)
    embed.add_field(name="총 수익률", value=pct(total_return), inline=True)
    embed.add_field(name="미실현 손익", value=money(unrealized), inline=True)
    embed.add_field(name="실현 손익", value=money(account.get("realized_pnl", 0)), inline=True)
    embed.add_field(name="보유 종목", value="\n".join(lines[:8]) if lines else "현재 보유 주식이 없습니다.", inline=False)
    embed.set_footer(text="총 수익률 = 현금 + 주식 평가액을 최초 자금과 비교")
    await interaction.followup.send(embed=embed, ephemeral=True)



@bot.tree.command(name="차트", description="최근 주가 흐름을 차트 이미지로 확인합니다.")
@app_commands.rename(symbol="회사명")
@app_commands.describe(symbol="회사 이름을 입력하거나 자동완성 목록에서 선택")
async def chart(interaction: discord.Interaction, symbol: str) -> None:
    await interaction.response.defer(
        ephemeral=False,
        thinking=True,
    )

    store, _ = get_services()
    companies = await asyncio.to_thread(store.get_companies)
    key = resolve_symbol(companies, symbol)
    company = companies.get(key) if key else None

    if not company or not key:
        await interaction.followup.send(
            "회사를 찾을 수 없습니다. `/시장`에서 이름을 확인해 주세요.",
            ephemeral=False,
        )
        return

    points = await asyncio.to_thread(
        store.get_price_history,
        key,
        240,
    )

    try:
        chart_file = await asyncio.to_thread(
            render_price_chart,
            key,
            company,
            points,
        )

        await interaction.followup.send(
            content=(
                f"**{company.get('name', key)} 주가 차트**"
                f" · 최근 {len(points)}개 시장 업데이트"
            ),
            file=discord.File(
                fp=chart_file,
                filename=f"{company.get('name', key)}_주가차트.png",
            ),
            ephemeral=False,
        )

    except Exception:
        logger.exception("Chart generation failed")
        await interaction.followup.send(
            "차트를 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=False,
        )

@bot.tree.command(name="거래내역", description="최근 체결된 나의 매매 내역을 확인합니다.")
async def history(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    store, _ = get_services()
    account = await asyncio.to_thread(store.get_player, interaction.user.id)
    if not account:
        await interaction.followup.send("먼저 `/계좌개설`로 계좌를 개설해 주세요.", ephemeral=True)
        return
    trades = await asyncio.to_thread(store.get_trade_history, interaction.user.id, 10)
    if not trades:
        trades = list(reversed(account.get("recent_trades", []) or []))[:10]
    if not trades:
        await interaction.followup.send("아직 매매 기록이 없습니다.", ephemeral=True)
        return
    lines = []
    for trade in trades:
        side_label = "매수" if trade.get("side") == "buy" else "매도"
        when = f"<t:{int(float(trade.get('timestamp', time.time())))}:R>"
        lines.append(
            f"{when} · **{side_label} {trade.get('company_name', trade.get('symbol', '알 수 없는 회사'))}** "
            f"{int(trade.get('quantity', 0)):,}주 × {money(trade.get('price', 0))} "
            f"(수수료 {money(trade.get('fee', 0))})"
        )
    embed = discord.Embed(title="최근 매매 기록", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="뉴스", description="최근 기업 공시와 경제 뉴스를 확인합니다.")
async def news(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    store, _ = get_services()
    items = await asyncio.to_thread(store.get_recent_news, 8)
    if not items:
        await interaction.followup.send("아직 뉴스가 없습니다. 시장이 움직이면 뉴스가 발생합니다.")
        return
    embed = discord.Embed(title="경제 뉴스", color=discord.Color.blurple())
    for item in items[:8]:
        ts = int(float(item.get("timestamp", time.time())))
        impact = float(item.get("impact", 0)) * 100
        if item.get("symbol") == "MARKET":
            affected = "시장 전체"
        else:
            affected = str(item.get("company_name") or item.get("symbol", "기업"))
        embed.add_field(
            name=f"{item.get('headline', '시장 소식')} · {affected}",
            value=f"{item.get('body', '')}\n영향 예상치: {pct(impact)} · <t:{ts}:R>",
            inline=False,
        )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="시장지수", description="주식시장 종합 지수와 시장 분위기를 확인합니다.")
async def index(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    store, _ = get_services()
    state = await asyncio.to_thread(store.get_market_state)
    value = float(state.get("index_value", 1000.0))
    previous = float(state.get("previous_index", value))
    change = (value - previous) / previous * 100 if previous else 0.0
    mood = float(state.get("mood", 0.0))
    mood_label = "낙관" if mood > 0.002 else "비관" if mood < -0.002 else "중립"
    embed = discord.Embed(title="시장 종합 주가지수", color=sign_color(change))
    embed.add_field(name="현재 지수", value=f"**{value:,.2f}**", inline=True)
    embed.add_field(name="직전 변동", value=pct(change), inline=True)
    embed.add_field(name="시장 분위기", value=mood_label, inline=True)
    embed.add_field(name="총 거래량(최근 틱 기준)", value=f"{int(state.get('total_volume', 0)):,}주", inline=True)
    embed.add_field(name="마지막 업데이트", value=f"<t:{int(state.get('last_tick', time.time()))}:R>", inline=True)
    embed.set_footer(text="종목들의 등락률을 이용한 자체 지수")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="랭킹", description="유저들의 총자산 또는 수익률 랭킹을 확인합니다.")
@app_commands.rename(kind="기준")
@app_commands.describe(kind="랭킹 기준을 선택하세요")
@app_commands.choices(kind=[
    app_commands.Choice(name="총자산", value="asset"),
    app_commands.Choice(name="수익률", value="return"),
])
async def rank(interaction: discord.Interaction, kind: app_commands.Choice[str]) -> None:
    await interaction.response.defer(thinking=True)
    store, _ = get_services()
    players, companies = await asyncio.gather(
        asyncio.to_thread(store.get_all_players),
        asyncio.to_thread(store.get_companies),
    )
    ranking: list[tuple[float, str, str, float]] = []
    for user_id, account in players.items():
        cash = int(account.get("cash", 0))
        stock_value = 0
        for symbol, holding in (account.get("holdings", {}) or {}).items():
            company = companies.get(symbol, {})
            stock_value += int((holding or {}).get("quantity", 0)) * int(company.get("price", 0))
        total_assets = cash + stock_value
        initial = max(1, int(account.get("initial_cash", settings.starting_cash)))
        return_pct = (total_assets - initial) / initial * 100
        metric = float(total_assets) if kind.value == "asset" else return_pct
        ranking.append((metric, str(account.get("username", user_id)), user_id, total_assets))
    ranking.sort(key=lambda row: row[0], reverse=True)
    label = "총자산" if kind.value == "asset" else "수익률"
    lines = []
    for place, (metric, username, user_id, assets) in enumerate(ranking[:10], start=1):
        if kind.value == "asset":
            score = money(metric)
        else:
            score = pct(metric)
        lines.append(f"**{place}. {discord.utils.escape_markdown(username)}** — {score} · 자산 {money(assets)}")
    embed = discord.Embed(
        title=f"투자자 {label} 랭킹",
        description="\n".join(lines) if lines else "아직 계좌가 없습니다. `/계좌개설`로 첫 계좌를 만들어 보세요.",
        color=discord.Color.blurple(),
    )
    await interaction.followup.send(embed=embed)


async def suggest_symbols(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    # Discord autocomplete has a short response deadline; read the tiny bundled seed file,
    # not Firebase, to avoid network latency in this callback.
    try:
        companies = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not load seed symbols for autocomplete")
        return []
    query = current.strip().casefold()
    choices = []
    for symbol, company in sorted(companies.items()):
        name = str(company.get("name", symbol))
        sector = str(company.get("sector", ""))
        if not query or query in symbol.casefold() or query in name.casefold() or query in sector.casefold():
            choices.append(app_commands.Choice(name=f"{name} · {sector}"[:100], value=symbol))
    return choices[:25]


quote.autocomplete("symbol")(suggest_symbols)
buy.autocomplete("symbol")(suggest_symbols)
sell.autocomplete("symbol")(suggest_symbols)
chart.autocomplete("symbol")(suggest_symbols)


@bot.tree.command(name="도움말", description="주식 봇 명령어 사용법을 확인합니다.")
async def help_stock(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="DSE 주식 봇 도움말", color=discord.Color.blurple())
    embed.description = (
        "`/계좌개설` 투자 계좌 만들기\n"
        "`/시장` 전체 종목·주가·등락률 보기\n"
        "`/종목정보 회사명` 기업 정보와 현재 주가\n"
        "`/매수 회사명 수량` 주식 매수\n"
        "`/매도 회사명 수량` 보유 주식 매도\n"
        "`/잔액` 현금 잔액\n"
        "`/내자산` 보유 주식·평가손익·수익률\n"
        "`/차트 회사명` 주가 차트\n"
        "`/거래내역` 최근 매매 기록\n"
        "`/뉴스` 경제 뉴스\n"
        "`/시장지수` 전체 시장 지수\n"
        "`/랭킹 기준` 총자산·수익률 순위\n"
        "`/상점` 아이템 확인 및 바로 구매\n"
        "`/내아이템` 보유 아이템 목록\n"
        "`/보유효과` 현재 적용 중인 아이템 효과 확인\n"
        "`/도움말` 이 도움말\n\n"
        "회사 이름은 자동완성 목록에서 선택할 수 있습니다."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    logger.error("Application command error: %s", error, exc_info=(type(error), error, error.__traceback__))
    message = "명령어를 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)



class StockManipulationModeView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "이 메뉴를 연 사용자만 조작할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="무작위 종목 상승 (+5%)",
        style=discord.ButtonStyle.success,
        emoji="📈",
    )
    async def random_rise(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self.owner_only(interaction):
            return

        view = StockManipulationConfirmView(
            owner_id=self.owner_id,
            mode="rise",
        )

        await interaction.response.edit_message(
            embed=view.make_embed(),
            view=view,
        )

    @discord.ui.button(
        label="특정 종목 하락 (-5%)",
        style=discord.ButtonStyle.danger,
        emoji="📉",
    )
    async def select_fall(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self.owner_only(interaction):
            return

        stock_bot = interaction.client

        if not isinstance(stock_bot, StockBot) or stock_bot.store is None:
            await interaction.response.send_message(
                "시장 서비스가 준비되지 않았습니다.",
                ephemeral=True,
            )
            return

        companies = await asyncio.to_thread(
            stock_bot.store.get_companies
        )

        # 최저가에 도달한 종목은 하락 대상에서 제외
        selectable = {
            symbol: row
            for symbol, row in companies.items()
            if int(row.get("price", 100)) > 100
        }

        if not selectable:
            await interaction.response.edit_message(
                content="현재 하락시킬 수 있는 종목이 없습니다.",
                embed=None,
                view=None,
            )
            return

        embed = discord.Embed(
            title="하락 대상 종목 선택",
            description=(
                "주가를 5% 내릴 종목을 선택하세요.\n"
                "선택한 뒤 최종 확인을 눌러야 아이템이 사용됩니다."
            ),
            color=discord.Color.blue(),
        )

        await interaction.response.edit_message(
            embed=embed,
            view=StockManipulationCompanySelectView(
                self.owner_id,
                selectable,
            ),
        )

    @discord.ui.button(
        label="취소",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self.owner_only(interaction):
            return

        await interaction.response.edit_message(
            content="사용을 취소했습니다. 아이템은 차감되지 않았습니다.",
            embed=None,
            view=None,
        )


class StockManipulationCompanySelect(discord.ui.Select):
    def __init__(self, owner_id: int, companies: dict[str, dict[str, Any]]):
        self.owner_id = owner_id
        self.companies = companies

        options = []

        for symbol, row in sorted(
            companies.items(),
            key=lambda pair: str(pair[1].get("name", pair[0])),
        ):
            options.append(
                discord.SelectOption(
                    label=str(row.get("name", symbol))[:100],
                    value=symbol,
                    description=(
                        f"현재가 ₩{int(row.get('price', 0)):,}"
                    )[:100],
                )
            )

        super().__init__(
            placeholder="하락시킬 종목을 선택하세요",
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "이 메뉴를 연 사용자만 조작할 수 있습니다.",
                ephemeral=True,
            )
            return

        symbol = self.values[0]
        row = self.companies[symbol]

        view = StockManipulationConfirmView(
            owner_id=self.owner_id,
            mode="fall",
            symbol=symbol,
            company_name=str(row.get("name", symbol)),
        )

        await interaction.response.edit_message(
            embed=view.make_embed(),
            view=view,
        )


class StockManipulationCompanySelectView(discord.ui.View):
    def __init__(self, owner_id: int, companies: dict[str, dict[str, Any]]):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.add_item(
            StockManipulationCompanySelect(owner_id, companies)
        )

    @discord.ui.button(
        label="취소",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "이 메뉴를 연 사용자만 조작할 수 있습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content="사용을 취소했습니다. 아이템은 차감되지 않았습니다.",
            embed=None,
            view=None,
        )


class StockManipulationConfirmView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        mode: str,
        symbol: str | None = None,
        company_name: str | None = None,
    ):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.mode = mode
        self.symbol = symbol
        self.company_name = company_name
        self.processing = False

    def make_embed(self) -> discord.Embed:
        if self.mode == "rise":
            description = (
                "무작위로 선정된 한 종목의 주가를 **5% 상승**시킵니다."
            )
        else:
            description = (
                f"**{self.company_name or self.symbol}**의 주가를 "
                "**5% 하락**시킵니다."
            )

        embed = discord.Embed(
            title="주가조작권 사용 확인",
            description=(
                f"{description}\n\n"
                "확인을 누르면 주가조작권 1개가 소모됩니다."
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text="취소하면 아이템은 소모되지 않습니다."
        )
        return embed

    @discord.ui.button(
        label="확인하고 사용",
        style=discord.ButtonStyle.danger,
        emoji="✅",
    )

    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "이 메뉴를 연 사용자만 조작할 수 있습니다.",
                ephemeral=True,
            )
            return

        if self.processing:
            await interaction.response.send_message(
                "이미 처리 중입니다.",
                ephemeral=True,
            )
            return

        self.processing = True

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # 기존 비공개 창만 갱신한다.
        await interaction.response.edit_message(
            content="주가조작권을 사용하고 있습니다...",
            embed=None,
            view=self,
        )

        stock_bot = interaction.client
        service = get_item_service()
        consumed = False

        try:
            if (
                not isinstance(stock_bot, StockBot)
                or stock_bot.store is None
            ):
                raise RuntimeError(
                    "시장 서비스가 준비되지 않았습니다."
                )

            async with stock_bot.market_lock:
                companies = await asyncio.to_thread(
                    stock_bot.store.get_companies
                )

                if not companies:
                    raise RuntimeError(
                        "시장 종목을 찾을 수 없습니다."
                    )

                if self.mode == "rise":
                    # 무작위 종목의 주가 상승
                    symbol = random.choice(list(companies.keys()))
                    change_ratio = MARKET_MANIPULATION_CHANGE

                else:
                    # 선택한 종목의 주가 하락
                    symbol = self.symbol
                    change_ratio = -MARKET_MANIPULATION_CHANGE

                    if not symbol or symbol not in companies:
                        raise ItemError(
                            "선택한 종목을 찾을 수 없습니다. "
                            "다시 시도해 주세요."
                        )

                    if int(
                        companies[symbol].get("price", 100)
                    ) <= 100:
                        raise ItemError(
                            "해당 종목은 최저가에 도달했습니다. "
                            "다른 종목을 선택해 주세요."
                        )

                # 주가조작권 1개를 먼저 차감한다.
                await asyncio.to_thread(
                    service.consume_one,
                    interaction.user.id,
                    CONSUMABLE_ITEM_ID,
                )
                consumed = True

                # 실제 주가 변경
                try:
                    result = await asyncio.to_thread(
                        apply_stock_manipulation,
                        stock_bot.store,
                        symbol,
                        change_ratio,
                        len(companies),
                    )

                except Exception:
                    # 주가 변경 자체가 실패했다면 소모품 반환을 시도한다.
                    try:
                        await asyncio.to_thread(
                            service.restore_one,
                            interaction.user.id,
                            CONSUMABLE_ITEM_ID,
                        )
                        consumed = False

                    except Exception:
                        logger.exception(
                            "주가조작권 반환 실패: user=%s",
                            interaction.user.id,
                        )

                    raise

            # 여기부터는 주가 변경 성공 이후 처리다.
            company_name = str(
                companies.get(symbol, {}).get("name", symbol)
            )

            actual_pct = float(result["actual_change_pct"])
            old_price = int(result["old_price"])
            new_price = int(result["new_price"])

            if actual_pct > 0:
                headline = (
                    f"의문의 세력에 의해 "
                    f"{company_name}의 주가가 급등했습니다"
                )
            else:
                headline = (
                    f"의문의 세력에 의해 "
                    f"{company_name}의 주가가 급락했습니다"
                )

            body = (
                "정체불명의 세력이 시장에 개입한 것으로 추정됩니다.\n"
                f"{company_name}의 주가는 "
                f"{money(old_price)}에서 {money(new_price)}으로 "
                "변경되었습니다."
            )

            news_item = {
                "timestamp": time.time(),
                "type": "item_manipulation",
                "source": "주가조작권",
                "headline": headline,
                "body": body,
                "symbol": symbol,
                "company_name": company_name,
                "scope": "company",
                "impact": actual_pct / 100.0,
                "old_price": old_price,
                "new_price": new_price,
            }

            # /뉴스 명령어에서도 주가조작 뉴스를 확인할 수 있도록 저장한다.
            try:
                await asyncio.to_thread(
                    stock_bot.store.add_news,
                    news_item,
                )
            except Exception:
                logger.exception(
                    "주가조작 뉴스 저장 실패: symbol=%s",
                    symbol,
                )

            # 공개 뉴스는 NEWS_CHANNEL_ID로만 게시한다.
            # 실패해도 이미 성공한 주가 변경을 되돌리거나
            # 아이템 사용 채널에 대신 게시하지 않는다.
            try:
                news_posted = await stock_bot.publish_news_item(
                    news_item,
                    emergency=False,
                )
            except Exception:
                logger.exception(
                    "주가조작 뉴스 게시 실패: symbol=%s",
                    symbol,
                )
                news_posted = False

            # 남은 주가조작권 수량 확인
            remaining_text = ""

            try:
                account = await asyncio.to_thread(
                    service.get_player,
                    interaction.user.id,
                )
                inventory = (account or {}).get("items", {})
                remaining = int(
                    inventory.get(CONSUMABLE_ITEM_ID, 0) or 0
                )
                remaining_text = (
                    f"\n남은 주가조작권: **{remaining}개**"
                )
            except Exception:
                logger.exception(
                    "주가조작권 잔여 수량 조회 실패"
                )

            result_message = (
                "**주가조작권 사용 완료**\n"
                f"종목: **{company_name}**\n"
                f"변경 전: **{money(old_price)}**\n"
                f"변경 후: **{money(new_price)}**\n"
                f"변동률: **{pct(actual_pct)}**"
                f"{remaining_text}"
            )

            if news_posted:
                result_message += (
                    "\n\n시장 속보를 뉴스 채널에 게시했습니다."
                )
            else:
                result_message += (
                    "\n\n주가는 변경됐지만 뉴스 게시에 실패했습니다. "
                    "관리자에게 `NEWS_CHANNEL_ID` 설정을 확인해 달라고 "
                    "해 주세요."
                )

            # 사용 결과는 사용자 본인에게만 보인다.
            await interaction.followup.send(
                result_message,
                ephemeral=True,
            )

        except ItemError as exc:
            await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )

        except Exception:
            logger.exception(
                "주가조작권 사용 실패: user=%s",
                interaction.user.id,
            )

            message = (
                "주가조작권 사용 중 오류가 발생했습니다. "
                "아이템 보유 수량과 종목 가격을 확인해 주세요."
            )

            if consumed:
                message += (
                    "\n주가 변경 처리 중 오류가 발생했으므로 "
                    "아이템 수량을 확인해 주세요."
                )

            await interaction.followup.send(
                message,
                ephemeral=True,
            )

@bot.tree.command(
    name="아이템사용",
    description="보유한 주가조작권을 사용합니다.",
)
async def use_item(interaction: discord.Interaction) -> None:
    # 명령어 실행 및 모든 후속 응답은 본인에게만 표시한다.
    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    try:
        service = get_item_service()

        account = await asyncio.to_thread(
            service.get_player,
            interaction.user.id,
        )

        if not account:
            await interaction.followup.send(
                "계좌가 없습니다. 먼저 `/계좌개설`을 이용해 주세요.",
                ephemeral=True,
            )
            return

        inventory = account.get("items", {})

        if not isinstance(inventory, dict):
            inventory = {}

        count = int(
            inventory.get(CONSUMABLE_ITEM_ID, 0) or 0
        )

        if count <= 0:
            await interaction.followup.send(
                "주가조작권을 보유하고 있지 않습니다.\n"
                "`/상점`에서 구매해 주세요.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="주가조작권 사용",
            description=(
                f"현재 보유 수량: **{count}개**\n\n"
                "사용할 효과를 선택하세요.\n\n"
                "📈 **무작위 종목 상승**\n"
                "무작위 종목의 주가를 약 5% 상승시킵니다.\n\n"
                "📉 **특정 종목 하락**\n"
                "선택한 종목의 주가를 약 5% 하락시킵니다.\n\n"
                "최종 확인을 눌러야 주가조작권 1개가 소모됩니다."
            ),
            color=discord.Color.gold(),
        )

        await interaction.followup.send(
            embed=embed,
            view=StockManipulationModeView(
                interaction.user.id
            ),
            ephemeral=True,
        )

    except Exception:
        logger.exception(
            "아이템 사용 창을 여는 중 오류 발생: user=%s",
            interaction.user.id,
        )

        await interaction.followup.send(
            "아이템 사용 창을 여는 중 오류가 발생했습니다. "
            "잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )

if __name__ == "__main__":
    try:
        settings.validate_for_startup()
        bot.run(settings.discord_token, log_handler=None)
    except ValueError as exc:
        logger.error("Startup configuration error: %s", exc)
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        logger.error("Startup error: %s", exc)
        raise SystemExit(1) from exc
