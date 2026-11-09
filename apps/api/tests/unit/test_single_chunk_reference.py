"""Whole-string chunk-reference classification through the public helpers."""
import unittest

from shared.utils.chunk_refs import extract_chunk_ref_spans, is_chunk_ref


class SingleChunkReferenceTests(unittest.TestCase):
    def test_two_images_separated_by_space(self):
        self.assertFalse(is_chunk_ref("[images/first.png] [images/second.png]"))

    def test_two_adjacent_images(self):
        self.assertFalse(is_chunk_ref("[images/first.png][images/second.jpg]"))

    def test_repeated_identical_reference(self):
        self.assertFalse(is_chunk_ref("[images/first.png][images/first.png]"))

    def test_two_tables(self):
        self.assertFalse(is_chunk_ref("[tables/first.html] [tables/second.csv]"))

    def test_prose_between_images(self):
        self.assertFalse(is_chunk_ref("[images/first.png] and [images/second.jpg]"))

    def test_extracting_multiple_refs_is_unchanged(self):
        text = "[images/first.png] [images/second.png]"
        self.assertEqual(
            extract_chunk_ref_spans(text),
            [
                {"ref": "[images/first.png]", "start": 0, "end": 18},
                {"ref": "[images/second.png]", "start": 19, "end": 38},
            ],
        )

    def test_single_image(self):
        self.assertTrue(is_chunk_ref("[images/plot.png]"))

    def test_single_table(self):
        self.assertTrue(is_chunk_ref("[tables/report.html]"))

    def test_case_insensitive_extension(self):
        self.assertTrue(is_chunk_ref("[images/plot.JPEG]"))

    def test_filename_with_brackets(self):
        self.assertTrue(is_chunk_ref("[images/chart [draft].png]"))

    def test_filename_with_inner_closing_bracket(self):
        self.assertTrue(is_chunk_ref("[images/chart]draft.png]"))

    def test_filename_with_inner_extension_and_closing_bracket(self):
        self.assertTrue(is_chunk_ref("[images/chart[plot.png].png]"))

    def test_surrounding_whitespace(self):
        self.assertTrue(is_chunk_ref(" \t[images/plot.png]\n"))

    def test_prose_after_reference(self):
        self.assertFalse(is_chunk_ref("[images/plot.png] caption"))

    def test_plain_text(self):
        self.assertFalse(is_chunk_ref("images/plot.png"))

    def test_empty_text(self):
        self.assertFalse(is_chunk_ref(""))


if __name__ == "__main__":
    unittest.main()
