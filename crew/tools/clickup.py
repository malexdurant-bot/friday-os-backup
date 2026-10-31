"""
Xcerebro 2.0 — ClickUp Tools

Task management access via the ClickUp API v2.
Requires CLICKUP_API_KEY (personal token) and CLICKUP_TEAM_ID in env.
"""

import json
import os
from typing import Type

import requests
from crewai.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

CLICKUP_BASE = "https://api.clickup.com/api/v2"


def _headers() -> dict:
    return {
        "Authorization": os.getenv("CLICKUP_API_KEY", ""),
        "Content-Type": "application/json",
    }


def _safe_request(method: str, url: str, **kwargs) -> dict:
    if not os.getenv("CLICKUP_API_KEY"):
        return {"error": "CLICKUP_API_KEY not configured in environment."}
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=15, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        logger.error(f"ClickUp API error {e.response.status_code}: {e.response.text[:300]}")
        return {"error": str(e), "status_code": e.response.status_code}
    except Exception as e:
        logger.error(f"ClickUp request failed: {e}")
        return {"error": str(e)}


class ReadTasksInput(BaseModel):
    list_id: str = Field(default="", description="ClickUp list ID to read tasks from. Empty = list the team's spaces/lists so you can find one.")
    include_closed: bool = Field(default=False, description="Include completed tasks")


class ClickUpReadTasksTool(BaseTool):
    name: str = "clickup_read_tasks"
    description: str = (
        "Read tasks from ClickUp. With no list_id, returns the team's spaces and "
        "lists (use this first to discover IDs). With a list_id, returns its tasks: "
        "name, status, assignees, due date, priority."
    )
    args_schema: Type[BaseModel] = ReadTasksInput

    def _run(self, list_id: str = "", include_closed: bool = False) -> str:
        team_id = os.getenv("CLICKUP_TEAM_ID", "")
        if not list_id:
            if not team_id:
                return json.dumps({"error": "CLICKUP_TEAM_ID not configured."})
            spaces = _safe_request("GET", f"{CLICKUP_BASE}/team/{team_id}/space")
            if "error" in spaces:
                return json.dumps(spaces)
            out = []
            for space in spaces.get("spaces", []):
                lists = _safe_request("GET", f"{CLICKUP_BASE}/space/{space['id']}/list")
                out.append({
                    "space": space["name"],
                    "space_id": space["id"],
                    "lists": [{"id": l["id"], "name": l["name"]} for l in lists.get("lists", [])],
                })
            return json.dumps({"spaces": out})

        params = {"include_closed": str(include_closed).lower()}
        data = _safe_request("GET", f"{CLICKUP_BASE}/list/{list_id}/task", params=params)
        if "error" in data:
            return json.dumps(data)
        tasks = [
            {
                "id": t["id"],
                "name": t["name"],
                "status": (t.get("status") or {}).get("status"),
                "assignees": [a.get("username") for a in t.get("assignees", [])],
                "due_date": t.get("due_date"),
                "priority": (t.get("priority") or {}).get("priority"),
                "url": t.get("url"),
            }
            for t in data.get("tasks", [])
        ]
        return json.dumps({"task_count": len(tasks), "tasks": tasks})


class CreateTaskInput(BaseModel):
    list_id: str = Field(description="ClickUp list ID to create the task in")
    name: str = Field(description="Task title")
    description: str = Field(default="", description="Task description/details")
    due_date_ms: int = Field(default=0, description="Optional due date as unix epoch milliseconds")


class ClickUpCreateTaskTool(BaseTool):
    name: str = "clickup_create_task"
    description: str = "Create a task in a ClickUp list. Use clickup_read_tasks with no list_id first to find list IDs."
    args_schema: Type[BaseModel] = CreateTaskInput

    def _run(self, list_id: str, name: str, description: str = "", due_date_ms: int = 0) -> str:
        payload: dict = {"name": name, "description": description}
        if due_date_ms:
            payload["due_date"] = due_date_ms
        data = _safe_request("POST", f"{CLICKUP_BASE}/list/{list_id}/task", json=payload)
        if "error" in data:
            return json.dumps(data)
        return json.dumps({"ok": True, "task_id": data.get("id"), "url": data.get("url")})


class GetTaskInput(BaseModel):
    task_id: str = Field(description="ClickUp task ID (e.g. 86dz70yxb)")


class ClickUpGetTaskTool(BaseTool):
    name: str = "clickup_get_task"
    description: str = "Get full details of one ClickUp task: status, dates, assignees, subtasks, dependencies, description."
    args_schema: Type[BaseModel] = GetTaskInput

    def _run(self, task_id: str) -> str:
        data = _safe_request("GET", f"{CLICKUP_BASE}/task/{task_id}", params={"include_subtasks": "true"})
        if "error" in data:
            return json.dumps(data)
        return json.dumps({
            "id": data.get("id"),
            "name": data.get("name"),
            "description": (data.get("description") or "")[:2000],
            "status": (data.get("status") or {}).get("status"),
            "priority": (data.get("priority") or {}).get("priority"),
            "start_date": data.get("start_date"),
            "due_date": data.get("due_date"),
            "assignees": [{"id": a["id"], "username": a.get("username")} for a in data.get("assignees", [])],
            "parent": data.get("parent"),
            "subtasks": [{"id": s["id"], "name": s["name"], "status": (s.get("status") or {}).get("status")}
                         for s in data.get("subtasks", [])],
            "dependencies": data.get("dependencies", []),
            "list_id": (data.get("list") or {}).get("id"),
            "url": data.get("url"),
        })


class UpdateTaskInput(BaseModel):
    task_id: str = Field(description="ClickUp task ID to update")
    name: str = Field(default="", description="New task name (empty = unchanged)")
    description: str = Field(default="", description="New description (empty = unchanged)")
    status: str = Field(default="", description="New status, e.g. 'in progress', 'complete' (empty = unchanged)")
    priority: int = Field(default=0, description="1=urgent 2=high 3=normal 4=low (0 = unchanged)")
    start_date_ms: int = Field(default=0, description="Start date, unix epoch ms (0 = unchanged). Set for gantt charts.")
    due_date_ms: int = Field(default=0, description="Due date, unix epoch ms (0 = unchanged). Set for gantt charts.")


class ClickUpUpdateTaskTool(BaseTool):
    name: str = "clickup_update_task"
    description: str = (
        "Update a ClickUp task's name, description, status, priority, start date, or due date. "
        "To complete a task, set status to 'complete'. Always set start AND due dates on "
        "project tasks so gantt charts render correctly."
    )
    args_schema: Type[BaseModel] = UpdateTaskInput

    def _run(self, task_id: str, name: str = "", description: str = "", status: str = "",
             priority: int = 0, start_date_ms: int = 0, due_date_ms: int = 0) -> str:
        payload: dict = {}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if status:
            payload["status"] = status
        if priority:
            payload["priority"] = priority
        if start_date_ms:
            payload["start_date"] = start_date_ms
        if due_date_ms:
            payload["due_date"] = due_date_ms
        if not payload:
            return json.dumps({"error": "No fields to update."})
        data = _safe_request("PUT", f"{CLICKUP_BASE}/task/{task_id}", json=payload)
        if "error" in data:
            return json.dumps(data)
        return json.dumps({"ok": True, "task_id": data.get("id"),
                           "status": (data.get("status") or {}).get("status")})


class UpdateAssigneesInput(BaseModel):
    task_id: str = Field(description="ClickUp task ID")
    add_user_ids: list[int] = Field(default_factory=list, description="User IDs to assign (from clickup_read_members)")
    remove_user_ids: list[int] = Field(default_factory=list, description="User IDs to unassign")


class ClickUpUpdateAssigneesTool(BaseTool):
    name: str = "clickup_update_assignees"
    description: str = "Assign or unassign people on a ClickUp task. Get user IDs with clickup_read_members first."
    args_schema: Type[BaseModel] = UpdateAssigneesInput

    def _run(self, task_id: str, add_user_ids: list = None, remove_user_ids: list = None) -> str:
        payload = {"assignees": {"add": add_user_ids or [], "rem": remove_user_ids or []}}
        data = _safe_request("PUT", f"{CLICKUP_BASE}/task/{task_id}", json=payload)
        if "error" in data:
            return json.dumps(data)
        return json.dumps({"ok": True,
                           "assignees": [a.get("username") for a in data.get("assignees", [])]})


class AddCommentInput(BaseModel):
    task_id: str = Field(description="ClickUp task ID")
    comment: str = Field(description="Comment text to add")


class ClickUpAddCommentTool(BaseTool):
    name: str = "clickup_add_comment"
    description: str = "Add a comment to a ClickUp task (status notes, accountability nudges, context)."
    args_schema: Type[BaseModel] = AddCommentInput

    def _run(self, task_id: str, comment: str) -> str:
        data = _safe_request("POST", f"{CLICKUP_BASE}/task/{task_id}/comment",
                             json={"comment_text": comment})
        if "error" in data:
            return json.dumps(data)
        return json.dumps({"ok": True, "comment_id": data.get("id")})


class SetDependencyInput(BaseModel):
    task_id: str = Field(description="The task that must WAIT")
    depends_on: str = Field(description="The task ID it waits for (prerequisite)")


class ClickUpSetDependencyTool(BaseTool):
    name: str = "clickup_set_dependency"
    description: str = (
        "Make one task depend on another (task waits for prerequisite). Dependencies plus "
        "start/due dates are what make ClickUp gantt charts meaningful."
    )
    args_schema: Type[BaseModel] = SetDependencyInput

    def _run(self, task_id: str, depends_on: str) -> str:
        data = _safe_request("POST", f"{CLICKUP_BASE}/task/{task_id}/dependency",
                             json={"depends_on": depends_on})
        if "error" in data:
            return json.dumps(data)
        return json.dumps({"ok": True, "task_id": task_id, "depends_on": depends_on})


class ReadMembersInput(BaseModel):
    pass


class ClickUpReadMembersTool(BaseTool):
    name: str = "clickup_read_members"
    description: str = "List workspace members with their user IDs (needed for assigning tasks)."
    args_schema: Type[BaseModel] = ReadMembersInput

    def _run(self) -> str:
        team_id = os.getenv("CLICKUP_TEAM_ID", "")
        if not team_id:
            return json.dumps({"error": "CLICKUP_TEAM_ID not configured."})
        data = _safe_request("GET", f"{CLICKUP_BASE}/team/{team_id}")
        if "error" in data:
            return json.dumps(data)
        members = [
            {"id": m["user"]["id"], "username": m["user"].get("username"), "email": m["user"].get("email")}
            for m in (data.get("team") or {}).get("members", [])
        ]
        return json.dumps({"members": members})


class CreateSubtaskInput(BaseModel):
    parent_task_id: str = Field(description="Parent task ID")
    list_id: str = Field(description="List ID the parent lives in")
    name: str = Field(description="Subtask title")
    description: str = Field(default="", description="Subtask details")
    start_date_ms: int = Field(default=0, description="Start date, unix epoch ms (set for gantt)")
    due_date_ms: int = Field(default=0, description="Due date, unix epoch ms (set for gantt)")


class ClickUpCreateSubtaskTool(BaseTool):
    name: str = "clickup_create_subtask"
    description: str = (
        "Create a subtask under a parent task. Use for breaking a project into steps — "
        "anything that isn't a simple task becomes a parent with dated subtasks."
    )
    args_schema: Type[BaseModel] = CreateSubtaskInput

    def _run(self, parent_task_id: str, list_id: str, name: str, description: str = "",
             start_date_ms: int = 0, due_date_ms: int = 0) -> str:
        payload: dict = {"name": name, "description": description, "parent": parent_task_id}
        if start_date_ms:
            payload["start_date"] = start_date_ms
        if due_date_ms:
            payload["due_date"] = due_date_ms
        data = _safe_request("POST", f"{CLICKUP_BASE}/list/{list_id}/task", json=payload)
        if "error" in data:
            return json.dumps(data)
        return json.dumps({"ok": True, "task_id": data.get("id"), "url": data.get("url")})


class CreateListInput(BaseModel):
    space_id: str = Field(description="Space ID to create the list in (discover via clickup_read_tasks with no list_id)")
    name: str = Field(description="List name (a 'project' lives as a list)")


class ClickUpCreateListTool(BaseTool):
    name: str = "clickup_create_list"
    description: str = "Create a new list in a ClickUp space (use for new projects / initiatives)."
    args_schema: Type[BaseModel] = CreateListInput

    def _run(self, space_id: str, name: str) -> str:
        data = _safe_request("POST", f"{CLICKUP_BASE}/space/{space_id}/list", json={"name": name})
        if "error" in data:
            return json.dumps(data)
        return json.dumps({"ok": True, "list_id": data.get("id"), "name": data.get("name")})


class DeleteTaskInput(BaseModel):
    task_id: str = Field(description="ClickUp task ID to delete permanently")


class ClickUpDeleteTaskTool(BaseTool):
    name: str = "clickup_delete_task"
    description: str = "Permanently delete a ClickUp task. Destructive — prefer setting status to 'complete' or archiving."
    args_schema: Type[BaseModel] = DeleteTaskInput

    def _run(self, task_id: str) -> str:
        data = _safe_request("DELETE", f"{CLICKUP_BASE}/task/{task_id}")
        if "error" in data:
            return json.dumps(data)
        return json.dumps({"ok": True, "deleted": task_id})


CLICKUP_TOOL_REGISTRY: dict[str, type] = {
    # reads + low-risk updates (ungated)
    "clickup.read_tasks": ClickUpReadTasksTool,
    "clickup.get_task": ClickUpGetTaskTool,
    "clickup.read_members": ClickUpReadMembersTool,
    "clickup.update_task": ClickUpUpdateTaskTool,
    "clickup.add_comment": ClickUpAddCommentTool,
    "clickup.set_dependency": ClickUpSetDependencyTool,
    # structural / destructive (task_management action type — gated)
    "clickup.create_task": ClickUpCreateTaskTool,
    "clickup.create_subtask": ClickUpCreateSubtaskTool,
    "clickup.create_list": ClickUpCreateListTool,
    "clickup.update_assignees": ClickUpUpdateAssigneesTool,
    "clickup.delete_task": ClickUpDeleteTaskTool,
}


def get_clickup_tools(tool_names: list[str]) -> list:
    tools = []
    for name in tool_names:
        cls = CLICKUP_TOOL_REGISTRY.get(name)
        if cls:
            tools.append(cls())
    return tools
