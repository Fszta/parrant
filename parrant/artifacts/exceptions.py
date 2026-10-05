class RegistryError(Exception):
    """Base exception for all registry-related errors."""


class ModelNotFoundError(RegistryError):
    """Raised when a requested model is not found."""


class RegistryNotLoadedError(RegistryError):
    """Raised when trying to access registry before loading data."""


class RegistryLoadError(Exception):
    """Base exception for registry loading errors."""
