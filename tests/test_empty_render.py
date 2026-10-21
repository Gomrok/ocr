"""Regression protection for silent white PDF renders and empty-success exports."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pymupdf
import cv2

from ocr_pipeline import render, pipeline
from ocr_pipeline.metrics import Metrics


class EmptyRenderTests(unittest.TestCase):
    def test_tiled_render_preserves_ink_across_rotation_and_tile_edges(self):
        with pymupdf.open() as document, tempfile.TemporaryDirectory() as directory:
            page = document.new_page(width=240.5, height=180.25)
            document.xref_set_key(page.xref,"Group","<< /Type /Group /S /Transparency /CS /DeviceRGB >>")
            page.insert_text((12,60), "EDGE 0046", fontsize=17)
            page.draw_line((0,0),(240,180),color=(0,0,0),width=.7)
            page.draw_rect((25,30,180,110),color=(.2,.3,.8),fill=(.9,.9,1),fill_opacity=.2)
            for rotation in (0,90,180,270):
                page.set_rotation(rotation)
                expected = page.get_pixmap(dpi=300,colorspace=pymupdf.csGRAY,alpha=False)
                pixels = np.frombuffer(expected.samples,dtype=np.uint8).reshape(expected.height,expected.width)
                with self.subTest(rotation=rotation), patch.object(render,"RENDER_TILE_EDGE",128):
                    master = render.render_master(page,1,Path(directory),dpi=300)
                actual=master.read_full()
                self.assertEqual(actual.shape,pixels.shape)
                # MuPDF rerasterizes clipped diagonal paths, so their antialias
                # intensities need not be byte-identical. No stroke may disappear
                # or be invented outside its original one-pixel edge neighbourhood.
                delta=np.abs(actual.astype(np.int16)-pixels.astype(np.int16))
                self.assertLessEqual(int(delta.max()),32)
                kernel=np.ones((3,3),np.uint8)
                original_ink=(pixels<245).astype(np.uint8)
                tiled_ink=(actual<245).astype(np.uint8)
                self.assertFalse(np.any(original_ink & (1-cv2.dilate(tiled_ink,kernel))))
                self.assertFalse(np.any(tiled_ink & (1-cv2.dilate(original_ink,kernel))))
                self.assertEqual(master.effective_dpi,300)
                self.assertEqual(master.render_method,"tiled")

    def test_white_render_with_visible_probe_fails_before_export(self):
        with pymupdf.open() as document, tempfile.TemporaryDirectory() as directory:
            page = document.new_page(width=72,height=72)
            page.insert_text((4,30),"CONTENT",fontsize=8)
            original = page.get_pixmap
            def failed_large_render(**kwargs):
                pix = original(**kwargs)
                if kwargs.get("dpi"):
                    pix.clear_with(255)
                return pix
            with patch.object(page,"get_pixmap",side_effect=failed_large_render):
                with self.assertRaisesRegex(RuntimeError,"lost visible page content"):
                    render.render_master(page,1,Path(directory),dpi=200)
            self.assertFalse(list(Path(directory).glob("*.tiff")))

    def test_blank_and_unrecognized_visible_pages_have_distinct_status(self):
        with pymupdf.open() as document, tempfile.TemporaryDirectory() as directory:
            page = document.new_page(width=120,height=100)
            engine = SimpleNamespace(name="test_engine")
            for visible in (False, True):
                if visible:
                    page.insert_text((10,40),"VISIBLE",fontsize=15)
                master = render.render_master(page,1,Path(directory),dpi=100)
                metrics = Metrics()
                try:
                    with patch.object(pipeline.E,"ocr_many",return_value=[]):
                        if visible:
                            with self.assertRaisesRegex(RuntimeError,"no text from a nonblank"):
                                pipeline.ocr_page(master,[engine],metrics)
                        else:
                            result = pipeline.ocr_page(master,[engine],metrics)
                            self.assertEqual(result["status"],"blank")
                            self.assertFalse(result["has_text"])
                finally:
                    metrics.stop()
