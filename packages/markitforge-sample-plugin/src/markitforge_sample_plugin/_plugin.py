import locale
from typing import BinaryIO, Any
from striprtf.striprtf import rtf_to_text

from markitforge import (
    MarkitForge,
    DocumentConverter,
    DocumentConverterResult,
    StreamInfo,
)

__plugin_interface_version__ = (
    1  # The version of the plugin interface that this plugin uses
)

ACCEPTED_MIME_TYPE_PREFIXES = [
    "text/rtf",
    "application/rtf",
]

ACCEPTED_FILE_EXTENSIONS = [".rtf"]


def register_converters(markitforge: MarkitForge, **kwargs):
    """
    Called during construction of MarkitForge instances to register converters provided by plugins.
    """

    # Simply create and attach an RtfConverter instance
    markitforge.register_converter(RtfConverter())


class RtfConverter(DocumentConverter):
    """
    Converts an RTF file in the simplest possible way.
    """

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        # Read the file stream into a str using the provided charset encoding, or using the system default
        encoding = stream_info.charset or locale.getpreferredencoding()
        stream_data = file_stream.read().decode(encoding)

        # Return the result
        return DocumentConverterResult(
            title=None,
            markdown=rtf_to_text(stream_data),
        )
