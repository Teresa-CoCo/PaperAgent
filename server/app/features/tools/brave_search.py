import httpx

from app.core.config import get_settings


class BraveSearchTool:
    async def search(self, query: str, count: int = 5) -> list[dict]:
        settings = get_settings()
        if not settings.brave_api_key:
            return []

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={"X-Subscription-Token": settings.brave_api_key},
                    params={"q": query, "count": count},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError:
            return []
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            }
            for item in data.get("web", {}).get("results", [])
        ]
