"""Shared exception types for the Wave Cinema backend."""


class WaveCinemaError(Exception):
    """Base class for all Wave Cinema backend errors."""


class RevisionConflict(WaveCinemaError):
    """A workspace write carried a stale expected_revision; the write was rejected."""


class RightsRequired(WaveCinemaError):
    """Song ingest attempted without explicit assent to the rights statement."""


class FormatRejected(WaveCinemaError):
    """Uploaded song bytes were not an accepted audio format (MP3/WAV only)."""


class NoApprovedQuote(WaveCinemaError):
    """Generation requested without an approved, unexpired, unconsumed quote."""


class QuoteExpired(WaveCinemaError):
    """A quote was used or approved after its expiry time."""


class GateBlocked(WaveCinemaError):
    """A pipeline gate (rights, first-scene review, selection) was not satisfied."""


class InsufficientFunds(WaveCinemaError):
    """A ledger hold/debit exceeded the account's available credit balance."""


class TakeProtected(WaveCinemaError):
    """The selected take of a scene cannot be deleted until another is selected."""


class TakeNotFound(WaveCinemaError):
    """No such take exists for the scene."""


class SceneNotFound(WaveCinemaError):
    """No such scene exists in the project plan."""


class ProjectNotFound(WaveCinemaError):
    """No such project exists."""
