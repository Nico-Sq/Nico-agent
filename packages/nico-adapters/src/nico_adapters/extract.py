from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.table import Table
from pypdf import PdfReader, apply_configuration
from pypdf.errors import LimitReachedError


MAX_FILE_BYTES = 5_000_000
MAX_TEXT_CHARS = 4_000
MAX_PDF_PAGE_CONTENT_BYTES = 2_000_000
MAX_PDF_CONTENT_BYTES = 4_000_000

_PDF_OUTPUT_LIMIT_FIELDS = (
    "array_based_stream_maximum_output_length",
    "jbig2_maximum_output_length",
    "lzw_maximum_output_length",
    "run_length_maximum_output_length",
    "zlib_maximum_output_length",
    "brotli_maximum_output_length",
    "flate_maximum_row_length",
    "image_maximum_buffer_size",
    "stream_decoding_work_maximum_length",
)


def _pdf_output_limits(limit: int) -> dict[str, int]:
    return {field: limit for field in _PDF_OUTPUT_LIMIT_FIELDS}


def extract_text(filename: str, data: bytes) -> str:
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("文件大小必须在 1 字节到 5 MB 之间")
    extension = Path(filename).suffix.lower()
    try:
        if extension in (".txt", ".md"):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("gb18030")
        elif extension == ".docx":
            with ZipFile(BytesIO(data)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 20_000_000:
                    raise ValueError("DOCX 解压后的内容过大")
            document = Document(BytesIO(data))
            parts = []
            for block in document.iter_inner_content():
                if isinstance(block, Table):
                    parts.extend("\t".join(cell.text for cell in row.cells) for row in block.rows)
                else:
                    parts.append(block.text)
            text = "\n".join(parts)
        elif extension == ".pdf":
            total_content_bytes = 0
            with apply_configuration(
                maximum_declared_stream_length=len(data),
                zlib_maximum_recovery_input_length=len(data),
                **_pdf_output_limits(MAX_PDF_PAGE_CONTENT_BYTES),
            ):
                reader = PdfReader(BytesIO(data))
                if reader.is_encrypted or len(reader.pages) > 20:
                    raise ValueError("仅支持未加密且不超过 20 页的 PDF")
                parts = []
                for page in reader.pages:
                    if page.get("/Contents") is None:
                        parts.append(page.extract_text() or "")
                        continue
                    remaining = MAX_PDF_CONTENT_BYTES - total_content_bytes
                    if remaining <= 0:
                        raise ValueError("PDF 内容过大")
                    output_limit = min(MAX_PDF_PAGE_CONTENT_BYTES, remaining)
                    with apply_configuration(**_pdf_output_limits(output_limit)):
                        contents = page.get_contents()
                        if contents is None:
                            parts.append(page.extract_text() or "")
                            continue
                        decoded = contents.get_data()
                        if len(decoded) > output_limit:
                            raise ValueError("PDF 内容过大")
                        total_content_bytes += len(decoded)
                        parts.append(page.extract_text() or "")
            text = "\n".join(parts)
        else:
            raise ValueError("仅支持 .txt、.md、.docx 和 .pdf 文件")
    except LimitReachedError as exc:
        raise ValueError("PDF 内容过大，无法安全提取") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取文件中的文本") from exc
    text = text.strip()
    if not text:
        raise ValueError("未提取到文本；扫描版 PDF 需要先进行 OCR")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("提取内容超过 4000 字符，请拆分材料或粘贴所需片段")
    return text
