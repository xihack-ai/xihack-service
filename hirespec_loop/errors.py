class HireSpecError(Exception):
    """A safe, user-displayable domain error; never contains provider secrets."""

    code = "HIRESPEC_ERROR"


class InputError(HireSpecError):
    code = "INPUT_ERROR"


class ModelTaskError(HireSpecError):
    code = "MODEL_TASK_ERROR"


class PolicyError(HireSpecError):
    code = "POLICY_BLOCKED"


class ClarificationRequired(HireSpecError):
    code = "NEEDS_CLARIFICATION"


class StaleProposalError(HireSpecError):
    code = "STALE_PROPOSAL"


class ApprovalError(HireSpecError):
    code = "APPROVAL_ERROR"
