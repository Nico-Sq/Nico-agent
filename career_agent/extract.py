from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.table import Table
from pypdf import PdfReader


MAX_FILE_BYTES = 5_000_000
MAX_TEXT_CHARS = 4_000


def extract_text(filename: str, data: bytes) -> str:
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("文件大小必须在 1 字节到 5 MB 之间")
    extension = Path(filename).suffix.lower()
    try:
        if extension in (".txt", ".md"):
            text = data.decode("utf-8-sig")
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
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted or len(reader.pages) > 20:
                raise ValueError("仅支持未加密且不超过 20 页的 PDF")
            parts = []
            for page in reader.pages:
                contents = page.get_contents()
                if contents is not None and len(contents.get_data()) > 2_000_000:
                    raise ValueError("PDF 页面内容过大")
                parts.append(page.extract_text() or "")
            text = "\n".join(parts)
        else:
            raise ValueError("仅支持 .txt、.md、.docx 和 .pdf 文件")
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
