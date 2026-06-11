"""
memory.py — The team's shared "company brain".

Every agent reads from and writes to ONE shared memory object, so the team
works together instead of as disconnected individuals:

  - goal        : what the user asked for
  - documents   : the parsed text of every uploaded file
  - tasks       : the plan — who is doing what, and its status
  - artifacts   : finished work products, keyed by name (e.g. each agent's output)
  - activity    : a running, timestamped feed of everything happening
                  (this is what powers the live "watch the team work" view)

It is thread-safe (a background worker writes while the website reads) and
saves itself to a JSON file so a run is never lost.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone


class SharedMemory:
    def __init__(self, job_id, store_dir="memory_store", goal="", output_dir=None):
        self.job_id = job_id
        self.store_dir = store_dir
        # Where agents write REAL output files (decks, PDFs, etc.). Defaults to a
        # per-job folder next to the memory store if the caller doesn't pass one.
        self._output_dir = output_dir or os.path.join(store_dir, f"{job_id}_files")
        self._lock = threading.RLock()
        self._state = {
            "job_id": job_id,
            "goal": goal,
            "status": "starting",          # starting | running | done | error
            "created_at": _now(),
            "documents": [],               # [{name, ext, chars, text, meta}]
            "tasks": [],                   # [{id, agent, instruction, status, result}]
            "artifacts": {},               # {name: text}
            "deliverables": [],            # [{agent, kind, title, filename, url}] real files
            "activity": [],                # [{ts, agent, type, message}]
            "final_report": None,
            "error": None,
            # Phase 3 — the persistent "company brain" this run is tied to, and any
            # terms the Data Analyst flagged it couldn't interpret (UNKNOWN_TERMS).
            # The result page reads `unknown_terms` to show the "confirm" banner.
            "company_id": None,
            "unknown_terms": [],
            # Phase 6 — the DB run id (so the result page can attach 👍/👎 feedback),
            # the parsed verification trail (数据来源与核验), and whether parsing the
            # Report Writer's SOURCES_JSON block succeeded ("ok"|"malformed"|"missing").
            "run_id": None,
            "sources": [],
            "sources_status": None,
            "sources_recovered": False,
            "output_dir": self._output_dir,
        }
        os.makedirs(store_dir, exist_ok=True)
        self._save()

    # ---- activity feed ----------------------------------------------------
    def log(self, agent, message, type="info"):
        """Record one line in the live activity feed. `type` drives the UI icon:
        info | thinking | working | handoff | result | error | success."""
        with self._lock:
            self._state["activity"].append(
                {"ts": _now(), "agent": agent, "type": type, "message": message}
            )
            self._save()

    # ---- documents --------------------------------------------------------
    def add_document(self, doc):
        with self._lock:
            self._state["documents"].append(doc)
            self._save()

    def documents(self):
        with self._lock:
            return list(self._state["documents"])

    # ---- tasks ------------------------------------------------------------
    def add_task(self, agent, instruction):
        with self._lock:
            task = {
                "id": len(self._state["tasks"]) + 1,
                "agent": agent,
                "instruction": instruction,
                "status": "pending",       # pending | in_progress | done | error
                "result": None,
            }
            self._state["tasks"].append(task)
            self._save()
            return task["id"]

    def update_task(self, task_id, **fields):
        with self._lock:
            for t in self._state["tasks"]:
                if t["id"] == task_id:
                    t.update(fields)
                    break
            self._save()

    # ---- artifacts (work products) ---------------------------------------
    def add_artifact(self, name, text):
        with self._lock:
            self._state["artifacts"][name] = text
            self._save()

    def get_artifact(self, name, default=None):
        with self._lock:
            return self._state["artifacts"].get(name, default)

    # ---- deliverables (real downloadable files) --------------------------
    def output_dir(self):
        """Folder where agents should write real output files for this job."""
        return self._output_dir

    def add_deliverable(self, agent, kind, title, filename):
        """Register a real file an agent produced so the UI can offer it as a
        download. `filename` is just the name inside this job's output folder."""
        with self._lock:
            entry = {"agent": agent, "kind": kind, "title": title,
                     "filename": filename, "url": f"/api/files/{self.job_id}/{filename}"}
            self._state["deliverables"].append(entry)
            self._save()
            return entry

    # ---- status & final output -------------------------------------------
    def set_status(self, status):
        with self._lock:
            self._state["status"] = status
            self._save()

    def set_final_report(self, text):
        with self._lock:
            self._state["final_report"] = text
            self._state["status"] = "done"
            self._save()

    def set_error(self, message):
        with self._lock:
            self._state["error"] = message
            self._state["status"] = "error"
            self._save()

    # ---- company brain (Phase 3) -----------------------------------------
    def set_company_id(self, company_id):
        """Tie this run to a persistent company-brain row (or None)."""
        with self._lock:
            self._state["company_id"] = company_id
            self._save()

    def set_unknown_terms(self, terms):
        """Record the terms the Data Analyst couldn't interpret, so the result
        page can ask the user to define them (one question per term)."""
        with self._lock:
            self._state["unknown_terms"] = list(terms or [])
            self._save()

    # ---- verification trail (Phase 6) ------------------------------------
    def set_run_id(self, run_id):
        """Tie this run to its persistent DB run id, so the result page can attach
        per-section 👍/👎 feedback to the right run."""
        with self._lock:
            self._state["run_id"] = run_id
            self._save()

    def set_sources(self, sources, status, recovered=False):
        """Record the parsed 数据来源与核验 rows and the parse status
        ("ok"|"malformed"|"missing") for the result page and the PDF.
        `recovered` marks rows salvaged from a truncated/dropped SOURCES_JSON
        block, so the contract-miss rate stays measurable after auto-repair."""
        with self._lock:
            self._state["sources"] = list(sources or [])
            self._state["sources_status"] = status
            self._state["sources_recovered"] = bool(recovered)
            self._save()

    # ---- reads ------------------------------------------------------------
    def goal(self):
        with self._lock:
            return self._state["goal"]

    def snapshot(self):
        """A deep-ish copy the website can safely read while work continues."""
        with self._lock:
            return json.loads(json.dumps(self._state))

    # ---- persistence ------------------------------------------------------
    def _path(self):
        return os.path.join(self.store_dir, f"{self.job_id}.json")

    def _save(self):
        # caller already holds the lock
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path())  # atomic write — never a half-written file

    @classmethod
    def load(cls, job_id, store_dir="memory_store"):
        path = os.path.join(store_dir, f"{job_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        obj = cls.__new__(cls)
        obj.job_id = job_id
        obj.store_dir = store_dir
        obj._output_dir = state.get("output_dir") or os.path.join(store_dir, f"{job_id}_files")
        obj._lock = threading.RLock()
        # Tolerate states written before deliverables / company-brain fields existed.
        state.setdefault("deliverables", [])
        state.setdefault("company_id", None)
        state.setdefault("unknown_terms", [])
        state.setdefault("run_id", None)
        state.setdefault("sources", [])
        state.setdefault("sources_status", None)
        state.setdefault("sources_recovered", False)
        obj._state = state
        return obj


def _now():
    return datetime.now(timezone.utc).isoformat()
