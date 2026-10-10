from .. import faults
from ..ledger import Ledger


def validate_candidate(cfg, store, cand) -> tuple[bool, str]:
    try:
        faults.validate(cfg, cand.fault, cand.target, cand.duration_s, dict(cand.params))
    except faults.ValidationError as e:
        reason = str(e)
    else:
        open_entries = Ledger(store).open_entries()
        if not open_entries:
            return True, "ok"
        reason = f"{len(open_entries)} active ledger faults"
    store.add_trace("validator", "-", f"{cand.id}", None, 0.0, False, f"rejected {cand.id}: {reason}")
    return False, reason
