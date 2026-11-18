"""Read storage authority metadata without opening a calendar or creating a profile."""
import json
from pathlib import Path


class LifecycleError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def read_authority_state(directory):
    root = Path(directory)
    state_path = root / 'storage-state.json'
    witness = root / '.storage-managed'
    if not state_path.exists():
        if witness.exists():
            raise LifecycleError('state_missing_recovery_required')
        return dict(format=1, state='NOT_STARTED')
    try:
        state = json.loads(state_path.read_text(encoding='utf8'))
        if state['format'] != 1 or state['state'] not in (
            'NOT_STARTED', 'PREPARING', 'STAGED', 'ACTIVATING', 'ACTIVE', 'RECOVERY_REQUIRED'
        ):
            raise ValueError()
        if state['state'] != 'NOT_STARTED' and not witness.is_file():
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise LifecycleError('state_corrupt_recovery_required') from exc
