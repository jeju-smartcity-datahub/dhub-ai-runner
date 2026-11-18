"""A ReAct agent built from a dhub2 Agent resource.

The model is called with the agent's tools until it answers without a tool
call. Supported: python tools and actors (``execute(**inputs)``), actor
``confirmation_policy`` (``confirm`` pauses for approval), ``roles`` (each
becomes a ``delegate_to_<role>`` tool), conversation variables, prompts, a
structured ``outputs`` schema and dhub2's builtin tools the runner answers
itself, such as text-to-SQL over the collection's Delta tables (``agent.tools``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from commons.collection import Collection, CollectionError
from commons.llm import create_chat_model, resolve_llm
from commons.scripts import Resources, load_function
from commons.storage import Storage
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)

from .tools import SPECS, Tools, builtin_name, is_dhub2_builtin

log = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], None]
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


class AgentError(Exception):
    pass


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    call: Callable[[dict[str, Any], dict[str, Any]], Any]
    confirmation: str = "auto"
    fixed_inputs: dict[str, Any] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Agent:
    def __init__(
        self,
        collection: Collection,
        resources: Resources,
        name: str | None = None,
        node: dict[str, Any] | None = None,
        conversations: list[dict[str, Any]] | None = None,
        depth: int = 0,
        storage: Storage | None = None,
    ):
        self.collection = collection
        self.resources = resources
        self.storage = storage
        if node is None:
            doc = collection.get("agent", name)
            if (doc.get("mode") or "react") != "react":
                raise AgentError(
                    f"agent {name!r}: mode {doc.get('mode')!r} is not supported (react only)"
                )
            semantic = [key for key in ("tables", "templates", "ontologies") if doc.get(key)]
            if semantic:
                raise AgentError(
                    f"agent {name!r}: the semantic layer ({', '.join(semantic)}) is not supported"
                )
            node = doc.get("agent") or {}
            conversations = doc.get("conversations")
            name = doc.get("name") or name
        self.name = name or node.get("name") or "agent"
        self.node = node
        self.conversations = conversations or []
        self.max_iterations = node.get("max_iterations") or 50
        self.outputs = node.get("outputs")
        self.model = create_chat_model(
            resolve_llm(collection, node["model"], node.get("model_provider"))
        )
        self.tools: dict[str, Tool] = {}
        # tool_names / actor_names are the older spellings of tools / actors.
        for ref in node.get("tools") or node.get("tool_names") or []:
            self._add_function_tool(ref, "tool")
        for ref in node.get("actors") or node.get("actor_names") or []:
            self._add_function_tool(ref, "actor")
        for ref in node.get("roles") or []:
            if depth >= 3:
                raise AgentError(f"agent {self.name!r}: roles are nested too deep")
            self._add_role(ref, depth)
        if self.conversations:
            self._add_variable_tool()
        schemas = [t.schema() for t in self.tools.values()]
        self.bound = self.model.bind_tools(schemas) if schemas else self.model
        self.prompt_prefix = self._render_prompts()

    # -- building ----------------------------------------------------------

    def _resolve(self, ref: Any, kind: str) -> dict[str, Any]:
        if isinstance(ref, str):
            ref = {"name": ref}
        # A full definition inline (python source).
        if (ref.get("function") or {}).get("source"):
            return ref
        doc = (
            self.collection.find(kind, ref.get("id"))
            or self.collection.find(kind, ref.get("name"))
            or self.collection.find("tool", ref.get("id"))
            or self.collection.find("tool", ref.get("name"))
        )
        if doc is None:
            raise CollectionError(
                f"agent {self.name!r}: {kind} {ref.get('name') or ref.get('id')!r} not found in collection"
            )
        # The reference's own non-empty fields (settings, policy, ...) win.
        return {**doc, **{k: v for k, v in ref.items() if v not in (None, "", {}, [])}}

    def _add_function_tool(self, ref: Any, kind: str) -> None:
        if kind == "tool" and self._add_builtin(ref):
            return
        doc = self._resolve(ref, kind)
        name = doc["name"]
        tool_type = doc.get("type") or "python"
        if tool_type in ("agent", "workflow"):
            log.warning("agent %s: %s tool %r is skipped (use roles)", self.name, tool_type, name)
            return
        if tool_type != "python":
            raise AgentError(f"agent {self.name!r}: {tool_type} tool {name!r} is not supported")
        settings = doc.get("settings") or {}
        policy = "auto"
        if kind == "actor":
            policy = (
                settings.get("confirmation_policy") or doc.get("confirmation_policy") or "confirm"
            )
            if policy == "deny":
                return
        function = doc.get("function") or {}
        source = function.get("source")
        if not source:
            raise AgentError(f"agent {self.name!r}: {kind} {name!r} has no source")
        namespace, _ = self.resources.namespace(
            source, function.get("decorators")
        )  # held for the agent's lifetime
        execute = load_function(source, "execute", namespace, f"tool:{name}")
        self._add_execute(name, doc, function.get("inputs"), execute, policy)

    def _add_builtin(self, ref: Any) -> bool:
        """Add a builtin tool (``agent.tools``) unless the collection defines
        a tool of its own under that name."""
        if isinstance(ref, str):
            ref = {"name": ref}
        name = builtin_name(ref)
        if name is None or (ref.get("function") or {}).get("source"):
            return False
        doc = self.collection.find("tool", ref.get("id")) or self.collection.find(
            "tool", ref.get("name")
        )
        if doc is not None and not is_dhub2_builtin(doc, name):
            return False
        if self.storage is None:
            raise AgentError(f"agent {self.name!r}: tool {name!r} needs the collection's tables")
        spec = SPECS[name]
        # dhub2's own description speaks of its API; the reference's settings
        # (guidance, fixed_inputs) and options still apply.
        doc = {
            "description": spec["description"],
            "settings": ref.get("settings"),
            "options": ref.get("options"),
        }
        execute = Tools(self.collection, self.storage).function(name)
        self._add_execute(name, doc, spec["inputs"], execute, "auto")
        return True

    def _add_execute(
        self,
        name: str,
        doc: dict[str, Any],
        inputs: dict[str, Any] | None,
        execute: Callable[..., Any],
        policy: str,
    ) -> None:
        settings = doc.get("settings") or {}
        fixed = settings.get("fixed_inputs") or {}
        parameters = json.loads(json.dumps(inputs or {"type": "object", "properties": {}}))
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
        for key in fixed:
            parameters["properties"].pop(key, None)
            if key in parameters.get("required", []):
                parameters["required"].remove(key)
        description = doc.get("description") or name
        if settings.get("guidance"):
            description += "\n" + settings["guidance"]
        timeout = (doc.get("options") or {}).get("timeout_seconds") or 180

        def call(args: dict[str, Any], session: dict[str, Any]) -> Any:
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                return pool.submit(execute, **{**args, **fixed}).result(timeout=timeout)
            except FutureTimeout:
                return {"error": f"tool {name} timed out after {timeout}s"}
            finally:
                pool.shutdown(wait=False)  # a timed-out call is left to finish on its own

        self.tools[_tool_name(name)] = Tool(
            _tool_name(name), description, parameters, call, policy, fixed
        )

    def _add_role(self, ref: Any, depth: int) -> None:
        if isinstance(ref, dict) and ref.get("instructions") is not None:
            node, role_name, description = ref, ref.get("name"), ref.get("description")
        else:
            key = ref if isinstance(ref, str) else ref.get("id") or ref.get("name")
            doc = self.collection.get("agent", key)
            node, role_name, description = (
                doc.get("agent") or {},
                doc.get("name"),
                doc.get("description"),
            )
        sub = Agent(
            self.collection,
            self.resources,
            name=role_name,
            node=node,
            depth=depth + 1,
            storage=self.storage,
        )
        tool_name = _tool_name(f"delegate_to_{role_name}")
        self.tools[tool_name] = Tool(
            tool_name,
            f"Delegate a task to the {role_name!r} agent. {description or node.get('instructions', '')[:300]}",
            {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What the agent should do"}
                },
                "required": ["task"],
            },
            lambda args, session: sub.ask(args["task"]),
        )

    def _add_variable_tool(self) -> None:
        names = [v["name"] for v in self.conversations]

        def call(args: dict[str, Any], session: dict[str, Any]) -> Any:
            if args.get("name") not in names:
                return {"error": f"unknown variable {args.get('name')!r}"}
            session.setdefault("variables", {})[args["name"]] = args.get("value")
            return {"ok": True}

        self.tools["update_variable"] = Tool(
            "update_variable",
            "Remember a conversation variable for the rest of this conversation.",
            {
                "type": "object",
                "properties": {"name": {"type": "string", "enum": names}, "value": {}},
                "required": ["name", "value"],
            },
            call,
        )

    def _render_prompts(self) -> str:
        parts = []
        for ref in self.node.get("prompts") or []:
            prompt = self.collection.get("prompt", ref.get("prompt_id"))
            if prompt.get("long_context"):
                log.warning(
                    "prompt %s: its long context document is not available in the runner; "
                    "only the prompt's content is used",
                    prompt.get("name"),
                )
            values = {v["name"]: v.get("default", "") for v in prompt.get("variables") or []}
            values.update(ref.get("variables") or {})
            parts.append(
                _PLACEHOLDER.sub(
                    lambda m, values=values: str(values.get(m.group(1), m.group(0))),
                    prompt.get("content") or "",
                )
            )
        return "\n\n".join(p for p in parts if p)

    def system_prompt(self, session: dict[str, Any]) -> str:
        parts = [self.prompt_prefix, self.node.get("instructions") or ""]
        if self.conversations:
            variables = session.get("variables") or {}
            lines = [
                f"- {v['name']} ({v.get('type', 'string')}): {v.get('description', '')} = "
                f"{json.dumps(variables.get(v['name'], v.get('default')), ensure_ascii=False)}"
                for v in self.conversations
            ]
            parts.append(
                "Conversation variables (update with update_variable):\n" + "\n".join(lines)
            )
        if self.node.get("current_date") is not False:
            parts.append(f"Today's date is {date.today().isoformat()}.")
        return "\n\n".join(p for p in parts if p)

    # -- running -----------------------------------------------------------

    def new_session(self) -> dict[str, Any]:
        return {
            "messages": [],
            "variables": {v["name"]: v.get("default") for v in self.conversations},
            "pending": [],
        }

    def chat(
        self,
        session: dict[str, Any],
        incoming: list[dict[str, Any]],
        on_event: EventHandler | None = None,
    ) -> dict[str, Any]:
        """Continue ``session`` with OpenAI-style ``incoming`` messages.

        Returns ``{"content", "pending"}``; ``pending`` lists actor calls
        waiting for approval. Approve or deny with a tool message per call:
        ``{"role": "tool", "tool_call_id": ..., "content": "{\\"approved\\": true}"}``.
        ``session`` is updated in place."""
        emit = on_event or (lambda kind, data: None)
        history: list[BaseMessage] = messages_from_dict(session.get("messages") or [])
        extra_system = [m["content"] for m in incoming if m.get("role") == "system"]
        pending = session.get("pending") or []
        if pending:
            approvals = {
                m.get("tool_call_id"): _approved(m.get("content"))
                for m in incoming
                if m.get("role") == "tool"
            }
            for call in pending:
                if call["id"] not in approvals:
                    raise AgentError(
                        f"tool call {call['id']} ({call['name']}) is waiting for approval"
                    )
                if approvals[call["id"]]:
                    content = self._execute(call, session, emit)
                else:
                    content = "The user denied this action."
                history.append(ToolMessage(content, tool_call_id=call["id"], name=call["name"]))
            session["pending"] = []
        for message in incoming:
            role = message.get("role")
            if role == "user":
                history.append(HumanMessage(message.get("content") or ""))
            elif role == "assistant":
                history.append(AIMessage(message.get("content") or ""))
        result = self._loop(history, session, emit, extra_system)
        session["messages"] = messages_to_dict(history)
        return result

    def _loop(
        self,
        history: list[BaseMessage],
        session: dict[str, Any],
        emit: EventHandler,
        extra_system: list[str],
    ) -> dict[str, Any]:
        system = "\n\n".join([self.system_prompt(session), *extra_system])
        for _ in range(self.max_iterations):
            reply = self.bound.invoke([SystemMessage(system), *history])
            history.append(reply)
            calls = getattr(reply, "tool_calls", None) or []
            if not calls:
                content = _text(reply.content)
                if self.outputs:
                    content = self._format(system, history)
                return {"content": content, "pending": []}
            pending = []
            for call in calls:
                tool = self.tools.get(call["name"])
                if tool is not None and tool.confirmation == "confirm":
                    pending.append({"id": call["id"], "name": call["name"], "args": call["args"]})
                    emit("approval", pending[-1])
                    continue
                content = self._execute(call, session, emit)
                history.append(ToolMessage(content, tool_call_id=call["id"], name=call["name"]))
            if pending:
                session["pending"] = pending
                return {"content": _text(reply.content), "pending": pending}
        return {"content": f"Stopped after {self.max_iterations} iterations.", "pending": []}

    def _execute(self, call: dict[str, Any], session: dict[str, Any], emit: EventHandler) -> str:
        tool = self.tools.get(call["name"])
        if tool is None:
            return json.dumps({"error": f"unknown tool {call['name']!r}"})
        emit("tool", {"name": call["name"], "args": call.get("args") or {}})
        try:
            result = tool.call(call.get("args") or {}, session)
        except Exception as exc:  # the model sees the error and can recover
            log.warning("tool %s failed", call["name"], exc_info=True)
            result = {"error": f"{type(exc).__name__}: {exc}"}
        failed = isinstance(result, dict) and "error" in result
        emit(
            "tool_result", {"name": call["name"], **({"error": result["error"]} if failed else {})}
        )
        return (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, default=str)
        )

    def _format(self, system: str, history: list[BaseMessage]) -> str:
        schema = {"title": "final_output", "description": "The final answer", **self.outputs}
        structured = self.model.with_structured_output(schema)
        result = structured.invoke(
            [
                SystemMessage(system),
                *history,
                HumanMessage("Return the final answer in the required structure."),
            ]
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    def ask(self, text: str) -> str:
        """One stateless question; actor calls needing approval are denied."""
        session = self.new_session()
        result = self.chat(session, [{"role": "user", "content": text}])
        if result["pending"]:
            names = [c["name"] for c in result["pending"]]
            return f"{result['content']}\n(Needs user approval to run: {names})"
        return result["content"]

    def invoke(self, inputs: dict[str, Any]) -> Any:
        """A pipeline agent step: inputs in as JSON, the answer out (parsed
        JSON when the agent declares ``outputs``)."""
        answer = self.ask(json.dumps(inputs, ensure_ascii=False, default=str))
        if self.outputs:
            try:
                return json.loads(answer)
            except json.JSONDecodeError:
                pass
        return answer


def _tool_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64]


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def _approved(content: Any) -> bool:
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return content.strip().lower() in ("yes", "y", "true", "approve", "approved")
    if isinstance(content, dict):
        return bool(content.get("approved"))
    return bool(content)
