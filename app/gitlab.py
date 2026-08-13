from typing import Any

import httpx

from .config import Settings


class GitLabClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.api_base,
            headers={"PRIVATE-TOKEN": settings.gitlab_token},
            timeout=httpx.Timeout(30.0),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()

    async def get_issue(self, project_id: int, issue_iid: int) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{project_id}/issues/{issue_iid}")

    async def get_project(self, project_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{project_id}")

    async def add_remove_labels(
        self,
        project_id: int,
        issue_iid: int,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        data: dict[str, str] = {}
        if add:
            data["add_labels"] = ",".join(add)
        if remove:
            data["remove_labels"] = ",".join(remove)
        return await self._request(
            "PUT",
            f"/projects/{project_id}/issues/{issue_iid}",
            data=data,
        )

    async def post_note(self, project_id: int, issue_iid: int, body: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            data={"body": body},
        )

    async def list_notes(self, project_id: int, issue_iid: int) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self.client.get(
                f"/projects/{project_id}/issues/{issue_iid}/notes",
                params={"sort": "asc", "order_by": "created_at", "per_page": 100, "page": page},
            )
            response.raise_for_status()
            batch = response.json()
            notes.extend(batch)
            next_page = response.headers.get("x-next-page")
            if not next_page:
                break
            page = int(next_page)
        return notes
