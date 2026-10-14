"""A Django MAILERS backend that sends email through listmonk's transactional API."""

from django_listmonk_mailer.backend import EmailBackend, ListmonkError

__version__ = "0.1.0"

__all__ = ["EmailBackend", "ListmonkError", "__version__"]
