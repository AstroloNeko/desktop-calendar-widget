"""Read storage authority metadata without opening a calendar or creating a profile."""
import json
from pathlib import Path
import stat


class LifecycleError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def read_authority_state(directory):
    root = Path(directory)
    state_path = root / 'storage-state.json'
    witness = root / '.storage-managed'
    def metadata(path):
        try:
            return path.lstat()
        except FileNotFoundError:
            return None
    try:
        state_stat, witness_stat = metadata(state_path), metadata(witness)
        if any(value is not None and not stat.S_ISREG(value.st_mode) for value in (state_stat, witness_stat)):
            raise ValueError()
        if state_stat is None:
            if witness_stat is not None:
                raise LifecycleError('state_missing_recovery_required')
            return dict(format=1, state='NOT_STARTED')
        state = json.loads(state_path.read_text(encoding='utf8'))
        if type(state['format']) is not int or state['format'] != 1 or state['state'] not in (
            'NOT_STARTED', 'PREPARING', 'STAGED', 'ACTIVATING', 'ACTIVE', 'RECOVERY_REQUIRED'
        ):
            raise ValueError()
        if state['state'] != 'NOT_STARTED' and witness_stat is None:
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise LifecycleError('state_corrupt_recovery_required') from exc
