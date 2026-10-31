
"""Generate regular news and emergency market events."""
from __future__ import annotations

import random
import time
from typing import Any

from services.firebase_store import FirebaseStore

# 업종별 일반 경제 뉴스: (제목, 내용)
SECTOR_NEWS = {
    "바이오·헬스케어": {
        "positive": [
            ("신약 후보물질 개발 성공", "핵심 신약 후보물질이 비임상 연구에서 긍정적인 결과를 보였습니다."),
            ("임상시험 주요 목표 달성", "신약 임상시험에서 주요 평가 목표를 달성했다는 소식이 전해졌습니다."),
            ("신약 개발 투자 유치", "신약 연구를 위한 신규 투자 유치에 성공해 연구 자금이 확보됐습니다."),
            ("신약 후보물질 기술이전", "개발 중인 신약 후보물질의 기술이전 계약이 체결됐습니다."),
        ],
        "negative": [
            ("신약 임상시험 목표 미달", "신약 임상시험에서 주요 평가 목표를 충족하지 못해 추가 연구가 필요해졌습니다."),
            ("신약 개발 일정 연기", "추가 검증이 필요해 핵심 신약의 개발 일정이 늦어질 전망입니다."),
            ("임상시험 참여자 모집 지연", "임상시험 참여자 모집이 예상보다 늦어지며 연구 일정에 차질이 생겼습니다."),
            ("신약 연구개발 비용 증가", "추가 실험과 검증이 필요해 신약 연구개발 비용 부담이 커졌습니다."),
        ],
    },
    "에너지": {
        "positive": [
            ("차세대 발전 기술 성과", "차세대 발전 기술의 효율을 높이는 데 성공했습니다."),
            ("대형 에너지 공급 계약", "산업 시설에 에너지를 공급하는 신규 계약을 체결했습니다."),
            ("에너지 저장 기술 개선", "에너지 저장 장치의 효율과 안정성을 개선했다는 소식이 전해졌습니다."),
        ],
        "negative": [
            ("발전 설비 가동 중단", "주요 발전 설비에서 문제가 발생해 일부 가동이 중단됐습니다."),
            ("에너지 생산 비용 상승", "연료와 설비 유지 비용이 상승해 수익성 우려가 커졌습니다."),
            ("에너지 저장 장치 결함", "저장 장치의 추가 점검이 필요해 일부 사업 일정이 지연됐습니다."),
        ],
    },
    "게임·엔터테인먼트": {
        "positive": [
            ("신작 게임 흥행", "신작 게임의 이용자가 빠르게 증가하며 시장의 관심을 받고 있습니다."),
            ("해외 게임 서비스 확대", "신규 해외 시장에 게임을 출시하며 이용자 기반을 넓혔습니다."),
            ("게임 사전예약 기록 경신", "출시 예정 게임이 사전예약에서 높은 관심을 끌고 있습니다."),
        ],
        "negative": [
            ("신작 게임 출시 연기", "완성도를 높이기 위한 추가 개발로 신작 출시가 연기됐습니다."),
            ("게임 서버 장애 발생", "접속 장애가 발생해 이용자 불편과 서비스 안정성 우려가 커졌습니다."),
            ("신작 이용자 반응 부진", "신작의 초기 이용자 반응이 기대에 미치지 못하고 있습니다."),
        ],
    },
    "식품·유통": {
        "positive": [
            ("신제품 판매 호조", "신제품이 소비자들의 좋은 반응을 얻으며 판매량이 증가했습니다."),
            ("해외 유통 계약 체결", "해외 유통업체와 계약을 체결해 신규 판매처를 확보했습니다."),
            ("주력 제품 판매 기록 경신", "주력 제품의 판매량이 증가하며 매출 성장 기대가 커졌습니다."),
        ],
        "negative": [
            ("주력 제품 판매 감소", "소비 수요가 둔화되며 주력 제품의 판매량이 감소했습니다."),
            ("원재료 가격 급등", "주요 원재료 가격이 올라 생산 비용 부담이 커졌습니다."),
            ("물류 차질 발생", "물류 일정에 차질이 생겨 일부 제품의 공급이 지연됐습니다."),
        ],
    },
    "모빌리티·자동차": {
        "positive": [
            ("신형 전기차 주행 성능 개선", "신형 전기차의 주행 효율을 높이는 기술 개발에 성공했습니다."),
            ("대규모 차량 공급 계약", "기업 고객과 대규모 차량 공급 계약을 체결했습니다."),
            ("전기차 판매량 증가", "신형 모델에 대한 수요가 늘어나며 판매 실적이 개선됐습니다."),
        ],
        "negative": [
            ("신차 출시 일정 연기", "부품 조달과 최종 검증 문제로 신차 출시 일정이 연기됐습니다."),
            ("차량 품질 점검 확대", "일부 차량에서 품질 문제가 발견돼 추가 점검이 진행됩니다."),
            ("배터리 개발 일정 지연", "차세대 배터리 검증에 추가 시간이 필요해 개발 일정이 늦어졌습니다."),
        ],
    },
    "IT·기술": {
        "positive": [
            ("AI 기술 성능 개선", "자체 인공지능 기술의 처리 성능과 효율을 개선했습니다."),
            ("대형 기술 서비스 계약", "기업 고객과 대규모 기술 서비스 계약을 체결했습니다."),
            ("클라우드 이용 기업 증가", "클라우드 서비스 이용 기업이 늘어나며 사업 확장 기대가 커졌습니다."),
        ],
        "negative": [
            ("주요 서비스 접속 장애", "주요 기술 서비스에서 접속 장애가 발생해 긴급 점검이 진행됐습니다."),
            ("신규 기술 출시 지연", "추가 보안 검증이 필요해 신규 기술의 출시가 늦어졌습니다."),
            ("개발 비용 증가", "핵심 기술 개발에 예상보다 많은 비용이 투입되고 있습니다."),
        ],
    },
    "과학·연구개발": {
        "positive": [
            ("핵심 연구 실험 성공", "핵심 연구 과제의 실험에서 목표한 결과를 확인했습니다."),
            ("신규 특허 등록", "미래 산업에 활용할 수 있는 신규 기술의 특허를 등록했습니다."),
            ("대형 연구 과제 선정", "정부 및 연구기관의 주요 연구개발 과제에 선정됐습니다."),
        ],
        "negative": [
            ("핵심 실험 결과 재현 실패", "반복 실험에서 동일한 결과를 얻지 못해 추가 검증이 필요해졌습니다."),
            ("연구개발 일정 연장", "예상하지 못한 기술적 문제로 핵심 연구 일정이 연장됐습니다."),
            ("연구 장비 문제 발생", "주요 연구 장비에 문제가 발생해 일부 실험이 중단됐습니다."),
        ],
    },
    "우주·농업기술": {
        "positive": [
            ("우주 작물 재배 실험 성공", "특수 환경에서 작물을 재배하는 실험에서 긍정적인 결과를 얻었습니다."),
            ("스마트 농업 생산성 향상", "새로운 재배 기술을 적용해 작물 생산성을 개선했습니다."),
            ("우주 농업 연구 계약 체결", "우주 환경 농업 기술과 관련한 신규 연구 계약을 체결했습니다."),
        ],
        "negative": [
            ("우주 재배 실험 실패", "작물 생육 조건을 맞추는 데 실패해 재배 환경을 재설계하게 됐습니다."),
            ("작물 생산량 감소", "재배 환경의 변화로 예상보다 작물 생산량이 줄었습니다."),
            ("연구 장비 발사 일정 연기", "연구 장비 준비 문제로 관련 실험 일정이 늦어졌습니다."),
        ],
    },
    "로봇·자동화": {
        "positive": [
            ("산업용 로봇 시험 성공", "신형 산업용 로봇이 성능 시험에서 목표를 달성했습니다."),
            ("공장 자동화 계약 수주", "대형 생산 시설의 자동화 설비 공급 계약을 수주했습니다."),
            ("로봇 작업 정밀도 개선", "로봇 제어 기술을 개선해 작업 정밀도를 높였습니다."),
        ],
        "negative": [
            ("로봇 제어 오류 발견", "시험 과정에서 제어 오류가 발견돼 추가 수정이 필요해졌습니다."),
            ("자동화 설비 납품 지연", "핵심 부품 공급 문제로 자동화 설비 납품이 늦어졌습니다."),
            ("로봇 안정성 재검증", "안전성 검증에서 추가 확인 사항이 발견됐습니다."),
        ],
    },
    "신재생에너지": {
        "positive": [
            ("태양광 발전 효율 향상", "신규 태양광 기술의 발전 효율을 높이는 데 성공했습니다."),
            ("풍력 발전 사업 수주", "대규모 풍력 발전 설비 구축 사업을 수주했습니다."),
            ("에너지 저장 기술 진전", "재생에너지 저장 기술의 성능을 개선했습니다."),
        ],
        "negative": [
            ("풍력 설비 설치 지연", "기상과 장비 문제로 풍력 설비 설치 일정이 늦어졌습니다."),
            ("태양광 모듈 효율 저하", "품질 검사에서 일부 모듈의 성능 저하가 확인됐습니다."),
            ("재생에너지 사업 비용 증가", "설비와 자재 비용이 상승해 사업 수익성 우려가 커졌습니다."),
        ],
    },
    "통신·네트워크": {
        "positive": [
            ("차세대 통신 시험 성공", "차세대 통신 기술 시험에서 목표한 속도와 안정성을 확인했습니다."),
            ("네트워크 구축 계약 체결", "대형 네트워크 구축 사업 계약을 체결했습니다."),
            ("통신망 서비스 지역 확대", "신규 지역에 통신망 서비스를 확대하게 됐습니다."),
        ],
        "negative": [
            ("통신망 장애 발생", "일부 통신 구간에서 장애가 발생해 긴급 복구가 진행됐습니다."),
            ("차세대 통신 개발 지연", "추가 성능 검증이 필요해 차세대 통신 기술 개발이 늦어졌습니다."),
            ("네트워크 장비 공급 차질", "핵심 네트워크 장비의 공급 일정에 차질이 발생했습니다."),
        ],
    },
    "첨단과학·양자기술": {
        "positive": [
            ("양자컴퓨터 연산 실험 성공", "양자컴퓨터의 핵심 연산 실험에서 의미 있는 성능 개선을 확인했습니다."),
            ("양자 오류 보정 기술 진전", "양자 연산의 오류를 줄이는 기술에서 연구 성과를 거뒀습니다."),
            ("첨단 측정 기술 특허 확보", "초정밀 측정 분야의 핵심 기술 특허를 확보했습니다."),
        ],
        "negative": [
            ("양자 연산 안정성 문제", "반복 실험에서 연산 안정성 문제가 발견돼 추가 연구가 필요해졌습니다."),
            ("양자 오류 보정 목표 미달", "오류 보정 실험이 목표에 도달하지 못해 연구 방향을 재검토하고 있습니다."),
            ("첨단 연구 장비 일정 지연", "핵심 연구 장비의 준비가 늦어져 실험 일정이 연기됐습니다."),
        ],
    },
}

# 업종 정보가 없거나 새 업종이 추가된 경우 사용할 기본 뉴스
DEFAULT_COMPANY_NEWS = {
    "positive": [
        ("신규 사업 계약 체결", "신규 사업 계약을 체결하며 성장 기대가 커졌습니다."),
        ("핵심 사업 성과 개선", "핵심 사업의 운영 성과가 개선됐다는 소식이 전해졌습니다."),
        ("신규 시장 진출", "새로운 시장에 진출하며 사업 확장 기회를 확보했습니다."),
    ],
    "negative": [
        ("사업 일정 지연", "예상하지 못한 문제로 주요 사업 일정이 지연됐습니다."),
        ("운영 비용 증가", "운영 비용이 증가하며 수익성 우려가 커졌습니다."),
        ("사업 목표 미달", "최근 사업 실적이 당초 목표에 미치지 못했습니다."),
    ],
}

# =========================================================
# 시장 전체 일반 경제 뉴스
# =========================================================

MARKET = [
    (
        "국내 투자 심리 개선",
        "투자자들의 시장 전망이 개선되며 전반적인 매수세가 나타나고 있습니다.",
    ),
    (
        "금융시장 변동성 확대",
        "국내외 경제 상황에 대한 불확실성이 커지며 시장 변동성이 확대됐습니다.",
    ),
    (
        "기업 투자 확대 전망",
        "주요 기업들의 신규 투자 계획이 알려지면서 시장의 기대감이 높아졌습니다.",
    ),
    (
        "경기 회복 기대감 확산",
        "경제 지표가 개선될 것이라는 전망이 나오면서 투자자들의 관심이 높아졌습니다.",
    ),
    (
        "시장 관망세 확대",
        "투자자들이 새로운 경제 지표 발표를 기다리면서 거래가 신중해지고 있습니다.",
    ),
]


# =========================================================
# 긴급 뉴스 문구
# 구조: 범위 -> 방향 -> (제목, 내용)
# =========================================================

EMERGENCY_TEXTS = {
    "market": {
        "positive": (
            "시장 전반에 강한 호재 발생",
            "긍정적인 경제 지표와 투자 심리 개선 소식이 전해지며 "
            "시장 전반에 활기가 돌고 있습니다.",
        ),
        "negative": (
            "시장 전체에 악재 확산",
            "국내외 경제 불확실성이 확대되면서 투자자들의 불안감이 "
            "커지고 있습니다.",
        ),
    },

    "sector": {
        "positive": (
            "관련 업종 대형 호재 발생",
            "해당 업종에 대규모 투자와 신규 사업 확대 소식이 전해지며 "
            "업계의 기대감이 커지고 있습니다.",
        ),
        "negative": (
            "관련 업종 악재 발생",
            "업종 전반에 비용 상승과 사업 불확실성에 대한 우려가 "
            "확산되고 있습니다.",
        ),
    },

    "company": {
        "positive": (
            "핵심 사업에서 대형 성과 발표",
            "핵심 사업의 성과가 시장의 예상보다 뛰어난 것으로 알려지며 "
            "투자자들의 관심이 집중되고 있습니다.",
        ),
        "negative": (
            "핵심 사업에 예상치 못한 문제 발생",
            "주요 사업의 일정과 실적에 영향을 줄 수 있는 문제가 "
            "발생했다는 소식이 전해졌습니다.",
        ),
    },
}

class NewsEngine:
    def __init__(self, store: FirebaseStore) -> None:
        self.store = store

    def maybe_emit(
        self,
        companies: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Chance-based regular news during normal market updates."""
        if not companies or random.random() > 0.075:
            return None
        return self.generate_one(companies)

    
    def generate_one(
        self,
        companies: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Generate sector-specific company news or broader market news."""
        if not companies:
            return None

        now = time.time()

        # 시장 전체 뉴스는 15%, 회사별 업종 뉴스는 85% 확률로 발생
        if random.random() < 0.15:
            headline, body = random.choice(MARKET)
            impact = random.choice([-1, 1]) * random.uniform(0.004, 0.012)

            item = {
                "timestamp": now,
                "type": "market",
                "scope": "market",
                "symbol": "MARKET",
                "headline": headline,
                "body": body,
                "impact": round(impact, 5),
                "source": "경제 뉴스",
            }

        else:
            symbol = random.choice(list(companies.keys()))
            company = companies[symbol]

            company_name = str(company.get("name", symbol))
            sector = str(company.get("sector", "기타"))

            # 기업의 업종에 맞는 뉴스 묶음을 선택
            news_templates = SECTOR_NEWS.get(
                sector,
                DEFAULT_COMPANY_NEWS,
            )

            direction = random.choice(["positive", "negative"])
            headline, body = random.choice(
                news_templates[direction]
            )

            impact = random.uniform(0.012, 0.035)
            if direction == "negative":
                impact = -impact

            item = {
                "timestamp": now,
                "type": direction,
                "scope": "company",
                "symbol": symbol,
                "company_name": company_name,
                "sector": sector,
                "headline": f"{company_name}: {headline}",
                "body": body,
                "impact": round(impact, 5),
                "source": "기업 공시",
            }

        self.store.add_news(item)
        return item

    def generate_emergency(
        self,
        companies: dict[str, dict[str, Any]],
        scope: str | None = None,
        target: str | None = None,
        direction: str | None = None,
    ) -> dict[str, Any] | None:
        """Create a market-wide, sector, or company emergency event."""
        if not companies:
            return None

        # Automatic events randomly choose scope and direction.
        if scope is None:
            scope = random.choices(
                ["market", "sector", "company"],
                weights=[35, 30, 35],
                k=1,
            )[0]

        if direction is None:
            direction = random.choice(["positive", "negative"])

        if scope not in {"market", "sector", "company"}:
            raise ValueError("긴급 뉴스 범위가 올바르지 않습니다.")

        if direction not in {"positive", "negative"}:
            raise ValueError("긴급 뉴스 방향이 올바르지 않습니다.")

        now = time.time()
        duration_ticks = 5

        if scope == "market":
            headline, body = EMERGENCY_TEXTS["market"][direction]
            symbol = "MARKET"
            target_label = "시장 전체"
            sector = None
            company_name = None
            magnitude = random.uniform(0.025, 0.035)

        elif scope == "sector":
            sectors = sorted({
                str(company.get("sector", "기타"))
                for company in companies.values()
            })

            if target is None:
                target = random.choice(sectors)

            sector = next(
                (
                    value for value in sectors
                    if value.casefold() == target.casefold()
                ),
                None,
            )

            if sector is None:
                return None

            headline, body = EMERGENCY_TEXTS["sector"][direction]
            headline = f"{sector} 긴급 속보: {headline}"
            body = f"{sector} 업종 관련 소식입니다. {body}"

            symbol = "SECTOR"
            target_label = sector
            company_name = sector
            magnitude = random.uniform(0.035, 0.05)

        else:
            if target is None:
                target = random.choice(list(companies.keys()))

            if target not in companies:
                return None

            company = companies[target]
            name = str(company.get("name", target))

            headline, body = EMERGENCY_TEXTS["company"][direction]
            headline = f"{name} 긴급 공시: {headline}"
            body = f"{name} 관련 소식입니다. {body}"

            symbol = target
            target_label = name
            company_name = name
            sector = None
            magnitude = random.uniform(0.045, 0.06)

        impact = magnitude if direction == "positive" else -magnitude

        item = {
            "timestamp": now,
            "type": "emergency",
            "scope": scope,
            "direction": direction,
            "symbol": symbol,
            "company_name": company_name,
            "sector": sector,
            "target_label": target_label,
            "headline": f"긴급 속보 | {headline}",
            "body": body,
            "impact": round(impact, 5),
            "duration_ticks": duration_ticks,
            "ticks_remaining": duration_ticks,
            "source": "긴급 뉴스 이벤트",
        }

        # Save the event so that /뉴스 can display it later.
        self.store.add_news(item)
        return item
