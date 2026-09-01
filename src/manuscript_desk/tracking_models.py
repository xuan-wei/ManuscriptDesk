from dataclasses import asdict, dataclass, field


@dataclass
class ManuscriptSnapshot:
    """Provider-neutral manuscript snapshot returned by a tracker."""

    provider: str
    journal_config_id: int | None
    journal: str
    manuscript_id: str
    original_id: str
    source_original_id: str
    version_number: int
    title: str
    author_role: str = "unknown"
    authorship: str = ""
    status_text: str = ""
    status: str = ""
    detailed_status: str = ""
    submitted_date: str = ""
    decision_date: str = ""
    status_date: str = ""
    final_disposition: str = ""
    workflow_folder: str = ""
    SE_or_DE: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JournalTrackResult:
    """Outcome for one configured journal."""

    journal_name: str
    provider: str
    journal_config_id: int | None
    success: bool
    manuscripts: list[dict] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    elapsed_seconds: float = 0.0

    @property
    def manuscripts_found(self) -> int:
        return len(self.manuscripts)

    def to_dict(self) -> dict:
        return {
            "journal_name": self.journal_name,
            "provider": self.provider,
            "journal_config_id": self.journal_config_id,
            "success": self.success,
            "manuscripts": self.manuscripts,
            "manuscripts_found": self.manuscripts_found,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "elapsed_seconds": self.elapsed_seconds,
        }
