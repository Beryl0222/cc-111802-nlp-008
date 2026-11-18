import json
from pathlib import Path

from .text import annotate


STATE_DEFAULTS = {
    "schema_version": 2,
    "active_taxonomy": None,
    "records": [],
    "revisions": [],
    "taxonomies": {},
    "judgments": [],
    "reports": [],
}


def empty_state():
    state = {key: value.copy() if isinstance(value, (dict, list)) else value
             for key, value in STATE_DEFAULTS.items()}
    return state


class RecordRepository:
    """Small atomic JSON store.

    ``read``/``replace`` keep the original record-list interface.  The topic
    layer uses ``read_state``/``write_state`` so that source revisions,
    judgments and reports live in the same atomically written document.
    """

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            raise ValueError("记录库格式无效")
        return value

    def read(self):
        state = self._load()
        return [] if state is None else state["records"]

    def read_state(self):
        state = self._load()
        if state is None:
            return empty_state()
        if not isinstance(state.get("records"), list):
            raise ValueError("记录库格式无效")
        for key, default in STATE_DEFAULTS.items():
            if key not in state:
                state[key] = default.copy() if isinstance(default, (dict, list)) else default
        for record in state["records"]:
            if not isinstance(record.get("analysis"), dict) or not record["analysis"].get("content_digest"):
                record["analysis"] = annotate(record)
        return state

    def write_state(self, state):
        if not isinstance(state, dict) or not isinstance(state.get("records"), list):
            raise ValueError("记录库格式无效")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def replace(self, records):
        state = self.read_state()
        state["records"] = list(records)
        self.write_state(state)
