class EditorError(Exception):
    """Expected input or persistence failure; safe to display to the user."""

class UnitsRequired(EditorError):
    """DXF has no declared units. A positive metres_per_unit must be supplied."""
