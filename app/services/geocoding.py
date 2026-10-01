"""GPS の位置から住所（都道府県・市区町村・区・町）を調べる（Google Geocoding API の逆ジオコーディング）。

録音アプリの「GPS から訪問先候補を取得」で使う。位置そのもの・API キーはログに出さない。
応答の形で公式ドキュメントを直接確認できていない点は docs/unverified-apis.md に記録している。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.errors import ExternalServiceError
from app.logs import log_event
from app.services.http import send

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_CHOME_RE = re.compile(r"(丁目|番地?|号|[0-9０-９])")


@dataclass(frozen=True)
class Place:
    """住所の部品。分からないものは None。"""

    prefecture: str | None = None  # 都道府県（administrative_area_level_1）
    city: str | None = None  # 市区町村（locality。東京23区は「千代田区」など）
    ward: str | None = None  # 政令指定都市の区（横浜市の「中区」など）
    town: str | None = None  # 町・大字（「山下町」など。丁目・番地は含めない）

    @property
    def label(self) -> str:
        return "".join(p for p in (self.prefecture, self.city, self.ward, self.town) if p)


def parse_place(body: dict[str, Any]) -> Place:
    """Geocoding API の応答から、いちばん詳しい結果の住所の部品を取り出す。"""
    results = body.get("results")
    if not isinstance(results, list):
        return Place()
    for result in results:
        comps = result.get("address_components") if isinstance(result, dict) else None
        if not isinstance(comps, list):
            continue
        found: dict[str, str] = {}
        sublocalities: list[str] = []
        for comp in comps:
            if not isinstance(comp, dict):
                continue
            name = str(comp.get("long_name") or "").strip()
            types = comp.get("types") if isinstance(comp.get("types"), list) else []
            if not name:
                continue
            if "administrative_area_level_1" in types:
                found.setdefault("prefecture", name)
            elif "locality" in types:
                found.setdefault("city", name)
            elif "ward" in types:
                found.setdefault("ward", name)
            elif any(str(t).startswith("sublocality") for t in types):
                sublocalities.append(name)
        city = found.get("city")
        if not city:
            continue
        ward = found.get("ward")
        town = None
        for name in sublocalities:
            if ward is None and city.endswith("市") and name.endswith("区"):
                ward = name
            elif town is None and name != ward and not _CHOME_RE.search(name):
                town = name
        return Place(prefecture=found.get("prefecture"), city=city, ward=ward, town=town)
    return Place()


class Geocoder:
    def __init__(self, http: httpx.AsyncClient, *, api_key: str, retry_base_delay: float = 1.0) -> None:
        self._http = http
        self._api_key = api_key
        self._retry_base_delay = retry_base_delay

    async def reverse(self, lat: float, lng: float) -> Place:
        resp = await send(
            self._http,
            "GET",
            GEOCODE_URL,
            service="geocoding",
            base_delay=self._retry_base_delay,
            params={"latlng": f"{lat:.6f},{lng:.6f}", "language": "ja", "key": self._api_key},
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        status = body.get("status") if isinstance(body, dict) else None
        if resp.status_code != 200 or status not in ("OK", "ZERO_RESULTS"):
            raise ExternalServiceError(
                "geocoding",
                f"位置から住所を調べられませんでした（{status or resp.status_code}）",
                status=resp.status_code,
                retryable=status in ("OVER_QUERY_LIMIT", "UNKNOWN_ERROR"),
            )
        place = parse_place(body) if status == "OK" else Place()
        log_event(logger, "geocoding.reversed", found=bool(place.city), has_ward=bool(place.ward))
        return place
