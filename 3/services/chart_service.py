
"""Create a PNG stock-price chart with Korean font support."""
from __future__ import annotations

from io import BytesIO
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import koreanize_matplotlib  # noqa: F401

# 음수 기호가 깨지는 현상 방지
plt.rcParams["axes.unicode_minus"] = False


def render_price_chart(
    symbol: str,
    company: dict[str, Any],
    points: list[dict[str, Any]],
) -> BytesIO:
    points = sorted(
        points,
        key=lambda point: float(point.get("timestamp", 0)),
    )

    prices = [
        int(point.get("price", company.get("price", 0)))
        for point in points
    ]

    if not prices:
        prices = [int(company.get("price", 0))]

    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=150)

    x_values = list(range(len(prices)))

    if len(prices) < 2:
        line_color = "#808080"  # 데이터 부족: 회색
    elif prices[-1] > prices[0]:
        line_color = "#d64c4c"  # 상승: 빨간색
    elif prices[-1] < prices[0]:
        line_color = "#3478c8"  # 하락: 파란색
    else:
        line_color = "#808080"  # 보합: 회색

    ax.plot(
        x_values,
        prices,
        color=line_color,
        linewidth=2.2,
    )

    ax.fill_between(
        x_values,
        prices,
        min(prices) * 0.998,
        color=line_color,
        alpha=0.12,
    )

    ax.set_title(
        f"{company.get('name', symbol)} 주가 추이",
        fontsize=15,
        pad=14,
    )
    ax.set_ylabel("주가 (원)")
    ax.set_xlabel("최근 시장 업데이트")

    ax.grid(True, alpha=0.22, linestyle="--")
    ax.ticklabel_format(style="plain", axis="y")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()

    output = BytesIO()

    try:
        fig.savefig(
            output,
            format="png",
            bbox_inches="tight",
        )
    finally:
        plt.close(fig)

    output.seek(0)
    return output
