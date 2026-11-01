import unittest
import zlib
from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import EncodedStreamObject, NameObject

from nico_adapters.extract import MAX_FILE_BYTES, extract_text


def _compressed_content_pdf(stream_sizes):
    writer = PdfWriter()
    for size in stream_sizes:
        page = writer.add_blank_page(width=100, height=100)
        stream = EncodedStreamObject()
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        stream._data = zlib.compress(b"A" * size)
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class ExtractTextTests(unittest.TestCase):
    def test_pdf_stream_expansion_is_bounded_during_decompression(self):
        data = _compressed_content_pdf([2_100_000])

        self.assertLess(len(data), MAX_FILE_BYTES)
        with self.assertRaisesRegex(ValueError, "PDF 内容过大"):
            extract_text("resume.pdf", data)

    def test_pdf_total_content_budget_is_shared_across_pages(self):
        data = _compressed_content_pdf([1_500_000, 1_500_000, 1_500_000])

        self.assertLess(len(data), MAX_FILE_BYTES)
        with self.assertRaisesRegex(ValueError, "PDF 内容过大"):
            extract_text("resume.pdf", data)


if __name__ == "__main__":
    unittest.main()
