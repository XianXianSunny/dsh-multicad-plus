"""
File mixin for AutoCAD adapter.

Handles file operations (save, open, close, new, switch).
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core import get_config

logger = logging.getLogger(__name__)


class FileMixin:
    """Mixin for file operations."""

    if TYPE_CHECKING:
        document: Any

        def _get_document(self, operation: str = "operation") -> Any: ...
        def _get_application(self, operation: str = "operation") -> Any: ...
        def _validate_document(self) -> bool: ...
        def resolve_export_path(
            self, filename: str, folder_type: str = "drawings"
        ) -> str: ...

    def save_drawing(
        self, filepath: str = "", filename: str = "", format: str = "dwg"
    ) -> bool:
        """Save drawing to file.

        Args:
            filepath: Full path to save file (e.g., 'C:/drawings/myfile.dwg')
            filename: Just the filename (e.g., 'myfile.dwg'). If provided without
                     filepath, uses configured output directory
            format: File format (dwg, dxf, etc.). Default: dwg

        Returns:
            bool: True if successful, False otherwise

        Note:
            - If both filepath and filename provided, filepath takes precedence
            - If only filename provided, saved to config output directory
            - If neither provided, uses current document name
        """
        try:
            document = self._get_document("save_drawing")
            config = get_config()

            # SECURITY: Resolve output directory first (reference for validation)
            _output_dir = Path(config.output.directory).expanduser().resolve()

            # ========== Determine Filename ==========
            if filepath:
                save_filename = Path(filepath).name
            elif filename:
                save_filename = filename
            else:
                save_filename = None

            # If still no filename, use document name or generate one
            if not save_filename:
                if document.Name:
                    save_filename = document.Name
                else:
                    from datetime import datetime

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    save_filename = f"drawing_{timestamp}.{format}"

            # ========== Ensure Correct File Extension ==========
            # Only add extension if it's not already there
            if not save_filename.lower().endswith(f".{format}"):
                save_filename = f"{save_filename}.{format}"

            # ========== Resolve Final Path ==========
            if filepath and Path(filepath).is_absolute() and getattr(config.output, "allow_arbitrary_paths", False):
                final_path = Path(filepath)
            else:
                # Use centralized export path resolution for standard saves
                final_path_str = self.resolve_export_path(save_filename, "drawings")
                final_path = Path(final_path_str)

            # Save the drawing
            document.SaveAs(str(final_path))
            logger.info(f"Saved drawing to {final_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save drawing: {e}")
            return False

    def open_drawing(self, filepath: str) -> bool:
        """Open a drawing file in the CAD application via COM.

        Args:
            filepath: Absolute path to the drawing file to open (e.g. ``C:/drawings/plan.dwg``).

        Returns:
            True if the file was opened successfully, False otherwise.
        """
        try:
            application = self._get_application("open_drawing")
            self.document = application.Documents.Open(filepath)
            logger.info(f"Opened drawing from {filepath}")
            return True
        except Exception as e:
            logger.error(f"Failed to open drawing: {e}")
            return False

    def new_drawing(self, reuse_blank: bool = True) -> bool:
        """Create (or reuse) a blank drawing document in the CAD application.

        Calling ``Documents.Add`` for every deliverable litters the CAD session
        with abandoned ``Drawing1``/``Drawing4`` windows. When ``reuse_blank`` is
        true an already open, never-saved, empty drawing is activated instead of
        stacking another window on top of it.

        Args:
            reuse_blank: Reuse an existing unsaved empty drawing (default: True).

        Returns:
            True if the document was created/activated successfully, False otherwise.
        """
        try:
            application = self._get_application("new_drawing")

            if reuse_blank:
                for doc in application.Documents:
                    if self._document_is_blank(doc):
                        doc.Activate()
                        try:
                            application.ActiveDocument = doc
                        except Exception:
                            pass
                        self.document = doc
                        logger.info(f"Reused blank drawing: {doc.Name}")
                        return True

            self.document = application.Documents.Add()
            self._refresh_document_reference()
            logger.info(f"Created new blank drawing: {self.document.Name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create new drawing: {e}")
            return False

    def _refresh_document_reference(self, auto_create: bool = True) -> bool:
        """Refresh internal document reference to ActiveDocument.

        This ensures self.document always points to the active document
        in the application. Useful after creating or switching documents.

        Args:
            auto_create: If True and no documents open, create a new one (default: True)

        Returns:
            True if successful, False otherwise
        """
        try:
            application = self._get_application("_refresh_document_reference")

            # Case 1: Documents are open, use the active one
            if application.Documents.Count > 0:
                self.document = application.ActiveDocument
                if self.document is not None:
                    logger.debug(f"Document reference refreshed: {self.document.Name}")
                return True

            # Case 2: No documents open
            if auto_create:
                logger.warning("No documents open. Creating a new blank document...")
                self.document = application.Documents.Add()
                if self.document is not None:
                    logger.info(f"Auto-created new document: {self.document.Name}")
                return True
            else:
                logger.warning("No documents open")
                return False

        except Exception as e:
            logger.error(f"Failed to refresh document reference: {e}")
            return False

    def get_open_drawings(self) -> list:
        """Get list of all open drawing filenames.

        Returns:
            List of drawing names (e.g., ["drawing1.dwg", "drawing2.dwg"])
        """
        try:
            application = self._get_application("get_open_drawings")
            drawings = []

            # Use direct iteration instead of Item indexing
            for doc in application.Documents:
                drawings.append(doc.Name)

            logger.info(f"Found {len(drawings)} open drawings: {drawings}")
            return drawings
        except Exception as e:
            logger.error(f"Failed to get open drawings: {e}")
            return []

    # ========== Scratch (blank) drawing management ==========

    def _document_is_blank(self, doc: Any) -> bool:
        """Return True for a never-saved drawing that holds no entities.

        Such a document is a pure scratch window: closing it loses nothing.
        """
        try:
            if doc.FullName:
                return False
        except Exception:
            return False

        try:
            if doc.ModelSpace.Count > 0:
                return False
        except Exception:
            logger.debug("Could not read ModelSpace.Count while probing blanks")

        try:
            if doc.PaperSpace.Count > 0:
                return False
        except Exception:
            pass

        return True

    def list_documents(self) -> list:
        """Return a detailed inventory of every open drawing.

        Returns:
            List of dicts with ``name``, ``path``, ``entities``, ``blank`` and
            ``active`` keys, in the application's own document order.
        """
        try:
            application = self._get_application("list_documents")

            active_name = None
            try:
                active_name = application.ActiveDocument.Name
            except Exception:
                pass

            inventory = []
            for doc in application.Documents:
                try:
                    name = doc.Name
                except Exception:
                    continue
                try:
                    path = doc.FullName or ""
                except Exception:
                    path = ""
                try:
                    count = int(doc.ModelSpace.Count)
                except Exception:
                    count = None
                inventory.append(
                    {
                        "name": name,
                        "path": path,
                        "entities": count,
                        "blank": bool(not path and not count),
                        "active": name == active_name,
                    }
                )
            return inventory
        except Exception as e:
            logger.error(f"Failed to list documents: {e}")
            return []

    def close_blank_documents(self, keep_current: bool = True) -> list:
        """Close every unsaved, empty drawing window.

        Args:
            keep_current: Kept for call compatibility. A blank document holds
                no entities and was never saved, so closing it cannot lose
                work - including when it happens to be the active drawing
                (the adapter then rebinds to whatever CAD activates next).

        Returns:
            Names of the documents that were actually closed.
        """
        closed: list = []
        try:
            application = self._get_application("close_blank_documents")

            targets = []
            for doc in application.Documents:
                try:
                    name = doc.Name
                except Exception:
                    continue
                if self._document_is_blank(doc):
                    targets.append(name)

            for name in targets:
                for doc in application.Documents:
                    try:
                        if doc.Name != name:
                            continue
                        doc.Close(False)
                        closed.append(name)
                        logger.info(f"Closed blank drawing: {name}")
                    except Exception as error:
                        logger.warning(f"Could not close blank drawing {name}: {error}")
                    break

            if closed:
                self._refresh_document_reference(auto_create=False)

            return closed
        except Exception as e:
            logger.error(f"Failed to close blank drawings: {e}")
            return closed

    def find_open_document_by_path(self, filepath: str) -> str:
        """Return the name of the open document stored at ``filepath``, if any.

        Saving over a drawing that is already open in the same session makes
        ``SaveAs`` fail silently, so callers can report the conflict early.
        """
        try:
            target = Path(filepath).expanduser().resolve()
        except Exception:
            return ""

        try:
            application = self._get_application("find_open_document_by_path")
            current_path = ""
            if self.document is not None:
                try:
                    current_path = self.document.FullName or ""
                except Exception:
                    current_path = ""

            for doc in application.Documents:
                try:
                    full = doc.FullName or ""
                except Exception:
                    continue
                if not full:
                    continue
                try:
                    if Path(full).resolve() == target and full != current_path:
                        return doc.Name
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Could not probe open documents: {e}")

        return ""

    def switch_drawing(self, drawing_name: str) -> bool:
        """Switch to a different open drawing.

        Args:
            drawing_name: Name of the drawing to switch to (e.g., "drawing1.dwg")

        Returns:
            True if successful, False otherwise
        """
        try:
            application = self._get_application("switch_drawing")

            # Use direct iteration instead of Item indexing
            for doc in application.Documents:
                if doc.Name == drawing_name:
                    # Intenta activar a través del documento
                    doc.Activate()
                    # Intenta también forzarlo a nivel de aplicación (ZWCAD/AutoCAD)
                    try:
                        application.ActiveDocument = doc
                    except Exception:
                        pass

                    self.document = doc

                    # Pump Windows messages briefly to let CAD process the GUI switch
                    try:
                        import pythoncom

                        for _ in range(5):
                            pythoncom.PumpWaitingMessages()
                    except ImportError:
                        pass

                    logger.info(f"Switched to drawing: {drawing_name}")
                    return True

            logger.warning(f"Drawing not found: '{drawing_name}'")
            return False

        except Exception as e:
            logger.error(f"Failed to switch drawing: {e}")
            return False

    def close_drawing(self, save_changes: bool = False) -> bool:
        """Close the current drawing.

        Args:
            save_changes: Whether to save changes before closing (default: False)
                         True = save changes
                         False = discard changes without prompting

        Returns:
            True if successful, False otherwise
        """
        try:
            if not self._validate_document() or self.document is None:
                logger.warning("No document to close")
                return False

            document = self.document
            doc_name = document.Name

            # Close document using COM API
            document.Close(save_changes)

            # Try to update connection to remaining open document
            refresh_success = self._refresh_document_reference(auto_create=False)

            if refresh_success and self.document is not None:
                logger.info(
                    f"Closed drawing: {doc_name} (save_changes={save_changes}). "
                    f"Switched to: {self.document.Name}"
                )
            else:
                # No documents left. Do not add a replacement window: that is
                # exactly what used to leave an empty "Drawing1" tab behind
                # after every close. The next operation creates one if needed.
                self.document = None
                logger.info(
                    f"Closed drawing: {doc_name} (save_changes={save_changes}). "
                    "No drawings open any more."
                )

            return True

        except Exception as e:
            logger.error(f"Failed to close drawing: {e}")
            return False
