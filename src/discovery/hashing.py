"""Project-wide content-hashing recipe.

One helper, one recipe, everywhere. Used by:

- `tasks.content_hash` — idempotency on the task queue (`(job_id,
  content_hash)` is UNIQUE).
- `raw_records.content_hash` — second-line dedup of Bronze rows.
- The LLM diskcache key — content hash + prompt VERSION + model.

The recipe is sha256 over canonical JSON: `sort_keys=True`,
`separators=(",", ":")`, `ensure_ascii=False`. Don't introduce a second
recipe anywhere — every column above must come from this function.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def hash_params(payload: Any) -> str:
    """Return the sha256 hex digest of `payload`'s canonical JSON form.

    Parameters
    ----------
    payload :
        Any JSON-serializable value. Dicts/lists/scalars only. Non-JSON
        types (datetime, Decimal, set, bytes, arbitrary objects) raise
        `TypeError` — convert at the call site.

    Returns
    -------
    str
        Lower-case 64-character hex digest.
    """
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
