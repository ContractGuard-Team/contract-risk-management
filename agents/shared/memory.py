from copy import deepcopy


class RequestMemory:
    """Explicit in-memory state for one job. Create a new instance for each job."""

    def __init__(self):
        self._items: dict[str, dict] = {}

    def put(self, key: str, value: dict):
        if not isinstance(key, str) or not key.strip() or not isinstance(value, dict):
            raise ValueError("Memory requires a non-empty key and a dictionary value")
        self._items[key] = deepcopy(value)

    def snapshot(self) -> dict[str, dict]:
        return deepcopy(self._items)

    def clear(self):
        self._items.clear()
