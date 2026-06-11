"""
base_agent.py — One reusable "employee".

There is no special code per role. An Agent is just a config entry (its name,
title, model, and system prompt) plus shared access to the team's memory.
That's the whole point of the framework: to add a new specialist you add a
dictionary to config.py — you do NOT write a new class.

When an agent runs it:
  1. announces itself in the live activity feed,
  2. reads shared context (the goal, the uploaded files, and what teammates
     have already produced),
  3. asks Claude to do its specific assignment,
  4. saves the result back into shared memory for the next teammate.
"""

from . import config, deliverables
from .token_manager import BudgetExceededError


class Agent:
    def __init__(self, agent_config, memory, llm):
        self.cfg = agent_config
        self.id = agent_config["id"]
        self.name = agent_config["name"]
        self.title = agent_config["title"]
        self.memory = memory
        self.llm = llm

    @property
    def label(self):
        return f"{self.name} ({self.title})"

    def run(self, instruction):
        self.memory.log(self.id, f"Starting: {instruction}", type="working")

        user_content = self._build_context(instruction)
        # "web_search" is a capability, not a file builder — pull it out so the
        # remaining names map cleanly onto deliverables.specs_for().
        tool_names = self.cfg.get("tools") or []
        web_search = "web_search" in tool_names
        specs = deliverables.specs_for([t for t in tool_names if t != "web_search"])
        try:
            if specs:
                # This agent can produce REAL files: let it call its tools.
                result = self._run_with_tools(specs, user_content, web_search=web_search)
            else:
                result = self.llm.complete(
                    system_prompt=self.cfg["system_prompt"],
                    user_content=user_content,
                    # effective_model() honours any per-agent model picked in the UI,
                    # falling back to this agent's default from config.py.
                    model=config.effective_model(self.id),
                    effort=self.cfg.get("effort", "high"),
                    # Per-agent output cap (org chart knob): the Report Writer
                    # needs extra room for its mandatory SOURCES_JSON trail.
                    max_tokens=self.cfg.get("max_tokens", 8000),
                    budget=self.cfg.get("budget", config.AGENT_TEAM_BUDGET),
                    web_search=web_search,
                )
        except BudgetExceededError as e:
            self.memory.log(self.id, f"Token budget reached — stopping before spending more. {e}",
                            type="warning")
            raise
        except Exception as e:
            self.memory.log(self.id, f"Hit an error: {e}", type="error")
            raise

        # An answer that stopped at the output ceiling was CUT OFF, not finished.
        # Say so in the feed — a silent truncation looks identical to success.
        if getattr(self.llm, "last_stop_reason", None) == "max_tokens":
            self.memory.log(self.id,
                            "Output hit its length ceiling — the end of this result "
                            "may be cut off.", type="warning")

        self.memory.add_artifact(self.id, result)
        preview = result.replace("\n", " ")[:160]
        self.memory.log(self.id, f"Finished. {preview}...", type="result")
        return result

    # ----------------------------------------------------------------------
    def _run_with_tools(self, specs, user_content, web_search=False):
        """Let this agent CALL tools to write real files. Each successful file is
        registered in shared memory so the UI can offer it as a download. If
        web_search is set, the agent can also search the live web while it works."""
        out_dir = self.memory.output_dir()

        def executor(name, tool_input):
            res = deliverables.run_tool(name, tool_input, out_dir)
            if res.get("ok"):
                self.memory.add_deliverable(self.id, res["kind"], res["title"], res["filename"])
                self.memory.log(self.id, f"Produced a {res['kind']}: {res['filename']}",
                                type="result")
                return res["message"] + " It is saved and offered to the user as a download."
            self.memory.log(self.id, f"A file couldn't be created: {res['message']}", type="error")
            return res["message"]

        return self.llm.complete_with_tools(
            system_prompt=self.cfg["system_prompt"],
            user_content=user_content,
            tool_specs=specs,
            tool_executor=executor,
            model=config.effective_model(self.id),
            effort=self.cfg.get("effort", "high"),
            # Same org-chart knob as the plain path (complete_with_tools'
            # default is 12000 — file-building turns run longer).
            max_tokens=self.cfg.get("max_tokens", 12000),
            budget=self.cfg.get("budget", config.AGENT_TEAM_BUDGET),
            web_search=web_search,
        )

    # ----------------------------------------------------------------------
    def _build_context(self, instruction):
        parts = [
            f"COMPANY GOAL:\n{self.memory.goal()}",
            f"\nYOUR ASSIGNMENT (delegated to you by the CEO):\n{instruction}",
        ]

        docs = self.memory.documents()
        if docs:
            parts.append(f"\n=== UPLOADED DOCUMENTS ({len(docs)}) ===")
            for d in docs:
                parts.append(
                    f"\n----- FILE: {d['name']} ({d['ext']}, {d['chars']:,} chars) -----\n{d['text']}"
                )

        teammate_work = self._teammate_artifacts()
        if teammate_work:
            parts.append("\n=== WORK ALREADY COMPLETED BY YOUR TEAMMATES ===")
            parts.append(teammate_work)

        if "web_search" in (self.cfg.get("tools") or []):
            parts.append(
                "\nYou can search the live web to verify facts or pull in current "
                "information (recent news, prices, today's events) when the task needs it. "
                "Search when accuracy depends on up-to-date facts; cite what you find."
            )

        parts.append(
            "\nNow complete YOUR assignment above. Stay focused on your role; "
            "build on your teammates' work rather than repeating it."
        )
        return "\n".join(parts)

    def _teammate_artifacts(self):
        snap = self.memory.snapshot()
        chunks = []
        for agent_id, text in snap.get("artifacts", {}).items():
            if agent_id == self.id:
                continue
            mate = config.get_agent(agent_id)
            title = mate["title"] if mate else agent_id
            chunks.append(f"\n--- {title} produced: ---\n{text}")
        return "\n".join(chunks)
