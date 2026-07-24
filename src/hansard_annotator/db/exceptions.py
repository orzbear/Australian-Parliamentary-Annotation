"""Phase 2 database error types."""


class DatabaseConfigurationError(RuntimeError):
    pass


class ImportValidationError(RuntimeError):
    pass


class ImportConflictError(RuntimeError):
    pass


class VerificationError(RuntimeError):
    pass
