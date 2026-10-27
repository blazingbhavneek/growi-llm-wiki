"""The single cross-document linking phase."""

from .service import LinkResult, LinkerCancelled, LinkerModeMismatch, link_document, link_documents, remove_document

__all__ = ["LinkResult", "LinkerCancelled", "LinkerModeMismatch", "link_document", "link_documents", "remove_document"]
